# -*- coding: utf-8 -*-
"""Identificadores de objetos de git calculados en Python, sin git.

Sirven para COMPROBAR, con una implementación independiente de la de
git, que lo que hay en el disco es exactamente lo que quedó guardado
en una copia de seguridad antes de borrarlo o pisarlo. Si git y este
módulo no coinciden, no se borra nada.

Un blob de git es `hash("blob <tamaño>\\0" + contenido)`; el algoritmo
es SHA-1 en los repositorios de siempre y SHA-256 en los nuevos
(`extensions.objectFormat=sha256`). Se deduce de la longitud del
identificador con el que se compara.
"""

import hashlib
import os
import stat

_TROZO = 1 << 20
NULL_SHA1 = "0" * 40
NULL_SHA256 = "0" * 64


def algorithm_for(oid):
    """'sha1' o 'sha256' según la longitud de un identificador."""
    return "sha256" if len(oid or "") == 64 else "sha1"


def null_oid(like):
    """El identificador «vacío» del mismo formato que `like`."""
    return NULL_SHA256 if len(like or "") == 64 else NULL_SHA1


def blob_id(data, algo="sha1"):
    """Identificador del blob con contenido `data` (bytes)."""
    h = hashlib.new(algo)
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


_FLAGS = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
          | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))


def file_blob_id(path, algo="sha1"):
    """Identificador del blob con el contenido EXACTO del archivo (sin
    filtros de final de línea), leyendo por trozos. Un enlace simbólico
    se mide como git lo guarda: el texto de su destino.

    Lanza OSError si no se puede leer; ValueError si no es un archivo
    normal ni un enlace (una FIFO, un dispositivo: abrirlos podría
    bloquear o leer sin fin).
    """
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        return blob_id(os.fsencode(os.readlink(path)), algo)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("no es un archivo normal")
    h = hashlib.new(algo)
    # Sin seguir enlaces y sin esperar: si entre el lstat y la apertura
    # alguien cambia el archivo por un enlace o una tubería, se falla en
    # vez de leer otra cosa o quedarse colgado.
    fd = os.open(path, _FLAGS)
    with os.fdopen(fd, "rb") as fh:
        real = os.fstat(fh.fileno())
        if not stat.S_ISREG(real.st_mode):
            raise ValueError("no es un archivo normal")
        h.update(b"blob %d\0" % real.st_size)
        leidos = 0
        while True:
            trozo = fh.read(_TROZO)
            if not trozo:
                break
            leidos += len(trozo)
            h.update(trozo)
    if leidos != real.st_size:
        # Cambió mientras se leía: el identificador no describiría
        # ningún contenido real.
        raise OSError("el archivo cambió mientras se leía")
    return h.hexdigest()
