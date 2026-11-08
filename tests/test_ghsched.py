# -*- coding: utf-8 -*-
"""El planificador de la descarga, el control de conexiones a la vez y
las confirmaciones agrupadas del almacén.

  · cuántas a la vez (`ghlimits`): sube de a poco mientras GitHub
    responde bien, se parte por la mitad cuando pide ir más despacio y
    no vuelve a subir hasta donde lo provocó;
  · el planificador (`ghsched`): reparte por prioridad, deja a GraphQL
    como mucho la mitad de las conexiones, entrega los resultados en su
    hilo, para todo ante un error y avisa si se queda sin nada que hacer
    sin haber terminado (un error de programación);
  · en la descarga de verdad: lo de cada PR empieza mientras siguen los
    demás listados, y contra un servidor que solo aguanta dos consultas
    a la vez, se adapta y el TXT sale igual;
  · el almacén confirma por tandas: lo confirmado nunca queda a medias,
    y cerrar confirma lo pendiente; uno del formato anterior se pone al
    día (solo se tira lo que ese formato guardaba mal) y uno de OTRA
    descarga se vacía;
  · si GraphQL no se puede usar, el progreso lo dice (`notice`).
"""

import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from typing import List, Optional

from tests.test_ghdump import Case

from gitpush_studio.core import ghstore
from gitpush_studio.core.errors import Cancelled, StepError
from gitpush_studio.core.ghdump import DumpOptions
from gitpush_studio.core.ghlimits import (OK, SERVER_ERROR, SLOW_DOWN,
                                          SharedLimits)
from gitpush_studio.core.ghpool import SessionPool
from gitpush_studio.core.ghsched import (Scheduler, SchedulerStalled, Source,
                                         Task)


# ------------------------------------------------------------------ #
#  Cuántas a la vez                                                   #
# ------------------------------------------------------------------ #
class LimitsTests(unittest.TestCase):

    def test_grows_slowly_up_to_the_maximum(self):
        limites = SharedLimits(maximum=8, initial=4)
        self.assertEqual(limites.concurrency(), 4)
        respuestas = 0
        while limites.concurrency() < 8:
            limites.record(OK)
            respuestas += 1
            self.assertLess(respuestas, 1000)
        # Una más cada 4·límite respuestas: 16 + 20 + 24 + 28.
        self.assertGreaterEqual(respuestas, 60)
        for _ in range(500):
            limites.record(OK)
        self.assertEqual(limites.concurrency(), 8, "nunca más del máximo")

    def test_a_slow_down_halves_and_sets_a_ceiling(self):
        limites = SharedLimits(maximum=8, initial=6)
        limites.record(SLOW_DOWN)
        self.assertEqual(limites.concurrency(), 3)
        self.assertEqual(limites.ceiling, 5)
        for _ in range(2000):
            limites.record(OK)
        self.assertEqual(limites.concurrency(), 5,
                         "no vuelve a donde GitHub pidió ir despacio")
        self.assertEqual(limites.slow_downs, 1)

    def test_never_below_one(self):
        limites = SharedLimits(maximum=8, initial=2)
        for _ in range(10):
            limites.record(SLOW_DOWN)
            limites.record(SERVER_ERROR)
        self.assertEqual(limites.concurrency(), 1)
        self.assertEqual(limites.ceiling, 1)

    def test_server_errors_lower_it_a_little(self):
        limites = SharedLimits(maximum=8, initial=8)
        limites.record(SERVER_ERROR)
        self.assertEqual(limites.concurrency(), 6)
        self.assertEqual(limites.ceiling, 8, "un 5xx no es un aviso")

    def test_a_lone_session_has_nothing_to_share(self):
        self.assertEqual(SharedLimits().concurrency(), 1)

    def test_one_reaction_per_window(self):
        # Ocho consultas salen juntas y dos reciben «ve más despacio»: es
        # el mismo exceso, se baja UNA vez (no a la cuarta parte).
        limites = SharedLimits(maximum=8, initial=8)
        tandas = [limites.acquire() for _ in range(8)]
        limites.release(tandas[0], SLOW_DOWN)
        limites.release(tandas[1], SLOW_DOWN)
        self.assertEqual(limites.concurrency(), 4)
        self.assertEqual(limites.slow_downs, 2)
        # Una que sale DESPUÉS de la bajada y también recibe el aviso: eso
        # sí es nuevo.
        for epoch in tandas[2:]:
            limites.release(epoch, OK)
        nueva = limites.acquire()
        limites.release(nueva, SLOW_DOWN)
        self.assertEqual(limites.concurrency(), 2)

    def test_a_slot_is_waited_for_and_the_wait_can_be_stopped(self):
        limites = SharedLimits(maximum=1)
        primero = limites.acquire()
        conseguido = threading.Event()

        def segundo():
            epoch = limites.acquire()
            conseguido.set()
            limites.release(epoch, OK)
        hilo = threading.Thread(target=segundo)
        hilo.start()
        self.assertFalse(conseguido.wait(0.3), "espera a que haya sitio")
        limites.release(primero, OK)
        self.assertTrue(conseguido.wait(2))
        hilo.join(2)
        # Ocupado y con «Detener» pulsado: no se queda esperando.
        ocupado = limites.acquire()
        parar = threading.Event()
        parar.set()
        with self.assertRaises(Cancelled):
            limites.acquire(parar)
        limites.release(ocupado, OK)
        self.assertEqual(limites.in_flight, 0)


# ------------------------------------------------------------------ #
#  El planificador                                                    #
# ------------------------------------------------------------------ #
class _Pool:
    """Lo mínimo de un `SessionPool` para el planificador."""

    def __init__(self, workers: int, concurrency: Optional[int] = None):
        self.real = SessionPool(lambda cancel: _Session(cancel), workers)
        self._concurrency = concurrency or workers
        self.aborted = 0

    def concurrency(self) -> int:
        return self._concurrency

    def submit(self, func):
        return self.real.submit(func)

    def abort(self) -> None:
        self.aborted += 1
        self.real.abort()

    def resume(self) -> None:
        self.real.resume()

    def close(self) -> None:
        self.real.close()


class _Session:
    def __init__(self, cancel) -> None:
        self.cancel = cancel
        self.requests = 0
        self.connections = 0

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled("Descarga detenida.")

    def close(self) -> None:
        pass


class _Counter(Source):
    """Una fuente de `n` tareas que anota cuántas suyas van a la vez."""

    def __init__(self, name: str, n: int, graphql: bool = False,
                 pause: float = 0.0, log: Optional[List[str]] = None,
                 shared: Optional[dict] = None) -> None:
        super().__init__()
        self.label = name
        self.graphql = graphql
        self.queue = deque(range(n))
        self.pause = pause
        self.log = log if log is not None else []
        self.results: List[int] = []
        self.threads = set()
        self.shared = shared if shared is not None else {}
        self.lock = threading.Lock()

    def next_task(self):
        if not self.queue:
            return None
        n = self.queue.popleft()
        self.log.append(f"{self.label}{n}")
        return Task(self, lambda s: self._work(n), n)

    def _work(self, n: int) -> int:
        with self.lock:
            clave = "graphql" if self.graphql else "rest"
            self.shared[clave] = self.shared.get(clave, 0) + 1
            maximo = "max_" + clave
            self.shared[maximo] = max(self.shared.get(maximo, 0),
                                      self.shared[clave])
        time.sleep(self.pause)
        with self.lock:
            self.shared[clave] -= 1
        return n * 10

    def on_result(self, task, result) -> None:
        self.results.append(result)
        self.threads.add(threading.get_ident())

    def finished(self) -> bool:
        return not self.queue and not self.running


class SchedulerTests(unittest.TestCase):

    def run_sources(self, pool, *sources):
        planificador = Scheduler(pool)
        for source in sources:
            planificador.add(source)
        try:
            planificador.run()
        finally:
            pool.close()
        return planificador

    def test_with_one_connection_in_order_of_priority(self):
        log: List[str] = []
        a = _Counter("a", 3, log=log)
        b = _Counter("b", 2, log=log)
        self.run_sources(_Pool(1), a, b)
        self.assertEqual(log, ["a0", "a1", "a2", "b0", "b1"])
        self.assertEqual(a.results, [0, 10, 20])
        self.assertEqual(a.threads, {threading.get_ident()},
                         "los resultados, siempre en el hilo que llama")

    def test_graphql_never_takes_more_than_half(self):
        # Mientras lo demás tenga trabajo (aquí, siempre), GraphQL no pasa
        # de la mitad.
        comun: dict = {}
        g = _Counter("g", 12, graphql=True, pause=0.03, shared=comun)
        r = _Counter("r", 60, pause=0.03, shared=comun)
        self.run_sources(_Pool(4), g, r)
        self.assertLessEqual(comun["max_graphql"], 2)
        self.assertGreaterEqual(comun["max_rest"], 2,
                                "lo demás llena el resto a la vez")
        self.assertEqual(sorted(g.results), [n * 10 for n in range(12)])

    def test_graphql_alone_uses_every_connection(self):
        # Al retomar con los listados ya hechos solo queda GraphQL: no
        # tiene sentido dejar la mitad de las conexiones paradas.
        comun: dict = {}
        g = _Counter("g", 12, graphql=True, pause=0.05, shared=comun)
        self.run_sources(_Pool(4), g)
        self.assertEqual(comun["max_graphql"], 4)

    def test_an_error_stops_everything(self):
        class Rota(_Counter):
            def _work(self, n):
                if n == 2:
                    raise StepError("falla la dos")
                time.sleep(0.02)
                return n

        pool = _Pool(4)
        rota = Rota("x", 30)
        with self.assertRaises(StepError):
            self.run_sources(pool, rota)
        self.assertEqual(pool.aborted, 1)
        self.assertLess(len(rota.results), 29)
        self.assertEqual(rota.running, 0)

    def test_a_stalled_plan_is_reported(self):
        class Atascada(Source):
            label = "atascada"

            def next_task(self):
                return None

            def on_result(self, task, result):
                pass

            def finished(self):
                return False

        with self.assertRaises(SchedulerStalled):
            self.run_sources(_Pool(2), Atascada())

    def test_sources_that_are_not_ready_wait(self):
        primero = _Counter("p", 3)

        class Despues(_Counter):
            def ready(self):
                return primero.finished()

        log: List[str] = []
        primero.log = log
        despues = Despues("d", 2, log=log)
        self.run_sources(_Pool(4), despues, primero)
        self.assertEqual(log[:3], ["p0", "p1", "p2"])


# ------------------------------------------------------------------ #
#  En la descarga de verdad                                           #
# ------------------------------------------------------------------ #
class OverlapTests(Case):

    def test_per_item_work_starts_while_other_listings_go_on(self):
        # Eventos lentos y por número de página: lo de cada PR (GraphQL)
        # empieza antes de que acabe ese listado.
        self.repo.events_paged = True
        for n in range(2, 8):
            self.hub.delay(f"events?per_page=100&page={n}", 0.15)
        self.dump()
        paths = self.hub.paths()
        primera_graphql = paths.index("/graphql")
        ultima_de_eventos = max(i for i, p in enumerate(paths)
                                if "/issues/events?per_page=100" in p)
        self.assertLess(primera_graphql, ultima_de_eventos)

    def test_a_server_that_only_takes_two_at_once(self):
        base = self.text(self.dump(self.tmp / "base.txt",
                                   options=DumpOptions(mentions=True,
                                                       workers=1)))
        self.hub.requests.clear()
        for n in range(4):
            self.repo.issues.append(dict(self.repo.issues[-1],
                                         number=8 + n,
                                         title=f"Más {8 + n}"))
        base = self.text(self.dump(self.tmp / "base.txt",
                                   options=DumpOptions(mentions=True,
                                                       workers=1)))
        self.hub.requests.clear()
        self.hub.delay("/repos/ana/app/", 0.05)
        self.hub.max_concurrent = 2
        avisos: List[str] = []
        r = self.dump(self.tmp / "rapido.txt",
                      options=DumpOptions(mentions=True, workers=8),
                      on_wait=lambda why, until: avisos.append(why))
        self.assertEqual(self.text(r), base)
        # Unos pocos avisos y se adapta (no uno por consulta: son decenas).
        # El peor caso, por cómo cuenta el falso (rechaza TODAS las que
        # coinciden en su espera de 0,05 s): la primera tanda entera (se
        # empieza con 4 a la vez) y, después, un intento por encima del
        # techo aprendido (3 a la vez): 4 + 3 = 7. Luego ya no pasa de 2.
        self.assertGreaterEqual(self.hub.slow_downs, 1)
        self.assertLessEqual(self.hub.slow_downs, 7)
        self.assertTrue(any("más despacio" in a for a in avisos))


class ProgressTests(Case):

    def test_progress_says_what_is_going_on(self):
        vistos = []
        self.dump(on_progress=vistos.append)
        self.assertEqual(vistos[-1].stage, "Listo")
        self.assertTrue(all(p.estimate >= p.requests for p in vistos))
        self.assertEqual([p.requests for p in vistos],
                         sorted(p.requests for p in vistos))
        etiquetas = {e for p in vistos for e in p.active}
        for esperada in ("issues", "comentarios", "eventos",
                         "revisiones, commits y menciones (GraphQL)"):
            self.assertIn(esperada, etiquetas)
        self.assertTrue(any(len(p.active) >= 3 for p in vistos),
                        "varias partes a la vez")
        self.assertTrue(any(p.stage.startswith("Descargando ")
                            for p in vistos))
        self.assertEqual({p.notice for p in vistos}, {""},
                         "sin avisos si todo va bien")

    def test_a_graphql_that_cannot_be_used_is_said_while_it_goes_on(self):
        self.hub.fault("graphql_unauth", match="/graphql", times=99)
        vistos = []
        r = self.dump(on_progress=vistos.append)
        self.assertEqual(vistos[0].notice, "", "aún no se sabía")
        con_aviso = [p for p in vistos if p.notice]
        self.assertTrue(con_aviso)
        self.assertTrue(any(p.stage != "Listo" for p in con_aviso),
                        "se dice mientras sigue, no solo al final")
        self.assertIn("GraphQL no se pudo usar", vistos[-1].notice)
        self.assertEqual(r.notice, vistos[-1].notice)


class SlotsAreReturnedTests(Case):
    """Una consulta que acaba mal devuelve SIEMPRE su puesto (si no, las
    siguientes esperarían para siempre)."""

    def session_with(self, limites: SharedLimits):
        from gitpush_studio.core.ghsession import ApiSession
        sesion = ApiSession(token=self.token, root=self.hub.root,
                            sleep=self.slept.append, limits=limites)
        self.addCleanup(sesion.close)
        return sesion

    def test_after_every_kind_of_trouble(self):
        limites = SharedLimits(maximum=1)
        sesion = self.session_with(limites)
        for fallo in ("huge", "bomb", "notjson"):
            self.hub.fault(fallo, "/repos/ana/app")
            with self.assertRaises(StepError):
                sesion.get("/repos/ana/app")
            self.assertEqual(limites.in_flight, 0, fallo)
        for fallo in ("error", "drop", "secondary"):
            self.hub.fault(fallo, "/repos/ana/app")
            self.assertTrue(sesion.get("/repos/ana/app").ok)
            self.assertEqual(limites.in_flight, 0, fallo)


# ------------------------------------------------------------------ #
#  El almacén confirma por tandas                                     #
# ------------------------------------------------------------------ #
class StoreBatchTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.now = [100.0]
        self.identity = {"format": ghstore.FORMAT, "repo_id": 1,
                         "commits": True, "mentions": False}

    def open(self):
        return ghstore.Store(self.tmp / "a.sqlite3", self.identity,
                             commit_every=0.5, clock=lambda: self.now[0])

    def seen_from_outside(self) -> int:
        db = sqlite3.connect(str(self.tmp / "a.sqlite3"))
        try:
            return db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        finally:
            db.close()

    def page(self, store, n: int, cursor: str) -> None:
        store.save("issues", cursor=cursor,
                   items=[(n, {"number": n, "kind": "issue"})])

    def test_pages_are_committed_together(self):
        almacen = self.open()
        for n in range(1, 6):
            self.page(almacen, n, f"p{n}")
        self.assertEqual(almacen.commits, 0)
        self.assertEqual(self.seen_from_outside(), 0, "aún sin confirmar")
        self.assertEqual(almacen.items_count(), 5, "pero ya se ven aquí")
        self.now[0] += 0.6
        self.page(almacen, 6, "p6")
        self.assertEqual(almacen.commits, 1)
        self.assertEqual(self.seen_from_outside(), 6)
        almacen.close()

    def test_closing_commits_what_is_pending(self):
        almacen = self.open()
        self.page(almacen, 1, "p1")
        almacen.close()
        self.assertEqual(self.seen_from_outside(), 1)
        otra = self.open()
        self.assertEqual(otra.stage("issues"), ("p1", False))
        otra.close()

    def test_a_crash_loses_only_the_pending_and_never_half(self):
        almacen = self.open()
        self.page(almacen, 1, "p1")
        almacen.flush()
        self.page(almacen, 2, "p2")
        # Muere sin confirmar (como si se cortara la corriente).
        almacen._db.close()
        otra = self.open()
        self.assertEqual(otra.numbers(), [1])
        self.assertEqual(otra.stage("issues"), ("p1", False),
                         "la página 2 y su cursor, ninguno de los dos")
        otra.close()

    def test_a_save_that_fails_leaves_nothing_half_done(self):
        almacen = self.open()
        self.page(almacen, 1, "p1")
        almacen.flush()
        with self.assertRaises(TypeError):
            # Los issues entran, pero la entrada no se puede guardar.
            almacen.save("issues", cursor="p2",
                         items=[(2, {"number": 2})],
                         entries=[("comment", "9", 2, "t", {"x": {1, 2}})])
        almacen.flush()
        self.assertEqual(almacen.numbers(), [1], "ni el issue 2")
        self.assertEqual(almacen.stage("issues"), ("p1", False))
        almacen.close()

    def test_a_batch_that_fails_leaves_nothing_half_done(self):
        # Lo de un lote de GraphQL: las entradas entran, pero marcar las
        # partes como hechas falla. No queda ninguna de las dos cosas.
        almacen = self.open()
        with self.assertRaises(sqlite3.Error):
            almacen.save_many([({"no", "vale"}, "", True)],
                              entries=[("review", "7", 2, "t", {"a": 1})])
        almacen.flush()
        self.assertEqual(almacen.count("review"), 0)
        self.assertEqual(almacen.done_stages("reviews:"), set())
        almacen.close()

    def old_store(self, identity: dict) -> None:
        """Un almacén con un poco de todo, guardado con `identity`."""
        almacen = ghstore.Store(self.tmp / "a.sqlite3", identity)
        almacen.save("issues", cursor="", done=True,
                     items=[(1, {"number": 1, "kind": "issue"}),
                            (2, {"number": 2, "kind": "pr"})])
        almacen.save_many(
            [("reviews:2", "", True), ("commits:2", "", True),
             ("timeline:1", "", True)],
            entries=[("comment", "101", 1, "t1", {"body": "hola"}),
                     ("review", "301", 2, "t2", {"state": "APPROVED"}),
                     ("commit", "1111111aaaa", 2, "t3", {"sha": "1111111aaaa"}),
                     ("mention", "ana/app#2@t4", 1, "t4", {"x": 1})])
        almacen.close()

    def test_a_store_of_the_previous_format_is_brought_up_to_date(self):
        # 3.6.0 guardaba mal los commits y las menciones (sin el número de
        # su PR o issue): se tiran y se vuelven a pedir. Lo demás, intacto.
        viejo = dict(self.identity, format=1)
        self.old_store(viejo)
        almacen = self.open()
        self.assertTrue(almacen.upgraded)
        self.assertTrue(almacen.resumed, "se sigue desde donde iba")
        self.assertEqual(almacen.numbers(), [1, 2])
        self.assertEqual(almacen.stage("issues"), ("", True))
        self.assertEqual(almacen.count("comment"), 1)
        self.assertEqual(almacen.count("review"), 1)
        self.assertEqual(almacen.count("commit"), 0)
        self.assertEqual(almacen.count("mention"), 0)
        self.assertEqual(almacen.done_stages("reviews:"), {"reviews:2"})
        self.assertEqual(almacen.done_stages("commits:"), set())
        self.assertEqual(almacen.done_stages("timeline:"), set())
        almacen.close()
        # Ya está al día: abrirlo otra vez no tira nada más.
        otra = self.open()
        self.assertFalse(otra.upgraded)
        self.assertEqual(otra.count("comment"), 1)
        otra.close()

    def test_a_store_of_another_download_is_emptied(self):
        # Del formato anterior pero con OTRAS opciones: no es este volcado.
        self.old_store(dict(self.identity, format=1, mentions=True))
        almacen = self.open()
        self.assertFalse(almacen.upgraded)
        self.assertFalse(almacen.resumed)
        self.assertEqual(almacen.numbers(), [])
        self.assertEqual(almacen.count("comment"), 0)
        almacen.close()
        # Y uno de un formato que no se sabe poner al día (o que ni es un
        # número: una identidad estropeada), tampoco; sin romperse.
        for formato in (0, [1], "1", True):
            shutil.rmtree(self.tmp)
            self.tmp.mkdir()
            self.old_store(dict(self.identity, format=formato))
            almacen = self.open()
            self.assertFalse(almacen.upgraded, formato)
            self.assertEqual(almacen.numbers(), [], formato)
            almacen.close()

    def test_removing_does_not_bother_to_commit(self):
        almacen = self.open()
        self.page(almacen, 1, "p1")
        almacen.remove()
        self.assertFalse((self.tmp / "a.sqlite3").exists())
        self.assertEqual(almacen.commits, 0)


if __name__ == "__main__":
    unittest.main()
