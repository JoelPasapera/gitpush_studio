# -*- coding: utf-8 -*-
"""Pipeline de publicación.

Un `PublishJob` describe QUÉ publicar (carpeta, remoto, modo…); los
`Callbacks` describen CÓMO se habla con quien mira (consola, progreso,
y las dos preguntas que necesitan respuesta humana). Cada publicación
tiene su propio `PublishContext`: no hay estado compartido, así que
`publish_many` puede lanzar varias a la vez con total seguridad.

Un paso que necesita una decisión llama al callback y espera; si la
respuesta es «no», lanza `Cancelled`. `STEPS` es la única definición
del orden: la numeración y el total se derivan de la lista.
"""

import os
import shutil
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .errors import Blocked, Cancelled, StepError
from .fs import fmt_size, normalize_path, relative_inside
from .git import Git, auth_env, looks_like_auth_error, push_error_message
from .history import describe_changes
from .atomic import TEMP_SUFFIX, clean_stale, temp_prefix
from .ignore import (IgnoreMatcher, anchor_pattern, append_gitignore,
                     clean_patterns, escape_pattern, has_wildcards)
from .licenses import normalize_key, write_license
from .pack import consolidate_project, free_txt_path
from .refnames import branch_problem
from .safekeeping import backup as make_backup, files_at_risk, git_dir
from .scan import scan_entries, stats_lines
from .urls import is_remote_url, parse_owner_repo, repo_web_url

DEFAULT_MESSAGE = "Actualizar proyecto completo"
MODES = ("mirror", "addedit")

# `git config --global` desde varios hilos a la vez pelearía por el
# mismo archivo de bloqueo: se serializa.
_GLOBAL_CONFIG_LOCK = threading.Lock()


# ------------------------------------------------------------------ #
#  Datos                                                              #
# ------------------------------------------------------------------ #
@dataclass
class PublishJob:
    folder: str
    url: str
    branch: str = "main"
    user: str = ""
    email: str = ""
    local_identity: bool = True
    message: str = DEFAULT_MESSAGE
    stamp: bool = False
    force: bool = False
    check: bool = True
    mode: str = "mirror"                  # mirror | addedit
    preview: bool = True
    ignore: list = field(default_factory=list)
    license: str = ""                     # clave de la licencia, o "" ninguna
    license_owner: str = ""               # a nombre de quién
    auto: bool = False
    auto_message: bool = False            # describir los cambios en el commit
    name: str = ""                        # etiqueta (p. ej. el perfil)
    # Unir el proyecto entero (árbol + contenido) en un único TXT antes
    # de subir. El TXT se queda en tu equipo: nunca se publica.
    consolidate: bool = False
    consolidate_path: str = ""            # vacío → <carpeta>/<nombre>.txt
    # Token de GitHub para conectarse sin pasar por el gestor de
    # credenciales (así no aparece la ventana «Select an account»). No se
    # guarda en el perfil: lo pone quien lanza la publicación.
    token: str = ""

    @classmethod
    def from_state(cls, s, auto=False, name=""):
        """Construye un trabajo a partir de un perfil / último estado
        guardado (el dict que produce la interfaz)."""
        mode = s.get("mode", "mirror")
        return cls(
            folder=normalize_path(s.get("folder", "")),
            url=(s.get("url") or "").strip(),
            branch=(s.get("branch") or "main").strip() or "main",
            user=(s.get("user") or "").strip(),
            email=(s.get("email") or "").strip(),
            local_identity=bool(s.get("local_identity", True)),
            message=(s.get("message") or "").strip() or DEFAULT_MESSAGE,
            stamp=bool(s.get("stamp", False)),
            force=bool(s.get("force", False)),
            check=bool(s.get("check", True)),
            mode=mode if mode in MODES else "mirror",
            preview=bool(s.get("preview", True)),
            ignore=clean_patterns(s.get("ignore", "")),
            auto=auto, name=name,
            auto_message=bool(s.get("auto_message", False)),
            license=normalize_key(s.get("license", "")) or "",
            license_owner=(s.get("license_owner") or "").strip(),
            consolidate=bool(s.get("consolidate", False)),
            consolidate_path=normalize_path(s.get("consolidate_path", "")),
        )

    def txt_destination(self):
        """Ruta del TXT: la elegida, o «<carpeta>/<nombre de carpeta>.txt».

        Si lo que se indicó es una carpeta, el TXT se llama como el
        proyecto y se guarda dentro de ella.
        """
        project = Path(self.folder)
        default = project / ((project.name or "proyecto") + ".txt")
        chosen = (self.consolidate_path or "").strip()
        if not chosen:
            return default
        p = Path(chosen)
        if p.is_dir():
            return p / default.name
        return p

    @property
    def label(self):
        return self.name or Path(self.folder).name or self.folder

    @property
    def addedit(self):
        return self.mode == "addedit"

    def validate(self):
        """Mensaje de error para el usuario, o None si todo está bien."""
        if not self.folder or not Path(self.folder).is_dir():
            return "Indica una carpeta de proyecto que exista."
        if not self.url:
            return ("Indica la URL del repositorio de GitHub, o créalo "
                    "con «➕ Crear repositorio nuevo…».")
        if not is_remote_url(self.url):
            return ("La URL debe empezar por https:// o git@ — ejemplo: "
                    "https://github.com/usuario/repo.git")
        if bool(self.user) != bool(self.email):
            return "Completa nombre Y correo, o deja ambos vacíos."
        if self.mode not in MODES:
            return "Modo desconocido."
        problema = branch_problem(self.branch)
        if problema:
            return problema
        return None

    def commit_message(self):
        msg = self.message.strip() or DEFAULT_MESSAGE
        if self.stamp or self.auto:
            msg += datetime.now().strftime(" — %Y-%m-%d %H:%M")
        return msg


@dataclass
class Callbacks:
    """Lo que el pipeline necesita del exterior. Todos tienen un valor
    por defecto «sin interfaz»: publicar sin callbacks funciona."""
    log: object = lambda text, tag="out": None
    progress: object = lambda n, total, label: None
    # findings → ("continue" | "ignore" | "cancel", rutas_seleccionadas)
    ask_review: object = lambda findings: ("continue", [])
    # (nuevos, modificados, borrados, modo) → True/False, una tupla
    # (seguir, mantener, copiar_a_la_carpeta) o un PreviewChoice, para
    # dejar fuera archivos concretos de esta publicación.
    ask_preview: object = lambda added, modified, deleted, mode: True


@dataclass
class PublishResult:
    job: PublishJob
    status: str                   # ok | error | cancelled | blocked
    message: str = ""
    web_url: str = ""
    seconds: float = 0.0
    # TXT del proyecto, si se pidió: ruta y estadísticas, o el motivo por
    # el que no se pudo generar. No afecta a `status`: el TXT es un extra
    # y su fallo nunca da por fallida una publicación que sí subió.
    txt_path: str = ""
    txt_stats: dict = field(default_factory=dict)
    txt_error: str = ""

    @property
    def ok(self):
        return self.status == "ok"


class PublishContext:
    """Estado de UNA publicación mientras avanza por los pasos."""

    def __init__(self, job, callbacks, cancel=None, git=None):
        self.job = job
        self.cb = callbacks
        self.cancel = cancel or threading.Event()
        self.git = git or Git(log=callbacks.log)
        self.folder = job.folder
        self.patterns = list(job.ignore)
        # Cabecera de autorización por entorno: ni en la línea de
        # comandos ni en .git/config, y git no consulta al gestor de
        # credenciales, que es quien abre la ventana de la cuenta.
        self.auth = auth_env(job.url, job.token)
        self.force = job.force and not job.addedit
        self.remote_exists = False
        # Rutas que la app dejó de rastrear porque el usuario las excluyó.
        self.untracked_by_request = set()
        # Resultado del TXT del proyecto (paso «Uniendo el proyecto…»).
        self.txt_path = ""
        self.txt_stats = {}
        self.txt_error = ""
        self.t0 = time.time()

    def log(self, text, tag="dim"):
        self.cb.log(text, tag)

    def check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled("Publicación detenida. No se ha subido nada.")

    def run(self, args, check=True, timeout=900, auth=False):
        """git en la carpeta del proyecto. Con `auth`, añade el token."""
        return self.git.run(args, cwd=self.folder, check=check,
                            timeout=timeout,
                            env_extra=self.auth if auth else None)

    def drop_auth(self, reason):
        """Deja de usar el token en esta publicación y avisa."""
        self.auth = {}
        self.log(f"  {reason} Lo intento con tu sesión de Git.", "warn")


# ------------------------------------------------------------------ #
#  Pasos                                                              #
# ------------------------------------------------------------------ #
def step_check_git(ctx):
    ctx.git.run(["--version"])            # sin cwd: la carpeta aún no importa


def step_init_repo(ctx):
    if (Path(ctx.folder) / ".git").exists():
        ctx.log("  El repositorio ya estaba inicializado.")
    else:
        ctx.run(["init"])


def step_identity(ctx):
    job = ctx.job
    if not (job.user and job.email):
        ctx.log("  Sin cambios: se usa la identidad ya configurada.")
        return
    if job.local_identity:
        ctx.run(["config", "user.name", job.user])
        ctx.run(["config", "user.email", job.email])
    else:
        with _GLOBAL_CONFIG_LOCK:
            ctx.run(["config", "--global", "user.name", job.user])
            ctx.run(["config", "--global", "user.email", job.email])


def step_license(ctx):
    """Añade un LICENSE si el usuario eligió uno y el proyecto no tiene.
    Nunca sobrescribe el que ya haya, y si algo falla no detiene la
    publicación: es un extra, no el objetivo."""
    job = ctx.job
    if not job.license:
        ctx.log("  Sin licencia: no se añade ninguna.")
        return
    owner = (job.license_owner or job.user
             or (parse_owner_repo(job.url)[0] or ""))
    name, message = write_license(ctx.folder, job.license, owner,
                                  token=job.token)
    ctx.log("  " + message, "ok" if name else "warn")


def step_consolidate(ctx):
    """Une el proyecto entero (árbol + contenido) en un único TXT.

    El TXT se queda en tu equipo y NUNCA se sube: si cae dentro del
    proyecto, su ruta entra en los patrones de ignorar, y este paso va
    justo antes del de .gitignore, que es quien los escribe y quien deja
    de rastrear lo que ya se subía. Va después de la licencia para que el
    LICENSE recién creado también aparezca en el TXT.

    Si algo falla se avisa y la publicación continúa: el TXT es un
    extra, no el objetivo del botón «Publicar».
    """
    job = ctx.job
    if not job.consolidate:
        ctx.log("  No se pidió TXT del proyecto.")
        return
    # Una sola ruta, resuelta una sola vez: la que se ignora, la que se
    # escribe y la que se informa tienen que ser la misma. `realpath`
    # deja fuera cualquier sorpresa con enlaces.
    try:
        # Se resuelve la CARPETA, no el nombre: si en el proyecto hubiera
        # un enlace con el nombre del TXT, resolverlo escribiría donde
        # apunte (fuera del proyecto). Un enlace cuenta como ocupado.
        pedido = Path(job.txt_destination())
        destino = Path(os.path.realpath(str(pedido.parent))) / pedido.name
        destino, renombrado = free_txt_path(destino)
        clean_stale(destino)
    except Exception as e:                                   # noqa: BLE001
        ctx.txt_error = str(e)
        ctx.log(f"  No se pudo elegir dónde guardar el TXT ({e}). La "
                "publicación continúa.", "warn")
        return
    if renombrado:
        ctx.log(f"  Ya había un «{Path(job.txt_destination()).name}» que no "
                f"es mío: no lo toco. El TXT va a «{destino.name}».", "warn")
    rel = relative_inside(destino, ctx.folder)
    if rel:
        # Antes de generarlo: así el patrón ya está puesto aunque la
        # generación falle a medias y deje un TXT incompleto en la
        # carpeta. Nunca se sube un archivo que el usuario no pidió.
        patron = anchor_pattern(rel)
        # Y su temporal de escritura (por si un cierre brusco lo dejara).
        carpeta = rel.rsplit("/", 1)[0] + "/" if "/" in rel else ""
        temporal = ("/" + "/".join(escape_pattern(t) for t in
                                   carpeta.split("/") if t)
                    + ("/" if carpeta else "")
                    + escape_pattern(temp_prefix(destino.name)) + "*"
                    + TEMP_SUFFIX)
        for nuevo in (patron, temporal):
            if nuevo not in ctx.patterns:
                ctx.patterns.append(nuevo)
    try:
        stats = consolidate_project(ctx.folder, destino, ctx.patterns,
                                    respect_ignores=True)
    except Exception as e:                                   # noqa: BLE001
        ctx.txt_error = str(e)
        ctx.log(f"  No se pudo generar el TXT ({e}). La publicación "
                "continúa.", "warn")
        return
    ctx.txt_path, ctx.txt_stats = str(destino), stats
    extra = ""
    if stats.get("binaries") or stats.get("big"):
        extra = (f", omitidos {stats['binaries']} binarios y "
                 f"{stats['big']} enormes")
    if stats.get("truncated"):
        extra += ", ⚠ salida truncada"
    ctx.log(f"  TXT con {stats['files']} archivos, "
            f"{fmt_size(stats['bytes'])}{extra}.", "ok")
    ctx.log(f"  {destino}", "cmd")
    if rel:
        ctx.log("  El TXT está dentro del proyecto: queda excluido de la "
                "publicación (se añade al .gitignore).", "warn")


def step_gitignore(ctx):
    """Antes de la revisión: así git ya sabe qué queda fuera y la
    revisión mira exactamente lo que se subirá."""
    new = append_gitignore(ctx.folder, ctx.patterns)
    if new:
        ctx.log(f"  Añadido: {', '.join(new)}")
    else:
        ctx.log("  Sin cambios necesarios.")
    _avisar_de_patrones_que_parecian_nombres(ctx)
    _dejar_de_rastrear_lo_ignorado(ctx)


def _avisar_de_patrones_que_parecian_nombres(ctx):
    """Si alguien escribe «datos[2024].csv» en el cuadro de ignorar,
    para git eso NO es ese archivo: «[2024]» es «uno de estos
    caracteres». El archivo existe, el patrón está escrito, y no pasa
    nada: el peor silencio posible. Aquí se dice en voz alta y se
    enseña cómo escribirlo.
    """
    raiz = Path(ctx.folder)
    for patron in clean_patterns(ctx.patterns):
        limpio = patron.strip("/")
        if not limpio or not has_wildcards(limpio):
            continue
        try:
            existe = (raiz / limpio).exists()
        except OSError:
            continue
        if existe:
            ctx.log(f"  ⚠ «{patron}» tiene comodines, así que para git no "
                    f"es el archivo que se llama así. Si quieres excluir "
                    f"ESE archivo, escríbelo «{anchor_pattern(limpio)}».",
                    "warn")


def _dejar_de_rastrear_lo_ignorado(ctx):
    """El .gitignore solo sirve para lo que git AÚN no rastrea.

    Si el proyecto ya era un repositorio y esos archivos estaban dentro,
    escribir el patrón no basta: git los sigue subiendo. Aquí se les
    quita el seguimiento (siguen en la carpeta) para que el cuadro de
    ignorar haga lo que promete. Solo se miran los patrones que el
    usuario ha escrito, no las reglas que el .gitignore ya tuviera: un
    archivo añadido a propósito con «git add -f» no debe desaparecer.
    """
    patrones = clean_patterns(ctx.patterns)
    if not patrones:
        return
    # Quien decide qué casa con un patrón es git: sus reglas («**/»,
    # negaciones, escapes, mayúsculas según el sistema de archivos) no
    # son las de fnmatch, y una diferencia aquí deja publicado un
    # archivo que el usuario mandó excluir. El comparador propio queda
    # solo como respaldo si no se puede preguntar.
    sobran = ctx.git.tracked_matching(ctx.folder, patrones)
    if sobran is None:
        matcher = IgnoreMatcher(patrones)
        if matcher.empty:
            return
        sobran = [rel for rel in ctx.git.tracked_files(ctx.folder)
                  if matcher.ignored_path(rel.lower())]
    if not sobran:
        return
    _untrack(ctx, sobran)
    muestra = ", ".join(sobran[:3]) + ("…" if len(sobran) > 3 else "")
    ctx.log(f"  {len(sobran)} archivo(s) que ya se subían dejan de "
            f"subirse ({muestra}): si estaban en GitHub, desaparecerán de "
            "allí. Siguen en tu carpeta.", "warn")


# Toda ruta que se pasa a `checkout`, `reset`, `rm` o `add` es el nombre
# EXACTO de un archivo. Sin esto git la lee como patrón: «[ab].txt» casa
# también con «a.txt», y un archivo llamado «:!a.txt» significa «todo
# menos a.txt»: `checkout FETCH_HEAD -- ':!a.txt'` pisaba TODOS tus
# archivos modificados con la versión de GitHub, y `rm --cached` dejaba
# de publicar el proyecto entero.
LITERAL = ["--literal-pathspecs"]


def _untrack(ctx, paths):
    """Si algo excluido ya estaba en un commit anterior, hay que dejar de
    rastrearlo (el archivo sigue en tu PC). Se apunta: si estaba en
    GitHub, desaparecerá de allí, y la vista previa tiene que decirlo
    también en el modo agregar/editar."""
    ctx.untracked_by_request.update(p.rstrip("/") for p in paths if p)
    for i in range(0, len(paths), 50):
        ctx.run(LITERAL + ["rm", "-r", "--cached", "--ignore-unmatch", "--",
                           *paths[i:i + 50]], check=False)


def _requested_removal(ctx, path):
    """¿Desaparece `path` de GitHub porque se excluyó a propósito?"""
    for p in ctx.untracked_by_request:
        if path == p or path.startswith(p + "/"):
            return True
    return False


def step_security_review(ctx):
    job = ctx.job
    if not job.check:
        ctx.log("  Desactivada en Opciones; se omite.", "warn")
        return
    entries = ctx.git.list_files(ctx.folder)
    findings, stats = scan_entries(entries)
    for texto, etiqueta in stats_lines(stats):
        ctx.log("  " + texto, etiqueta)
    if not findings:
        ctx.log("  ✔ Sin archivos grandes ni secretos a la vista.", "ok")
        return
    serios = [f for f in findings if f.get("confidence", "alta") != "baja"]
    if job.auto:
        # En modo automático nunca subimos con hallazgos creíbles ni
        # abrimos ventanas: se pausa y se avisa. Lo que solo «parece» un
        # secreto no puede dejar la vigilancia parada para siempre.
        if serios:
            ctx.log(f"  ⚠ {len(serios)} hallazgo(s).", "warn")
            raise Blocked("La revisión encontró archivos grandes o secretos: "
                          "no he subido nada. Revísalos con «🛡 Analizar».")
        ctx.log(f"  ⚠ {len(findings)} aviso(s) poco probables; continúo.",
                "warn")
        return
    ctx.log(f"  ⚠ {len(findings)} hallazgo(s). Esperando tu decisión…",
            "warn")
    decision, selected = ctx.cb.ask_review(findings)
    ctx.check_cancel()
    if decision == "cancel":
        raise Cancelled("Publicación cancelada. No se ha subido nada.")
    if decision == "ignore" and selected:
        patterns = [anchor_pattern(p) for p in selected]
        ctx.patterns.extend(p for p in patterns if p not in ctx.patterns)
        new = append_gitignore(ctx.folder, patterns)
        if new:
            ctx.log(f"  Añadido al .gitignore: {', '.join(new)}")
        _untrack(ctx, list(selected))
        ctx.log(f"  Se excluirán {len(selected)} elemento(s).")
    else:
        ctx.log("  Continuamos sin excluir nada.", "warn")


def step_stage(ctx):
    # Los repositorios anidados sin registrar se dejan fuera ANTES de
    # `git add`: los convertiría en un enlace roto y, si aún no tienen
    # ningún commit, `git add` fallaría entero y no se podría publicar.
    fuera = nested_repos_to_skip(ctx.git, ctx.folder)
    excluir = ([f":(exclude,literal){r}" for r in fuera])
    if ctx.job.addedit:
        # --ignore-removal registra archivos nuevos y modificados, pero
        # NO los borrados: lo que ya no está en la carpeta sigue
        # existiendo en el repositorio.
        ctx.run(["add", "--ignore-removal", "--", "."] + excluir)
    else:
        ctx.run(["add", "-A", "--", "."] + excluir)
    # Red de seguridad: un enlace nuevo que se colara igualmente.
    quitados = _quitar_repos_embebidos(ctx)
    avisar = sorted(set(fuera) | set(quitados))
    if avisar:
        muestra = ", ".join(f"«{r}»" for r in avisar[:3]) + (
            f" y {len(avisar) - 3} más" if len(avisar) > 3 else "")
        if len(avisar) == 1:
            que = ("es otro repositorio de git dentro del proyecto (tiene "
                   "su propia carpeta .git), así que NO se sube")
        else:
            que = ("son otros repositorios de git dentro del proyecto "
                   "(tienen su propia carpeta .git), así que NO se suben")
        ctx.log(f"  ⚠ {muestra}: {que}. Git solo guardaría un enlace a un "
                "commit que GitHub no tiene y ninguno de sus archivos. Si "
                "quieres publicar esos archivos aquí, quita su carpeta .git; "
                "si es un proyecto aparte, publícalo por separado.", "warn")


def registered_submodules(git, folder):
    """Rutas de los submódulos declarados en el .gitmodules de la carpeta."""
    registrados = set()
    if os.path.isfile(os.path.join(folder, ".gitmodules")):
        q = git.run(["config", "-z", "--file", ".gitmodules",
                     "--get-regexp", r"^submodule\..*\.path$"], cwd=folder,
                    check=False, echo=False, binary=True)
        for item in (q.stdout or b"").split(b"\0"):
            _, sep, valor = item.partition(b"\n")
            if sep:
                registrados.add(os.fsdecode(valor).strip().strip("/"))
    return registrados


def nested_repos_to_skip(git, folder):
    """Carpetas SIN RASTREAR (y no ignoradas) que son otro repositorio
    de git y no están declaradas como submódulo: se quedan fuera de la
    publicación."""
    p = git.run(["status", "--porcelain", "-z", "--untracked-files=all"],
                cwd=folder, check=False, echo=False, binary=True)
    candidatos = []
    for item in (p.stdout or b"").split(b"\0"):
        if item.startswith(b"?? ") and item.endswith(b"/"):
            rel = os.fsdecode(item[3:]).rstrip("/")
            if os.path.lexists(os.path.join(folder, rel, ".git")):
                candidatos.append(rel)
    if not candidatos:
        return []
    registrados = registered_submodules(git, folder)
    return [r for r in candidatos if r not in registrados]


def embedded_repos(git, folder):
    """Rutas que el índice acaba de registrar como OTRO repositorio
    (gitlink, modo 160000) sin ser un submódulo de .gitmodules.

    Es lo que hace `git add` al encontrar una carpeta con su propio .git
    dentro del proyecto: no guarda sus archivos, solo un enlace a un
    commit de ese repositorio que GitHub no tiene. Solo cuentan los
    enlaces NUEVOS (o un archivo que pasa a serlo): uno que ya estaba
    publicado se deja como está.
    """
    p = git.run(["diff", "--cached", "--raw", "-z", "--no-renames",
                 "--no-abbrev"], cwd=folder, check=False, echo=False,
                binary=True)
    partes = (p.stdout or b"").split(b"\0")
    nuevos = []
    for i in range(0, len(partes) - 1, 2):
        campos = partes[i].lstrip(b":").split()
        if len(campos) >= 5 and campos[1] == b"160000" \
                and campos[0] != b"160000":
            nuevos.append(os.fsdecode(partes[i + 1]))
    if not nuevos:
        return []
    registrados = registered_submodules(git, folder)
    return [r for r in nuevos if r not in registrados]


def _quitar_repos_embebidos(ctx):
    """Saca del índice los enlaces nuevos a repositorios anidados que no
    son submódulos (publicarlos dejaría en GitHub una carpeta que no se
    puede abrir y ninguno de sus archivos). Devuelve las rutas."""
    embebidos = embedded_repos(ctx.git, ctx.folder)
    if embebidos:
        # Solo el índice: `reset` con rutas no toca la carpeta, y en una
        # rama sin commits también funciona (quita la entrada).
        ctx.git.run(["--literal-pathspecs", "reset", "-q", "--"]
                    + embebidos, cwd=ctx.folder, check=True, echo=False)
    return embebidos


def _ensure_branch(ctx):
    """La carpeta manda y la rama es solo el destino: la rama local
    actual pasa a llamarse como la elegida, con el contenido de la
    carpeta tal cual (nunca se cambia de rama con checkout, que
    alteraría los archivos del usuario). Lo que haya en GitHub en esa
    rama se combina después, en el paso de merge.

    Si ya existía OTRA rama local con ese nombre, primero se la aparta:
    `branch -M` es `--force` y la borraría con todos sus commits, que
    solo quedarían en el reflog (y un `git gc` se los llevaría). Aquí
    nada destructivo se hace sin dejar antes algo recuperable.
    """
    job = ctx.job
    current = ctx.git.current_branch(ctx.folder)
    if current == job.branch:
        return
    _apartar_rama_existente(ctx, job.branch)
    p = ctx.run(["branch", "-M", job.branch], check=False)
    if p.returncode != 0:
        ctx.run(["checkout", "-B", job.branch])
    if current:
        ctx.log(f"  La rama local «{current}» ahora se llama "
                f"«{job.branch}».")


def _apartar_rama_existente(ctx, destino):
    """Renombra la rama `destino` a «destino-apartada-FECHA» si existe.

    Nada se pierde: la rama sigue ahí con otro nombre y el usuario puede
    volver a ella cuando quiera.
    """
    ramas = set(ctx.git.local_branches(ctx.folder))
    if destino not in ramas:
        return
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    nombre, i = f"{destino}-apartada-{stamp}", 2
    while nombre in ramas:
        nombre, i = f"{destino}-apartada-{stamp}-{i}", i + 1
    p = ctx.run(["branch", "-m", destino, nombre], check=False)
    if p.returncode != 0:
        raise StepError(
            f"Ya existe una rama local «{destino}» con su propio historial "
            f"y no he podido apartarla, así que no sigo: publicar la "
            f"borraría. Renómbrala o bórrala tú y vuelve a intentarlo.")
    ctx.log(f"  Ya había una rama local «{destino}»: la he guardado como "
            f"«{nombre}» para no perder sus commits.", "warn")


def step_branch_and_remote(ctx):
    job = ctx.job
    _ensure_branch(ctx)
    ctx.git.ensure_remote(ctx.folder, job.url)
    if ctx.force:
        return
    # Siempre la RAMA, con su nombre completo: con «git fetch origin
    # main» a secas, una ETIQUETA llamada «main» en GitHub ganaría a la
    # rama y se combinaría con otra cosa.
    origen = f"refs/heads/{job.branch}"
    p = ctx.run(["fetch", "origin", origen], check=False, timeout=900,
                auth=True)
    if p.returncode != 0 and ctx.auth and _auth_failed(p):
        ctx.drop_auth("El token guardado no fue aceptado.")
        p = ctx.run(["fetch", "origin", origen], check=False, timeout=900)
    ctx.remote_exists = p.returncode == 0
    if not ctx.remote_exists:
        ctx.log("  La rama aún no existe en GitHub: se publicará por "
                "primera vez.")


@dataclass
class PreviewChoice:
    """Lo que el usuario decidió en la vista previa.

    Las tres listas sacan archivos de ESTA publicación sin tocar el resto:
    lo borrado se conserva en GitHub, lo nuevo no se sube y lo modificado
    se queda como está allí.
    """
    go: bool = True
    keep_deleted: list = field(default_factory=list)
    restore_to_folder: bool = True    # los mantenidos vuelven a la carpeta
    skip_added: list = field(default_factory=list)
    ignore_skipped: bool = True       # los no subidos van al .gitignore
    skip_modified: list = field(default_factory=list)

    @classmethod
    def from_answer(cls, answer):
        """Admite un PreviewChoice, True/False o la tupla antigua
        (seguir, mantener, copiar_a_la_carpeta)."""
        if isinstance(answer, cls):
            return answer
        if isinstance(answer, (tuple, list)):
            return cls(go=bool(answer[0]) if answer else False,
                       keep_deleted=list(answer[1])
                       if len(answer) > 1 and answer[1] else [],
                       restore_to_folder=bool(answer[2])
                       if len(answer) > 2 else True)
        return cls(go=bool(answer))

    @property
    def empty(self):
        return not (self.keep_deleted or self.skip_added
                    or self.skip_modified)


def _batched(ctx, cmd, paths, size=50):
    """Una llamada a git por cada 50 rutas, no una por archivo. Las rutas
    van SIEMPRE como literales (ver `LITERAL`)."""
    for i in range(0, len(paths), size):
        ctx.run(LITERAL + cmd + paths[i:i + size])


def _blocked(root, rel):
    """¿Hay un ARCHIVO (o un enlace) donde `rel` necesita una carpeta?
    `git checkout` lo borraría para poder crear la carpeta: un archivo
    tuyo, quizá ignorado y sin copia en ningún sitio."""
    partes = rel.split("/")
    for i in range(1, len(partes)):
        tramo = root.joinpath(*partes[:i])
        try:
            if not os.path.isdir(tramo) or os.path.islink(tramo):
                return os.path.lexists(tramo)
        except OSError:
            return True
    return False


def apply_preview_choices(ctx, choice, added, modified, deleted):
    """Deja el índice como pidió el usuario en la vista previa.

    · mantener un borrado  → vuelve al índice desde FETCH_HEAD (lo que hay
      hoy en GitHub) y, si se pide, también a la carpeta;
    · no subir un archivo nuevo → sale del índice (el archivo sigue en tu
      PC) y, si se pide, se añade al .gitignore para no volver a
      preguntar;
    · no actualizar un modificado → el índice recupera la versión de
      GitHub y TU archivo no se toca; como sigue siendo distinto, la
      próxima publicación lo volverá a proponer.
    """
    keep = [p for p in choice.keep_deleted if p in set(deleted)]
    skip_mod = [p for p in choice.skip_modified if p in set(modified)]
    skip_add = [p for p in choice.skip_added if p in set(added)]

    if keep:
        # Un archivo puede faltar en GitHub-vs-índice y aun así EXISTIR en la
        # carpeta (por ejemplo si está ignorado). Ahí `git checkout` lo
        # machacaría con la versión remota y se perdería el contenido local,
        # que al no estar rastreado no se podría recuperar: a esos solo se
        # les devuelve la entrada al índice.
        root = Path(ctx.folder)
        present = [p for p in keep
                   if not choice.restore_to_folder
                   or os.path.lexists(root / p) or _blocked(root, p)]
        missing = [p for p in keep if p not in set(present)]
        if missing:
            _batched(ctx, ["checkout", "FETCH_HEAD", "--"], missing)
            ctx.log(f"  Se mantienen {len(missing)} archivo(s): siguen en "
                    "GitHub y vuelven a tu carpeta.", "ok")
        if present:
            _batched(ctx, ["reset", "-q", "FETCH_HEAD", "--"], present)
            ctx.log(f"  Se mantienen {len(present)} archivo(s): siguen en "
                    "GitHub; tu copia local no se toca.", "ok")

    if skip_mod:
        _batched(ctx, ["reset", "-q", "FETCH_HEAD", "--"], skip_mod)
        ctx.log(f"  {len(skip_mod)} archivo(s) no se actualizan en GitHub; "
                "tu versión local se queda como está.", "ok")

    if skip_add:
        _batched(ctx, ["rm", "-q", "--cached", "-f", "--ignore-unmatch", "--"],
                 skip_add)
        ctx.log(f"  {len(skip_add)} archivo(s) no se suben; siguen en tu "
                "carpeta.", "ok")
        if choice.ignore_skipped:
            patterns = [anchor_pattern(p) for p in skip_add]
            ctx.patterns.extend(p for p in patterns
                                if p not in ctx.patterns)
            new = append_gitignore(ctx.folder, patterns)
            if new:
                ctx.log(f"  Añadido al .gitignore: {', '.join(new[:5])}"
                        + (f" y {len(new) - 5} más" if len(new) > 5 else ""))
                # Salvo que el propio .gitignore sea uno de los archivos que
                # el usuario dejó fuera: entonces su decisión manda.
                if ".gitignore" not in set(skip_add + skip_mod + keep):
                    ctx.run(["add", "--", ".gitignore"])


def step_preview(ctx):
    job = ctx.job
    if not job.preview:
        ctx.log("  Vista previa desactivada en Opciones.")
        return
    if job.auto:
        ctx.log("  Publicación automática: se omite la vista previa.")
        return
    added, modified, deleted = ctx.git.diff_vs_remote(
        ctx.folder, ctx.remote_exists and not ctx.force)
    if job.addedit:
        # En este modo lo que falta en tu carpeta se conserva en GitHub.
        # Lo que SÍ desaparece es lo que se excluyó a propósito (cuadro
        # de ignorar, revisión de seguridad): eso se anuncia y se puede
        # mantener, como cualquier otro borrado.
        deleted = [p for p in deleted if _requested_removal(ctx, p)]
    if not (added or modified or deleted):
        ctx.log("  Nada que cambiar: GitHub ya está al día.")
        return
    ctx.log(f"  {len(added)} nuevos, {len(modified)} modificados, "
            f"{len(deleted)} a borrar.")
    choice = PreviewChoice.from_answer(
        ctx.cb.ask_preview(added, modified, deleted, job.mode))
    ctx.check_cancel()
    if not choice.go:
        raise Cancelled("Publicación cancelada en la vista previa. No se "
                        "ha subido nada.")
    if not choice.empty:
        apply_preview_choices(ctx, choice, added, modified, deleted)


def step_commit(ctx):
    args = ["commit", "-m", ctx.job.commit_message()]
    if ctx.job.auto_message:
        subject, body = describe_changes(*ctx.git.staged_changes(ctx.folder))
        if subject:
            args = ["commit", "-m", subject] + (["-m", body] if body else [])
            ctx.log(f"  Mensaje: {subject}")
    p = ctx.run(args, check=False)
    if p.returncode == 0:
        return
    out = ((p.stdout or "") + (p.stderr or "")).lower()
    if "nothing to commit" in out or "nothing added to commit" in out \
            or "no changes added to commit" in out:
        ctx.log("  ⚠ No hay cambios nuevos; se intentará el push "
                "igualmente.", "warn")
    elif "tell me who you are" in out:
        raise StepError("Git no sabe quién eres. Completa nombre y correo "
                        "en «Identidad Git».")
    else:
        raise StepError("Falló el commit. Revisa la consola.")


def step_merge_remote(ctx):
    """Combina con lo que ya hay en GitHub para que el push normal
    funcione sin forzar y sin perder el historial.
      · agregar/editar → «-X ours»: fusiona los dos árboles; tus
        versiones mandan en lo que coincide y lo que solo está en
        GitHub se conserva.
      · espejo → «-s ours»: el árbol queda EXACTAMENTE como tu carpeta,
        pero los commits del remoto siguen en el historial."""
    if ctx.force:
        ctx.log("  Push forzado activado: se omite la combinación. El "
                "historial remoto será reemplazado por el tuyo.", "warn")
        return
    if not ctx.remote_exists:
        ctx.log("  Primera publicación de esta rama: nada que combinar.")
        return
    if ctx.git.is_ancestor(ctx.folder, "FETCH_HEAD", "HEAD"):
        ctx.log("  Ya tenías todo lo del remoto; nada que combinar.")
        return
    strategy = ["-X", "ours"] if ctx.job.addedit else ["-s", "ours"]
    # «-s ours» deja el árbol exactamente como el tuyo: no escribe nada
    # en la carpeta. «-X ours» sí trae lo que solo está en GitHub, y git
    # pisa SIN AVISAR los archivos ignorados que estén en esas rutas
    # (un .env tuyo que GitHub también tiene). Se apartan antes y vuelven
    # después, con una copia exacta por si algo fallara a medias.
    apartados = _apartar_lo_expuesto(ctx) if ctx.job.addedit else None
    try:
        p = ctx.run(["merge", "--allow-unrelated-histories", *strategy,
                     "--no-edit", "FETCH_HEAD"], check=False)
        if p.returncode != 0:
            ctx.run(["merge", "--abort"], check=False)
    finally:
        if apartados:
            _devolver_lo_apartado(ctx, *apartados)
    if p.returncode != 0:
        out = ((p.stdout or "") + (p.stderr or "")).lower()
        if "tell me who you are" in out:
            raise StepError("Git no sabe quién eres. Completa nombre y "
                            "correo en «Identidad Git».")
        raise StepError("No pude combinar tu carpeta con lo que hay en "
                        "GitHub.\n  Prueba con «⬇ Clonar repositorio…» "
                        "para traerlo primero, copia ahí tus archivos y "
                        "vuelve a publicar.")
    if ctx.job.addedit:
        ctx.log("  Lo que solo estaba en GitHub se conserva; tus versiones "
                "mandan en lo que coincide.")
    else:
        ctx.log("  GitHub quedará igual que tu carpeta, conservando el "
                "historial anterior.")


def _apartar_lo_expuesto(ctx):
    """Archivos tuyos SIN rastrear (ignorados casi siempre) que el merge
    escribiría por estar también en GitHub: copia exacta en una rama de
    respaldo y fuera de su sitio mientras dura el merge.

    Devuelve (carpeta_temporal, rutas, rama) o None si no hay ninguno.
    """
    expuestos = files_at_risk(ctx.git, ctx.folder, "FETCH_HEAD")
    if not expuestos:
        return None
    rama = make_backup(ctx.git, ctx.folder, "antes de combinar con GitHub",
                       identity=_identity(ctx.job), at_risk=expuestos)
    destino = tempfile.mkdtemp(prefix="gitpush-apartados-",
                               dir=str(git_dir(ctx.git, ctx.folder)))
    movidos = []
    try:
        for rel in expuestos:
            dst = os.path.join(destino, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(os.path.join(ctx.folder, rel), dst)
            movidos.append(rel)
    except OSError as e:
        _devolver_lo_apartado(ctx, destino, movidos, rama)
        raise StepError(
            f"No pude apartar «{expuestos[len(movidos)]}» para protegerlo "
            f"durante la combinación ({e.strerror or e}). No he subido "
            "nada; ciérralo si está abierto en otro programa y vuelve a "
            "intentarlo.") from None
    muestra = ", ".join(expuestos[:3]) + ("…" if len(expuestos) > 3 else "")
    ctx.log(f"  🛡 {len(expuestos)} archivo(s) tuyos que no se publican "
            f"también existen en GitHub ({muestra}): GitHub conserva los "
            "suyos y los tuyos no se tocan."
            + (f" Copia exacta en «{rama}»." if rama else ""), "warn")
    return destino, movidos, rama


def _devolver_lo_apartado(ctx, destino, movidos, rama):
    """Devuelve a su sitio lo apartado, por encima de lo que el merge
    haya escrito en esas rutas (lo de GitHub sigue en el commit)."""
    perdidos = []
    for rel in movidos:
        src = os.path.join(destino, rel)
        dst = os.path.join(ctx.folder, rel)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.isdir(dst) and not os.path.islink(dst):
                raise IsADirectoryError(dst)
            os.replace(src, dst)
        except OSError:
            perdidos.append(rel)
    if perdidos:
        ctx.log(f"  ⚠ No pude devolver {len(perdidos)} archivo(s) a su "
                f"sitio (por ejemplo «{perdidos[0]}»: lo que llegó de GitHub "
                "ocupa esa ruta con un archivo o una carpeta). Están "
                f"intactos en «{destino}»"
                + (f" y en la rama «{rama}»." if rama else "."), "warn")
    else:
        shutil.rmtree(destino, ignore_errors=True)


def _identity(job):
    return (job.user, job.email) if job.user and job.email else None


def _auth_failed(p):
    return looks_like_auth_error(((p.stdout or "") + (p.stderr or "")).lower())


def step_push(ctx):
    job = ctx.job
    destino = f"refs/heads/{job.branch}"
    args = ["push", "-u"] + (["-f"] if ctx.force else []) \
        + ["origin", f"{destino}:{destino}"]
    p = ctx.run(args, check=False, timeout=1800, auth=True)
    if p.returncode != 0 and ctx.auth and _auth_failed(p):
        # El token puede haber caducado: que decida el gestor de
        # credenciales de siempre en vez de dejar al usuario tirado.
        ctx.drop_auth("El token guardado no fue aceptado.")
        p = ctx.run(args, check=False, timeout=1800)
    if p.returncode != 0:
        out = ((p.stdout or "") + (p.stderr or "")).lower()
        raise StepError(push_error_message(out))


STEPS = (
    ("Comprobando que Git esté instalado…",              step_check_git),
    ("Preparando el repositorio local…",                 step_init_repo),
    ("Configurando la identidad Git…",                   step_identity),
    ("Añadiendo la licencia…",                           step_license),
    ("Uniendo el proyecto en un TXT…",                   step_consolidate),
    ("Actualizando .gitignore…",                         step_gitignore),
    ("Revisión de seguridad (archivos grandes y "
     "secretos)…",                                       step_security_review),
    ("Añadiendo archivos…",                              step_stage),
    ("Fijando la rama y consultando GitHub…",            step_branch_and_remote),
    ("Comprobando qué cambiará en GitHub…",              step_preview),
    ("Creando el commit…",                               step_commit),
    ("Combinando con lo que ya hay en GitHub…",          step_merge_remote),
    ("Subiendo a GitHub (git push)…",                    step_push),
)
TOTAL_STEPS = len(STEPS)


# ------------------------------------------------------------------ #
#  Ejecución                                                          #
# ------------------------------------------------------------------ #
def publish(job, callbacks=None, cancel=None, git=None):
    """Publica un proyecto. Nunca lanza: devuelve un PublishResult."""
    cb = callbacks or Callbacks()
    error = job.validate()
    if error:
        return PublishResult(job, "error", error)
    ctx = PublishContext(job, cb, cancel, git)
    cb.log("🔀 Modo agregar/editar: no se borrará nada de GitHub."
           if job.addedit else
           "🔀 Modo espejo: GitHub quedará igual que tu carpeta.", "dim")
    if job.auto:
        cb.log("🤖 Publicación automática en marcha.", "dim")
    def result(status, message="", web=""):
        # El TXT viaja en todos los casos: si ya se generó, el usuario
        # tiene que saberlo aunque la subida se detenga después.
        return PublishResult(job, status, message, web,
                             time.time() - ctx.t0,
                             txt_path=ctx.txt_path, txt_stats=ctx.txt_stats,
                             txt_error=ctx.txt_error)

    try:
        for n, (label, step) in enumerate(STEPS, 1):
            ctx.check_cancel()
            cb.progress(n, TOTAL_STEPS, label)
            cb.log(f"[{n}/{TOTAL_STEPS}] {label}", "out")
            step(ctx)
        web = repo_web_url(job.url)
        cb.log(f"✔ ¡Proyecto publicado en {time.time() - ctx.t0:.1f} s!",
               "ok")
        cb.log(f"  {web}", "cmd")
        return result("ok", "", web)
    except Cancelled as e:
        cb.log(f"✋ {e}", "warn")
        return result("cancelled", str(e))
    except Blocked as e:
        cb.log(f"🤖 {e}", "warn")
        return result("blocked", str(e))
    except StepError as e:
        cb.log(f"✖ {e}", "err")
        return result("error", str(e))
    except Exception as e:                                  # imprevisto
        cb.log(f"✖ Error inesperado: {e}", "err")
        return result("error", f"Error inesperado: {e}")


def _url_key(url):
    return repo_web_url(url).rstrip("/").lower()


def _folder_key(folder):
    return os.path.normcase(os.path.abspath(folder))


def publish_many(jobs, callbacks_for=None, workers=3, cancel=None):
    """Publica varios proyectos a la vez.

    Proyectos distintos con remotos distintos van en paralelo (hasta
    `workers`). Los que comparten remoto se ejecutan en serie entre sí
    (dos push simultáneos al mismo repositorio se pisarían), y una misma
    carpeta nunca se publica dos veces a la vez. `callbacks_for(job)`
    devuelve los Callbacks de cada trabajo, para que la interfaz pueda
    dar a cada uno su fila de progreso. Devuelve los resultados en el
    orden de `jobs`.
    """
    jobs = list(jobs)
    if not jobs:
        return []
    callbacks_for = callbacks_for or (lambda job: Callbacks())
    cancel = cancel or threading.Event()
    folder_locks, locks_guard = {}, threading.Lock()

    def folder_lock(job):
        key = _folder_key(job.folder)
        with locks_guard:
            return folder_locks.setdefault(key, threading.Lock())

    groups = OrderedDict()
    for i, job in enumerate(jobs):
        groups.setdefault(_url_key(job.url), []).append(i)

    results = [None] * len(jobs)

    def run_group(indices):
        for i in indices:
            job = jobs[i]
            if cancel.is_set():
                results[i] = PublishResult(job, "cancelled",
                                           "Publicación detenida.")
                continue
            with folder_lock(job):
                results[i] = publish(job, callbacks_for(job), cancel)

    with ThreadPoolExecutor(max_workers=max(1, min(workers,
                                                   len(groups)))) as ex:
        list(ex.map(run_group, groups.values()))
    return results
