# -*- coding: utf-8 -*-
"""Utilidades de sistema de archivos compartidas por el núcleo."""

import os
import re
import subprocess
from pathlib import Path

IS_WINDOWS = os.name == "nt"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
MB = 1024 * 1024

# Extensiones que nunca se leen como texto (ni en la revisión ni al
# empaquetar): se marcan como binarias sin abrir el archivo.
BIN_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tif",
    ".pdf", ".zip", ".rar", ".7z", ".gz", ".tar", ".bz2", ".xz",
    ".mp3", ".wav", ".ogg", ".flac", ".mp4", ".avi", ".mov", ".mkv",
    ".webm", ".exe", ".dll", ".so", ".dylib", ".bin", ".dat", ".iso",
    ".dmg", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".psd", ".ai",
    ".sketch", ".pyc", ".class", ".jar", ".xlsx", ".docx", ".pptx",
})


def fmt_size(n):
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.0f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def is_binary_ext(rel_or_name):
    return os.path.splitext(rel_or_name)[1].lower() in BIN_EXTS


def display_name(name):
    """Nombre listo para escribirse en UTF-8.

    En Linux y macOS un nombre de archivo puede no ser UTF-8 (venido de
    un ZIP antiguo, de otro sistema): Python lo representa con
    «sustitutos» que revientan al codificar. Aquí se muestran sus bytes
    reales con «�» donde no se entienden: nunca una excepción.
    """
    try:
        name.encode("utf-8")
        return name
    except UnicodeEncodeError:
        return os.fsencode(name).decode("utf-8", "replace")


def looks_binary(raw):
    """Un NUL en los primeros 4 KB delata un binario (heurística de git)."""
    return b"\x00" in raw[:4096]


def normalize_path(p):
    """Acepta rutas con comillas y rutas estilo Git Bash (/c/Users/…)."""
    p = (p or "").strip().strip('"').strip("'")
    if not p:
        return ""
    m = re.match(r"^/([a-zA-Z])/(.*)$", p)
    if m and IS_WINDOWS:
        p = f"{m.group(1).upper()}:/{m.group(2)}"
    return str(Path(p))


def same_file(a, b):
    """Compara dos rutas ya absolutas sin tocar el disco."""
    return os.path.normcase(os.path.abspath(a)) == \
        os.path.normcase(os.path.abspath(b))


def relative_inside(target, folder):
    """Ruta de `target` relativa a `folder`, o "" si cae fuera.

    Se usa `realpath`, no `abspath`: con un enlace simbólico (o una
    junction o una unidad mapeada de Windows) que apunte DENTRO del
    proyecto, `abspath` diría que el archivo está fuera y no se añadiría
    al .gitignore… pero git lo encontraría dentro y lo subiría. Quien
    escribe el archivo también resuelve los enlaces, así que los dos
    lados tienen que hablar de la misma ruta real.

    La comparación se hace con `normcase` (en Windows la misma carpeta se
    escribe de muchas maneras), pero la ruta que se devuelve conserva las
    mayúsculas originales: va a parar al .gitignore.
    """
    base = os.path.realpath(str(folder))
    p = os.path.realpath(str(target))
    nb, np_ = os.path.normcase(base), os.path.normcase(p)
    if np_ == nb or not np_.startswith(nb.rstrip(os.sep) + os.sep):
        return ""
    return os.path.relpath(p, base).replace("\\", "/")
