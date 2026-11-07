# -*- coding: utf-8 -*-
"""El TXT de «solo lo que cambió» desde la última publicación.

Pasarle a una IA el proyecto entero cada vez que se toca un archivo es
caro y lento. Con esto el TXT lleva solo lo que cambió desde el último
commit (lo último que se publicó con la app), de una de dos maneras:

  · «archivos»: los archivos que cambiaron, enteros, en el formato de
    siempre (se pueden aplicar de vuelta) y, para los borrados, una nota
    «<BORRADO…>» (que nunca se aplica: aquí no se borra nada);
  · «diferencias»: solo las líneas que entran y salen de cada archivo,
    en bloques «DIFERENCIAS: ruta» (otra cabecera, para que nadie pueda
    aplicarlos como si fueran el contenido entero).

El árbol de carpetas sigue siendo el del proyecto completo: a quien lee
le sirve para situar cada cambio.
"""

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import diff
from .errors import StepError
from .fs import NO_WINDOW, display_name, fmt_size
from .git import GIT_MISSING, Git
from .ignore import IgnoreMatcher, clean_patterns
from .redact import redact_bytes
from .txtbody import small_body
from .txtformat import (DIFF_PREFIX, NOTE_BINARY, NOTE_DELETED, NOTE_FOR_AI,
                        SEP_DASH, SEP_EQ, module_header, note)

CHANGES_NONE = ""
CHANGES_FILES = "archivos"
CHANGES_DIFF = "diferencias"
CHANGE_MODES = (CHANGES_NONE, CHANGES_FILES, CHANGES_DIFF)

# Lo más grande que se compara línea a línea en el modo «diferencias»
# (más allá, una nota con los tamaños).
MAX_DIFF_BYTES = 32 * 1024 * 1024
# Cuántas versiones anteriores se piden a git de una vez.
_HEAD_BATCH = 64


@dataclass
class ChangeSet:
    """Qué cambió en la carpeta respecto al último commit."""
    base: str = ""                  # sha corto ("" si aún no hay commits)
    base_date: str = ""
    modified: List[str] = field(default_factory=list)
    added: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.modified) + len(self.added) + len(self.deleted)

    def describe(self) -> str:
        """«3 modificados, 1 nuevo y 2 borrados»."""
        parts = []
        if self.modified:
            parts.append(f"{len(self.modified)} "
                         f"{'modificado' if len(self.modified) == 1 else 'modificados'}")
        if self.added:
            parts.append(f"{len(self.added)} "
                         f"{'nuevo' if len(self.added) == 1 else 'nuevos'}")
        if self.deleted:
            parts.append(f"{len(self.deleted)} "
                         f"{'borrado' if len(self.deleted) == 1 else 'borrados'}")
        if not parts:
            return "ningún cambio"
        if len(parts) == 1:
            return parts[0]
        return ", ".join(parts[:-1]) + " y " + parts[-1]

    def reference(self) -> str:
        """«el commit abc1234 (2026-11-05 10:20)» o «el principio»."""
        if not self.base:
            return "el principio (el repositorio aún no tiene commits)"
        when = f", del {self.base_date}" if self.base_date else ""
        return f"el commit {self.base}{when}"


def changes_since_last_commit(folder: str, git: Optional[Git] = None
                              ) -> ChangeSet:
    """Archivos modificados, nuevos y borrados respecto a HEAD, con las
    mismas reglas de ignorado que la publicación (.gitignore, la lista
    privada, las exclusiones globales)."""
    root = Path(folder)
    if not (root / ".git").exists():
        raise StepError("«Solo lo que cambió» necesita que la carpeta sea un "
                        "repositorio de git (con al menos una publicación). "
                        "Esta no lo es: genera el TXT completo.")
    git = git or Git()
    result = ChangeSet()
    if git.has_commits(str(root)):
        result.base = git.sha(str(root), "HEAD")[:7]
        dated = git.run(["log", "-1", "--format=%cd",
                         "--date=format:%Y-%m-%d %H:%M"], cwd=str(root),
                        check=False, echo=False)
        result.base_date = (dated.stdout or "").strip()
    status = git.run(["status", "--porcelain=v1", "-z",
                      "--untracked-files=all", "--no-renames"],
                     cwd=str(root), check=False, echo=False, binary=True)
    for record in (status.stdout or b"").split(b"\0"):
        if len(record) < 4:
            continue
        code = record[:2].decode("ascii", "replace")
        rel = os.fsdecode(record[3:])
        if rel.endswith("/"):
            continue                       # un repositorio anidado entero
        on_disk = os.path.lexists(str(root / rel))
        if code == "??" or "A" in code:
            result.added.append(rel)
        elif "D" in code and not on_disk:
            result.deleted.append(rel)
        else:
            result.modified.append(rel)
    result.modified.sort(key=str.lower)
    result.added.sort(key=str.lower)
    result.deleted.sort(key=str.lower)
    return result


def select_changed(entries: Sequence[Tuple[str, str, int]],
                   change_set: ChangeSet, extra_patterns: Sequence[str]
                   ) -> Tuple[List[Tuple[str, str, int]], List[str]]:
    """(entradas que cambiaron, rutas borradas) dentro de lo que el TXT
    incluiría: lo excluido por el cuadro de ignorar no aparece."""
    changed = set(change_set.modified) | set(change_set.added)
    kept = [entry for entry in entries if entry[0] in changed]
    matcher = IgnoreMatcher(clean_patterns(extra_patterns))
    deleted = [rel for rel in change_set.deleted
               if not matcher.ignored_path(rel.lower())]
    return kept, deleted


def deleted_note(change_set: ChangeSet) -> bytes:
    return note(f"{NOTE_DELETED}: ya no está en la carpeta; estaba en "
                f"{change_set.reference()}")


def write_deleted(write: Callable[[bytes], object], deleted: Sequence[str],
                  change_set: ChangeSet, written: int,
                  offsets: Optional[List[Tuple[int, str]]] = None) -> int:
    """Un módulo con la nota «BORRADO» por cada archivo borrado."""
    body = deleted_note(change_set)
    for rel in deleted:
        if offsets is not None:
            offsets.append((written, display_name(rel)))
        block = module_header(display_name(rel)) + body + SEP_EQ
        write(block)
        written += len(block)
    return written


# ------------------------------------------------------------------ #
#  Modo «diferencias»                                                 #
# ------------------------------------------------------------------ #
def read_head_versions(git: Git, folder: str, rels: Sequence[str]
                       ) -> Dict[str, Optional[bytes]]:
    """{ruta: contenido en HEAD} de varias rutas con un solo
    `git cat-file --batch` (None: no existía en HEAD)."""
    wanted = [rel for rel in rels if "\n" not in rel]
    out: Dict[str, Optional[bytes]] = {rel: None for rel in rels}
    if not wanted:
        return out
    try:
        process = subprocess.Popen(
            [git.exe, "cat-file", "--batch"], cwd=folder,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=NO_WINDOW,
            env=git._env())
    except FileNotFoundError:
        raise StepError(GIT_MISSING) from None
    try:
        for start in range(0, len(wanted), _HEAD_BATCH):
            batch = wanted[start:start + _HEAD_BATCH]
            request = b"".join(b"HEAD:" + os.fsencode(rel) + b"\n"
                               for rel in batch)
            process.stdin.write(request)
            process.stdin.flush()
            for rel in batch:
                header = process.stdout.readline().split()
                if len(header) < 3 or header[-1] == b"missing":
                    continue
                if header[1] != b"blob":
                    size = int(header[2])
                    process.stdout.read(size + 1)
                    continue
                size = int(header[2])
                data = process.stdout.read(size)
                process.stdout.read(1)
                out[rel] = data
    finally:
        for pipe in (process.stdin, process.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        process.wait()
    return out


def _body_lines(raw: bytes, rel: str, redact: bool) -> Tuple[
        Optional[List[str]], int]:
    """(líneas tal como irían en el TXT, secretos tachados) o (None, 0)
    si es binario."""
    body = small_body(raw)
    if body is None:
        return None, 0
    count = 0
    if redact:
        body, count = redact_bytes(body, rel)
    text = body.decode("utf-8", "replace")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if lines == [""]:
        lines = []
    return lines, count


def render_hunks(old: Sequence[str], new: Sequence[str]) -> bytes:
    """Los trozos «@@» de un diff unificado, como bytes UTF-8."""
    parts: List[str] = []
    for hunk in diff.hunks(old, new):
        parts.append(diff.hunk_header(hunk))
        for line in hunk.lines:
            parts.append(line.tag + line.text)
    if not parts:
        return b""
    return ("\n".join(parts) + "\n").encode("utf-8")


def _read_current(path: str) -> Tuple[Optional[bytes], str]:
    try:
        size = os.lstat(path).st_size
        if size > MAX_DIFF_BYTES:
            return None, f"demasiado grande para compararlo ({fmt_size(size)})"
        with open(path, "rb") as handle:
            return handle.read(), ""
    except OSError as e:
        return None, e.strerror or str(e)


def diff_body(rel: str, old: Optional[bytes], new: Optional[bytes],
              redact: bool, noise: str = "") -> Tuple[bytes, Dict[str, int]]:
    """El cuerpo de un bloque «DIFERENCIAS» y lo que hay que contar."""
    counts = {"diffs": 0, "binaries": 0, "redacted": 0, "for_ai": 0}
    if noise:
        counts["for_ai"] = 1
        size = len(new or b"")
        return note(f"{NOTE_FOR_AI}: {noise} ({fmt_size(size)})"), counts
    old_lines: List[str] = []
    if old is not None:
        lines, hidden = _body_lines(old, rel, redact)
        if lines is None:
            counts["binaries"] = 1
            before = fmt_size(len(old))
            after = fmt_size(len(new or b""))
            return note(f"{NOTE_BINARY}: cambió ({before} → {after})"), \
                counts
        old_lines = lines
        counts["redacted"] += hidden
    new_lines, hidden = _body_lines(new or b"", rel, redact)
    if new_lines is None:
        counts["binaries"] = 1
        return note(f"{NOTE_BINARY} ({fmt_size(len(new or b''))})"), counts
    counts["redacted"] += hidden
    rendered = render_hunks(old_lines, new_lines)
    if not rendered:
        return note("SIN DIFERENCIAS DE CONTENIDO (cambiaron los blancos, "
                    "los finales de línea o los permisos)"), counts
    counts["diffs"] = 1
    return rendered, counts


def diff_header(display_path: str) -> bytes:
    return (DIFF_PREFIX + display_path + "\n").encode("utf-8") + SEP_DASH


def write_diff_modules(write: Callable[[bytes], object], folder: str,
                       entries: Sequence[Tuple[str, str, int]],
                       deleted: Sequence[str], change_set: ChangeSet,
                       counters: Dict[str, int], written: int,
                       redact: bool = False,
                       noise_reason: Optional[Callable[[str, int, bytes],
                                                       str]] = None,
                       watch=None, git: Optional[Git] = None,
                       offsets: Optional[List[Tuple[int, str]]] = None
                       ) -> int:
    """Un bloque «DIFERENCIAS: ruta» por archivo cambiado (y por borrado).

    Con `offsets` se anota (posición, ruta) del comienzo de cada bloque,
    para poder repartir el TXT en partes."""
    git = git or Git()
    for start in range(0, len(entries), _HEAD_BATCH):
        batch = entries[start:start + _HEAD_BATCH]
        previous = read_head_versions(git, folder,
                                      [rel for rel, _p, _s in batch])
        for rel, path, size in batch:
            current, problem = _read_current(path)
            if current is None:
                body = note(f"NO SE PUDO COMPARAR: {problem}")
                counters["errors"] = counters.get("errors", 0) + 1
            else:
                noise = noise_reason(rel, size, current) if noise_reason \
                    else ""
                body, counts = diff_body(rel, previous.get(rel), current,
                                         redact, noise)
                for key, value in counts.items():
                    counters[key] = counters.get(key, 0) + value
                if counts["redacted"]:
                    counters["redacted_files"] = \
                        counters.get("redacted_files", 0) + 1
            if offsets is not None:
                offsets.append((written, display_name(rel)))
            block = diff_header(display_name(rel)) + body + SEP_EQ
            write(block)
            written += len(block)
            if watch is not None:
                watch.done += 1
                watch(written)
    body = deleted_note(change_set)
    for rel in deleted:
        if offsets is not None:
            offsets.append((written, display_name(rel)))
        block = diff_header(display_name(rel)) + body + SEP_EQ
        write(block)
        written += len(block)
    return written
