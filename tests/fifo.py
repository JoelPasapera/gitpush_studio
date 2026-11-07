# -*- coding: utf-8 -*-
"""Tuberías con nombre en las pruebas: que un cuelgue falle en segundos.

Las pruebas que exigen que algo NO se quede esperando en una tubería lo
lanzan en un hilo y le dan un tiempo. Si el código se rompe y sí abre la
tubería, ese hilo queda bloqueado en `open()` para siempre: la prueba
falla a su hora, pero el proceso ya no termina (al salir, Python espera
a los hilos de un ThreadPoolExecutor) y una pasada de pruebas de
mutación se quedaba colgada hasta su tope de 15 minutos.

`join_or_release` espera al hilo y, si sigue bloqueado, abre el otro
extremo de la tubería sin bloquear y lo cierra: quien esperaba recibe
un fin de archivo y sigue. La prueba sigue fallando (se colgó), pero
termina enseguida.
"""

import os
import threading
import time
from pathlib import Path
from typing import Union

PathLike = Union[str, Path]
_RELEASE_SECONDS = 10.0
_RELEASE_STEP = 0.05


def release(path: PathLike) -> bool:
    """Desbloquea a quien esté esperando en `open(path)` para leer.

    Abrir para escribir sin bloquear solo funciona si alguien la tiene
    abierta (o esperando) para leer; si no, el sistema responde ENXIO y
    no hay nada que desbloquear. Devuelve si se pudo abrir.
    """
    try:
        descriptor = os.open(str(path), os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    os.close(descriptor)
    return True


def join_or_release(thread: threading.Thread, path: PathLike,
                    timeout: float) -> bool:
    """Espera a `thread` hasta `timeout` segundos. Devuelve True si se
    colgó: entonces, antes de volver, desbloquea la tubería `path` las
    veces que haga falta (el código roto puede abrirla más de una vez)
    para que el hilo y el proceso de las pruebas puedan terminar."""
    thread.join(timeout)
    if not thread.is_alive():
        return False
    deadline = time.monotonic() + _RELEASE_SECONDS
    while thread.is_alive() and time.monotonic() < deadline:
        release(path)
        thread.join(_RELEASE_STEP)
    return True
