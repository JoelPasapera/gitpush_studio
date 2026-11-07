# -*- coding: utf-8 -*-
"""Archivos de más de 100 MB: subirlos con Git LFS en lugar de dejarlos fuera.

GitHub rechaza cualquier archivo de más de 100 MB. Con Git LFS («Large
File Storage»), git guarda en el repositorio un puntero pequeño y el
archivo de verdad va a un almacén aparte de GitHub. Para eso:

  · Git LFS tiene que estar instalado (https://git-lfs.com);
  · `git lfs install --local` lo activa solo en este repositorio;
  · `git lfs track --filename RUTA` añade UNA línea por archivo al
    .gitattributes del proyecto (es necesario: así sabe git qué archivos
    van por LFS, también en el equipo de quien clone el repositorio).

LFS no puede arreglar un archivo que ya está en un commit anterior (de
una publicación que falló, por ejemplo): para eso habría que reescribir
el historial, y eso no se hace nunca sin que lo decidas tú. En ese caso
se explica y no se toca nada.

GitHub da gratis un espacio y un tráfico limitados para LFS (1 GB de cada
al mes en las cuentas gratuitas); más allá, se paga.
"""

import os
from typing import Callable, List, Optional, Sequence, Set, Tuple

from .errors import StepError
from .git import Git

LFS_MISSING = ("Git LFS no está instalado. Descárgalo de https://git-lfs.com "
               "(es gratis), instálalo y vuelve a publicar; o deja fuera los "
               "archivos grandes.")
LFS_NOTE = ("GitHub da gratis 1 GB de espacio y 1 GB de descarga al mes para "
            "archivos LFS; a partir de ahí se paga.")


def lfs_available(git: Git) -> bool:
    process = git.run(["lfs", "version"], check=False, echo=False)
    return process.returncode == 0


def in_history(git: Git, folder: str, rel: str) -> bool:
    """¿Está `rel` en algún commit (de cualquier rama)?"""
    process = git.run(["--literal-pathspecs", "log", "--all", "-1",
                       "--format=%H", "--", rel], cwd=folder, check=False,
                      echo=False)
    return bool((process.stdout or "").strip())


def track(folder: str, rels: Sequence[str], git: Optional[Git] = None,
          log: Optional[Callable[[str, str], None]] = None
          ) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Pasa `rels` a Git LFS. Devuelve (los que ya van por LFS, [(ruta,
    por qué no)]). Lanza StepError si Git LFS no está instalado."""
    git = git or Git()
    say = log or (lambda text, tag="dim": None)
    if not lfs_available(git):
        raise StepError(LFS_MISSING)
    tracked: List[str] = []
    problems: List[Tuple[str, str]] = []
    for rel in rels:
        if in_history(git, folder, rel):
            problems.append((rel, "ya está en un commit anterior: LFS no lo "
                                  "puede arreglar sin reescribir el "
                                  "historial, y eso no lo hago por mi "
                                  "cuenta"))
    # Todo o nada: si alguno no puede ir por LFS no se toca nada (ni el
    # .gitattributes ni la configuración del repositorio).
    if problems or not rels:
        return tracked, problems
    pending = list(rels)
    setup = git.run(["lfs", "install", "--local"], cwd=folder, check=False,
                    echo=False)
    if setup.returncode != 0:
        raise StepError("No pude activar Git LFS en este repositorio: "
                        + ((setup.stderr or setup.stdout or "").strip()
                           or "error desconocido"))
    for rel in pending:
        process = git.run(["lfs", "track", "--filename", "--", rel],
                          cwd=folder, check=False, echo=False)
        if process.returncode == 0:
            tracked.append(rel)
        else:
            problems.append((rel, (process.stderr or process.stdout
                                   or "git lfs track falló").strip()))
    if tracked:
        say(f"  📦 {len(tracked)} archivo(s) grande(s) irán por Git LFS (se "
            "añadió una línea por archivo al .gitattributes). " + LFS_NOTE,
            "ok")
    return tracked, problems


def lfs_tracked(git: Git, folder: str, paths: Sequence[str]) -> Set[str]:
    """Las rutas de `paths` que van por Git LFS (atributo filter=lfs)."""
    if not paths or not os.path.isdir(os.path.join(folder, ".git")):
        return set()
    request = b"".join(os.fsencode(path) + b"\0" for path in paths)
    process = git.run(["check-attr", "-z", "--stdin", "filter"], cwd=folder,
                      check=False, echo=False, binary=True, input=request)
    if process.returncode != 0:
        return set()
    fields = (process.stdout or b"").split(b"\0")
    out: Set[str] = set()
    for index in range(0, len(fields) - 2, 3):
        path, _attribute, value = fields[index:index + 3]
        if value == b"lfs":
            out.add(os.fsdecode(path))
    return out


def drop_lfs_size_findings(git: Git, folder: str, findings: List[dict]
                           ) -> List[dict]:
    """Quita los avisos de tamaño de los archivos que ya van por Git LFS:
    a GitHub solo llega su puntero, que es pequeño."""
    sized = [f["path"] for f in findings if f.get("kind") in ("huge", "big")]
    if not sized:
        return findings
    by_lfs = lfs_tracked(git, folder, sized)
    return [f for f in findings
            if not (f.get("kind") in ("huge", "big") and f["path"] in by_lfs)]
