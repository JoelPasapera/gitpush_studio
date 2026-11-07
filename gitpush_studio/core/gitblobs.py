# -*- coding: utf-8 -*-
"""Leer del índice de git los archivos que no cambiaron, con un solo proceso.

Generar el TXT de un repositorio grande abre miles de archivos. En
Windows cada apertura cuesta (el antivirus examina cada archivo al
abrirlo), y la mayoría de esos archivos son exactamente lo que git ya
tiene guardado. Para esos, el contenido se pide a git de una vez
(`git cat-file --batch`: un proceso, una tubería), y solo los archivos
con cambios se leen de la carpeta.

«Exactamente» es la clave, y se garantiza así:

  · solo archivos normales rastreados (ni enlaces ni submódulos) que git
    da por SIN cambios (`git diff-files`, que vuelve a mirar el contenido
    de los que podrían haber cambiado en el mismo segundo);
  · nunca los marcados «assume-unchanged» o «skip-worktree» (git deja de
    mirar su contenido real), ni los que tienen un filtro (Git LFS y
    parecidos), «ident» o «working-tree-encoding»: en esos lo que guarda
    git no es lo que hay en la carpeta;
  · así, la única diferencia posible entre lo guardado y la carpeta son
    los finales de línea (CRLF en Windows). Se usa lo guardado si mide
    exactamente lo mismo que el archivo (entonces es idéntico), o si no
    tiene ningún «\r» y midiendo uno más por cada «\n» coincide (entonces
    el archivo es lo guardado con todos sus «\n» como «\r\n»). En
    cualquier otro caso se lee del disco.

Si cualquier cosa falla, todo se lee del disco como siempre: esto solo
puede hacer el TXT más rápido, nunca distinto.
"""

import os
import subprocess
import threading
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple

from .fs import NO_WINDOW, is_binary_ext
from .git import Git
from .reader import SMALL, iter_contents, read_whole

# Por debajo de esto, abrir los archivos uno a uno ya es rápido.
MIN_FILES = 500
# Dónde compensa: donde abrir un archivo es caro.
ENABLED = os.name == "nt"

Entry = Tuple[str, str, int]
_UNSET = (b"unspecified", b"unset")


def _split_z(raw: bytes) -> List[bytes]:
    return [item for item in raw.split(b"\0") if item]


def _tracked_regular(git: Git, folder: str) -> Dict[str, str]:
    """{ruta: sha del índice} de lo rastreado como archivo normal (modo
    100644 o 100755)."""
    process = git.run(["ls-files", "-s", "-z"], cwd=folder, check=False,
                      echo=False, binary=True)
    out: Dict[str, str] = {}
    if process.returncode != 0:
        return out
    for item in _split_z(process.stdout or b""):
        info, _tab, path = item.partition(b"\t")
        fields = info.split()
        if len(fields) == 3 and fields[0] in (b"100644", b"100755") \
                and fields[2] == b"0":
            out[os.fsdecode(path)] = fields[1].decode("ascii")
    return out


def _flagged(git: Git, folder: str) -> Set[str]:
    """«assume-unchanged» (letra minúscula) o «skip-worktree» (S)."""
    process = git.run(["ls-files", "-t", "-v", "-z"], cwd=folder,
                      check=False, echo=False, binary=True)
    out: Set[str] = set()
    for item in _split_z(process.stdout or b""):
        tag = item[:1]
        if tag.islower() or tag == b"S":
            out.add(os.fsdecode(item[2:]))
    return out


def _dirty(git: Git, folder: str) -> Optional[Set[str]]:
    """Rastreados con cambios respecto al índice (None: no se sabe)."""
    process = git.run(["diff-files", "--name-only", "-z"], cwd=folder,
                      check=False, echo=False, binary=True)
    if process.returncode != 0:
        return None
    return {os.fsdecode(item) for item in _split_z(process.stdout or b"")}


_CONVERTING_ATTRIBUTES = ("filter", "ident", "working-tree-encoding")


def _filtered(git: Git, folder: str, paths: Sequence[str]) -> Set[str]:
    """Los que git convierte al sacarlos a la carpeta con algo más que
    los finales de línea: filtros (Git LFS y parecidos), «ident» y
    «working-tree-encoding»."""
    if not paths:
        return set()
    request = b"".join(os.fsencode(path) + b"\0" for path in paths)
    process = git.run(["check-attr", "-z", "--stdin",
                       *_CONVERTING_ATTRIBUTES], cwd=folder,
                      check=False, echo=False, binary=True, input=request)
    if process.returncode != 0:
        return set(paths)                    # ante la duda, del disco
    fields = (process.stdout or b"").split(b"\0")
    out: Set[str] = set()
    for index in range(0, len(fields) - 2, 3):
        path, _attribute, value = fields[index:index + 3]
        if value not in _UNSET:
            out.add(os.fsdecode(path))
    return out


def clean_entries(git: Git, folder: str, entries: Sequence[Entry],
                  small: int = SMALL) -> Dict[str, str]:
    """{ruta: sha} de lo de `entries` que se puede pedir a git en vez de
    leerlo del disco.

    Solo lo pequeño (lo grande se copia por bloques del disco) y no
    binario por extensión (eso ni se lee)."""
    candidates = [rel for rel, _path, size in entries
                  if size <= small and not is_binary_ext(rel)
                  and "\n" not in rel and "\r" not in rel]
    if not candidates:
        return {}
    dirty = _dirty(git, folder)
    if dirty is None:
        return {}
    tracked = _tracked_regular(git, folder)
    flagged = _flagged(git, folder)
    usable = [rel for rel in candidates
              if rel in tracked and rel not in dirty and rel not in flagged]
    filtered = _filtered(git, folder, usable)
    return {rel: tracked[rel] for rel in usable if rel not in filtered}


class _BlobStream:
    """Un `git cat-file --batch`: se le piden contenidos guardados (por su
    sha) y los devuelve en el mismo orden.

    Un hilo escribe las peticiones mientras se leen las respuestas: git
    nunca se queda esperando a que se le pida la siguiente."""

    def __init__(self, git: Git, folder: str,
                 requests: Sequence[Tuple[str, str]]) -> None:
        self._process = subprocess.Popen(
            [git.exe, "cat-file", "--batch"], cwd=folder,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=NO_WINDOW,
            env=git._env())
        self._writer = threading.Thread(target=self._feed,
                                        args=(requests,), daemon=True)
        self._writer.start()

    def _feed(self, requests: Sequence[Tuple[str, str]]) -> None:
        try:
            for _rel, sha in requests:
                self._process.stdin.write(sha.encode("ascii") + b"\n")
            self._process.stdin.close()
        except (OSError, ValueError):
            pass                             # se cerró antes de terminar

    def next(self) -> Optional[bytes]:
        """El contenido siguiente, o None si git no lo tiene."""
        header = self._process.stdout.readline()
        if not header:
            raise OSError("git dejó de responder")
        fields = header.split()
        if len(fields) < 3 or fields[-1] == b"missing":
            return None
        size = int(fields[2])
        data = self._process.stdout.read(size)
        self._process.stdout.read(1)        # el salto de línea final
        if len(data) != size:
            raise OSError("git cortó un contenido a medias")
        return data

    def close(self) -> None:
        try:
            self._process.kill()
        except OSError:
            pass
        for pipe in (self._process.stdout, self._process.stdin):
            try:
                pipe.close()
            except (OSError, ValueError):
                pass
        self._process.wait()
        self._writer.join(timeout=2)


def iter_contents_with_git(entries: Sequence[Entry], folder: str,
                           git: Optional[Git] = None, small: int = SMALL,
                           stats: Optional[dict] = None
                           ) -> Iterator[Tuple[str, str, int, object]]:
    """Como `reader.iter_contents`, pero los archivos sin cambios salen
    de git. Mismo orden, mismo resultado. `stats["from_git"]` cuenta
    cuántos vinieron de git."""
    git = git or Git()
    try:
        clean = clean_entries(git, folder, entries, small)
    except (OSError, ValueError):
        clean = set()
    if not clean:
        yield from iter_contents(entries, small=small)
        return
    from_git = [entry for entry in entries if entry[0] in clean]
    from_disk = [entry for entry in entries if entry[0] not in clean]
    disk = iter_contents(from_disk, small=small)
    try:
        blobs = _BlobStream(git, folder,
                            [(rel, clean[rel]) for rel, _p, _s in from_git])
    except OSError:
        disk.close()
        yield from iter_contents(entries, small=small)
        return
    served = 0
    broken = False
    try:
        for rel, path, size in entries:
            if rel not in clean:
                yield next(disk)
                continue
            data: Optional[bytes] = None
            if not broken:
                try:
                    data = blobs.next()
                except (OSError, ValueError):
                    broken = True            # el resto, del disco
            content = as_in_folder(data, size) if data is not None \
                else None
            if content is not None:
                served += 1
                yield (rel, path, size, content)
            else:
                # git no lo tiene, o no se puede saber con certeza que es
                # lo mismo que hay en la carpeta: del disco.
                yield (rel, path, size, read_whole(path, small))
    finally:
        blobs.close()
        disk.close()
        if stats is not None:
            stats["from_git"] = served


def as_in_folder(stored: bytes, size_on_disk: int) -> Optional[bytes]:
    """El contenido tal como está en la carpeta, deducido de lo guardado
    por git, o None si no se puede asegurar.

    Para un archivo sin cambios y sin más conversión que la de finales de
    línea, la carpeta tiene lo guardado con algunos «\n» convertidos en
    «\r\n». Si mide lo mismo, no se convirtió ninguno. Si lo guardado no
    tiene ningún «\r» y mide lo mismo más uno por «\n», se convirtieron
    todos. Cualquier otra cosa podría ser una mezcla: no se adivina.
    """
    if len(stored) == size_on_disk:
        return stored
    if b"\r" in stored:
        return None
    if len(stored) + stored.count(b"\n") == size_on_disk:
        return stored.replace(b"\n", b"\r\n")
    return None


def worth_it(folder: str, entries: Sequence[Entry]) -> bool:
    """¿Compensa en esta carpeta? Repositorio, sistema donde abrir es caro
    y bastantes archivos."""
    return ENABLED and len(entries) >= MIN_FILES and \
        os.path.isdir(os.path.join(folder, ".git"))
