# -*- coding: utf-8 -*-
"""Preparar VARIAS publicaciones a la misma cuenta de GitHub.

Una `BatchEntry` es una fila de la lista: una carpeta, el nombre que
tendrá su repositorio y si además se quiere el TXT del proyecto. Este
módulo solo decide; no publica nada:

    suggest_repo_name   nombre de repositorio válido a partir de una carpeta
    validate_entries    todo lo que puede estar mal, ANTES de tocar GitHub
    resolve_entries     nombre → URL, creando en GitHub los que falten
    jobs_from_entries   filas ya resueltas → PublishJob

Separar validar de resolver importa: validar no gasta ni una petición ni
escribe nada, así que la interfaz puede avisar de los errores mientras el
usuario escribe. Resolver sí habla con GitHub, y lo hace una sola vez por
fila, antes de que empiece cualquier `git push`: si el token está mal o
un nombre está ocupado, se sabe antes de haber subido nada.
"""

import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .errors import StepError
from .fs import normalize_path
from .github import create_repo, whoami
from .publish import PublishJob
from .urls import parse_owner_repo, repo_web_url

# GitHub acepta letras, números, punto, guion y subrayado. Todo lo demás
# se convierte en guion.
_INVALID = re.compile(r"[^A-Za-z0-9._-]+")
_DASHES = re.compile(r"-{2,}")
MAX_NAME = 100
RESERVED_NAMES = ("", ".", "..")


def _sin_tildes(text):
    """«ñandú» → «nandu».

    GitHub cambia por guiones todo lo que no sea ASCII, así que «Cálculo
    Numérico» se quedaría en «C-lculo-Num-rico». Quitar la tilde y dejar
    la letra base conserva el nombre: en español es el caso normal, no
    una rareza. Lo que no tenga equivalente ASCII (otro alfabeto, un
    emoji) sí acaba en guion.
    """
    descompuesto = unicodedata.normalize("NFKD", text)
    return "".join(c for c in descompuesto
                   if not unicodedata.combining(c))


def suggest_repo_name(folder):
    """Nombre de repositorio a partir de una carpeta: «Mi App v2» →
    «Mi-App-v2». Devuelve "" si no queda nada utilizable."""
    name = Path(normalize_path(str(folder or "")) or "").name
    if not name:
        # Una raíz («C:\» o «/»): no hay nombre que proponer.
        return ""
    name = _DASHES.sub("-", _INVALID.sub("-", _sin_tildes(name)))
    name = name.strip("-._")
    return name[:MAX_NAME] if name not in RESERVED_NAMES else ""


def check_repo_name(name):
    """Mensaje de error del nombre de repositorio, o None si vale."""
    n = (name or "").strip()
    if not n:
        return "Escribe el nombre del repositorio."
    if n in RESERVED_NAMES:
        return f"«{n}» no es un nombre de repositorio válido."
    if len(n) > MAX_NAME:
        return f"El nombre no puede pasar de {MAX_NAME} caracteres."
    sobra = sorted(set(_INVALID.findall(n)))
    if sobra:
        return ("GitHub solo admite letras, números, punto, guion y "
                "subrayado. Quita: " + " ".join(repr(s) for s in sobra))
    return None


@dataclass
class BatchEntry:
    """Una fila de la lista de proyectos a publicar."""
    folder: str = ""
    repo: str = ""
    consolidate: bool = True          # el usuario puede desmarcarlo
    url: str = ""                     # lo rellena `resolve_entries`
    web_url: str = ""
    created: bool = False             # el repositorio se acaba de crear

    def __post_init__(self):
        # Quien construye la fila puede traer un Path (de un diálogo de
        # carpetas, por ejemplo): aquí dentro siempre es texto.
        self.folder = str(self.folder or "")
        self.repo = str(self.repo or "")

    @property
    def label(self):
        return self.repo or Path(self.folder).name or self.folder

    def folder_key(self):
        return os.path.normcase(os.path.abspath(normalize_path(self.folder)))


def validate_entries(entries):
    """Lista de problemas (texto listo para mostrar). Vacía si todo bien.

    Se revisa todo y se devuelven TODOS los problemas de una vez: es
    mucho menos frustrante que arreglarlos de uno en uno.
    """
    problems = []
    if not entries:
        return ["Añade al menos un proyecto."]
    por_carpeta, por_repo, por_url = {}, {}, {}
    validas = []
    for i, e in enumerate(entries, 1):
        folder = normalize_path(e.folder)
        if not folder:
            problems.append(f"Proyecto {i}: indica la carpeta.")
        elif not Path(folder).is_dir():
            problems.append(f"Proyecto {i}: la carpeta «{folder}» no existe.")
        else:
            por_carpeta.setdefault(e.folder_key(), []).append(i)
            validas.append((i, e.folder_key()))
        error = check_repo_name(e.repo)
        if error:
            problems.append(f"Proyecto {i}: {error}")
        else:
            por_repo.setdefault(e.repo.strip().lower(), []).append(i)
        if e.url:
            por_url.setdefault(_url_key(e.url), []).append(i)
    for key, nums in por_carpeta.items():
        if len(nums) > 1:
            problems.append(
                "La misma carpeta está repetida en los proyectos "
                + ", ".join(str(n) for n in nums)
                + f" ({Path(key).name}). Déjala una sola vez.")
    for name, nums in por_repo.items():
        if len(nums) > 1:
            problems.append(
                f"El repositorio «{name}» se repite en los proyectos "
                + ", ".join(str(n) for n in nums)
                + ". Dos carpetas no pueden subir al mismo repositorio.")
    # Mismo destino por URL aunque los nombres escritos sean distintos:
    # es lo que pasaría si una fila arrastrase la URL de un intento
    # anterior. En modo espejo el segundo borraría lo que subió el
    # primero, y los dos dirían «ok».
    ya_avisados = {n for nums in por_repo.values() if len(nums) > 1
                   for n in nums}
    for url, nums in por_url.items():
        if len(nums) > 1 and not set(nums) <= ya_avisados:
            problems.append(
                f"Los proyectos {', '.join(str(n) for n in nums)} apuntan "
                f"al mismo repositorio ({url}). Uno borraría lo que sube el "
                "otro.")
    problems.extend(_nesting_problems(validas))
    return problems


def _url_key(url):
    """Clave para comparar destinos. `repo_web_url` ya quita el «.git»
    final (con una expresión regular: un `rstrip(".git")` se comería la
    «t» de «digit»)."""
    return repo_web_url(url).rstrip("/").lower()


def _nesting_problems(validas):
    """Una carpeta del lote dentro de otra carpeta del lote.

    No es un aviso de estilo: git rechaza `git add -A` cuando encuentra
    un repositorio dentro de otro («does not have a commit checked out»),
    y si el de dentro acaba primero, el de fuera se queda con un gitlink
    a un commit que no existe allí (un clon roto). Además el TXT del
    proyecto de fuera leería la carpeta del de dentro mientras aquél
    escribe la suya, así que su contenido saldría distinto cada vez.
    """
    problems = []
    for i, dentro in validas:
        for j, fuera in validas:
            if i == j:
                continue
            if dentro.startswith(fuera.rstrip(os.sep) + os.sep):
                problems.append(
                    f"El proyecto {i} ({Path(dentro).name}) está dentro del "
                    f"proyecto {j} ({Path(fuera).name}). Publica uno de los "
                    "dos, o hazlo en dos tandas.")
    return problems


def repo_url(owner, name):
    return f"https://github.com/{owner}/{name}.git"


def resolve_entries(entries, token="", owner="", create_missing=True,
                    private=True, on_progress=None, on_created=None,
                    api_create=None, api_whoami=None):
    """Da a cada fila su URL. Devuelve (dueño, problemas).

    Con `create_missing` y un token, los repositorios que no existan se
    crean en la cuenta del token (GitHub devuelve «ya existe» para los
    que sí están, y entonces se reutilizan sin tocarlos). Sin token hace
    falta el nombre de usuario: la URL se compone y, si el repositorio no
    existe, lo dirá `git push` con su propio mensaje.

    `api_create` y `api_whoami` existen para las pruebas; por defecto son
    las funciones reales de `core.github`.
    """
    crear = api_create or create_repo
    quien = api_whoami or whoami
    entries = list(entries)
    problems = []

    pendientes = [e for e in entries if not e.url]
    if not pendientes:
        return (owner or "").strip(), []

    dueno = (owner or "").strip()
    if token and not dueno:
        dueno = quien(token)
        if not dueno:
            return "", ["El token no es válido o ha caducado: GitHub no "
                        "dice a quién pertenece. Revísalo o escribe tu "
                        "nombre de usuario."]
    if not dueno and not (token and create_missing):
        return "", ["Escribe tu nombre de usuario de GitHub (o guarda un "
                    "token para que se detecte solo)."]

    total = len(entries)
    for i, e in enumerate(entries, 1):
        name = e.repo.strip()
        if on_progress:
            on_progress(e.label, i, total)
        if e.url:
            # URL puesta a mano (un espejo, otro servidor): manda sobre
            # cualquier cosa que se pudiera componer o crear aquí.
            continue
        if token and create_missing:
            try:
                clone, web, existia = crear(token, name, private=private)
            except StepError as exc:
                problems.append(f"{name}: {exc}")
                continue
            except Exception as exc:                         # noqa: BLE001
                problems.append(f"{name}: no se pudo crear ({exc})")
                continue
            e.url, e.web_url, e.created = clone, web, not existia
            if e.created and on_created:
                # Se avisa EN EL MOMENTO, no al final: si el proceso se
                # interrumpe después, el usuario necesita saber qué
                # repositorios quedaron creados (y vacíos) en su cuenta.
                on_created(name, web)
            # La cuenta real manda sobre lo que se haya escrito: el
            # repositorio se crea donde vive el token.
            real = parse_owner_repo(clone)[0]
            if real:
                dueno = real
        else:
            e.url = repo_url(dueno, name)
            e.web_url = e.url[:-4]
            e.created = False
    return dueno, problems


def jobs_from_entries(entries, base_state, token="", name_prefix=""):
    """Convierte filas YA resueltas en trabajos de publicación.

    `base_state` son las opciones comunes (rama, modo, licencia, mensaje,
    patrones de ignorar…) con el mismo formato que usa la interfaz, así
    que un trabajo del lote y uno del formulario se construyen igual.
    Cada fila manda en lo suyo: su carpeta, su URL y su TXT.
    """
    jobs = []
    for e in entries:
        state = dict(base_state)
        state["folder"] = normalize_path(e.folder)
        state["url"] = e.url
        state["consolidate"] = bool(e.consolidate)
        state.pop("consolidate_path", None)   # cada TXT va en su carpeta
        job = PublishJob.from_state(state, name=name_prefix + e.label)
        job.token = token
        jobs.append(job)
    return jobs


def project_folders(parent, limit=200):
    """Subcarpetas de `parent` que parecen proyectos, ordenadas por
    nombre. Se omiten las ocultas y las de herramientas (.git,
    node_modules, venv…): sirve para «añadir varias carpetas de golpe».
    """
    base = Path(normalize_path(str(parent or "")) or ".")
    if not base.is_dir():
        return []
    fuera = {"node_modules", "venv", ".venv", "__pycache__", "dist",
             "build", "target", "env"}
    out = []
    try:
        for child in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if len(out) >= limit:
                break
            if child.is_dir() and not child.name.startswith(".") \
                    and child.name.lower() not in fuera:
                out.append(str(child))
    except OSError:
        return out
    return out


@dataclass
class BatchPlan:
    """Lo que se va a publicar, ya comprobado. Solo lo construye
    `plan_batch`: si llega aquí, está listo para lanzarse."""
    owner: str = ""
    jobs: list = field(default_factory=list)
    created: list = field(default_factory=list)   # repos nuevos en GitHub
    problems: list = field(default_factory=list)

    @property
    def ok(self):
        return bool(self.jobs) and not self.problems


def plan_batch(entries, base_state, token="", owner="", create_missing=True,
               private=True, on_progress=None, **api):
    """Valida, resuelve y arma los trabajos. Nunca lanza.

    Si algo falla, `problems` lo explica y `jobs` queda vacío: no se
    publica un lote a medias.
    """
    problems = validate_entries(entries)
    if problems:
        return BatchPlan(problems=problems)
    dueno, problems = resolve_entries(
        entries, token=token, owner=owner, create_missing=create_missing,
        private=private, on_progress=on_progress, **api)
    if problems:
        return BatchPlan(owner=dueno, problems=problems)
    jobs = jobs_from_entries(entries, base_state, token=token)
    # Última comprobación con las mismas reglas que una publicación
    # suelta: así una URL imposible se ve aquí y no a mitad del lote.
    malos = [f"{job.label}: {error}" for job, error in
             ((j, j.validate()) for j in jobs) if error]
    if malos:
        return BatchPlan(owner=dueno, problems=malos)
    return BatchPlan(owner=dueno, jobs=jobs,
                     created=[e.repo for e in entries if e.created])
