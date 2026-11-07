# -*- coding: utf-8 -*-
"""El TXT dividido en partes que quepan en la conversación con una IA.

Cada IA admite un máximo de texto por conversación (su «contexto», que
se mide en tokens). Un proyecto grande no cabe de una vez: aquí se
reparte el TXT ya generado en partes de un tamaño máximo, sin partir
nunca un archivo salvo que él solo no quepa en una parte:

  · la parte 1 lleva la presentación y el árbol completo;
  · cada parte siguiente empieza con una cabecera que dice de qué
    proyecto es y qué archivos trae;
  · un archivo que no cabe entero se reparte con las notas «<SIGUE EN LA
    PARTE n>» y «<VIENE DE LA PARTE n>», y ninguno de sus trozos se
    puede aplicar de vuelta como si fuera el archivo completo;
  · el resumen va al final de la última.

Las partes se guardan en una carpeta junto al TXT («nombre-partes»). Si
ya existe y tiene algo que no son partes nuestras, se usa otro nombre:
nunca se borra ni se pisa nada ajeno.
"""

import os
import re
import stat
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, List, Optional, Sequence, Tuple

from .atomic import atomic_open
from .errors import StepError
from .txtformat import (EQUALS_LINE, HASH_LINE, NOTE_CONTINUED,
                        NOTE_CONTINUES, PART_TITLE, ROOT_PREFIX, SEP_EQ,
                        TXT_STARTS, note)

# Una estimación, no una cuenta exacta: depende de cada IA. En código y
# texto en español sale algo menos de cuatro bytes por token.
BYTES_PER_TOKEN = 3.6
MIN_PART_TOKENS = 4000
PARTS_SUFFIX = "-partes"
COPY_CHUNK = 1 << 20
# Un trozo de archivo partido nunca es más pequeño que esto: si en la
# parte queda menos sitio, el trozo empieza en la parte siguiente.
_MIN_PIECE = 4096
# Lo que se reserva en cada parte para su cabecera y su pie, y en cada
# trozo de un archivo partido para sus notas «SIGUE EN…» / «VIENE DE…».
_PART_OVERHEAD = 1024
_NOTES_ROOM = 256

_PART_NAME = re.compile(r"^parte-(\d+)-de-(\d+)\.txt$")


def estimate_tokens(n_bytes: int) -> int:
    """Tokens aproximados de un texto de `n_bytes` bytes."""
    return int(n_bytes / BYTES_PER_TOKEN + 0.5)


def tokens_to_bytes(tokens: int) -> int:
    return int(max(tokens, MIN_PART_TOKENS) * BYTES_PER_TOKEN)


# ------------------------------------------------------------------ #
#  Dónde van las partes                                               #
# ------------------------------------------------------------------ #
def is_part_file(path: Path) -> bool:
    if not _PART_NAME.match(path.name):
        return False
    try:
        if not stat.S_ISREG(os.lstat(str(path)).st_mode):
            return False
        with open(str(path), "rb") as handle:
            return handle.read(64).startswith(TXT_STARTS)
    except OSError:
        return False


def _folder_is_ours(folder: Path) -> bool:
    """¿No existe, o es una carpeta normal con solo partes nuestras?"""
    if not os.path.lexists(str(folder)):
        return True
    try:
        info = os.lstat(str(folder))
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    try:
        names = os.listdir(str(folder))
    except OSError:
        return False
    return all(is_part_file(folder / name) for name in names)


def parts_folder(txt_path: Path, limit: int = 99) -> Path:
    """La carpeta de las partes junto al TXT, sin pisar nada ajeno."""
    base = txt_path.with_name(txt_path.stem + PARTS_SUFFIX)
    if _folder_is_ours(base):
        return base
    for number in range(2, limit + 1):
        candidate = txt_path.with_name(f"{txt_path.stem}{PARTS_SUFFIX}-{number}")
        if _folder_is_ours(candidate):
            return candidate
    raise StepError(f"No encuentro un nombre libre para las partes junto a "
                    f"«{txt_path.name}».")


# ------------------------------------------------------------------ #
#  El reparto                                                         #
# ------------------------------------------------------------------ #
@dataclass
class _Segment:
    """Un trozo de una parte: bytes del TXT original o texto nuevo."""
    kind: str               # "root" | "copy" | "text" | "header"
    start: int = 0
    end: int = 0
    data: bytes = b""


@dataclass
class _Part:
    segments: List[_Segment]
    size: int
    first: str = ""
    last: str = ""


def _read_range(source: BinaryIO, start: int, end: int) -> bytes:
    source.seek(start)
    return source.read(end - start)


def _cut_at_line(source: BinaryIO, start: int, limit: int) -> int:
    """El final de línea más cercano antes de `limit` (sin pasar de
    `start`); si no hay ninguno en el último mega, el propio `limit`."""
    window_start = max(start, limit - COPY_CHUNK)
    window = _read_range(source, window_start, limit)
    newline = window.rfind(b"\n")
    if newline < 0:
        return limit
    return window_start + newline + 1


def _module_header_length(source: BinaryIO, start: int) -> int:
    """Lo que ocupan «MODULO: ruta» y la raya (las dos primeras líneas)."""
    head = _read_range(source, start, start + 8192)
    first = head.find(b"\n")
    second = head.find(b"\n", first + 1)
    if first < 0 or second < 0:
        return 0
    return second + 1


def plan_parts(source: BinaryIO, modules: Sequence[Tuple[int, int, str]],
               root: Tuple[int, int], summary: Tuple[int, int],
               budget: int) -> List[_Part]:
    """Reparte el TXT: `modules` son (inicio, fin, ruta) de cada módulo."""
    parts: List[_Part] = [_Part([_Segment("root", root[0], root[1])],
                                root[1] - root[0])]

    def new_part() -> _Part:
        part = _Part([_Segment("header")], _PART_OVERHEAD)
        parts.append(part)
        return part

    current = parts[0]
    for start, end, rel in modules:
        length = end - start
        fits_in_a_new_part = length <= budget - _PART_OVERHEAD
        fresh = not current.first and current is not parts[0]
        if current.size + length > budget and fits_in_a_new_part \
                and not fresh:
            current = new_part()
        if current.size + length <= budget:
            current.segments.append(_Segment("copy", start, end))
            current.size += length
            current.first = current.first or rel
            current.last = rel
            continue
        # No cabe ni en una parte vacía: se reparte por líneas.
        header_length = _module_header_length(source, start)
        body_start = start + header_length
        body_end = end - len(SEP_EQ)
        position = body_start
        piece_number = 0
        while position < body_end:
            room = budget - current.size - header_length - _NOTES_ROOM
            if room < _MIN_PIECE and current.first:
                current = new_part()
                continue
            room = max(room, _MIN_PIECE)
            limit = min(body_end, position + room)
            cut = limit if limit == body_end else \
                _cut_at_line(source, position, limit)
            if cut <= position:
                cut = limit
            current.segments.append(_Segment("copy", start, body_start))
            if piece_number:
                current.segments.append(_Segment("text", data=note(
                    f"{NOTE_CONTINUED} {len(parts) - 1}: es la continuación "
                    "de este archivo")))
            current.segments.append(_Segment("copy", position, cut))
            if cut >= body_end:
                current.segments.append(_Segment("copy", body_end, end))
            else:
                follow = (b"" if source_ends_with_newline(source, cut)
                          else b"\n")
                current.segments.append(_Segment("text", data=follow + note(
                    f"{NOTE_CONTINUES} {len(parts) + 1}: este archivo no "
                    "cabe entero en una parte") + SEP_EQ))
            current.size += header_length + (cut - position)
            current.first = current.first or rel
            current.last = rel
            position = cut
            piece_number += 1
            if position < body_end:
                current = new_part()
    parts[-1].segments.append(_Segment("copy", summary[0], summary[1]))
    return parts


def source_ends_with_newline(source: BinaryIO, position: int) -> bool:
    """¿El byte justo antes de `position` es un salto de línea?"""
    if position <= 0:
        return True
    source.seek(position - 1)
    return source.read(1) == b"\n"


# ------------------------------------------------------------------ #
#  Escritura                                                          #
# ------------------------------------------------------------------ #
def _part_header(title: str, number: int, total: int, part: _Part) -> bytes:
    archivos = (f"del «{part.first}» al «{part.last}»"
                if part.first != part.last else f"«{part.first}»")
    first_line = ROOT_PREFIX + PART_TITLE.format(root=title, number=number,
                                                 total=total)
    explanation = textwrap.fill(
        f"Continuación del TXT del proyecto «{title}». Esta parte trae "
        f"{archivos}. El árbol completo y la explicación del formato están "
        "en la parte 1; el resumen, al final de la última.", 78)
    text = f"{first_line}\n{explanation}\n{EQUALS_LINE}\n\n"
    return text.encode("utf-8")


def _part_footer(number: int, total: int) -> bytes:
    return (f"{HASH_LINE}\nFIN DE LA PARTE {number} DE {total}: sigue en la "
            f"parte {number + 1}.\n{HASH_LINE}\n").encode("utf-8")


def _copy(source: BinaryIO, out: BinaryIO, start: int, end: int) -> None:
    source.seek(start)
    remaining = end - start
    while remaining > 0:
        chunk = source.read(min(COPY_CHUNK, remaining))
        if not chunk:
            break
        out.write(chunk)
        remaining -= len(chunk)


def _write_root(source: BinaryIO, out: BinaryIO, segment: _Segment,
                title: str, total: int) -> None:
    """El bloque de presentación, con «· PARTE 1 DE N» en la primera
    línea."""
    head = _read_range(source, segment.start, min(segment.end,
                                                  segment.start + 4096))
    newline = head.find(b"\n")
    if newline < 0 or not head.startswith(ROOT_PREFIX.encode("utf-8")):
        _copy(source, out, segment.start, segment.end)
        return
    out.write((ROOT_PREFIX + PART_TITLE.format(root=title, number=1,
                                               total=total) + "\n"
               ).encode("utf-8"))
    _copy(source, out, segment.start + newline + 1, segment.end)


def split_txt(txt_path: Path, offsets: Sequence[Tuple[int, str]],
              summary_offset: int, max_tokens: int, title: str
              ) -> List[Path]:
    """Escribe las partes del TXT y devuelve sus rutas (en orden).

    `offsets` son (posición, ruta) del comienzo de cada módulo en el TXT
    y `summary_offset`, la del resumen final. Si el TXT entero cabe en
    una parte, no se escribe nada y se devuelve una lista vacía.
    """
    size = txt_path.stat().st_size
    budget = tokens_to_bytes(max_tokens)
    if size <= budget:
        # Cabe entero: las partes de una generación anterior ya no valen.
        _remove_old_parts(txt_path)
        return []
    starts = [offset for offset, _rel in offsets]
    modules: List[Tuple[int, int, str]] = []
    for index, (offset, rel) in enumerate(offsets):
        end = starts[index + 1] if index + 1 < len(starts) else summary_offset
        modules.append((offset, end, rel))
    root_end = starts[0] if starts else summary_offset
    folder = parts_folder(txt_path)
    with open(str(txt_path), "rb") as source:
        parts = plan_parts(source, modules, (0, root_end),
                           (summary_offset, size), budget)
        total = len(parts)
        os.makedirs(str(folder), exist_ok=True)
        width = max(2, len(str(total)))
        written: List[Path] = []
        for number, part in enumerate(parts, 1):
            name = (f"parte-{str(number).zfill(width)}-de-"
                    f"{str(total).zfill(width)}.txt")
            path = folder / name
            with atomic_open(path, fsync=False, buffering=COPY_CHUNK) as out:
                for segment in part.segments:
                    if segment.kind == "root":
                        _write_root(source, out, segment, title, total)
                    elif segment.kind == "header":
                        out.write(_part_header(title, number, total, part))
                    elif segment.kind == "text":
                        out.write(segment.data)
                    else:
                        _copy(source, out, segment.start, segment.end)
                if number < total:
                    out.write(_part_footer(number, total))
            written.append(path)
    _remove_stale_parts(folder, written)
    return written


def _remove_stale_parts(folder: Path, keep: Sequence[Path]) -> None:
    """Quita las partes de una generación anterior (solo partes nuestras:
    nombre «parte-N-de-M.txt» y contenido que empieza como un TXT de la
    app) que ya no forman parte de esta."""
    keep_names = {path.name for path in keep}
    try:
        names = os.listdir(str(folder))
    except OSError:
        return
    for name in names:
        path = folder / name
        if name in keep_names or not is_part_file(path):
            continue
        try:
            os.unlink(str(path))
        except OSError:
            pass


def _remove_old_parts(txt_path: Path) -> None:
    """Quita las partes anteriores del TXT (solo si su carpeta no tiene
    nada más que partes nuestras) y la carpeta, si queda vacía."""
    folder = txt_path.with_name(txt_path.stem + PARTS_SUFFIX)
    if not os.path.lexists(str(folder)) or not _folder_is_ours(folder):
        return
    _remove_stale_parts(folder, [])
    try:
        os.rmdir(str(folder))
    except OSError:
        pass


def without_own_parts(entries, txt_path: Path):
    """Las entradas (rel, ruta, tamaño) sin las partes de este mismo TXT:
    si quedaron dentro del proyecto, el TXT siguiente no las incluye."""
    prefix = txt_path.stem + PARTS_SUFFIX
    parent_of_txt = os.path.normcase(str(txt_path.parent))
    kept = []
    for entry in entries:
        folder = Path(entry[1]).parent
        mine = (os.path.normcase(str(folder.parent)) == parent_of_txt
                and folder.name.startswith(prefix)
                and is_part_file(Path(entry[1])))
        if not mine:
            kept.append(entry)
    return kept


def existing_parts(txt_path: Path) -> Optional[List[Path]]:
    """Las partes ya generadas de un TXT, en orden (None si no hay)."""
    folder = txt_path.with_name(txt_path.stem + PARTS_SUFFIX)
    if not folder.is_dir():
        return None
    found = sorted((path for path in folder.iterdir() if is_part_file(path)),
                   key=lambda path: int(_PART_NAME.match(path.name).group(1)))
    return found or None
