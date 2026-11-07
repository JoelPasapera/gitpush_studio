# -*- coding: utf-8 -*-
"""Tachar secretos en el TXT (y recuperarlos al aplicar cambios de vuelta).

El TXT del proyecto se le pasa a una IA o a otra persona. Si el proyecto
tiene una contraseña, un token o una llave dentro, el TXT la lleva a la
vista. Con «Tachar secretos», cada valor que la revisión de seguridad
reconoce como secreto se sustituye por «••••••••»:

    API_TOKEN=ghp_abc…xyz        →   API_TOKEN=ghp_••••••••
    postgres://ana:clave@host    →   postgres://ana:••••••••@host
    -----BEGIN PRIVATE KEY-----  →   -----BEGIN PRIVATE KEY-----
    MIIEvQIBADANBgkqhki…              ••••••••
    -----END PRIVATE KEY-----         -----END PRIVATE KEY-----

Se usan las mismas reglas que la revisión de seguridad (`secret_rules`):
lo que esa revisión daría por secreto de verdad (confianza alta o media)
se tacha; lo que solo parece un ejemplo («changeme», «xxxx») se deja.
En los archivos que por su NOMBRE son de claves (.env, credentials.json,
secrets.yml…) se tachan además TODOS los valores de las líneas
«CLAVE=valor» o «clave: valor», y si no se reconoce ninguna línea así,
el contenido entero.

Al revés (`unredact_lines`): cuando una IA devuelve el archivo con los
«••••••••» que vio, aplicarlo tal cual escribiría los puntos en lugar de
la clave real. Esas líneas se emparejan con las del original (tachadas
igual) y se recupera el valor real; si una línea con «••••••••» no
corresponde a ninguna del original, el archivo no se aplica.
"""

import re
from typing import Callable, List, Optional, Sequence, Tuple

from . import diff
from .secret_rules import (ALL_ANCHORS, BAJA, CONTENT_RULES, name_rule)

MASK = "••••••••"
MASK_BYTES = MASK.encode("utf-8")
# Lo que delata un valor tachado en una línea devuelta por una IA (que a
# veces acorta o alarga la ristra de puntos).
MASK_SIGN = "••••"

_PRIVATE_KEY_LABEL = "llave privada dentro"
_KEY_END = b"-----END"
# Una llave privada que empieza en los últimos bytes de un lote se pasa
# entera al lote siguiente (una llave RSA de 4096 bits ocupa unos 3 KB).
_KEY_LOOKBACK = 8192

# «CLAVE=valor», «export CLAVE=valor», «clave: valor», «"clave": "valor"».
_ASSIGNMENT_LINE = re.compile(
    rb"^(?P<lead>[ \t]*(?:export[ \t]+)?[\"']?[A-Za-z_][\w.\-]*[\"']?"
    rb"[ \t]*(?:=|:)[ \t]*)(?P<value>[^\r\n]*?)(?P<trail>[ \t]*,?[ \t]*\r?)$",
    re.MULTILINE)

Span = Tuple[int, int]


def _secret_values(data: bytes, rel_lower: str) -> List[Span]:
    """Tramos (inicio, fin) de los valores que las reglas de contenido
    dan por secretos de verdad."""
    low = data.lower()
    present = {anchor for anchor in ALL_ANCHORS if anchor in low}
    if not present:
        return []
    spans: List[Span] = []
    for rule in CONTENT_RULES:
        if rule.anchors and present.isdisjoint(rule.anchors):
            continue
        for value, quoted, position in rule.candidates(data, low):
            confidence = rule.judge(value, rule.confidence, rel_lower,
                                    quoted)
            if confidence is None or confidence == BAJA or not value:
                continue
            start = data.find(value, position)
            if start < 0:
                continue
            end = start + len(value)
            if rule.label == _PRIVATE_KEY_LABEL:
                start, end = _private_key_body(data, start)
            spans.append((start, end))
    return spans


def _private_key_body(data: bytes, start: int) -> Span:
    """El cuerpo de una llave privada: desde la línea que sigue a BEGIN
    hasta justo antes de la línea END (o el final, si no la hay)."""
    if data[start:start + 2] == b"\r\n":
        start += 2
    elif data[start:start + 1] == b"\n":
        start += 1
    end = data.find(_KEY_END, start)
    if end < 0:
        return start, len(data)
    # Sin el salto de línea que precede a «-----END».
    while end > start and data[end - 1:end] in (b"\n", b"\r"):
        end -= 1
    return start, end


# Valores que dan forma al archivo (abren un bloque de JSON o de YAML)
# en lugar de ser un dato: tacharlos rompería su estructura sin ocultar
# nada.
_STRUCTURAL = frozenset((b"", b"{", b"[", b"{}", b"[]", b"|", b">", b"|-",
                         b">-", b"|+", b">+"))


def _assignment_values(data: bytes) -> List[Span]:
    """Los valores de todas las líneas «clave=valor» / «clave: valor»."""
    spans: List[Span] = []
    for match in _ASSIGNMENT_LINE.finditer(data):
        start, end = match.span("value")
        value = data[start:end]
        if value.strip() in _STRUCTURAL:
            continue
        # Comillas alrededor del valor: se tacha lo de dentro.
        if len(value) >= 2 and value[:1] in (b'"', b"'") \
                and value[-1:] == value[:1]:
            start += 1
            end -= 1
        if end > start:
            spans.append((start, end))
    return spans


def _merge(spans: Sequence[Span]) -> List[Span]:
    merged: List[Span] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def secret_spans(data: bytes, rel: str) -> Tuple[List[Span], bool]:
    """(tramos a tachar, ¿se tacha el archivo entero?) del contenido
    `data` (UTF-8) del archivo `rel`."""
    rel_lower = rel.lower()
    spans = _secret_values(data, rel_lower)
    by_name = name_rule(rel)
    if by_name is not None and by_name[1] != BAJA:
        assignments = _assignment_values(data)
        if not assignments and data.strip():
            return [], True
        spans += assignments
    return _merge(spans), False


def whole_file_note(rel: str) -> bytes:
    """Lo que sustituye a un archivo de claves que no se puede tachar
    valor a valor."""
    by_name = name_rule(rel)
    reason = by_name[0] if by_name else "archivo de claves"
    return (f"<SECRETO TACHADO: {reason}; su contenido no se incluye>\n"
            ).encode("utf-8")


def redact_bytes(data: bytes, rel: str) -> Tuple[bytes, int]:
    """(contenido con los secretos tachados, cuántos se tacharon)."""
    spans, whole = secret_spans(data, rel)
    if whole:
        return whole_file_note(rel), 1
    if not spans:
        return data, 0
    return _apply_spans(data, spans), len(spans)


class Redactor:
    """Tacha secretos en un contenido que llega por trozos (un archivo
    enorme que se copia por bloques).

    Se trabaja por lotes de LÍNEAS completas: ningún secreto de una sola
    línea queda partido entre dos lotes (una línea solo se parte si mide
    más de `max_line`). El único secreto que ocupa varias líneas, la
    llave privada, se sigue de un lote al siguiente hasta su «-----END».

    Un archivo que por su nombre es de claves (.env, secrets.yml…) y es
    tan grande que llega por trozos no se tacha valor a valor: se omite
    entero, que es lo prudente.
    """

    def __init__(self, write: Callable[[bytes], object], rel: str,
                 batch: int = 1 << 20, max_line: int = 8 << 20) -> None:
        self._write = write
        self._rel = rel
        self._batch = batch
        self._max_line = max(max_line, batch)
        self._pending = bytearray()
        self._inside_key = False
        by_name = name_rule(rel)
        self._whole = by_name is not None and by_name[1] != BAJA
        self._noted = False
        self.count = 0

    def feed(self, data: bytes) -> None:
        if self._whole:
            self._note_once(data)
            return
        self._pending += data
        if len(self._pending) < self._batch:
            return
        cut = self._safe_cut()
        if cut == 0:
            if len(self._pending) < self._max_line:
                return                     # se espera a tener más
            cut = len(self._pending)       # una línea descomunal
        chunk = bytes(self._pending[:cut])
        del self._pending[:cut]
        self._flush(chunk)

    def _safe_cut(self) -> int:
        """Hasta dónde se puede tratar ya: hasta el último salto de línea,
        pero sin partir una llave privada que empieza cerca del final (se
        deja entera para el lote siguiente, con su cabecera)."""
        cut = self._pending.rfind(b"\n") + 1
        if cut == 0:
            return 0
        window_start = max(0, cut - _KEY_LOOKBACK)
        begin = self._pending.rfind(b"-----BEGIN", window_start, cut)
        if begin < 0:
            return cut
        header = bytes(self._pending[begin:begin + 80])
        if b"PRIVATE KEY" not in header:
            return cut
        if self._pending.find(_KEY_END, begin, cut) >= 0:
            return cut
        return self._pending.rfind(b"\n", 0, begin) + 1

    def close(self) -> None:
        if self._pending:
            chunk = bytes(self._pending)
            self._pending.clear()
            self._flush(chunk)

    def _note_once(self, data: bytes) -> None:
        if self._noted or not data.strip():
            return
        self._noted = True
        self.count += 1
        self._write(whole_file_note(self._rel))

    def _flush(self, chunk: bytes) -> None:
        if self._inside_key:
            end = chunk.find(_KEY_END)
            if end < 0:
                return                     # todo el lote es cuerpo de llave
            # El «••••••••» ya salió en el lote anterior: solo falta el
            # salto de línea antes de la línea «-----END».
            self._inside_key = False
            self._write(b"\n")
            chunk = chunk[end:]
        spans, _whole = secret_spans(chunk, self._rel)
        if _key_left_open(chunk, spans):
            self._inside_key = True
        self.count += len(spans)
        self._write(_apply_spans(chunk, spans))


def _key_left_open(chunk: bytes, spans: Sequence[Span]) -> bool:
    """¿Termina el lote dentro de una llave privada (tachada hasta el
    final del lote y sin su «-----END»)?"""
    last_begin = chunk.rfind(b"-----BEGIN")
    if last_begin < 0 or b"PRIVATE KEY" not in chunk[last_begin:last_begin
                                                       + 80]:
        return False
    if chunk.find(_KEY_END, last_begin) >= 0:
        return False
    for _start, end in spans:
        if end == len(chunk):
            return True
    return False


def _apply_spans(data: bytes, spans: Sequence[Span]) -> bytes:
    pieces: List[bytes] = []
    cursor = 0
    for start, end in spans:
        pieces.append(data[cursor:start])
        pieces.append(MASK_BYTES)
        cursor = end
    pieces.append(data[cursor:])
    return b"".join(pieces)


# ------------------------------------------------------------------ #
#  Al aplicar cambios de vuelta                                        #
# ------------------------------------------------------------------ #
def has_mask(line: str) -> bool:
    return MASK_SIGN in line


def _redacted_lines(original_lines: Sequence[str], rel: str) -> List[str]:
    """Las líneas del original tal como quedaron tachadas en el TXT."""
    text = "\n".join(original_lines) + "\n"
    redacted, _count = redact_bytes(text.encode("utf-8"), rel)
    lines = redacted.decode("utf-8", "replace").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _line_mapping(redacted: Sequence[str],
                  original_lines: Sequence[str]) -> List[List[str]]:
    """Para cada línea tachada, las líneas originales que representa.

    Una línea con un valor tachado corresponde a una línea original; una
    llave privada tachada («••••••••» en una sola línea) corresponde a
    todas las líneas de su cuerpo. Lo que no se puede emparejar con
    seguridad queda vacío (y no se podrá recuperar).
    """
    mapping: List[List[str]] = []
    for code in diff.opcodes(list(redacted), list(original_lines)):
        a_length = code.a_end - code.a_start
        b_length = code.b_end - code.b_start
        if code.tag == diff.EQUAL or (code.tag == diff.REPLACE
                                      and a_length == b_length):
            for index in range(a_length):
                mapping.append([original_lines[code.b_start + index]])
        elif code.tag == diff.REPLACE and a_length == 1:
            mapping.append(list(original_lines[code.b_start:code.b_end]))
        else:
            for _index in range(a_length):
                mapping.append([])
    return mapping


def unredact_lines(new_lines: Sequence[str],
                   original_lines: Optional[Sequence[str]],
                   rel: str = "") -> Tuple[List[str], str]:
    """Las líneas devueltas con los valores reales en lugar de
    «••••••••», o ([], motivo) si alguno no se puede recuperar.

    Las líneas sin «••••» se dejan como están. Las que lo llevan se
    emparejan, por posición, con las del original tachado: si casan, se
    devuelve la línea (o líneas) originales; si no, no hay de dónde
    sacar el valor real y se rechaza el archivo entero.
    """
    if not any(has_mask(line) for line in new_lines):
        return list(new_lines), ""
    if original_lines is None:
        return [], ("lleva secretos tachados («••••••••») y no hay archivo "
                    "original del que recuperarlos")
    redacted = _redacted_lines(original_lines, rel)
    mapping = _line_mapping(redacted, original_lines)
    keys_new = [line.rstrip(" \t\r") for line in new_lines]
    keys_redacted = [line.rstrip(" \t\r") for line in redacted]
    out: List[str] = []
    for code in diff.opcodes(keys_redacted, keys_new):
        if code.tag == diff.EQUAL:
            for index in range(code.a_end - code.a_start):
                redacted_index = code.a_start + index
                new_line = new_lines[code.b_start + index]
                if has_mask(new_line):
                    originals = mapping[redacted_index]
                    if not originals:
                        return [], _cannot_restore(code.b_start + index)
                    out.extend(originals)
                else:
                    out.append(new_line)
            continue
        for index in range(code.b_start, code.b_end):
            if has_mask(new_lines[index]):
                return [], _cannot_restore(index)
            out.append(new_lines[index])
    return out, ""


def _cannot_restore(index: int) -> str:
    return (f"la línea {index + 1} lleva un secreto tachado («••••••••») "
            "que no corresponde a ninguna línea del original: aplicarla "
            "escribiría los puntos en lugar del valor real")
