# -*- coding: utf-8 -*-
"""Coincidencia estilo .gitignore (simplificada) y edición del .gitignore.

Cuando la carpeta ya es un repositorio, la fuente de verdad es git
(véase `git.Git.list_files`): esta implementación se usa para carpetas
sin repositorio y para aplicar el cuadro de ignorar sobre listas
planas. Simplificaciones conocidas: no admite negaciones («!»), trata
«**» como «*» y compara sin distinguir mayúsculas.
"""

import fnmatch
import re
from pathlib import Path

_GLOB_CHARS = ("*", "?", "[")


class IgnoreMatcher:
    """Patrones de ignorado precompilados.

    En vez de recorrer todos los patrones por cada archivo, compila una
    sola vez:
      · nombres literales      → conjuntos, coste O(1) por archivo
      · comodines de nombre    → UNA regex combinada
      · patrones con ruta      → UNA regex combinada (anclados a la raíz)
    """

    __slots__ = ("lit_names", "lit_dirs", "name_rx", "path_rx", "empty")

    def __init__(self, patterns):
        lit_names, lit_dirs, globs, paths = set(), set(), [], []
        for raw in patterns or ():
            p = (raw or "").strip()
            if not p or p[0] in "#!":
                continue
            only_dir = p.endswith("/")
            p = p.rstrip("/")
            anchored = p.startswith("/")
            p = p.lstrip("/")
            if not p:
                continue
            if anchored or "/" in p:
                paths.append(p.lower())
            elif any(c in p for c in _GLOB_CHARS):
                globs.append(p.lower())
            else:
                lit_dirs.add(p.lower())
                if not only_dir:
                    lit_names.add(p.lower())
        self.lit_names = lit_names
        self.lit_dirs = lit_dirs
        self.name_rx = re.compile(
            "|".join(fnmatch.translate(g) for g in globs)) if globs else None
        # Un patrón con ruta cubre la ruta exacta y todo lo que cuelga.
        self.path_rx = re.compile(
            "|".join(fnmatch.translate(p) + "|" + fnmatch.translate(p + "/*")
                     for p in paths)) if paths else None
        self.empty = not (lit_names or lit_dirs or globs or paths)

    def ignored(self, rel_lower, name_lower, is_dir):
        """Para recorridos que podan directorios: solo mira esta entrada."""
        if self.empty:
            return False
        if name_lower in (self.lit_dirs if is_dir else self.lit_names):
            return True
        if self.name_rx is not None and self.name_rx.match(name_lower):
            return True
        if self.path_rx is not None and self.path_rx.match(rel_lower):
            return True
        return False

    def ignored_path(self, rel_lower):
        """Para listas planas (sin poda): comprueba el archivo y cada
        carpeta ancestro, como haría el recorrido."""
        if self.empty:
            return False
        parts = rel_lower.split("/")
        prefix = ""
        for seg in parts[:-1]:
            prefix = seg if not prefix else prefix + "/" + seg
            if self.ignored(prefix, seg, True):
                return True
        return self.ignored(rel_lower, parts[-1], False)


def clean_patterns(patterns):
    """Quita vacíos y espacios; admite una lista o un texto multilínea."""
    if isinstance(patterns, str):
        patterns = patterns.splitlines()
    return [p.strip() for p in (patterns or ()) if p and p.strip()]


def read_gitignore(folder):
    gi = Path(folder) / ".gitignore"
    if not gi.is_file():
        return []
    try:
        return gi.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def merged_patterns(folder, extra_patterns):
    """Cuadro de ignorar + .gitignore raíz, sin duplicados ni comentarios."""
    patterns = clean_patterns(extra_patterns)
    seen = set(patterns)
    for ln in read_gitignore(folder):
        ln = ln.strip()
        if ln and not ln.startswith("#") and ln not in seen:
            patterns.append(ln)
            seen.add(ln)
    return patterns


def append_gitignore(folder, patterns):
    """Añade al .gitignore los patrones que aún no estén. Devuelve los
    añadidos (vacío si no hizo falta escribir)."""
    gi = Path(folder) / ".gitignore"
    existing = read_gitignore(folder)
    existing_set = {ln.strip() for ln in existing}
    new = [p for p in clean_patterns(patterns) if p not in existing_set]
    if new:
        text = "\n".join(existing + new).strip() + "\n"
        gi.write_text(text, encoding="utf-8")
    return new


def anchor_pattern(rel_path):
    """Convierte una ruta concreta en un patrón que solo la excluye a
    ella: «/sub/.env» y no cualquier «.env» del proyecto."""
    p = rel_path.strip().replace("\\", "/")
    if not p or any(c in p for c in _GLOB_CHARS):
        return p
    return p if p.startswith("/") else "/" + p
