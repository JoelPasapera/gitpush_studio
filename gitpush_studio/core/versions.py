# -*- coding: utf-8 -*-
"""Las dos versiones de un archivo en la vista previa de la publicación:
la que hay hoy en GitHub y la que se va a subir.

En la vista previa ya está todo preparado: lo de GitHub está en
FETCH_HEAD (se acaba de traer) y lo que se va a subir, en el índice. Se
leen de git, sin tocar la carpeta, y se pasan al mismo texto que el TXT
(UTF-16 y ANSI convertidos, sin BOM, sin blancos al final) para que la
comparación no se llene de diferencias que no lo son.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from .fs import fmt_size
from .git import Git
from .txtbody import small_body

# Más grande que esto no se compara línea a línea en pantalla.
MAX_SHOW_BYTES = 8 * 1024 * 1024
KIND_ADDED = "added"
KIND_MODIFIED = "modified"
KIND_DELETED = "deleted"


@dataclass
class FileVersions:
    """Las líneas de cada lado, o una nota si no hay texto que comparar."""
    old_lines: List[str] = field(default_factory=list)
    new_lines: List[str] = field(default_factory=list)
    note: str = ""


def _blob(git: Git, folder: str, spec: str) -> Optional[bytes]:
    """El contenido de `spec` («FETCH_HEAD:ruta», «:ruta»), o None."""
    process = git.run(["cat-file", "blob", spec], cwd=folder, check=False,
                      echo=False, binary=True)
    if process.returncode != 0:
        return None
    return process.stdout or b""


def _size(git: Git, folder: str, spec: str) -> int:
    process = git.run(["cat-file", "-s", spec], cwd=folder, check=False,
                      echo=False)
    try:
        return int((process.stdout or "0").strip() or 0)
    except ValueError:
        return 0


def text_lines(raw: bytes) -> Optional[List[str]]:
    """Las líneas como en el TXT, o None si es binario."""
    body = small_body(raw)
    if body is None:
        return None
    lines = body.decode("utf-8", "replace").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if lines == [""]:
        return []
    return lines


def for_preview(folder: str, kind: str, path: str,
                git: Optional[Git] = None) -> FileVersions:
    """Las dos versiones de `path` según su grupo en la vista previa."""
    git = git or Git()
    old_spec = None if kind == KIND_ADDED else "FETCH_HEAD:" + path
    new_spec = None if kind == KIND_DELETED else ":" + path
    sizes = [_size(git, folder, spec) for spec in (old_spec, new_spec)
             if spec]
    if any(size > MAX_SHOW_BYTES for size in sizes):
        return FileVersions(note="Es demasiado grande para compararlo aquí ("
                                 + " → ".join(fmt_size(s) for s in sizes)
                                 + ").")
    old = _blob(git, folder, old_spec) if old_spec else b""
    new = _blob(git, folder, new_spec) if new_spec else b""
    if old is None or new is None:
        return FileVersions(note="No pude leer una de las dos versiones "
                                 "de git.")
    old_lines = text_lines(old) if old_spec else []
    new_lines = text_lines(new) if new_spec else []
    if old_lines is None or new_lines is None:
        before = fmt_size(len(old)) if old_spec else "no existía"
        after = fmt_size(len(new)) if new_spec else "se borra"
        return FileVersions(note=f"Archivo binario: {before} → {after}. No "
                                 "se puede comparar línea a línea.")
    return FileVersions(old_lines=old_lines, new_lines=new_lines)
