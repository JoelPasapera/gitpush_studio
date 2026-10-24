# -*- coding: utf-8 -*-
"""Enumerar los archivos de un proyecto.

Dos fuentes: el recorrido propio (`collect_included`, para carpetas sin
repositorio) y git (`Git.list_files`, la verdad exacta de lo que se
publica). `project_entries` elige por ti.
"""

import hashlib
import os
from pathlib import Path

from .errors import StepError
from .fs import same_file
from .git import Git
from .ignore import (IgnoreMatcher, clean_patterns, merged_patterns,
                     nested_rules, read_gitignore)


def _nested_matcher(abs_dir, rel_dir):
    """(prefijo, IgnoreMatcher) del .gitignore de una subcarpeta, o None."""
    reglas = nested_rules(read_gitignore(abs_dir))
    if not reglas:
        return None
    return (rel_dir.lower() + "/", IgnoreMatcher(reglas))


def _ignored_by_nested(nested, rel_lower, name_lower, is_dir):
    for prefijo, m in nested:
        if m.ignored(rel_lower[len(prefijo):], name_lower, is_dir):
            return True
    return False


def collect_included(project_dir, patterns, exclude_file=None,
                     nested_gitignores=False):
    """Recorre el proyecto UNA vez y devuelve ([(rel, ruta, tamaño)],
    ignorados).

    Usa os.scandir: el sistema ya devuelve el tipo y (en Windows) el
    tamaño junto al nombre, así que evita un stat() por archivo. Poda
    los directorios ignorados antes de entrar, por eso no hace falta
    volver a comprobar los segmentos padre de cada archivo. Omite
    siempre `.git`.

    Con `nested_gitignores`, el .gitignore de cada subcarpeta se aplica
    a lo que cuelga de ella (como hace git). Sin esto, un
    «api/.gitignore» con «secrets.yml» no impedía que ese archivo
    entrara en el TXT de una carpeta que aún no es repositorio.
    """
    matcher = (patterns if isinstance(patterns, IgnoreMatcher)
               else IgnoreMatcher(patterns))
    root = os.path.abspath(os.fspath(project_dir))
    skip_path = (os.path.normcase(os.path.abspath(os.fspath(exclude_file)))
                 if exclude_file is not None else None)

    included, skipped = [], 0
    stack = [("", root, ())]
    push, ignored = stack.append, matcher.ignored
    while stack:
        rel_dir, abs_dir, nested = stack.pop()
        if nested_gitignores and rel_dir:
            propio = _nested_matcher(abs_dir, rel_dir)
            if propio is not None:
                nested = nested + (propio,)
        try:
            it = os.scandir(abs_dir)
        except OSError:
            continue
        with it:
            for e in it:
                name = e.name
                low = name.lower()
                rel = name if not rel_dir else rel_dir + "/" + name
                try:
                    if e.is_dir(follow_symlinks=False):
                        if low != ".git" and not ignored(rel.lower(), low,
                                                         True) \
                                and not (nested and _ignored_by_nested(
                                    nested, rel.lower(), low, True)):
                            push((rel, e.path, nested))
                        continue
                    if ignored(rel.lower(), low, False) or (
                            nested and _ignored_by_nested(
                                nested, rel.lower(), low, False)):
                        skipped += 1
                        continue
                    if skip_path is not None \
                            and os.path.normcase(e.path) == skip_path:
                        continue          # jamás incluirse a sí mismo
                    size = e.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                included.append((rel.lower(), rel, e.path, size))

    included.sort(key=_first)
    return [(rel, path, size) for _, rel, path, size in included], skipped


def _first(item):
    return item[0]


def project_entries(folder, extra_patterns=(), respect_ignores=True,
                    exclude_file=None, use_git=True, git=None):
    """Archivos del proyecto: ([(rel, ruta, tamaño)], ignorados).

    Si la carpeta es un repositorio (y `use_git`), pregunta a git y
    aplica encima el cuadro de ignorar; si no, recorre con
    `IgnoreMatcher` (.gitignore raíz + cuadro). `ignorados` solo se
    conoce con exactitud en el recorrido propio.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(f"Directorio no válido: {folder}")
    extra = clean_patterns(extra_patterns)

    if use_git and respect_ignores and (folder / ".git").exists():
        try:
            entries = (git or Git()).list_files(folder)
        except (StepError, OSError):
            entries = None
        if entries is not None:
            matcher = IgnoreMatcher(extra)
            out, skipped = [], 0
            for rel, fp, size in entries:
                if matcher.ignored_path(rel.lower()):
                    skipped += 1
                    continue
                if exclude_file is not None and same_file(fp, exclude_file):
                    continue
                out.append((rel, fp, size))
            return out, skipped

    patterns = merged_patterns(folder, extra) if respect_ignores else extra
    return collect_included(folder, IgnoreMatcher(patterns), exclude_file,
                            nested_gitignores=respect_ignores)


def render_tree(root_name, rel_paths):
    """Dibuja el árbol del proyecto a partir de las rutas incluidas."""
    tree = {}
    for rel in rel_paths:
        node = tree
        parts = rel.split("/")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = None
    lines = [root_name + "/"]

    def walk(node, prefix):
        dirs = sorted((k for k, v in node.items() if isinstance(v, dict)),
                      key=str.lower)
        files = sorted((k for k, v in node.items() if v is None),
                       key=str.lower)
        entries = dirs + files
        for i, name in enumerate(entries):
            last = i == len(entries) - 1
            branch = "└── " if last else "├── "
            suffix = "/" if isinstance(node[name], dict) else ""
            lines.append(prefix + branch + name + suffix)
            if isinstance(node[name], dict):
                walk(node[name], prefix + ("    " if last else "│   "))

    walk(tree, "")
    return "\n".join(lines)


def folder_fingerprint(folder, extra_patterns):
    """Huella rápida del contenido (rutas + tamaños + fechas).

    Respaldo para carpetas que aún no son repositorio; cuando ya lo son
    es mejor `Git.is_dirty`, que usa la caché del índice.
    """
    folder = Path(folder)
    if not folder.is_dir():
        return None
    patterns = merged_patterns(folder, extra_patterns)
    h = hashlib.md5()
    entries, _ = collect_included(folder, IgnoreMatcher(patterns),
                                  nested_gitignores=True)
    for rel, fp, size in entries:            # ya vienen ordenadas
        try:
            mtime = os.lstat(fp).st_mtime_ns
        except OSError:
            continue
        # «surrogateescape»: un nombre que no es UTF-8 aporta sus bytes
        # reales a la huella en vez de tumbar la vigilancia automática.
        h.update(f"{rel}|{size}|{mtime}\n".encode("utf-8",
                                                  "surrogateescape"))
    return h.hexdigest()
