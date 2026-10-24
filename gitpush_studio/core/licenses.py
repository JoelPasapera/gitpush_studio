# -*- coding: utf-8 -*-
"""Añadir un archivo LICENSE al proyecto.

Tres licencias vienen incluidas y funcionan sin conexión (Apache 2.0,
MIT y GPL 3.0); las demás se piden a la API de GitHub la primera vez y
quedan guardadas en ~/.gitpush_studio/licencias para las siguientes.

El texto legal nunca se inventa ni se reescribe: se copia tal cual y
solo se rellenan los huecos que el propio texto trae para el año y el
titular. Y nunca se sobrescribe una licencia que el proyecto ya tenga.
"""

import re
from datetime import date
from pathlib import Path

from . import github
from .atomic import create_exclusive, write_bytes
from .config import CONFIG_DIR

BUNDLED_DIR = Path(__file__).resolve().parent.parent / "licencias"
CACHE_DIR = CONFIG_DIR / "licencias"
BUNDLED = ("apache-2.0", "mit", "gpl-3.0")
DEFAULT = "apache-2.0"
NONE = ""

# (clave, nombre visible, qué permite en una línea). El orden es el del
# desplegable; primero las que no necesitan conexión.
CATALOG = (
    ("apache-2.0", "Apache 2.0",
     "permisiva, con permiso de patentes; hay que conservar los avisos"),
    ("mit", "MIT",
     "la más simple: haz lo que quieras conservando el aviso de autoría"),
    ("gpl-3.0", "GPL 3.0",
     "quien la use y la distribuya debe publicar su código con esta misma "
     "licencia"),
    ("agpl-3.0", "AGPL 3.0",
     "como la GPL, y también si el programa se usa a través de la red"),
    ("gpl-2.0", "GPL 2.0", "versión anterior de la GPL"),
    ("lgpl-3.0", "LGPL 3.0",
     "para librerías: se puede enlazar desde programas privativos"),
    ("lgpl-2.1", "LGPL 2.1", "versión anterior de la LGPL"),
    ("bsd-3-clause", "BSD 3 cláusulas",
     "permisiva; además prohíbe usar tu nombre para promocionar"),
    ("bsd-2-clause", "BSD 2 cláusulas", "permisiva y muy corta"),
    ("mpl-2.0", "Mozilla 2.0",
     "intermedia: solo los archivos que modifiques siguen siendo abiertos"),
    ("epl-2.0", "Eclipse 2.0", "intermedia, del ecosistema Java"),
    ("bsl-1.0", "Boost 1.0", "permisiva, sin exigir avisos en los binarios"),
    ("unlicense", "Unlicense", "renuncias a todos los derechos"),
    ("cc0-1.0", "CC0 1.0", "dominio público; pensada para obras, no código"),
)
NAMES = {key: name for key, name, _ in CATALOG}
KEYS = tuple(key for key, _, _ in CATALOG)

# Nombres con los que se reconoce una licencia ya existente: LICENSE con
# cualquier extensión, las variantes de doble licencia de Rust
# (LICENSE-MIT, LICENSE-APACHE), COPYING.LESSER, UNLICENSE, COPYRIGHT…
# y la carpeta LICENSES/ del estándar REUSE. Si no se reconocen, se
# escribiría un LICENSE NUEVO al lado: una licencia contradictoria.
EXISTING_NAMES = ("license", "licence", "copying", "licencia")
EXISTING_SUFFIXES = ("", ".txt", ".md", ".rst")
_EXISTING_RX = re.compile(r"^(?:un)?licen[cs](?:e|ia)s?(?:$|[.\-_ ])|"
                          r"^copying(?:$|[.\-_ ])|^copyright(?:$|[.\-_ ])",
                          re.IGNORECASE)

# Huecos que dejan los textos oficiales para el año y el titular.
YEAR_HOLES = ("[yyyy]", "[year]", "<year>", "[año]")
OWNER_HOLES = ("[name of copyright owner]", "[fullname]", "<name of author>",
               "[name of author]")


def display_name(key):
    return NAMES.get(key, key)


def is_bundled(key):
    return key in BUNDLED


def existing_license(folder):
    """Nombre del archivo de licencia que ya tenga el proyecto, o ''."""
    folder = Path(folder)
    try:
        entries = list(folder.iterdir())
    except OSError:
        return ""
    for entry in sorted(entries, key=lambda e: e.name.lower()):
        if _EXISTING_RX.match(entry.name):
            try:
                if entry.is_file() or entry.is_dir():
                    return entry.name
            except OSError:
                continue
    return ""


def _bundled_text(key):
    path = BUNDLED_DIR / f"{key}.txt"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _cached_text(key):
    try:
        return (CACHE_DIR / f"{key}.txt").read_text(encoding="utf-8")
    except OSError:
        return ""


def _save_cache(key, text):
    # Atómico: una caché cortada a medias se usaría luego como el texto
    # de la licencia, y acabaría publicada incompleta.
    try:
        write_bytes(CACHE_DIR / f"{key}.txt", text.encode("utf-8"))
    except OSError:
        pass          # sin caché se vuelve a pedir; no es un error


def fetch_text(key, token=""):
    """Pide el texto oficial a GitHub. Devuelve '' si no se pudo."""
    try:
        code, body = github.api("GET", f"/licenses/{key}", token)
    except Exception:                                       # noqa: BLE001
        return ""
    if code != 200 or not isinstance(body, dict):
        return ""
    return body.get("body") or ""


def license_text(key, token=""):
    """Incluida → guardada → descargada. '' si no hay forma de tenerla."""
    if not key:
        return ""
    text = _bundled_text(key) or _cached_text(key)
    if text:
        return text
    text = fetch_text(key, token)
    if text:
        _save_cache(key, text)
    return text


def fill(text, owner, year=None):
    """Rellena los huecos del texto oficial. Lo demás no se toca."""
    year = str(year or date.today().year)
    for hole in YEAR_HOLES:
        text = text.replace(hole, year)
    for hole in OWNER_HOLES:
        text = text.replace(hole, owner)
    # La GPL escribe «<year>  <name of author>» junto; ya está cubierto.
    return text


def needs_owner(text):
    return any(h in text for h in OWNER_HOLES)


def write_license(folder, key, owner, year=None, token="", filename="LICENSE"):
    """Escribe la licencia en la carpeta. Devuelve (nombre, mensaje):
    `nombre` vacío significa que no se escribió, y `mensaje` explica por
    qué (ya había una, no se pudo obtener el texto, falta el titular)."""
    folder = Path(folder)
    if not key:
        return "", "Sin licencia: no se añade ninguna."
    ya = existing_license(folder)
    if ya:
        return "", f"El proyecto ya tiene «{ya}»: no se toca."
    text = license_text(key, token)
    if not text:
        return "", (f"No pude obtener el texto de {display_name(key)} (hace "
                    "falta conexión la primera vez). Apache 2.0, MIT y "
                    "GPL 3.0 no la necesitan.")
    if needs_owner(text) and not (owner or "").strip():
        return "", ("No sé a nombre de quién ponerla: escribe el titular o "
                    "completa «Identidad Git».")
    text = fill(text, (owner or "").strip(), year)
    if not text.endswith("\n"):
        text += "\n"
    try:
        create_exclusive(folder / filename, text.encode("utf-8"))
    except FileExistsError:
        return "", f"El proyecto ya tiene «{filename}»: no se toca."
    return filename, f"Añadido {filename} ({display_name(key)})."


def normalize_key(value):
    """Acepta la clave, el nombre visible o «ninguna»."""
    v = (value or "").strip().lower()
    if v in ("", "ninguna", "sin licencia", "none", "no"):
        return NONE
    if v in KEYS:
        return v
    for key, name, _ in CATALOG:
        if v == name.lower():
            return key
    v = re.sub(r"\s+", "-", v)
    return v if v in KEYS else None        # None: clave desconocida
