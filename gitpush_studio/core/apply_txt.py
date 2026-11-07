# -*- coding: utf-8 -*-
"""Aplicar un TXT (o la respuesta de una IA) de vuelta al proyecto.

El TXT del proyecto se le pasa a una IA, la IA devuelve los archivos
cambiados en el mismo formato («MODULO: ruta» + contenido) y aquí se
llevan esos cambios a la carpeta. En dos tiempos:

    plan = plan_apply(carpeta, texto)        # no toca NADA: solo mira
    … se enseñan las diferencias y se elige qué aplicar …
    resultado = apply_plan(plan, rutas_elegidas)

Reglas (cada una tiene su prueba):

  · Nunca fuera de la carpeta. Se rechazan las rutas absolutas, las que
    suben con «..», las que entran en .git y las que pasan por un enlace
    simbólico (o una unión de Windows), aunque apunte dentro.
  · Nunca se borra nada ni se vacía un archivo. Una nota del TXT
    («binario omitido», «borrado»…) no es un contenido y no se aplica.
  · Lo que no cambió no cambia. El archivo se modifica LÍNEA A LÍNEA:
    cada línea igual se copia byte a byte del original (con su final de
    línea, sus blancos del final, su codificación y hasta sus bytes
    raros), y solo las líneas nuevas se escriben, con el final de línea
    y la codificación del archivo (UTF-8, UTF-16, ANSI, con o sin BOM).
    Una IA que quita los blancos del final de una línea o cambia los
    «\\r\\n» por «\\n» no provoca ningún cambio.
  · Antes de escribir, una copia exacta: en un repositorio, una rama
    «respaldo-FECHA» que no se publica y se recupera desde «Versiones»;
    fuera de git, una carpeta en ~/.gitpush_studio/respaldos.
  · Entre revisar y aplicar no puede cambiar nada: si un archivo se
    editó mientras tanto (o apareció uno donde se iba a crear otro), no
    se escribe ninguno.
  · Lo que parece un resumen de la IA («# … resto del código igual …»
    en lugar de cien líneas) o una respuesta cortada a medias se marca
    como sospechoso y no se aplica salvo que se elija a mano.
"""

import os
import re
import shutil
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import diff
from .atomic import create_exclusive, write_bytes
from .config import CONFIG_DIR
from .errors import StepError
from .pack import is_generated_txt
from .redact import unredact_lines
from .safekeeping import backup as git_backup
from .safekeeping import stat_signature
from .textenc import detect, legacy_encoding
from .txtformat import ParsedTxt, TxtModule, parse_txt

STATUS_NEW = "nuevo"
STATUS_MODIFIED = "modificado"
STATUS_SAME = "igual"
STATUS_REJECTED = "rechazado"
APPLICABLE = (STATUS_NEW, STATUS_MODIFIED)

BACKUP_ROOT = CONFIG_DIR / "respaldos"
# Más allá de esto un archivo no se compara línea a línea (no es código
# que alguien pase a una IA): se rechaza en vez de dejar la ventana
# pensando un minuto.
MAX_APPLY_BYTES = 64 * 1024 * 1024

# Los blancos que el TXT recorta al final de cada archivo (los mismos
# que `bytes.rstrip()`), y los que no cuentan al comparar una línea.
_TRAILING = " \t\n\r\x0b\x0c"
_LINE_BLANKS = " \t\r\x0b\x0c"

# Nombres que Windows no deja crear (y que romperían el proyecto al
# clonarlo allí): CON, PRN, AUX, NUL, COM1-9, LPT1-9, con o sin extensión.
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$",
                       re.IGNORECASE)
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')

# Uniones y enlaces de Windows (os.lstat no los marca siempre como
# enlace): IO_REPARSE_TAG_MOUNT_POINT y IO_REPARSE_TAG_SYMLINK.
_LINK_REPARSE_TAGS = (0xA0000003, 0xA000000C)

# Lo que escribe una IA cuando resume en vez de copiar: «...», «…»,
# «# ... resto igual ...», «// existing code», «<!-- sin cambios -->».
_ELLIPSIS_ONLY = re.compile(
    r"^\s*(?:#|//|/\*+|\*|--|;|<!--|%|')?\s*(?:\.{3,}|…)\s*"
    r"(?:\*+/|-->)?\s*$")
_PLACEHOLDER_WORDS = re.compile(
    r"(resto del (código|archivo|fichero)|el resto (igual|sin cambios)|"
    r"sin cambios|código (existente|anterior|omitido)|"
    r"rest of (the )?(code|file)|existing code|unchanged|"
    r"code (omitted|unchanged)|same as (before|original))",
    re.IGNORECASE)
_COMMENT_START = re.compile(r"^\s*(#|//|/\*|\*|--|;|<!--|%|')")
# Para que una línea así cuente, el mismo bloque tiene que llevarse al
# menos estas líneas del original.
_PLACEHOLDER_MIN_REMOVED = 3
# Un final del archivo que desaparece entero: al menos estas líneas y
# esta fracción del archivo.
_TRUNCATION_MIN_LINES = 10
_TRUNCATION_FRACTION = 0.3
# «Cambia más de la mitad del archivo» solo se avisa en archivos de al
# menos estas líneas: en uno de tres líneas es lo normal.
_BIG_CHANGE_MIN_LINES = 10


# ------------------------------------------------------------------ #
#  Datos                                                              #
# ------------------------------------------------------------------ #
@dataclass
class FileChange:
    """Lo que pasaría con un archivo si se aplicara el TXT."""
    path: str                       # relativa, con «/», como en el TXT
    target: Optional[Path]          # dónde se escribiría (None: rechazado)
    status: str
    reason: str = ""                # por qué se rechaza
    warnings: List[str] = field(default_factory=list)
    suspicious: bool = False        # no se marca por defecto
    old_lines: List[str] = field(default_factory=list)
    new_lines: List[str] = field(default_factory=list)
    codes: List[diff.Opcode] = field(default_factory=list)
    data: bytes = b""               # el archivo entero tal como quedaría
    signature: Optional[tuple] = None   # lstat del original al revisar
    encoding: str = ""
    added: int = 0
    removed: int = 0
    line_number: int = 0            # de la cabecera, en el TXT

    @property
    def applicable(self) -> bool:
        return self.status in APPLICABLE


@dataclass
class ApplyPlan:
    folder: Path
    kind: str
    changes: List[FileChange]
    notes: List[str] = field(default_factory=list)

    @property
    def applicable(self) -> List[FileChange]:
        return [c for c in self.changes if c.applicable]

    def default_selection(self) -> List[str]:
        """Lo que se marca de entrada: lo aplicable que no es sospechoso."""
        return [c.path for c in self.applicable if not c.suspicious]

    def counts(self) -> Dict[str, int]:
        out = {STATUS_NEW: 0, STATUS_MODIFIED: 0, STATUS_SAME: 0,
               STATUS_REJECTED: 0}
        for change in self.changes:
            out[change.status] += 1
        return out


@dataclass
class ApplyResult:
    written: List[str] = field(default_factory=list)
    created: List[str] = field(default_factory=list)
    failed: List[Tuple[str, str]] = field(default_factory=list)
    backup: str = ""               # rama o carpeta del respaldo
    backup_kind: str = ""          # "rama" | "carpeta" | ""

    @property
    def ok(self) -> bool:
        return not self.failed


@dataclass
class _Original:
    """Un archivo existente, troceado en líneas sin perder ni un byte."""
    bom: bytes
    encoding: str                  # con la que se escriben las líneas nuevas
    raw_lines: List[bytes]         # bytes exactos de cada línea (con su fin)
    shown_lines: List[str]         # cada línea tal como sale en el TXT
    visible: int                   # líneas antes de los blancos del final
    newline: str                   # el final de línea más frecuente


# ------------------------------------------------------------------ #
#  Rutas                                                              #
# ------------------------------------------------------------------ #
def split_safe_path(raw: str) -> Tuple[List[str], str]:
    """(partes de la ruta, "") o ([], motivo) si la ruta no es segura.

    Segura quiere decir relativa, sin «..», sin pasar por .git y sin
    caracteres de control. Las partes vacías y los «.» se quitan.
    """
    path = (raw or "").strip()
    if not path:
        return [], "la ruta está vacía"
    if "\0" in path or any(ord(ch) < 32 for ch in path):
        return [], "la ruta tiene caracteres de control"
    if path.startswith(("/", "\\", "~")) or re.match(r"^[A-Za-z]:", path):
        return [], "es una ruta absoluta: solo aplico rutas dentro del proyecto"
    parts: List[str] = []
    for part in path.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return [], "sube de carpeta con «..»: se saldría del proyecto"
        if part.lower() == ".git":
            return [], "está dentro de .git (los datos internos de git)"
        parts.append(part)
    if not parts:
        return [], "la ruta está vacía"
    return parts, ""


def windows_name_problem(part: str) -> str:
    """Por qué `part` no vale como nombre en Windows, o ""."""
    if any(ch in _WINDOWS_FORBIDDEN for ch in part):
        return f"«{part}» lleva caracteres que Windows no admite (<>:\"|?*)"
    if part.endswith((" ", ".")):
        return f"«{part}» acaba en espacio o punto, y Windows no lo admite"
    if _RESERVED.match(part):
        return f"«{part}» es un nombre reservado de Windows"
    return ""


def _is_link(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    tag = getattr(info, "st_reparse_tag", 0)
    return tag in _LINK_REPARSE_TAGS


def _inside(folder: Path, path: Path) -> bool:
    base = os.path.normcase(os.path.realpath(str(folder)))
    real = os.path.normcase(os.path.realpath(str(path)))
    return real == base or real.startswith(base.rstrip(os.sep) + os.sep)


def _exact_name_present(parent: Path, name: str) -> Tuple[bool, str]:
    """(¿existe con ese nombre exacto?, nombre parecido que ya existe).

    En Windows y macOS «Main.py» y «main.py» son el mismo archivo; en
    Linux, dos distintos que chocarían al clonar en Windows.
    """
    try:
        names = os.listdir(str(parent))
    except OSError:
        return False, ""
    if name in names:
        return True, ""
    folded = name.casefold()
    for existing in names:
        if existing.casefold() == folded:
            return False, existing
    return False, ""


def resolve_target(folder: Path, parts: Sequence[str]
                   ) -> Tuple[Optional[Path], bool, str]:
    """(ruta, ¿existe?, motivo de rechazo) de `parts` dentro de `folder`.

    Se recorre la ruta tramo a tramo con lstat: un enlace o una unión en
    cualquier tramo la rechaza (aunque apunte dentro: lo que se ve en el
    TXT no sería lo que se escribe). Al final, la ruta real tiene que
    seguir dentro de la carpeta.
    """
    current = folder
    for depth, part in enumerate(parts):
        last = depth == len(parts) - 1
        exact, similar = _exact_name_present(current, part)
        if similar:
            return None, False, (f"ya existe «{similar}» (solo cambian "
                                 "mayúsculas y minúsculas): no creo otro")
        current = current / part
        if not exact:
            # A partir de aquí no existe nada: el resto se creará.
            for pending in parts[depth:]:
                problem = windows_name_problem(pending)
                if problem:
                    return None, False, problem
            if not _inside(folder, current.parent):
                return None, False, "se saldría de la carpeta del proyecto"
            return folder.joinpath(*parts), False, ""
        try:
            info = os.lstat(str(current))
        except FileNotFoundError:
            return folder.joinpath(*parts), False, ""
        except OSError as e:
            return None, False, f"no pude mirarlo ({e.strerror or e})"
        if _is_link(info):
            return None, False, (f"«{'/'.join(parts[:depth + 1])}» es un "
                                 "enlace simbólico: no escribo a través de "
                                 "enlaces")
        if not last and not stat.S_ISDIR(info.st_mode):
            return None, False, (f"«{'/'.join(parts[:depth + 1])}» es un "
                                 "archivo, no una carpeta")
        if last:
            if stat.S_ISDIR(info.st_mode):
                return None, True, "es una carpeta, no un archivo"
            if not stat.S_ISREG(info.st_mode):
                return None, True, "no es un archivo normal"
    if not _inside(folder, current):
        return None, True, "se saldría de la carpeta del proyecto"
    return current, True, ""


# ------------------------------------------------------------------ #
#  El archivo original, línea a línea                                 #
# ------------------------------------------------------------------ #
def _split_keepends(data: bytes) -> List[bytes]:
    """Líneas de `data` separadas SOLO por «\\n», cada una con su fin."""
    lines: List[bytes] = []
    start = 0
    while True:
        end = data.find(b"\n", start)
        if end < 0:
            if start < len(data):
                lines.append(data[start:])
            return lines
        lines.append(data[start:end + 1])
        start = end + 1


def _strip_terminator(line: str) -> str:
    return line[:-1] if line.endswith("\n") else line


def _visible_count(shown_lines: Sequence[str]) -> int:
    """Líneas hasta el último carácter que no es blanco: las que el TXT
    enseña. Las de después (blancos del final) no se tocan nunca."""
    visible = len(shown_lines)
    while visible > 0 and not shown_lines[visible - 1].strip(_TRAILING):
        visible -= 1
    return visible


def _dominant_newline(raw_lines: Sequence[bytes], crlf: bytes,
                      lf: bytes) -> str:
    windows = 0
    unix = 0
    for line in raw_lines:
        if line.endswith(crlf):
            windows += 1
        elif line.endswith(lf):
            unix += 1
    return "\r\n" if windows > unix else "\n"


def load_original(raw: bytes) -> Tuple[Optional[_Original], str]:
    """El archivo troceado, o (None, motivo) si no se puede reescribir
    línea a línea sin riesgo (binario, UTF-16 roto…)."""
    encoding, bom_length = detect(raw, strict=True)
    if encoding is None:
        return None, "es un archivo binario"
    bom = raw[:bom_length]
    body = raw[bom_length:]
    if encoding.startswith("utf-16"):
        try:
            text = body.decode(encoding, "strict")
        except UnicodeDecodeError:
            return None, ("está en UTF-16 con datos dañados: no lo reescribo "
                          "para no estropearlo más")
        pieces = text.split("\n")
        texts = [piece + "\n" for piece in pieces[:-1]]
        if pieces[-1]:
            texts.append(pieces[-1])
        raw_lines = [piece.encode(encoding) for piece in texts]
        shown = [_strip_terminator(piece) for piece in texts]
        newline = _dominant_newline(raw_lines, "\r\n".encode(encoding),
                                    "\n".encode(encoding))
        return _Original(bom, encoding, raw_lines, shown,
                         _visible_count(shown), newline), ""

    raw_lines = _split_keepends(body)
    try:
        body.decode("utf-8")
        read_as = "utf-8"
        write_as = "utf-8"
    except UnicodeDecodeError as error:
        # La misma regla que el TXT (ver `txtbody`): si antes del primer
        # byte inválido todo era ASCII, el archivo está en ANSI; si no,
        # es UTF-8 con algún byte suelto roto (que se conserva tal cual
        # en las líneas que no cambian).
        if body[:error.start].isascii():
            read_as = legacy_encoding()
            write_as = read_as
        else:
            read_as = "utf-8"
            write_as = "utf-8"
    shown = [_strip_terminator(line.decode(read_as, "replace"))
             for line in raw_lines]
    newline = _dominant_newline(raw_lines, b"\r\n", b"\n")
    return _Original(bom, write_as, raw_lines, shown, _visible_count(shown),
                     newline), ""


def _key(line: str) -> str:
    """Lo que cuenta al comparar dos líneas: todo menos los blancos del
    final (una IA los quita a menudo) y el «\\r»."""
    return line.rstrip(_LINE_BLANKS)


# ------------------------------------------------------------------ #
#  Construir el archivo nuevo                                         #
# ------------------------------------------------------------------ #
def _encode_line(text: str, encoding: str) -> bytes:
    return text.encode(encoding, "strict")


def build_patched(original: _Original, new_lines: Sequence[str],
                  codes: Sequence[diff.Opcode]) -> bytes:
    """El archivo con los cambios: las líneas iguales salen byte a byte
    del original y las nuevas se codifican como el original.

    Lanza UnicodeEncodeError si una línea nueva no cabe en la
    codificación del archivo (un emoji en un archivo ANSI).
    """
    newline_bytes = _encode_line(original.newline, original.encoding)
    line_feed = _encode_line("\n", original.encoding)
    out: List[bytes] = [original.bom]
    unterminated = False         # ¿la última línea escrita no tenía fin?
    last_was_new = False
    for code in codes:
        if code.tag == diff.EQUAL:
            for index in range(code.a_start, code.a_end):
                if unterminated:
                    out.append(newline_bytes)
                raw_line = original.raw_lines[index]
                out.append(raw_line)
                unterminated = not raw_line.endswith(line_feed)
                last_was_new = False
            continue
        for index in range(code.b_start, code.b_end):
            if unterminated:
                out.append(newline_bytes)
                unterminated = False
            text = new_lines[index].rstrip("\r")
            out.append(_encode_line(text, original.encoding) + newline_bytes)
            last_was_new = True
    tail = original.raw_lines[original.visible:]
    out.extend(tail)
    data = b"".join(out)
    # Si el original no acababa en salto de línea, el nuevo tampoco: es
    # una convención del archivo (y una diferencia que nadie pidió).
    original_ends_bare = bool(original.raw_lines) and \
        not original.raw_lines[-1].endswith(line_feed)
    if (original_ends_bare and not tail and last_was_new
            and data.endswith(newline_bytes)):
        data = data[:-len(newline_bytes)]
    return data


def _new_file_data(lines: Sequence[str]) -> bytes:
    if not lines:
        return b""
    return ("\n".join(line.rstrip("\r") for line in lines) + "\n"
            ).encode("utf-8")


# ------------------------------------------------------------------ #
#  ¿Parece un resumen o una respuesta cortada?                         #
# ------------------------------------------------------------------ #
def _is_placeholder(line: str) -> bool:
    if _ELLIPSIS_ONLY.match(line):
        return True
    if _COMMENT_START.match(line) and _PLACEHOLDER_WORDS.search(line):
        return True
    return False


def suspicion(codes: Sequence[diff.Opcode], new_lines: Sequence[str],
              old_count: int) -> str:
    """Por qué el cambio parece un resumen o una respuesta cortada, o ""."""
    for code in codes:
        if code.tag not in (diff.REPLACE, diff.INSERT):
            continue
        removed = code.a_end - code.a_start
        for index in range(code.b_start, code.b_end):
            if removed >= _PLACEHOLDER_MIN_REMOVED and \
                    _is_placeholder(new_lines[index]):
                return (f"la línea {index + 1} («{new_lines[index].strip()}») "
                        f"parece un resumen y sustituye {removed} líneas "
                        "del original: la IA pudo abreviar en vez de copiar")
    if codes:
        last = codes[-1]
        if last.tag in (diff.DELETE, diff.REPLACE) and last.a_end == old_count:
            removed = last.a_end - last.a_start
            added = last.b_end - last.b_start
            if (removed - added >= _TRUNCATION_MIN_LINES
                    and removed >= _TRUNCATION_FRACTION * old_count):
                return (f"desaparecen las últimas {removed - added} líneas "
                        "del archivo: la respuesta pudo quedarse cortada")
    return ""


# ------------------------------------------------------------------ #
#  El plan                                                            #
# ------------------------------------------------------------------ #
_NOTE_REASONS = {
    "binario": "en el TXT solo hay una nota (es un binario): no hay "
               "contenido que aplicar",
    "enlace": "en el TXT es un enlace simbólico: no hay contenido que "
              "aplicar",
    "especial": "en el TXT es un archivo especial: no hay contenido",
    "error": "el TXT no pudo leerlo: no hay contenido que aplicar",
    "cortado": "el TXT avisaba de que estaba cortado",
    "omitido": "el TXT lo dejaba fuera a propósito: no hay contenido",
    "borrado": "el TXT dice que se borró, y aquí nunca se borra nada",
    "secreto": "en el TXT estaba tachado entero por llevar secretos: no hay "
               "contenido que aplicar",
}


def _read_original(target: Path) -> Tuple[Optional[bytes], Optional[tuple],
                                          str]:
    """(bytes, firma, motivo de rechazo) del archivo existente."""
    try:
        info = os.lstat(str(target))
    except OSError as e:
        return None, None, f"no pude leerlo ({e.strerror or e})"
    if info.st_size > MAX_APPLY_BYTES:
        return None, None, ("es demasiado grande para compararlo línea a "
                            "línea (más de 64 MB)")
    if getattr(info, "st_nlink", 1) > 1:
        return None, None, ("tiene enlaces duros: reescribirlo los "
                            "separaría y dejaría versiones distintas")
    try:
        with open(str(target), "rb") as handle:
            raw = handle.read()
    except OSError as e:
        return None, None, f"no pude leerlo ({e.strerror or e})"
    return raw, stat_signature(info), ""


def _compare(original: _Original, lines: Sequence[str]
             ) -> List[diff.Opcode]:
    old_keys = [_key(line) for line in original.shown_lines[:original.visible]]
    new_keys = [_key(line) for line in lines]
    return diff.opcodes(old_keys, new_keys)


def _plan_existing(change: FileChange, module: TxtModule) -> None:
    raw, signature, problem = _read_original(change.target)
    if problem:
        change.status, change.reason = STATUS_REJECTED, problem
        return
    if is_generated_txt(str(change.target)):
        change.status = STATUS_REJECTED
        change.reason = "es un TXT generado por la app, no un archivo del proyecto"
        return
    original, problem = load_original(raw)
    if original is None:
        change.status, change.reason = STATUS_REJECTED, problem
        return
    change.signature = signature
    change.encoding = original.encoding
    shown = original.shown_lines[:original.visible]
    change.old_lines = list(shown)

    variants = [list(module.lines)]
    if module.fenced_lines is not None:
        variants.append(list(module.fenced_lines))
    best: Optional[Tuple[int, List[str], List[diff.Opcode]]] = None
    for lines in variants:
        # Los «••••••••» de un TXT con secretos tachados vuelven a ser los
        # valores reales del original (o el archivo no se aplica).
        lines, problem = unredact_lines(lines, shown, change.path)
        if problem:
            change.status, change.reason = STATUS_REJECTED, problem
            return
        codes = _compare(original, lines)
        added, removed = diff.change_counts(codes)
        if best is None or added + removed < best[0]:
            best = (added + removed, lines, codes)
    _, lines, codes = best
    change.new_lines = list(lines)
    change.codes = codes
    change.added, change.removed = diff.change_counts(codes)

    if not lines and original.visible:
        change.status = STATUS_REJECTED
        change.reason = "en el TXT viene vacío, y no vacío archivos"
        return
    if not diff.has_changes(codes):
        change.status = STATUS_SAME
        return
    try:
        change.data = build_patched(original, lines, codes)
    except UnicodeEncodeError as e:
        bad = e.object[e.start:e.end]
        change.status = STATUS_REJECTED
        change.reason = (f"tiene caracteres («{bad}») que no caben en la "
                         f"codificación del archivo ({original.encoding})")
        return
    change.status = STATUS_MODIFIED
    reason = suspicion(codes, lines, original.visible)
    if reason:
        change.suspicious = True
        change.warnings.append(reason)
    elif (original.visible >= _BIG_CHANGE_MIN_LINES
          and change.removed * 2 > original.visible):
        change.warnings.append("cambia más de la mitad del archivo")


def _plan_module(folder: Path, module: TxtModule) -> FileChange:
    relative = module.path if not module.repo else \
        module.repo + "/" + module.path
    change = FileChange(path=relative, target=None, status=STATUS_REJECTED,
                        line_number=module.line_number,
                        new_lines=list(module.lines))
    if not module.terminated:
        change.warnings.append("no tiene separador final: comprueba que no "
                               "se haya colado texto de la conversación")
    if module.note:
        change.reason = _NOTE_REASONS.get(module.note, "es solo una nota")
        return change
    if module.incomplete:
        change.reason = ("el TXT avisaba de que este contenido no estaba "
                         "entero: aplicarlo cortaría el archivo")
        return change
    parts, problem = split_safe_path(relative)
    if problem:
        change.reason = problem
        return change
    change.path = "/".join(parts)
    target, exists, problem = resolve_target(folder, parts)
    if problem:
        change.reason = problem
        return change
    change.target = target
    if exists:
        _plan_existing(change, module)
        return change
    lines, problem = unredact_lines(list(module.lines), None, change.path)
    if problem:
        change.reason = problem
        return change
    change.new_lines = lines
    change.data = _new_file_data(lines)
    change.added = len(lines)
    change.encoding = "utf-8"
    change.status = STATUS_NEW
    change.codes = [diff.Opcode(diff.INSERT, 0, 0, 0, len(lines))] \
        if lines else []
    return change


def plan_apply(folder: str, text: str) -> ApplyPlan:
    """Qué pasaría al aplicar `text` en `folder`. No toca nada."""
    root = Path(folder)
    if not root.is_dir():
        raise StepError("La carpeta del proyecto no existe.")
    root = root.resolve()
    parsed: ParsedTxt = parse_txt(text)
    plan = ApplyPlan(folder=root, kind=parsed.kind, changes=[])
    if not parsed.modules:
        plan.notes.append("No encontré ningún archivo en el texto: cada uno "
                          "tiene que empezar con una línea «MODULO: ruta».")
        return plan
    if parsed.kind == "proyecto" and parsed.root and \
            parsed.root.casefold() != root.name.casefold():
        plan.notes.append(f"El TXT es del proyecto «{parsed.root}» y la "
                          f"carpeta elegida se llama «{root.name}»: "
                          "comprueba que es la correcta.")
    if parsed.kind == "perfil":
        plan.notes.append("Es el TXT de un perfil: cada archivo va a la "
                          "carpeta de su repositorio dentro de la elegida.")
    seen: Dict[str, int] = {}
    for module in parsed.modules:
        change = _plan_module(root, module)
        plan.changes.append(change)
        key = change.path.casefold()
        seen[key] = seen.get(key, 0) + 1
        if module.ignored_tail:
            excerpt = module.ignored_tail.splitlines()[0][:80]
            plan.notes.append(f"Ignoré texto que no pertenece a ningún "
                              f"archivo (tras «{module.path}»): «{excerpt}»")
    for change in plan.changes:
        times = seen.get(change.path.casefold(), 0)
        if times > 1 and change.status != STATUS_REJECTED:
            change.status = STATUS_REJECTED
            change.reason = (f"aparece {times} veces en el TXT: no sé cuál "
                             "es la buena")
    return plan


# ------------------------------------------------------------------ #
#  Aplicar                                                            #
# ------------------------------------------------------------------ #
def _current_signature(target: Path) -> Optional[tuple]:
    try:
        return stat_signature(os.lstat(str(target)))
    except FileNotFoundError:
        return None
    except OSError:
        return ("ilegible",)


def _verify_unchanged(plan: ApplyPlan, chosen: Sequence[FileChange]) -> None:
    """StepError (sin escribir nada) si algo cambió desde la revisión."""
    changed: List[str] = []
    for change in chosen:
        parts, problem = split_safe_path(change.path)
        if problem:
            changed.append(change.path)
            continue
        target, _exists, problem = resolve_target(plan.folder, parts)
        if problem or target != change.target:
            changed.append(change.path)
            continue
        if _current_signature(change.target) != change.signature:
            changed.append(change.path)
    if changed:
        sample = ", ".join(f"«{p}»" for p in changed[:5])
        raise StepError(f"Cambió algo desde que revisaste los cambios "
                        f"({sample}). No he escrito nada: vuelve a "
                        "analizar el texto.")


def _copy_backup(plan: ApplyPlan, chosen: Sequence[FileChange],
                 backup_root: Path) -> str:
    """Copia exacta de los archivos que se van a modificar, fuera del
    proyecto. Devuelve la carpeta."""
    stamp = time.strftime("%Y-%m-%d-%H%M%S")
    base = backup_root / f"{plan.folder.name}-{stamp}"
    destination = base
    number = 2
    while destination.exists():
        destination = Path(f"{base}-{number}")
        number += 1
    destination.mkdir(parents=True)
    for change in chosen:
        if change.status != STATUS_MODIFIED:
            continue
        copy = destination.joinpath(*change.path.split("/"))
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(change.target), str(copy))
        if copy.read_bytes() != change.target.read_bytes():
            raise StepError(f"La copia de seguridad de «{change.path}» no "
                            "salió idéntica: no he tocado nada.")
    (destination / "LEEME.txt").write_text(
        f"Copia exacta de los archivos de «{plan.folder}» tal como estaban "
        f"antes de aplicar un TXT ({time.strftime('%Y-%m-%d %H:%M')}).\n"
        "Para deshacer, copia estos archivos de vuelta a su sitio.\n",
        encoding="utf-8")
    return str(destination)


def _make_parents(folder: Path, target: Path) -> None:
    """Crea las carpetas que falten hasta `target`, comprobando que
    ninguna de las que ya existen se ha convertido en un enlace."""
    relative = target.parent.relative_to(folder)
    current = folder
    for part in relative.parts:
        current = current / part
        try:
            info = os.lstat(str(current))
        except FileNotFoundError:
            os.mkdir(str(current))
            continue
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise StepError(f"«{current.name}» ya no es una carpeta normal.")


def apply_plan(plan: ApplyPlan, selected: Iterable[str], git=None,
               identity=None, backup_root: Optional[Path] = None,
               log: Optional[Callable[[str, str], None]] = None
               ) -> ApplyResult:
    """Escribe los cambios elegidos (rutas de `selected`).

    Primero comprueba que nada cambió desde la revisión y hace la copia
    exacta; si cualquiera de las dos cosas falla, no se escribe nada.
    Después escribe cada archivo de forma atómica (un temporal que
    sustituye al original de una vez, con sus permisos).
    """
    say = log or (lambda text, tag="dim": None)
    wanted = set(selected)
    chosen = [c for c in plan.changes if c.applicable and c.path in wanted]
    result = ApplyResult()
    if not chosen:
        return result
    _verify_unchanged(plan, chosen)

    modifies = [c for c in chosen if c.status == STATUS_MODIFIED]
    if modifies:
        if (plan.folder / ".git").exists():
            if git is None:
                from .git import Git
                git = Git()
            branch = git_backup(git, str(plan.folder),
                                reason="antes de aplicar un TXT",
                                identity=identity,
                                at_risk=[c.path for c in modifies])
            result.backup, result.backup_kind = branch, "rama"
            if branch:
                say(f"  Copia exacta previa en la rama «{branch}» (no se "
                    "publica; se recupera desde «Versiones»).", "ok")
        else:
            where = _copy_backup(plan, modifies, backup_root or BACKUP_ROOT)
            result.backup, result.backup_kind = where, "carpeta"
            say(f"  Copia exacta previa en «{where}».", "ok")
        _verify_unchanged(plan, chosen)

    for change in chosen:
        try:
            if change.status == STATUS_MODIFIED:
                if _current_signature(change.target) != change.signature:
                    raise StepError("cambió mientras se aplicaba")
                write_bytes(change.target, change.data)
                result.written.append(change.path)
            else:
                _make_parents(plan.folder, change.target)
                create_exclusive(change.target, change.data)
                result.created.append(change.path)
        except FileExistsError:
            result.failed.append((change.path, "apareció un archivo con ese "
                                               "nombre mientras tanto"))
        except StepError as e:
            result.failed.append((change.path, str(e)))
        except OSError as e:
            result.failed.append((change.path, e.strerror or str(e)))
    return result


def describe(change: FileChange) -> str:
    """Una línea para la consola o la terminal."""
    if change.status == STATUS_REJECTED:
        return f"✖ {change.path}: {change.reason}"
    if change.status == STATUS_SAME:
        return f"= {change.path}: sin cambios"
    sign = "+" if change.status == STATUS_NEW else "~"
    text = f"{sign} {change.path}  (+{change.added} −{change.removed})"
    if change.suspicious:
        text += "  ⚠ sospechoso"
    return text
