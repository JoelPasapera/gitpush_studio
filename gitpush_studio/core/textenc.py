# -*- coding: utf-8 -*-
"""¿Es texto? ¿En qué codificación? Sin librerías y sin leer de más.

Un archivo UTF-16 (lo que escribe `>` en PowerShell de Windows, muchos
.reg, algunos .csv exportados) está lleno de bytes nulos, y la regla
«un NUL delata un binario» lo daba por binario: la revisión de secretos
no lo leía y el TXT lo omitía, aunque git lo sube tal cual. Aquí se
reconoce por su marca (BOM) o, sin ella, por el patrón de nulos: en
UTF-16 casi todo el texto occidental tiene el byte alto a cero, así que
los nulos caen todos en las posiciones pares o todos en las impares.
"""

UTF8_BOM = b"\xef\xbb\xbf"
_PROBE = 4096


def detect(raw):
    """(codificación, bytes de BOM) del comienzo `raw`.

    La codificación es "utf-8", "utf-16-le", "utf-16-be", o None si
    parece binario. Con UTF-8 sin BOM no se valida nada: un texto con
    algún byte suelto inválido sigue siendo texto (se leerá con «�»).
    """
    if raw.startswith(UTF8_BOM):
        return "utf-8", 3
    if raw[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"):
        return None, 0                               # UTF-32: no merece
    if raw[:2] == b"\xff\xfe":
        return "utf-16-le", 2
    if raw[:2] == b"\xfe\xff":
        return "utf-16-be", 2
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
        return "utf-16-le", 0
    if nulos_pares >= 0.9 * mitad and nulos_impares <= 0.1 * mitad:
        return "utf-16-be", 0
    return None, 0


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
