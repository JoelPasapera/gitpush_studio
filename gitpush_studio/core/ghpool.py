# -*- coding: utf-8 -*-
"""Varias conexiones con GitHub a la vez.

Cada consulta a GitHub pasa casi todo su tiempo esperando: a la red y a
que GitHub prepare una página de 100 issues (en un repositorio grande,
las páginas del final tardan segundos). De una en una, una descarga de
miles de páginas son horas de espera; con varias conexiones a la vez,
mientras una espera, las demás avanzan.

`SessionPool` mantiene un hilo y una sesión (`ApiSession`, con su
conexión que se reutiliza) por trabajador. `submit` encarga una tarea
(la usa el planificador, `ghsched`) y `run` reparte una lista de tareas
y devuelve los resultados según terminan. Quien guarda los resultados es
siempre el hilo que llama (el almacén no se comparte entre hilos).

Las sesiones de un mismo `SessionPool` comparten lo que saben de los
límites de GitHub (`ghlimits.SharedLimits`): si GitHub pide ir más
despacio a una, o una ve la cuota agotada, TODAS esperan, en vez de
seguir lanzando consultas que GitHub rechazaría; y de ahí sale también
cuántas consultas van a la vez (`concurrency`): se empieza con 4 y se
sube hasta el máximo mientras GitHub responda bien. «Detener» corta
también las esperas de todos los hilos, y si una tarea falla, las demás
se paran.

Con un solo trabajador no hay hilos: las tareas se hacen en el hilo que
llama, con la misma conexión que todo lo demás.
"""

import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from typing import (Callable, Dict, Iterable, Iterator, List, Optional,
                    Tuple, TypeVar)

from .errors import Cancelled
from .ghlimits import SharedLimits
from .ghsession import CANCELLED, ApiSession

# Conexiones a la vez COMO MUCHO (cuántas de verdad lo decide
# `SharedLimits` según responda GitHub) y con cuántas se empieza.
DEFAULT_WORKERS = 8
MAX_WORKERS = 8
INITIAL_WORKERS = 4
_POLL = 0.25

T = TypeVar("T")
R = TypeVar("R")
SessionFactory = Callable[[object], ApiSession]


class AnyEvent:
    """Se comporta como un `threading.Event` que está activo si lo está
    cualquiera de los que agrupa (el «Detener» del usuario y el aviso
    interno de que otra tarea falló)."""

    def __init__(self, *events: threading.Event) -> None:
        self._events = events

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)

    def wait(self, timeout: Optional[float] = None) -> bool:
        """True en cuanto alguno se active; False si pasa `timeout`."""
        limite = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.is_set():
                return True
            if limite is not None:
                resta = limite - time.monotonic()
                if resta <= 0:
                    return False
                paso = min(_POLL, resta)
            else:
                paso = _POLL
            self._events[0].wait(paso)


class SessionPool:
    """`workers` hilos, cada uno con su propia sesión con GitHub."""

    def __init__(self, factory: SessionFactory, workers: int = DEFAULT_WORKERS,
                 cancel: Optional[threading.Event] = None,
                 limits: Optional[SharedLimits] = None) -> None:
        self.workers = max(1, min(int(workers or 1), MAX_WORKERS))
        # Lo que comparten sus sesiones (si las crea `make_pool`).
        self.limits = limits
        self.cancel = cancel or threading.Event()
        self._abort = threading.Event()
        self._combined = AnyEvent(self.cancel, self._abort)
        self._factory = factory
        self._local = threading.local()
        self._sessions: List[ApiSession] = []
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=self.workers, thread_name_prefix="github")

    # ---- sesiones -------------------------------------------------------
    def session(self) -> ApiSession:
        """La sesión del hilo actual (se crea la primera vez)."""
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._factory(self._combined)
            self._local.session = session
            with self._lock:
                self._sessions.append(session)
        return session

    @property
    def requests(self) -> int:
        with self._lock:
            return sum(s.requests for s in self._sessions)

    @property
    def connections(self) -> int:
        with self._lock:
            return sum(s.connections for s in self._sessions)

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled(CANCELLED)

    def concurrency(self) -> int:
        """Cuántas tareas conviene tener en marcha ahora: lo que diga el
        control de conexiones a la vez, sin pasar de los hilos que hay."""
        if self.limits is None:
            return self.workers
        return max(1, min(self.workers, self.limits.concurrency()))

    def abort(self) -> None:
        """Avisa a todas las tareas en marcha de que paren (sus esperas
        y su próxima consulta lanzan Cancelled)."""
        self._abort.set()

    def resume(self) -> None:
        """Quita el aviso de `abort` (para volver a encargar tareas)."""
        self._abort.clear()

    def close(self) -> None:
        self._abort.set()
        self._executor.shutdown(wait=True)
        with self._lock:
            sesiones = list(self._sessions)
            self._sessions.clear()
        for session in sesiones:
            session.close()

    def __enter__(self) -> "SessionPool":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---- tareas -----------------------------------------------------------
    def _call(self, func: Callable[[ApiSession, T], R], task: T) -> R:
        if self._combined.is_set():
            raise Cancelled(CANCELLED)
        return func(self.session(), task)

    def _call_one(self, func: Callable[[ApiSession], R]) -> R:
        if self._combined.is_set():
            raise Cancelled(CANCELLED)
        return func(self.session())

    def submit(self, func: Callable[[ApiSession], R]) -> "Future[R]":
        """Encarga `func(sesión)` a un hilo y devuelve su `Future`. Con un
        solo trabajador se hace aquí mismo (el `Future` ya vuelve hecho)."""
        if self.workers > 1:
            return self._executor.submit(self._call_one, func)
        hecho: "Future[R]" = Future()
        try:
            hecho.set_result(self._call_one(func))
        except Exception as error:                      # noqa: BLE001
            hecho.set_exception(error)
        return hecho

    def run(self, func: Callable[[ApiSession, T], R], tasks: Iterable[T]
            ) -> Iterator[Tuple[T, R]]:
        """(tarea, resultado) de `func(sesión, tarea)` según terminan.

        Nunca hay más de dos tareas por hilo en cola: «Detener» no tiene
        que esperar a miles de tareas ya encargadas. Si una tarea falla
        (o se deja de leer a mitad), se avisa a las demás para que paren
        y se lanza su error cuando todas han terminado.
        """
        self._abort.clear()
        if self.workers == 1:
            # Una sola conexión: en este mismo hilo y con su sesión (la
            # misma que usa todo lo demás: ni un hilo ni una conexión más).
            for task in tasks:
                yield task, self._call(func, task)
            return
        pendientes: Dict[Future, T] = {}
        origen = iter(tasks)

        def encargar() -> bool:
            try:
                task = next(origen)
            except StopIteration:
                return False
            pendientes[self._executor.submit(self._call, func, task)] = task
            return True

        for _ in range(self.workers * 2):
            if not encargar():
                break
        try:
            while pendientes:
                hechas, _resto = wait_futures(set(pendientes),
                                              return_when=FIRST_COMPLETED)
                for futuro in hechas:
                    task = pendientes.pop(futuro)
                    resultado = futuro.result()
                    yield task, resultado
                    encargar()
        except BaseException:
            self._abort.set()
            for futuro in pendientes:
                futuro.cancel()
            wait_futures(set(pendientes))
            raise


def make_pool(token: str, root: str, cancel: Optional[threading.Event],
              on_wait=None, proxy=None, sleep=None,
              workers: int = DEFAULT_WORKERS) -> SessionPool:
    """Un `SessionPool` cuyas sesiones comparten token, raíz, proxy,
    avisos de espera y lo que saben de los límites de GitHub (también
    cuántas consultas van a la vez: empieza con `INITIAL_WORKERS` y sube
    hasta `workers` si GitHub responde bien)."""
    tope = max(1, min(int(workers or 1), MAX_WORKERS))
    limits = SharedLimits(maximum=tope, initial=min(INITIAL_WORKERS, tope))

    def factory(combined: object) -> ApiSession:
        return ApiSession(token=token, root=root, cancel=combined,
                          on_wait=on_wait, proxy=proxy, sleep=sleep,
                          limits=limits)
    return SessionPool(factory, workers=tope, cancel=cancel, limits=limits)
