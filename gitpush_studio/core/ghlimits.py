# -*- coding: utf-8 -*-
"""Lo que se sabe de los límites de GitHub, compartido por todas las
conexiones de una misma descarga.

Tres cosas, protegidas por un candado (las usan varios hilos a la vez):

  · la CUOTA de cada recurso («core» para REST, «graphql»…): si una
    conexión la ve agotada, las demás esperan al reinicio en vez de
    lanzar consultas que GitHub rechazaría (y que, repetidas, cuentan
    como abuso). Cada recurso es un cubo distinto: agotar uno no impide
    usar el otro;
  · el «VE MÁS DESPACIO» (el límite secundario de GitHub): cuando se lo
    pide a una conexión, esperan todas hasta cuando diga;
  · CUÁNTAS CONSULTAS A LA VEZ. GitHub no dice cuántas aguanta: depende
    de lo pesadas que sean y de lo ocupado que esté. Se descubre como lo
    hace TCP con la red (AIMD: subir de a poco, bajar a la mitad): cada
    respuesta normal sube un poco el número de conexiones a la vez,
    hasta el máximo; un «ve más despacio» lo parte por la mitad. Como
    cada aviso de GitHub cuesta una espera larga (suele pedir un minuto),
    además se recuerda el TECHO: después de un aviso ya no se vuelve a
    subir hasta el número que lo provocó.

Cada consulta ocupa un PUESTO mientras va y viene (`acquire`/`release`):
si el número baja, lo nota hasta la que ya estaba en marcha y solo
reintentaba (sin esto, tras la espera saldrían todas a la vez y GitHub
volvería a protestar). Y, como TCP, se reacciona UNA vez por tanda: los
avisos de consultas que salieron antes de la última bajada ya están
contados (si no, una sola ráfaga partiría el número por la mitad
varias veces seguidas).
"""

import threading
from typing import Dict, List, Optional, Tuple

from .errors import Cancelled

# Lo que pasó con una consulta, para ajustar cuántas van a la vez.
OK = "ok"                      # respondió con normalidad (también un 404)
SLOW_DOWN = "despacio"         # GitHub pidió ir más despacio
SERVER_ERROR = "fallo"         # 5xx o la red falló

# Cuánto sube el número de conexiones a la vez por cada respuesta normal:
# con `limite` conexiones, una más cada 4·`limite` respuestas.
GROWTH = 0.25
# Cuánto baja ante un fallo del servidor (no es un aviso: baja menos).
SERVER_ERROR_FACTOR = 0.75
# Cada cuánto se mira si se pidió parar mientras se espera un puesto.
_POLL = 0.25
# Lo que dice una descarga detenida (aquí y en `ghsession`).
CANCELLED = "Descarga detenida."

Quota = Tuple[Optional[int], Optional[int], Optional[int]]


class SharedLimits:
    """Los límites de GitHub vistos por las conexiones de una descarga.

    `maximum`: conexiones a la vez como mucho; `initial`: con cuántas se
    empieza (por defecto, el máximo). Una sesión suelta se crea su propio
    `SharedLimits()` (una conexión: nada que repartir).
    """

    def __init__(self, maximum: int = 1, initial: Optional[int] = None
                 ) -> None:
        self._lock = threading.Lock()
        self._room = threading.Condition(self._lock)
        self._in_flight = 0            # consultas que van ahora mismo
        self._epoch = 0                # sube con cada bajada del número
        # recurso → [quedan, límite, cuándo se reinicia]
        self._quota: Dict[str, List[Optional[int]]] = {}
        self._resume_at = 0.0          # nadie pregunta antes de esto
        self.maximum = max(1, int(maximum))
        inicio = self.maximum if initial is None else int(initial)
        self._limit = float(max(1, min(self.maximum, inicio)))
        self._ceiling = float(self.maximum)
        self.slow_downs = 0            # avisos de «ve más despacio»

    # ---- cuota ------------------------------------------------------------
    def quota(self, resource: str) -> Quota:
        """(quedan, límite, reinicio) de un recurso, o Nones si no se
        sabe aún."""
        with self._lock:
            valores = self._quota.get(resource) or [None, None, None]
            return valores[0], valores[1], valores[2]

    def update(self, headers: Dict[str, str]) -> None:
        """Lo que dicen las cabeceras X-RateLimit-* de una respuesta."""
        resource = headers.get("x-ratelimit-resource", "core").strip() \
            or "core"
        with self._lock:
            valores = self._quota.setdefault(resource, [None, None, None])
            for index, name in enumerate(("x-ratelimit-remaining",
                                          "x-ratelimit-limit",
                                          "x-ratelimit-reset")):
                value = headers.get(name)
                if value is None:
                    continue
                try:
                    valores[index] = int(value)
                except ValueError:
                    pass

    def exhaust(self, resource: str) -> None:
        """GitHub dijo que no quedan consultas de `resource`."""
        with self._lock:
            self._quota.setdefault(resource, [None, None, None])[0] = 0

    # ---- «ve más despacio» ------------------------------------------------
    def hold(self, until: float) -> None:
        """Nadie pregunta antes de `until` (GitHub pidió ir despacio)."""
        with self._lock:
            self._resume_at = max(self._resume_at, until)

    @property
    def resume_at(self) -> float:
        with self._lock:
            return self._resume_at

    # ---- cuántas a la vez -------------------------------------------------
    def concurrency(self) -> int:
        """Cuántas consultas pueden ir a la vez ahora mismo."""
        with self._lock:
            return max(1, int(self._limit))

    @property
    def ceiling(self) -> int:
        """Hasta dónde puede volver a subir (tras un aviso, menos)."""
        with self._lock:
            return max(1, int(self._ceiling))

    def acquire(self, cancel=None) -> int:
        """Espera un puesto entre las consultas que van a la vez y lo
        ocupa. Devuelve la tanda (`epoch`) en la que sale, para
        `release`. Lanza Cancelled si `cancel` se activa mientras espera."""
        with self._room:
            while self._in_flight >= max(1, int(self._limit)):
                if cancel is not None and cancel.is_set():
                    raise Cancelled(CANCELLED)
                self._room.wait(_POLL)
            self._in_flight += 1
            return self._epoch

    def release(self, epoch: int, outcome: Optional[str]) -> None:
        """Deja el puesto y cuenta lo que pasó (`OK`, `SLOW_DOWN`,
        `SERVER_ERROR` o None si no dice nada de la carga)."""
        with self._room:
            self._in_flight = max(0, self._in_flight - 1)
            self._adjust(outcome, epoch)
            self._room.notify_all()

    def record(self, outcome: Optional[str],
               epoch: Optional[int] = None) -> None:
        """Ajusta cuántas van a la vez según lo que pasó con una consulta
        que no ocupaba puesto (`epoch` None: la tanda actual)."""
        with self._room:
            self._adjust(outcome, self._epoch if epoch is None else epoch)
            self._room.notify_all()

    def _adjust(self, outcome: Optional[str], epoch: int) -> None:
        """(Con el candado ya tomado.)"""
        if outcome == OK:
            if self._limit < self._ceiling:
                self._limit = min(self._ceiling,
                                  self._limit + GROWTH / self._limit)
        elif outcome == SLOW_DOWN:
            self.slow_downs += 1
            if epoch != self._epoch:
                return                 # de antes de la última bajada
            # El número que provocó el aviso ya no se vuelve a alcanzar.
            self._ceiling = max(1.0, float(int(self._limit)) - 1.0)
            self._limit = max(1.0, self._limit / 2.0)
            self._epoch += 1
        elif outcome == SERVER_ERROR:
            if epoch != self._epoch:
                return
            self._limit = max(1.0, self._limit * SERVER_ERROR_FACTOR)
            self._epoch += 1

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight
