# -*- coding: utf-8 -*-
"""Dibuja el icono de GitPush Studio desde cero (sin librerías de imagen).

    python tools/crear_icono.py

Escribe `gitpush_studio/assets/gitpush.png` (256×256) y
`gitpush_studio/assets/gitpush.ico` (16, 24, 32, 48, 64 y 256 píxeles),
que usan la ventana, el icono de la bandeja y el .exe de Windows.

El dibujo es una flecha hacia arriba (subir a GitHub) en un cuadrado de
esquinas redondeadas con los colores de la app. Cada píxel se calcula
con 4×4 muestras (suavizado de bordes) y se guarda como PNG escrito a
mano: cabecera IHDR, datos IDAT comprimidos con zlib y fin IEND. El .ico
lleva dentro esos PNG (Windows lo admite desde Vista).
"""

import struct
import sys
import zlib
from pathlib import Path
from typing import List, Sequence, Tuple

RAIZ = Path(__file__).resolve().parent.parent
DESTINO = RAIZ / "gitpush_studio" / "assets"
SIZES = (16, 24, 32, 48, 64, 256)
SAMPLES = 4                        # muestras por lado de cada píxel

Color = Tuple[int, int, int]
BACKGROUND: Color = (0x16, 0x1B, 0x22)    # PANEL del tema
BORDER: Color = (0x30, 0x36, 0x3D)
ARROW: Color = (0x2E, 0xA0, 0x43)         # GREEN_H del tema

Point = Tuple[float, float]
ARROW_HEAD: Sequence[Point] = ((0.50, 0.15), (0.83, 0.50), (0.17, 0.50))
SHAFT = (0.37, 0.47, 0.63, 0.85)          # x0, y0, x1, y1
MARGIN = 0.04
RADIUS = 0.22
BORDER_WIDTH = 0.035


def inside_rounded_square(x: float, y: float, inset: float = 0.0) -> bool:
    low = MARGIN + inset
    high = 1.0 - MARGIN - inset
    radius = max(0.0, RADIUS - inset)
    if x < low or x > high or y < low or y > high:
        return False
    corner_x = min(max(x, low + radius), high - radius)
    corner_y = min(max(y, low + radius), high - radius)
    dx = x - corner_x
    dy = y - corner_y
    return dx * dx + dy * dy <= radius * radius


def _edge(a: Point, b: Point, p: Point) -> float:
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def inside_arrow(x: float, y: float) -> bool:
    a, b, c = ARROW_HEAD
    point = (x, y)
    signs = (_edge(a, b, point), _edge(b, c, point), _edge(c, a, point))
    in_head = all(s >= 0 for s in signs) or all(s <= 0 for s in signs)
    x0, y0, x1, y1 = SHAFT
    in_shaft = x0 <= x <= x1 and y0 <= y <= y1
    return in_head or in_shaft


def render(size: int) -> bytes:
    """Los píxeles RGBA (fila a fila) del icono de `size`×`size`."""
    pixels = bytearray()
    step = 1.0 / (size * SAMPLES)
    for row in range(size):
        for column in range(size):
            red = green = blue = alpha = 0.0
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    x = (column * SAMPLES + sx + 0.5) * step
                    y = (row * SAMPLES + sy + 0.5) * step
                    if not inside_rounded_square(x, y):
                        continue
                    if inside_arrow(x, y):
                        color = ARROW
                    elif inside_rounded_square(x, y, BORDER_WIDTH):
                        color = BACKGROUND
                    else:
                        color = BORDER
                    red += color[0]
                    green += color[1]
                    blue += color[2]
                    alpha += 255
            count = SAMPLES * SAMPLES
            covered = alpha / 255
            if covered:
                pixels += bytes((int(red / covered + 0.5),
                                 int(green / covered + 0.5),
                                 int(blue / covered + 0.5),
                                 int(alpha / count + 0.5)))
            else:
                pixels += b"\0\0\0\0"
    return bytes(pixels)


def _chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return (struct.pack(">I", len(data)) + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))


def png(size: int, rgba: bytes) -> bytes:
    """Un PNG RGBA de 8 bits por canal, sin filtros (tipo 0 por fila)."""
    stride = size * 4
    raw = bytearray()
    for row in range(size):
        raw.append(0)
        raw += rgba[row * stride:(row + 1) * stride]
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b""))


def ico(images: List[Tuple[int, bytes]]) -> bytes:
    """Un .ico con varias imágenes PNG dentro."""
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = b""
    offset = 6 + 16 * len(images)
    blobs = b""
    for size, data in images:
        side = 0 if size >= 256 else size       # 0 significa 256
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32,
                               len(data), offset)
        offset += len(data)
        blobs += data
    return header + entries + blobs


def main() -> int:
    DESTINO.mkdir(parents=True, exist_ok=True)
    images = [(size, png(size, render(size))) for size in SIZES]
    (DESTINO / "gitpush.ico").write_bytes(ico(images))
    (DESTINO / "gitpush.png").write_bytes(dict(images)[256])
    print(f"✔ {DESTINO / 'gitpush.ico'} y gitpush.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
