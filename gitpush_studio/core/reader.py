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

from collections import deque
from concurrent.futures import ThreadPoolExecutor

from .fs import is_binary_ext


def read_batch(items, limit):
    """Lee un lote de (rel, ruta, tamaño) de una vez, en un hilo."""
    out = []
    for rel, path, size in items:
        try:
            with open(path, "rb") as fh:
                out.append((rel, size, fh.read(limit)))
        except Exception as e:                              # noqa: BLE001
            out.append((rel, size, e))
    return out


def iter_payloads(entries, max_file_bytes, workers=3, ahead=3,
                  batch_files=64, batch_bytes=4 << 20):
    """Entrega (rel, tamaño, contenido) en el orden de `entries`.

    `contenido` es bytes, la excepción de lectura, o None cuando el
    archivo se omitió sin abrir (demasiado grande o binario por
    extensión). `entries` son tuplas (rel, ruta_abs, tamaño).
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
