# -*- coding: utf-8 -*-
"""¿Es texto? ¿En qué codificación? Sin librerías y sin leer de más.

Un archivo UTF-16 (lo que escribe `>` en PowerShell de Windows, muchos
.reg, algunos .csv exportados) está lleno de bytes nulos, y la regla
«un NUL delata un binario» lo daba por binario: la revisión de secretos
no lo leía y el TXT lo omitía, aunque git lo sube tal cual. Aquí se
reconoce por su marca (BOM) o, sin ella, por el patrón de nulos: en
UTF-16 casi todo el texto occidental tiene el byte alto a cero, así que
los nulos caen todos en las posiciones pares o todos en las impares.

Con `strict` (el TXT) se exige además que lo que sale al leerlo como
UTF-16 parezca texto: un binario con valores de 16 bits pequeños (un
array de números, audio casi en silencio) tiene el mismo patrón de nulos
y, sin esta comprobación, se volcaba como una ristra de caracteres de
control. La revisión de secretos no la usa: ante la duda, prefiere leer.
"""

import codecs
import locale
import os
import re

UTF8_BOM = b"\xef\xbb\xbf"
_ANSI = []                 # la codificación ANSI de este equipo, una vez
_PROBE = 4096
# Caracteres que en un texto de verdad casi no aparecen: controles (salvo
# tabulador, saltos de línea, avance de página y ESC) y «�».
_RAROS = re.compile("[\x00-\x08\x0e-\x1a\x1c-\x1f\x7f\ufffd]")


def _parece_texto(muestra, codificacion):
    """¿`muestra` (bytes, longitud par) leída en `codificacion` parece
    texto? Como mucho un 1 % de caracteres raros."""
    texto = muestra.decode(codificacion, "replace")
    return len(_RAROS.findall(texto)) * 100 <= len(texto)


def detect(raw, strict=False):
    """(codificación, bytes de BOM) del comienzo `raw`.

    La codificación es "utf-8", "utf-16-le", "utf-16-be", o None si
    parece binario. Con UTF-8 sin BOM no se valida nada: un texto con
    algún byte suelto inválido sigue siendo texto (se leerá con «�»).
    Solo se miran los primeros 4 KB: da igual pasar el archivo entero o
    solo su principio.
    """
    if raw.startswith(UTF8_BOM):
        return "utf-8", 3
    if raw[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"):
        return None, 0                               # UTF-32: no merece
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        codificacion = "utf-16-le" if raw[:1] == b"\xff" else "utf-16-be"
        cuerpo = raw[2:_PROBE]
        cuerpo = cuerpo[:len(cuerpo) - len(cuerpo) % 2]
        if strict and not _parece_texto(cuerpo, codificacion):
            return None, 0
        return codificacion, 2
    muestra = raw[:_PROBE]
    if b"\x00" not in muestra:
        return "utf-8", 0
    n = len(muestra) - (len(muestra) % 2)
    if n < 16:
        return None, 0
    pares = muestra[0:n:2]
    impares = muestra[1:n:2]
    mitad = n // 2
    nulos_pares = pares.count(0)
    nulos_impares = impares.count(0)
    if nulos_impares >= 0.9 * mitad and nulos_pares <= 0.1 * mitad:
        codificacion = "utf-16-le"
    elif nulos_pares >= 0.9 * mitad and nulos_impares <= 0.1 * mitad:
        codificacion = "utf-16-be"
    else:
        return None, 0
    if strict and not _parece_texto(muestra[:n], codificacion):
        return None, 0
    return codificacion, 0


def to_utf8(raw):
    """El texto de `raw` en UTF-8 (sin BOM), o None si es binario."""
    codificacion, bom = detect(raw)
    if codificacion is None:
        return None
    if codificacion == "utf-8":
        return raw[bom:] if bom else raw
    cuerpo = raw[bom:]
    if len(cuerpo) % 2:
        cuerpo = cuerpo[:-1]                         # cortado a medias
    return cuerpo.decode(codificacion, "replace").encode("utf-8")


def legacy_encoding():
    """La codificación «ANSI» de este equipo: la de los .txt de toda la
    vida, el Bloc de notas antiguo o los CSV que exporta Excel.

    En Windows es la del sistema (cp1252 en español o inglés, cp1251 en
    ruso, cp932 en japonés…). En los demás sistemas, y si Windows ya usa
    UTF-8, cp1252: la de los textos occidentales antiguos.
    """
    if not _ANSI:
        _ANSI.append(_ansi_del_sistema())
    return _ANSI[0]


def _ansi_del_sistema():
    if os.name == "nt":
        pedir = getattr(locale, "getencoding", None)       # 3.11+
        try:
            nombre = pedir() if pedir else locale.getpreferredencoding(False)
            real = codecs.lookup(nombre).name
        except (LookupError, ValueError, TypeError):
            real = ""
        if real and real not in ("utf-8", "ascii"):
            return real
    return "cp1252"
