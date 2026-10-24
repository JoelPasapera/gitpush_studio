# -*- coding: utf-8 -*-
"""Lectura de muchos archivos pequeños en paralelo, con memoria acotada.

Las lecturas se agrupan en LOTES acotados a la vez por número de
archivos y por bytes: la sobrecarga de coordinar hilos se reparte entre
decenas de archivos (era mayor que la propia lectura cuando son
pequeños), la memoria queda acotada aunque el proyecto sea enorme, y el
disco sigue trabajando mientras el consumidor procesa el lote anterior.
Lo que no hay que leer (demasiado grande, o binario por extensión) ni
siquiera se abre.
"""

import os
import stat
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from .fs import is_binary_ext

_FLAGS = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
          | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))


class Link(str):
    """Un enlace simbólico: su destino (texto), NUNCA su contenido.

    Seguirlo permitiría que un repositorio descargado trajera un enlace
    «notas.md → ~/.ssh/id_ed25519» y que el TXT (o la revisión) copiara
    una clave privada de tu equipo. Git tampoco sube el contenido: sube
    el texto del destino.
    """


class Special(str):
    """Algo que no es un archivo normal (tubería, dispositivo, socket):
    abrirlo podría bloquear para siempre o leer sin fin. El texto es el
    motivo."""


class Cut(bytes):
    """Contenido leído hasta el límite: el archivo creció después de
    listarlo. Se usa como bytes, pero quien escribe el TXT puede avisar
    de que está cortado."""


def read_one(path, limit):
    """Contenido de `path` (hasta `limit` bytes), o Link/Special/Cut, o la
    excepción de lectura. Nunca sigue enlaces ni abre tuberías."""
    try:
        info = os.lstat(path)
    except OSError as e:
        return e
    if stat.S_ISLNK(info.st_mode):
        try:
            return Link(os.readlink(path))
        except OSError as e:
            return e
    if not stat.S_ISREG(info.st_mode):
        return Special("no es un archivo normal")
    try:
        fd = os.open(path, _FLAGS)
    except OSError as e:
        return e
    try:
        # Entre el lstat y el open podría haberse cambiado por otra cosa:
        # se vuelve a comprobar sobre lo que de verdad se abrió.
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return Special("no es un archivo normal")
        fh = os.fdopen(fd, "rb")
        fd = None
        with fh:
            datos = fh.read(limit + 1)
    except OSError as e:
        return e
    finally:
        if fd is not None:
            os.close(fd)
    if len(datos) > limit:
        return Cut(datos[:limit])
    return datos


def read_batch(items, limit):
    """Lee un lote de (rel, ruta, tamaño) de una vez, en un hilo."""
    out = []
    for rel, path, size in items:
        try:
            out.append((rel, size, read_one(path, limit)))
        except Exception as e:                              # noqa: BLE001
            out.append((rel, size, e))
    return out


def iter_payloads(entries, max_file_bytes, workers=3, ahead=3,
                  batch_files=64, batch_bytes=4 << 20):
    """Entrega (rel, tamaño, contenido) en el orden de `entries`.

    `contenido` es bytes (`Cut` si se cortó en el límite), un `Link` o
    un `Special` (no se abrió: ver sus clases), la excepción de lectura,
    o None cuando el archivo se omitió sin abrir (demasiado grande o
    binario por extensión). `entries` son tuplas (rel, ruta_abs, tamaño).
    """
    def make_batches():
        batch, nbytes = [], 0
        for rel, path, size in entries:
            if size > max_file_bytes or is_binary_ext(rel):
                if batch:                 # el orden manda: primero el lote
                    yield ("read", batch)
                    batch, nbytes = [], 0
                yield ("skip", (rel, size))
                continue
            batch.append((rel, path, size))
            nbytes += size
            if len(batch) >= batch_files or nbytes >= batch_bytes:
                yield ("read", batch)
                batch, nbytes = [], 0
        if batch:
            yield ("read", batch)

    src = make_batches()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        pending = deque()

        def fill():
            while len(pending) < ahead:
                item = next(src, None)
                if item is None:
                    return
                kind, payload = item
                if kind == "skip":
                    pending.append(("skip", payload))
                else:
                    pending.append(("read", ex.submit(read_batch, payload,
                                                      max_file_bytes)))

        fill()
        while pending:
            kind, payload = pending.popleft()
            fill()
            if kind == "skip":
                rel, size = payload
                yield rel, size, None
            else:
                for rel, size, raw in payload.result():
                    yield rel, size, raw
