# -*- coding: utf-8 -*-
"""Utilidades de sistema de archivos compartidas por el núcleo."""

import os
import re
import subprocess
import sys
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


def open_in_explorer(path):
    """Abre un archivo o carpeta con el programa predeterminado."""
    try:
        if IS_WINDOWS:
            os.startfile(path)                     # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
        return True
    except OSError:
        return False
