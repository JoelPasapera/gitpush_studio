# -*- coding: utf-8 -*-
"""El planificador de una descarga: muchas fuentes de consultas, las
mismas conexiones.

Una descarga tiene partes que no dependen unas de otras: los listados de
todo el repositorio (issues, pull requests, comentarios, comentarios en
el código, eventos) y lo de cada pull request o issue (por GraphQL o,
lo que no cabe, por REST), que solo necesita saber qué issues y pull
requests hay. Hechas una detrás de otra, en cada cambio de parte quedan
conexiones paradas: mientras se pide la primera página de un listado
(para saber cuántas tiene), mientras se acaba la última tanda, mientras
se espera la comprobación de los eventos…

Aquí cada parte es una FUENTE (`Source`) que propone consultas cuando
puede, y el planificador (`Scheduler`) mantiene las conexiones llenas
con las de todas, por orden de prioridad (el orden en que se añaden):
la primera fuente con algo que pedir se lleva la conexión libre. Las de
GraphQL no ocupan más de la mitad de las conexiones mientras haya otras
consultas esperando (GitHub les da menos margen, y así los listados
siguen avanzando); si solo queda GraphQL, se las queda todas.

Las consultas se hacen en los hilos de `ghpool`; lo que traen se entrega
a su fuente (`on_result`) SIEMPRE en el hilo que llama a `run`, de una
en una y en el orden en que se encargaron (el almacén no se comparte
entre hilos, y así el resultado no depende de qué hilo terminó antes).
Cuántas van a la vez lo dice el `SessionPool` (se adapta a lo que GitHub
aguante). Si una consulta falla o se pulsa «Detener», las demás se
paran y el error sale de `run`.
"""

import itertools
from concurrent.futures import FIRST_COMPLETED
from concurrent.futures import wait as wait_futures
from typing import Callable, Dict, List, Optional

from .ghsession import ApiSession

Runner = Callable[[ApiSession], object]


class Task:
    """Una consulta (o unas pocas seguidas) que hace una conexión.

    `run(sesión)` se ejecuta en un hilo de trabajo y lo que devuelve va a
    `source.on_result(task, resultado)` en el hilo del planificador.
    `data` es lo que la fuente necesite para reconocerla."""

    __slots__ = ("source", "run", "data", "order")

    def __init__(self, source: "Source", run: Runner,
                 data: object = None) -> None:
        self.source = source
        self.run = run
        self.data = data
        self.order = 0                 # cuándo se encargó (lo pone run)


class Source:
    """Una fuente de consultas. Las subclases implementan `next_task`,
    `on_result` y `finished`; `running` lo lleva el planificador."""

    label = ""                         # para «Descargando …»
    graphql = False                    # sus consultas son de GraphQL

    def __init__(self) -> None:
        self.running = 0               # tareas suyas en marcha
        # Un aviso para quien mira, si algo no fue como debía (por
        # ejemplo, GraphQL no se pudo usar y todo va más lento).
        self.notice = ""

    def ready(self) -> bool:
        """¿Puede proponer ya? (Por ejemplo, GraphQL espera a saber qué
        pull requests hay.)"""
        return True

    def next_task(self) -> Optional[Task]:
        """Una consulta para hacer ahora, o None si por ahora no tiene
        (espera resultados) o ya no le queda nada."""
        raise NotImplementedError

    def on_result(self, task: Task, result: object) -> None:
        """Lo que trajo una de sus consultas (en el hilo del planificador)."""
        raise NotImplementedError

    def finished(self) -> bool:
        raise NotImplementedError

    def pending(self) -> int:
        """Consultas que calcula que le faltan (para la estimación)."""
        return 0


class SchedulerStalled(RuntimeError):
    """Ninguna fuente tiene nada que pedir, nada está en marcha y no han
    terminado todas: un error de programación, no de GitHub."""


class Scheduler:
    """Reparte las consultas de varias fuentes entre las conexiones de un
    `SessionPool`."""

    def __init__(self, pool, on_tick: Optional[Callable[[], None]] = None
                 ) -> None:
        self._pool = pool
        self._on_tick = on_tick
        self.sources: List[Source] = []
        self._order = itertools.count()

    def add(self, source: Source) -> None:
        """Añade una fuente (las primeras tienen prioridad)."""
        self.sources.append(source)

    def active(self) -> List[Source]:
        """Las fuentes que están trabajando o pueden hacerlo ya."""
        return [s for s in self.sources
                if not s.finished() and (s.running or s.ready())]

    # ---- reparto ------------------------------------------------------------
    def _next(self, running: Dict["Future[object]", Task],
              limit: int) -> Optional[Task]:
        """La siguiente consulta, por prioridad. GraphQL se queda con
        como mucho la mitad de las conexiones... salvo que nadie más
        tenga nada que pedir (entonces, para qué dejarlas paradas)."""
        de_graphql = sum(1 for t in running.values() if t.source.graphql)
        tope_graphql = max(1, limit // 2)
        task = self._pick(cap_graphql=de_graphql >= tope_graphql)
        if task is None and de_graphql >= tope_graphql:
            task = self._pick(cap_graphql=False)
        return task

    def _pick(self, cap_graphql: bool) -> Optional[Task]:
        for source in self.sources:
            if source.finished() or not source.ready():
                continue
            if source.graphql and cap_graphql:
                continue
            task = source.next_task()
            if task is not None:
                return task
        return None

    def _fill(self, running: Dict["Future[object]", Task]) -> None:
        limit = self._pool.concurrency()
        while len(running) < limit:
            task = self._next(running, limit)
            if task is None:
                return
            task.order = next(self._order)
            task.source.running += 1
            running[self._pool.submit(task.run)] = task

    # ---- marcha -------------------------------------------------------------
    def run(self) -> None:
        """Hasta que todas las fuentes terminen. Lanza el primer error de
        una consulta (o Cancelled) tras parar las demás."""
        running: Dict["Future[object]", Task] = {}
        try:
            while True:
                self._fill(running)
                if not running:
                    if all(s.finished() for s in self.sources):
                        return
                    pendientes = [s.label for s in self.sources
                                  if not s.finished()]
                    raise SchedulerStalled("El planificador se quedó sin "
                                           f"nada que pedir: {pendientes}")
                hechas, _resto = wait_futures(set(running),
                                              return_when=FIRST_COMPLETED)
                for futuro in sorted(hechas, key=lambda f: running[f].order):
                    task = running.pop(futuro)
                    task.source.running -= 1
                    task.source.on_result(task, futuro.result())
                if self._on_tick is not None:
                    self._on_tick()
        except BaseException:
            self._pool.abort()
            for futuro in running:
                futuro.cancel()
            wait_futures(set(running))
            for task in running.values():
                task.source.running -= 1
            raise
        finally:
            self._pool.resume()
