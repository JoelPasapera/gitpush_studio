# -*- coding: utf-8 -*-
"""Lectura de los archivos del proyecto: segura, en paralelo y con la
memoria acotada.

Dos maneras de pedirlos:

  · `iter_payloads(entries, límite)`: el contenido entero de cada archivo
    de hasta `límite` bytes (la revisión de secretos y las analíticas).
    Los mayores y los binarios por extensión ni se abren.
  · `iter_contents(entries)`: para el TXT, SIN límite de tamaño. Los
    archivos pequeños llegan enteros, leídos en paralelo; los grandes
    llegan como `LARGE`, para que quien escribe los copie por bloques
    (`open_regular`) sin cargarlos nunca enteros en memoria.

Las lecturas se agrupan en LOTES acotados por número de archivos y por
bytes, y hay un tope de lotes y de bytes en vuelo: la sobrecarga de
coordinar hilos se reparte entre decenas de archivos, la memoria no
depende del tamaño del proyecto, y el disco sigue trabajando mientras se
procesa lo anterior. Cuántos hilos leen a la vez se decide MIDIENDO lo
que cuesta abrir cada archivo (ver `Pace`): uno solo si el sistema los
sirve de su caché al momento, muchos si cada apertura es una espera (el
antivirus de Windows revisando archivo por archivo).

Cada archivo se lee con UNA lectura del tamaño exacto que tiene al
abrirlo, más un byte para saber si creció. Pedir «hasta el límite»
reservaba el límite entero para cada archivo (2 MB para uno de 200
bytes) y era lo que más costaba: cinco veces más que leerlo.

Nunca se sigue un enlace (se entrega su destino como texto) ni se abre
algo que no sea un archivo normal: una tubería dejaría la lectura
esperando para siempre.
"""

import math
import os
import stat
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from .fs import is_binary_ext

_FLAGS = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
          | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
# Para copiar por bloques: en Windows, avisar de que la lectura será
# secuencial agranda la lectura anticipada de la caché del sistema.
SEQUENTIAL = _FLAGS | getattr(os, "O_SEQUENTIAL", 0)

SMALL = 8 << 20            # hasta aquí, enteros en memoria y en paralelo
BATCH_FILES = 64           # archivos por lote
BATCH_BYTES = 4 << 20      # bytes por lote
WINDOW_BYTES = 64 << 20    # bytes leídos por adelantado, como mucho


def default_workers():
    """Hilos de lectura como MÁXIMO (cuántos se usan lo decide `Pace`
    midiendo). No depende de los núcleos: cuando hacen falta muchos es
    porque cada apertura es una espera (el antivirus, una unidad de red),
    no cálculo."""
    return 16


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
    """Contenido incompleto a propósito: el archivo creció después de
    abrirlo (o de listarlo, si se pidió con límite). Se usa como bytes,
    pero quien escribe el TXT puede avisar de que está cortado."""


class _Large(object):
    """Centinela de `iter_contents`: archivo grande, sin leer. Lo copia
    por bloques quien consume."""
    __slots__ = ()

    def __repr__(self):
        return "LARGE"


LARGE = _Large()


# ------------------------------------------------------------------ #
#  Un archivo                                                         #
# ------------------------------------------------------------------ #
def open_regular(path, flags=_FLAGS):
    """Abre `path` SOLO si es un archivo normal.

    Devuelve (descriptor, tamaño al abrirlo) —quien lo recibe lo cierra—
    o, sin abrir nada, el motivo: un `Link` con el destino del enlace, un
    `Special`, o la excepción del sistema.
    """
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
        fd = os.open(path, flags)
    except OSError as e:
        return e
    try:
        # Entre el lstat y el open podría haberse cambiado por otra cosa:
        # se vuelve a comprobar sobre lo que de verdad se abrió.
        actual = os.fstat(fd)
    except OSError as e:
        os.close(fd)
        return e
    if not stat.S_ISREG(actual.st_mode):
        os.close(fd)
        return Special("no es un archivo normal")
    return fd, actual.st_size


def read_upto(fd, n):
    """El contenido del archivo abierto en `fd`, que medía `n` bytes al
    abrirlo, y como mucho un byte más: si llega ese byte, es que creció.

    Lo normal es UNA lectura: un archivo normal solo devuelve menos de lo
    pedido cuando se acaba. Si se encogió (o el sistema de archivos
    devuelve lecturas cortas) se sigue leyendo hasta el final.
    """
    quiero = n + 1
    datos = os.read(fd, quiero)
    if len(datos) >= n:
        return datos
    partes, total = [datos], len(datos)
    while datos and total < quiero:
        datos = os.read(fd, quiero - total)
        partes.append(datos)
        total += len(datos)
    return b"".join(partes)


def _read_more(fd, cuanto):
    """Hasta `cuanto` bytes más (o hasta el final), por bloques."""
    partes = []
    while cuanto > 0:
        datos = os.read(fd, min(cuanto, 1 << 20))
        if not datos:
            break
        partes.append(datos)
        cuanto -= len(datos)
    return b"".join(partes)


def read_one(path, limit):
    """Contenido de `path` (hasta `limit` bytes; `Cut` si tiene más), o
    Link/Special, o la excepción de lectura. Nunca sigue enlaces ni abre
    tuberías."""
    abierto = open_regular(path)
    if not isinstance(abierto, tuple):
        return abierto
    fd, n = abierto
    try:
        datos = read_upto(fd, min(n, limit))
        if n < len(datos) <= limit:
            # Creció desde que se abrió: como siempre, todo hasta el límite.
            datos += _read_more(fd, limit + 1 - len(datos))
    except OSError as e:
        return e
    finally:
        os.close(fd)
    if len(datos) > limit:
        return Cut(datos[:limit])
    return datos


def read_whole(path, small=SMALL):
    """Para el TXT: el contenido de `path` tal como era al abrirlo.

    bytes si estaba entero; `Cut` (lo que medía al abrirlo) si creció
    mientras se leía; `LARGE` si al abrirlo ya pasaba de `small` (se
    listó pequeño y creció: lo copia por bloques quien escribe); o
    Link/Special/la excepción, como `open_regular`.
    """
    abierto = open_regular(path)
    if not isinstance(abierto, tuple):
        return abierto
    fd, n = abierto
    try:
        if n > small:
            return LARGE
        datos = read_upto(fd, n)
    except OSError as e:
        return e
    finally:
        os.close(fd)
    if len(datos) > n:
        return Cut(datos[:n])
    return datos


def read_batch(items, limit):
    """Lee un lote de (rel, ruta, tamaño) de una vez, en un hilo."""
    return [_payload(item, limit) for item in items]


def _payload(item, limit):
    rel, path, size = item
    try:
        return (rel, size, read_one(path, limit))
    except Exception as e:                                  # noqa: BLE001
        return (rel, size, e)


def _whole(item, small):
    rel, path, size = item
    try:
        return (rel, path, size, read_whole(path, small))
    except Exception as e:                                  # noqa: BLE001
        return (rel, path, size, e)


# ------------------------------------------------------------------ #
#  Muchos archivos                                                    #
# ------------------------------------------------------------------ #
class Pace(object):
    """Cuántos lotes se leen A LA VEZ, según lo que cuesta cada archivo.

    Abrir un archivo cuesta unos 5 µs si el sistema ya lo tiene en caché
    (Linux, macOS) y varios milisegundos si un antivirus lo revisa antes
    de dejarlo abrir (Windows). En el primer caso, varios hilos leyendo a
    la vez solo se pelean por el intérprete: medido, el doble de lento.
    En el segundo, es lo que más acelera: mientras uno espera, otros
    leen. Así que se MIDE: un lote se lee solo al principio (y otra vez
    cada pocos segundos, por si cambia), y a partir de lo que tardó cada
    archivo se decide cuántos lotes van en paralelo: uno más por cada
    `STEP` segundos de espera por archivo, hasta `maximum`. Con un lote
    de pocos archivos no basta (uno solo, frío, lo falsearía): se sigue
    midiendo hasta reunir `MIN_FILES` archivos o `MIN_SECONDS`.
    """

    STEP = 25e-6
    EVERY = 5.0
    MIN_FILES = 32
    MIN_SECONDS = 0.05

    def __init__(self, maximum, clock=time.monotonic):
        self.maximum = max(1, maximum)
        self.limit = 1                  # empieza midiendo
        self._clock = clock
        self._probe = True              # el próximo lote va solo
        self._next = None
        self._segundos = 0.0            # lo medido en esta ronda
        self._archivos = 0

    def alone(self, in_flight):
        """¿Un lote pedido ahora correría solo (y sirve para medir)?"""
        return self._probe and in_flight == 0

    def measured(self, seconds, files, alone):
        if not (alone and files and self._probe):
            return
        self._segundos += seconds
        self._archivos += files
        if self._archivos < self.MIN_FILES \
                and self._segundos < self.MIN_SECONDS:
            return                      # poca muestra: el próximo lote, solo
        per_file = self._segundos / self._archivos
        self.limit = max(1, min(self.maximum,
                                int(math.ceil(per_file / self.STEP))))
        self._probe = False
        self._segundos, self._archivos = 0.0, 0
        self._next = self._clock() + self.EVERY

    def tick(self):
        """Al pedir lotes: ¿toca volver a medir?"""
        if self._next is not None and self._clock() >= self._next:
            self._next = None
            self._probe = True
            self.limit = 1


def _run_batch(run_one, lote):
    """Un lote, en un hilo: (resultados, archivos leídos, segundos). Lo que
    no hay que leer (`tal_cual`) va dentro del lote en su sitio, así que
    un binario entre dos textos no parte el lote en dos."""
    t0 = time.perf_counter()
    out, leidos = [], 0
    for entrada, tal_cual in lote:
        if tal_cual is None:
            out.append(run_one(entrada))
            leidos += 1
        else:
            out.append(tal_cual)
    return out, leidos, time.perf_counter() - t0


def _prefetch(entries, direct, run_one, workers, ahead, batch_files,
              batch_bytes, window_bytes):
    """`run_one(entrada)` para cada entrada, en el orden de `entries`,
    leyendo por delante con hasta `workers` hilos y por lotes.

    `direct(entrada)` decide qué NO se lee: devuelve lo que hay que
    entregar tal cual, o None para leerla. Como mucho hay `window_bytes`
    bytes por leer en vuelo, y `ahead` lotes (o los que decida `Pace`
    midiendo, si `ahead` es None): la memoria queda acotada aunque haya
    millones de archivos.
    """
    def lotes():
        lote, nbytes = [], 0
        for entrada in entries:
            tal_cual = direct(entrada)
            lote.append((entrada, tal_cual))
            if tal_cual is None:
                nbytes += entrada[2]
            if len(lote) >= batch_files or nbytes >= batch_bytes:
                yield lote, nbytes
                lote, nbytes = [], 0
        if lote:
            yield lote, nbytes

    origen = lotes()
    pendientes = deque()
    en_vuelo = [0, 0]                       # lotes, bytes
    ritmo = Pace(workers) if ahead is None else None
    ex = ThreadPoolExecutor(max_workers=max(1, workers))

    def tope():
        return ahead if ritmo is None else ritmo.limit

    def llenar():
        if ritmo is not None:
            ritmo.tick()
        while en_vuelo[0] < tope() and en_vuelo[1] < window_bytes:
            item = next(origen, None)
            if item is None:
                return
            lote, nbytes = item
            solo = ritmo is not None and ritmo.alone(en_vuelo[0])
            pendientes.append((ex.submit(_run_batch, run_one, lote), nbytes,
                               solo))
            en_vuelo[0] += 1
            en_vuelo[1] += nbytes

    try:
        llenar()
        while pendientes:
            futuro, nbytes, solo = pendientes.popleft()
            hecho, leidos, segundos = futuro.result()
            en_vuelo[0] -= 1
            en_vuelo[1] -= nbytes
            if ritmo is not None:
                ritmo.measured(segundos, leidos, solo)
            llenar()
            for item in hecho:
                yield item
    finally:
        # Si quien consume se detiene (error, cancelación), los lotes que
        # aún no empezaron no se leen y no queda ningún hilo trabajando.
        ex.shutdown(wait=True, cancel_futures=True)


def _lstat(entrada):
    try:
        return os.lstat(entrada[1])
    except OSError:
        return None


def lstat_many(paths, workers=None):
    """`os.lstat` de cada ruta (None si no existe o falla), en el mismo
    orden. En paralelo solo si compensa (ver `Pace`): Linux responde de
    su caché en un par de µs, mientras que Windows abre y cierra cada
    archivo para contestar."""
    entradas = [(None, ruta, 1) for ruta in paths]
    return list(_prefetch(entradas, lambda _e: None, _lstat,
                          workers or default_workers(), None, 256,
                          1 << 62, 1 << 62))


def iter_payloads(entries, max_file_bytes, workers=None, ahead=None,
                  batch_files=BATCH_FILES, batch_bytes=BATCH_BYTES):
    """Entrega (rel, tamaño, contenido) en el orden de `entries`.

    `contenido` es bytes (`Cut` si pasaba del límite), un `Link` o un
    `Special` (no se abrió: ver sus clases), la excepción de lectura, o
    None cuando el archivo se omitió sin abrir (demasiado grande o
    binario por extensión). `entries` son tuplas (rel, ruta_abs, tamaño).
    """
    def direct(entrada):
        rel, _ruta, size = entrada
        if size > max_file_bytes or is_binary_ext(rel):
            return (rel, size, None)
        return None

    return _prefetch(entries, direct,
                     lambda entrada: _payload(entrada, max_file_bytes),
                     workers or default_workers(), ahead, batch_files,
                     batch_bytes, WINDOW_BYTES)


def iter_contents(entries, small=SMALL, workers=None, ahead=None,
                  batch_files=BATCH_FILES, batch_bytes=BATCH_BYTES,
                  window_bytes=WINDOW_BYTES):
    """Para el TXT: (rel, ruta, tamaño, contenido) en el orden de
    `entries`, SIN límite de tamaño.

    `contenido` es bytes (el archivo entero), `Cut` (creció mientras se
    leía: lo que medía al abrirlo), `LARGE` (pasa de `small`: hay que
    copiarlo por bloques), un `Link`, un `Special`, la excepción, o None
    (binario por extensión: ni se abre).
    """
    def direct(entrada):
        rel, ruta, size = entrada
        if is_binary_ext(rel):
            return (rel, ruta, size, None)
        if size > small:
            return (rel, ruta, size, LARGE)
        return None

    return _prefetch(entries, direct,
                     lambda entrada: _whole(entrada, small),
                     workers or default_workers(), ahead, batch_files,
                     batch_bytes, window_bytes)
