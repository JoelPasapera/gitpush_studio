# -*- coding: utf-8 -*-
"""Líneas de código, de comentario y en blanco, sin recorrer byte a byte.

Un clasificador ingenuo avanza carácter a carácter con una máquina de
estados en Python: correcto, pero lento en proyectos grandes. Aquí el
trabajo pesado lo hace el motor de expresiones regulares (en C):

1. Un único patrón por lenguaje busca el SIGUIENTE token interesante
   (apertura de comentario o de cadena). Python solo itera por token,
   nunca por byte, y las cadenas se saltan enteras para que un «//»
   dentro de "http://…" no cuente como comentario.
2. Cada comentario se ENMASCARA: sus bytes pasan a espacios con
   `bytes.translate`, salvo los saltos de línea, que se conservan para
   no alterar la numeración.
3. Se cuentan las líneas en blanco antes y después de enmascarar:

       en blanco           = blancas en el original
       solo comentario     = blancas tras enmascarar − blancas originales
       código              = total − blancas tras enmascarar

   Una línea que pasa a estar en blanco al quitar los comentarios es,
   exactamente, una línea que solo tenía comentario. Una línea con
   código y un comentario al final sigue teniendo código: cuenta como
   código, que es el criterio de cloc y tokei.

Una línea solo de espacios cuenta como «en blanco» aunque esté dentro de
un comentario de bloque.
"""

import re
import threading
from dataclasses import dataclass

from .languages import Language

UTF8_BOM = b"\xef\xbb\xbf"
UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")

_BLANK = re.compile(rb"^[ \t\r\f\v]*$", re.MULTILINE)
# Todo byte a espacio, salvo el salto de línea: el enmascarado no puede
# mover ni una línea de sitio.
_MASK = bytes(10 if i == 10 else 32 for i in range(256))
# Lo que puede ir delante de un docstring en su línea: sangría y, como
# mucho, el prefijo de la cadena (r, b, u, f y combinaciones).
_DOC_PREFIX = re.compile(rb"[ \t]*[rRbBuUfF]{0,2}")


@dataclass
class LineCounts:
    code: int = 0
    comment: int = 0
    blank: int = 0

    @property
    def total(self):
        return self.code + self.comment + self.blank

    def add(self, other):
        self.code += other.code
        self.comment += other.comment
        self.blank += other.blank
        return self


class _Scanner:
    """Las expresiones de un lenguaje, compiladas una sola vez."""

    __slots__ = ("master", "payload", "nested")

    def __init__(self, lang):
        # Orden de la alternancia: el motor elige la PRIMERA alternativa
        # que casa en una posición, no la más larga. Por eso van primero
        # los bloques («--[[» antes que «--»), luego las cadenas más
        # largas («\"\"\"» antes que «\"») y al final los de línea.
        tokens = []
        for start, end in sorted(lang.block, key=lambda t: -len(t[0])):
            tokens.append(("block", re.escape(start), (start, end)))
        for opener, body in sorted(lang.strings, key=lambda t: -len(t[0])):
            tipo = "doc" if opener in lang.docstrings else "string"
            tokens.append((tipo, re.escape(opener),
                           re.compile(body, re.MULTILINE)))
        for frag in lang.line:
            tokens.append(("line", frag, None))
        self.payload = {}
        partes = []
        for i, (kind, frag, extra) in enumerate(tokens):
            nombre = "t%d" % i
            partes.append(b"(?P<" + nombre.encode() + b">" + frag + b")")
            self.payload[nombre] = (kind, extra)
        self.master = re.compile(b"|".join(partes), re.MULTILINE) \
            if partes else None
        self.nested = {}
        if lang.nested:
            for start, end in lang.block:
                self.nested[start] = re.compile(
                    re.escape(start) + b"|" + re.escape(end))

    def _block_end(self, buf, after, start, end):
        """Posición justo detrás del cierre del bloque (o el final)."""
        rx = self.nested.get(start)
        if rx is None:
            i = buf.find(end, after)
            return len(buf) if i < 0 else i + len(end)
        profundidad, pos = 1, after
        while True:
            m = rx.search(buf, pos)
            if m is None:
                return len(buf)
            if m.group() == start:
                profundidad += 1
            else:
                profundidad -= 1
                if profundidad == 0:
                    return m.end()
            pos = m.end()

    def mask(self, buf):
        """Copia de `buf` con los comentarios en espacios, o None si no
        tiene ninguno (entonces no hace falta ni copiar)."""
        rx = self.master
        if rx is None:
            return None
        n = len(buf)
        out = None
        m = rx.search(buf)
        while m is not None:
            kind, extra = self.payload[m.lastgroup]
            s = m.start()
            if kind == "doc":
                # Docstring solo si abre la línea; si va detrás de
                # código («x = \"\"\"…\"\"\"») es un valor más.
                ini = buf.rfind(b"\n", 0, s) + 1
                if _DOC_PREFIX.fullmatch(buf, ini, s):
                    sm = extra.match(buf, s)
                    e = max(sm.end() if sm else 0, m.end())
                    if out is None:
                        out = bytearray(buf)
                    out[ini:e] = buf[ini:e].translate(_MASK)
                    pos = e
                    if pos >= n:
                        break
                    m = rx.search(buf, pos)
                    continue
                kind = "string"
            if kind == "string":
                sm = extra.match(buf, s)
                pos = max(sm.end() if sm else 0, m.end())
            else:
                if kind == "line":
                    e = buf.find(b"\n", m.end())
                    e = n if e < 0 else e
                else:
                    start, end = extra
                    e = self._block_end(buf, m.end(), start, end)
                if out is None:
                    out = bytearray(buf)
                out[s:e] = buf[s:e].translate(_MASK)
                pos = e
            if pos >= n:
                break
            m = rx.search(buf, pos)
        return out


_SCANNERS = {}
_SCANNERS_LOCK = threading.Lock()


def scanner_for(lang):
    """Escáner compilado del lenguaje (uno por lenguaje y proceso)."""
    sc = _SCANNERS.get(lang.name)
    if sc is None:
        with _SCANNERS_LOCK:
            sc = _SCANNERS.get(lang.name)
            if sc is None:
                sc = _Scanner(lang)
                _SCANNERS[lang.name] = sc
    return sc


def normalize_text(raw):
    """Bytes listos para contar: sin BOM y, si venían en UTF-16, en UTF-8.

    Un archivo UTF-16 está lleno de bytes nulos y pasaría por binario; es
    texto, y en Windows no es raro (lo produce PowerShell, por ejemplo).
    """
    if raw.startswith(UTF8_BOM):
        return raw[3:]
    if raw[:2] in UTF16_BOMS:
        try:
            return raw.decode("utf-16").encode("utf-8")
        except UnicodeDecodeError:
            return raw
    return raw


def is_utf16(raw):
    return raw[:2] in UTF16_BOMS


def count_lines(buf, lang):
    """`LineCounts` de un texto ya normalizado en el lenguaje `lang`."""
    if not buf:
        return LineCounts()
    cuerpo = buf[:-1] if buf.endswith(b"\n") else buf
    total = cuerpo.count(b"\n") + 1
    blancas = len(_BLANK.findall(cuerpo))
    if isinstance(lang, Language) and lang.has_comments:
        enmascarado = scanner_for(lang).mask(cuerpo)
        if enmascarado is not None:
            blancas_sin = len(_BLANK.findall(enmascarado))
            return LineCounts(total - blancas_sin, blancas_sin - blancas,
                              blancas)
    return LineCounts(total - blancas, 0, blancas)


def average_line_length(buf):
    """Bytes por línea: delata los archivos minificados o generados."""
    if not buf:
        return 0.0
    return len(buf) / (buf.count(b"\n") + 1)
