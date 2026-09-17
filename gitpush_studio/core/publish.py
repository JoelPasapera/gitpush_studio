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
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .errors import Blocked, Cancelled, StepError
from .fs import normalize_path
from .git import Git, push_error_message
from .history import describe_changes
from .ignore import anchor_pattern, append_gitignore, clean_patterns
from .scan import scan_entries
from .urls import is_remote_url, repo_web_url

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
    auto: bool = False
    auto_message: bool = False            # describir los cambios en el commit
    name: str = ""                        # etiqueta (p. ej. el perfil)

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
        )

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
        self.force = job.force and not job.addedit
        self.remote_exists = False
        self.t0 = time.time()

    def log(self, text, tag="dim"):
        self.cb.log(text, tag)

    def check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled("Publicación detenida. No se ha subido nada.")

    def run(self, args, check=True, timeout=900):
        """git en la carpeta del proyecto."""
        return self.git.run(args, cwd=self.folder, check=check,
                            timeout=timeout)


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


def step_gitignore(ctx):
    """Antes de la revisión: así git ya sabe qué queda fuera y la
    revisión mira exactamente lo que se subirá."""
    new = append_gitignore(ctx.folder, ctx.patterns)
    if new:
        ctx.log(f"  Añadido: {', '.join(new)}")
    else:
        ctx.log("  Sin cambios necesarios.")


def _untrack(ctx, paths):
    """Si algo excluido ya estaba en un commit anterior, hay que dejar de
    rastrearlo (el archivo sigue en tu PC)."""
    for i in range(0, len(paths), 50):
        ctx.run(["rm", "-r", "--cached", "--ignore-unmatch", "--",
                 *paths[i:i + 50]], check=False)


def step_security_review(ctx):
    job = ctx.job
    if not job.check:
        ctx.log("  Desactivada en Opciones; se omite.", "warn")
        return
    entries = ctx.git.list_files(ctx.folder)
    findings, stats = scan_entries(entries)
    ctx.log(f"  Revisados {stats['files']} archivos en "
            f"{stats['seconds']:.1f} s.")
    if not findings:
        ctx.log("  ✔ Sin archivos grandes ni secretos a la vista.", "ok")
        return
    if job.auto:
        # En modo automático nunca subimos con hallazgos ni abrimos
        # ventanas: se pausa y se avisa.
        ctx.log(f"  ⚠ {len(findings)} hallazgo(s).", "warn")
        raise Blocked("La revisión encontró archivos grandes o secretos: "
                      "no he subido nada. Revísalos con «🛡 Analizar».")
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
    if ctx.job.addedit:
        # --ignore-removal registra archivos nuevos y modificados, pero
        # NO los borrados: lo que ya no está en la carpeta sigue
        # existiendo en el repositorio.
        ctx.run(["add", "--ignore-removal", "."])
    else:
        ctx.run(["add", "-A"])


def _ensure_branch(ctx):
    """La carpeta manda y la rama es solo el destino: la rama local
    actual pasa a llamarse como la elegida, con el contenido de la
    carpeta tal cual (nunca se cambia de rama con checkout, que
    alteraría los archivos del usuario). Lo que haya en GitHub en esa
    rama se combina después, en el paso de merge."""
    job = ctx.job
    current = ctx.git.current_branch(ctx.folder)
    if current == job.branch:
        return
    p = ctx.run(["branch", "-M", job.branch], check=False)
    if p.returncode != 0:
        ctx.run(["checkout", "-B", job.branch])
    if current:
        ctx.log(f"  La rama local «{current}» ahora se llama "
                f"«{job.branch}».")


def step_branch_and_remote(ctx):
    job = ctx.job
    _ensure_branch(ctx)
    ctx.git.ensure_remote(ctx.folder, job.url)
    if ctx.force:
        return
    p = ctx.run(["fetch", "origin", job.branch], check=False, timeout=900)
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
    """Una llamada a git por cada 50 rutas, no una por archivo."""
    for i in range(0, len(paths), size):
        ctx.run(cmd + paths[i:i + size])


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
                   if not choice.restore_to_folder or (root / p).exists()]
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
        deleted = []          # en este modo lo que falta en local se conserva
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
    p = ctx.run(["merge", "--allow-unrelated-histories", *strategy,
                 "--no-edit", "FETCH_HEAD"], check=False)
    if p.returncode != 0:
        ctx.run(["merge", "--abort"], check=False)
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


def step_push(ctx):
    job = ctx.job
    args = ["push", "-u"] + (["-f"] if ctx.force else []) \
        + ["origin", job.branch]
    p = ctx.run(args, check=False, timeout=1800)
    if p.returncode != 0:
        out = ((p.stdout or "") + (p.stderr or "")).lower()
        raise StepError(push_error_message(out))


STEPS = (
    ("Comprobando que Git esté instalado…",              step_check_git),
    ("Preparando el repositorio local…",                 step_init_repo),
    ("Configurando la identidad Git…",                   step_identity),
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
    try:
        for n, (label, step) in enumerate(STEPS, 1):
            ctx.check_cancel()
            cb.progress(n, TOTAL_STEPS, label)
            cb.log(f"[{n}/{TOTAL_STEPS}] {label}", "out")
            step(ctx)
        web = repo_web_url(job.url)
        secs = time.time() - ctx.t0
        cb.log(f"✔ ¡Proyecto publicado en {secs:.1f} s!", "ok")
        cb.log(f"  {web}", "cmd")
        return PublishResult(job, "ok", "", web, secs)
    except Cancelled as e:
        cb.log(f"✋ {e}", "warn")
        return PublishResult(job, "cancelled", str(e), "",
                             time.time() - ctx.t0)
    except Blocked as e:
        cb.log(f"🤖 {e}", "warn")
        return PublishResult(job, "blocked", str(e), "",
                             time.time() - ctx.t0)
    except StepError as e:
        cb.log(f"✖ {e}", "err")
        return PublishResult(job, "error", str(e), "",
                             time.time() - ctx.t0)
    except Exception as e:                                  # imprevisto
        cb.log(f"✖ Error inesperado: {e}", "err")
        return PublishResult(job, "error", f"Error inesperado: {e}", "",
                             time.time() - ctx.t0)


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
