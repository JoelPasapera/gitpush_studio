# -*- coding: utf-8 -*-
"""Escrituras que no dejan archivos a medias.

`open(ruta, "w")` vacía el archivo ANTES de escribir: si el programa se
cierra, se va la luz o el disco se llena a mitad, queda un archivo
truncado en lugar del bueno. Aquí se escribe en un temporal junto al
destino, se fuerza a disco y se cambia por el original de una vez
(`os.replace`, atómico en Windows, macOS y Linux): quien lea el archivo
ve la versión vieja o la nueva entera, nunca un trozo.

En Windows un antivirus o el indexador pueden tener abierto el destino
un instante y el cambio falla con «acceso denegado»: se reintenta unas
veces y, si sigue sin poder, se copia el contenido encima (ya no es
atómico, pero tampoco se pierde la escritura).
"""

import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

_REINTENTOS = 6


def _replace(tmp, destino):
    for intento in range(_REINTENTOS):
        try:
            os.replace(tmp, destino)
            return
        except PermissionError:
            if intento == _REINTENTOS - 1:
                break
            time.sleep(0.05 * (intento + 1))
    # Último recurso: copiar encima y quitar el temporal. Nunca a través
    # de un enlace: copiar ahí escribiría donde apunte.
    if os.path.islink(destino):
        raise PermissionError(f"no puedo reemplazar «{destino}»")
    shutil.copyfile(tmp, destino)
    try:
        os.unlink(tmp)
    except OSError:
        pass


def temp_prefix(name):
    """Cómo empiezan los temporales de `name` (para reconocerlos)."""
    return "." + name + "."


TEMP_SUFFIX = ".tmp"


def clean_stale(path, older_than=3600):
    """Borra temporales olvidados de `path` (de un cierre brusco a mitad
    de escritura) con más de `older_than` segundos. Solo los nuestros:
    mismo prefijo y sufijo, archivos normales, en la misma carpeta."""
    destino = Path(path)
    prefijo = temp_prefix(destino.name)
    limite = time.time() - older_than
    try:
        candidatos = list(os.scandir(str(destino.parent)))
    except OSError:
        return 0
    borrados = 0
    for e in candidatos:
        if not (e.name.startswith(prefijo) and e.name.endswith(TEMP_SUFFIX)):
            continue
        try:
            if e.is_file(follow_symlinks=False) and \
                    e.stat(follow_symlinks=False).st_mtime < limite:
                os.unlink(e.path)
                borrados += 1
        except OSError:
            pass
    return borrados


@contextmanager
def atomic_open(path, fsync=True):
    """`with atomic_open(ruta) as fh:` → archivo binario temporal.

    Al salir sin error, el temporal sustituye a `path`. Si hay una
    excepción, el temporal se borra y `path` queda como estaba.
    """
    destino = Path(path)
    destino.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=temp_prefix(destino.name),
                               suffix=TEMP_SUFFIX, dir=str(destino.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            yield fh
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())
        # `mkstemp` crea el temporal solo para su dueño (0600). Si el
        # destino ya existía, se le respetan sus permisos; uno nuevo se
        # queda privado, que para un TXT o un informe es lo prudente.
        try:
            os.chmod(tmp, os.stat(str(destino)).st_mode & 0o7777)
        except OSError:
            pass
        _replace(tmp, str(destino))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_bytes(path, data, fsync=True):
    with atomic_open(path, fsync=fsync) as fh:
        fh.write(data)


def write_text(path, text, encoding="utf-8", fsync=True):
    write_bytes(path, text.encode(encoding), fsync=fsync)


def create_exclusive(path, data):
    """Crea `path` con `data` SOLO si no existe, sin quedar nunca a
    medias: se escribe un temporal y se enlaza con `os.link`, que falla
    si el destino ya existe (no se pisa nada creado entre medias). Donde
    el sistema no admite enlaces se usa «x» (creación exclusiva).

    Lanza FileExistsError si ya existía.
    """
    destino = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=temp_prefix(destino.name),
                               suffix=TEMP_SUFFIX, dir=str(destino.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if os.name != "nt":
            # `mkstemp` lo crea privado (0600); un archivo del proyecto
            # se queda legible como cualquier otro.
            os.chmod(tmp, 0o644)
        try:
            os.link(tmp, str(destino))
            return
        except FileExistsError:
            raise
        except OSError:
            pass                        # sin enlaces duros (FAT, red…)
        with open(str(destino), "xb") as fh:
            fh.write(data)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
