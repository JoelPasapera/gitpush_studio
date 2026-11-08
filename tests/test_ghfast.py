# -*- coding: utf-8 -*-
"""La descarga rápida: páginas a la vez y lotes por GraphQL.

Lo que se exige, contra el GitHub falso (`tests.fakegithub`):

  · el MISMO TXT, byte a byte, que la descarga de siempre (REST, de una
    en una): por GraphQL y en paralelo no se pierde ni se cambia nada,
    tampoco los nombres de los bots (GraphQL los llama distinto);
  · lo que no cabe en una consulta de GraphQL, lo que falla y lo que
    GraphQL no deja ver, por REST; sin token, nunca GraphQL;
  · que una lista «cabe» lo dice `pageInfo.hasNextPage`, NO `totalCount`
    (en la historia de un issue GitHub cuenta ahí TODO, no solo las
    menciones: tomarlo por «no cabe» mandó a REST, uno a uno, casi todo
    lo de ghidra en 3.6.0);
  · una lista que no cabía nunca hace abandonar GraphQL; si GraphQL deja
    de funcionar de verdad, se dice por qué (`notice`);
  · varias páginas a la vez de verdad, cada una pedida una sola vez;
  · nada se pierde aunque GitHub reciba novedades A MITAD de la
    descarga: las que van al final (issues, comentarios) las recoge la
    cola, y las que van delante (eventos, del más reciente al más
    antiguo) no hacen saltarse nada aunque las páginas lleguen
    desordenadas;
  · «Detener» a mitad de un listado en paralelo se retoma sin perder
    nada (volviendo a pedir la página anterior a la que faltaba);
  · un almacén a medias de la versión anterior (todo por REST) se sigue
    por GraphQL pidiendo SOLO lo que faltaba;
  · un «última página» hacia otro servidor se rechaza: el token no sale.
"""

import re
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from typing import Dict, List

from tests.fakegithub import _user
from tests.test_ghdump import Case

from gitpush_studio.core import ghgraphql, ghstore
from gitpush_studio.core.errors import Cancelled, StepError
from gitpush_studio.core.ghdump import DumpOptions
from gitpush_studio.core.ghfetch import (PARALLEL_PREFIX, page_url,
                                         parallel_cursor,
                                         parse_parallel_cursor)
from gitpush_studio.core.ghpool import AnyEvent, SessionPool
from gitpush_studio.core.ghsession import (ApiSession, SharedLimits,
                                           page_number)

REST = DumpOptions(mentions=True, workers=1, graphql=False)
FAST = DumpOptions(mentions=True)                 # 4 conexiones y GraphQL
PER_ITEM_REST = re.compile(r"/(pulls/\d+/(reviews|commits)|issues/\d+/"
                           r"timeline)")


def filler(number: int, day: int) -> dict:
    """Un issue sin más, como los de relleno de `standard_repo`."""
    return {"number": number, "title": f"Relleno {number}", "state": "open",
            "locked": False, "user": _user("ana"),
            "author_association": "OWNER",
            "created_at": f"2025-05-{day:02d}T10:00:00Z",
            "updated_at": f"2025-05-{day:02d}T10:00:00Z",
            "closed_at": None, "labels": [], "assignees": [],
            "milestone": None, "comments": 0, "body": f"Texto {number}",
            "reactions": {},
            "html_url": f"https://github.com/ana/app/issues/{number}"}


def numbered(path: str) -> int:
    """La página de una consulta (1 si no lleva «page»)."""
    return page_number("http://x" + path) or 1


class FastCase(Case):

    def rest_text(self, **kw) -> str:
        """El TXT de la descarga de siempre: REST y de una en una."""
        options = kw.pop("options", REST)
        texto = self.text(self.dump(self.tmp / "rest.txt", options=options,
                                    **kw))
        self.hub.requests.clear()
        self.hub.graphql_queries.clear()
        return texto

    def fast(self, **kw):
        kw.setdefault("options", FAST)
        return self.dump(self.tmp / "rapido.txt", **kw)

    def add_fillers(self, count: int, first: int = 8) -> None:
        for k in range(count):
            self.repo.issues.append(filler(first + k, 1 + k % 28))

    def issue_pages(self) -> List[int]:
        return [numbered(p) for p in self.hub.paths()
                if p.startswith("/repos/ana/app/issues?state=all")]

    def stages(self, prefix: str) -> Dict[str, str]:
        """{nombre: cursor} de lo que hay en el almacén (leído a mano,
        sin abrirlo con otra identidad, que lo vaciaría)."""
        ruta = ghstore.store_path("ana", "app", base=self.tmp / "almacen")
        db = sqlite3.connect(str(ruta))
        try:
            filas = db.execute("SELECT name, cursor, done FROM stages "
                               "WHERE name LIKE ?", (prefix + "%",))
            return {name: (cursor, bool(done)) for name, cursor, done in filas}
        finally:
            db.close()

    def serve_hook(self, condition, action) -> None:
        """`action()` la primera vez que se sirve algo que cumpla
        `condition(consulta)`."""
        hecho = threading.Event()

        def hook(what: str) -> None:
            if not hecho.is_set() and condition(what):
                hecho.set()
                action()
        self.hub.on_served = hook


# ------------------------------------------------------------------ #
#  El mismo TXT                                                       #
# ------------------------------------------------------------------ #
class SameTxtTests(FastCase):

    def test_graphql_and_pages_at_once_give_the_same_txt(self):
        base = self.rest_text()
        r = self.fast()
        self.assertEqual(self.text(r), base)
        self.assertFalse([p for p in self.hub.paths()
                          if PER_ITEM_REST.search(p)],
                         "revisiones, commits y menciones van por GraphQL")
        # UNA consulta: cada uno con todo lo suyo en un mismo alias.
        self.assertEqual(len(self.hub.graphql_queries), 1)
        self.assertEqual(r.counts, {"comment": 5, "code": 3, "event": 12,
                                    "review": 3, "commit": 3, "mention": 1})
        self.assertEqual(r.notice, "", "todo fue como debía")

    def test_histories_much_longer_than_their_mentions_stay_in_graphql(self):
        # El fallo de 3.6.0 con ghidra: en la historia de un issue, GitHub
        # cuenta en `totalCount` TODO (etiquetas, comentarios, cierres…)
        # aunque se pidan solo las menciones. Tomar eso por «no cabe»
        # mandaba casi todas las historias, una a una, por REST.
        def evento(numero: int, k: int) -> dict:
            return {"event": "labeled", "actor": _user("ana"),
                    "created_at": f"2025-01-{1 + k % 28:02d}T00:00:00Z",
                    "label": {"name": f"etiqueta {numero}-{k}"}}
        for numero, cuantos in ((1, 130), (3, 6), (5, 3)):
            self.repo.timeline[numero] = [evento(numero, k)
                                          for k in range(cuantos)] \
                + self.repo.timeline.get(numero, [])
        base = self.rest_text()
        r = self.fast()
        self.assertEqual(self.text(r), base)
        self.assertFalse([p for p in self.hub.paths()
                          if PER_ITEM_REST.search(p)],
                         "ninguna historia va uno a uno por REST")
        self.assertEqual(len(self.hub.graphql_queries), 1)
        self.assertEqual(r.notice, "")

    def test_queries_are_compact(self):
        # Los campos van una vez, en fragmentos: cada alias solo los
        # nombra. 50 issues y 50 PR con todo caben en pocos KB.
        from gitpush_studio.core.ghgraphql import BatchItem, build_query
        lote = [BatchItem(n, False, ("timeline",)) for n in range(1, 51)]
        lote += [BatchItem(n, True, ("reviews", "commits", "timeline"))
                 for n in range(51, 101)]
        consulta = build_query(lote)
        self.assertLess(len(consulta), 8000)
        self.assertEqual(consulta.count("fragment "), 4)
        # ¿Vino entera cada lista? `hasNextPage`, nunca `totalCount`.
        self.assertEqual(consulta.count("pageInfo { hasNextPage }"), 4)
        self.assertNotIn("totalCount", consulta)

    def test_only_what_is_asked_for(self):
        opciones = DumpOptions(commits=False, mentions=False)
        base = self.rest_text(options=DumpOptions(
            commits=False, mentions=False, workers=1, graphql=False))
        r = self.fast(options=opciones)
        self.assertEqual(self.text(r), base)
        self.assertEqual(len(self.hub.graphql_queries), 1)
        consulta = self.hub.graphql_queries[0]
        self.assertIn("reviews(first:", consulta)
        self.assertNotIn("commits(first:", consulta)
        self.assertNotIn("issueOrPullRequest", consulta)

    def test_bots_are_named_as_rest_names_them(self):
        self.repo.reviews[4] = [{
            "id": 305, "user": _user("renovate[bot]", "Bot"),
            "author_association": "NONE", "state": "COMMENTED",
            "body": "Revisión automática.",
            "submitted_at": "2025-03-01T11:00:00Z",
            "commit_id": "3333333cccc", "html_url": "x"}]
        self.repo.commits[4].append({
            "sha": "4444444dddd", "author": _user("dependabot[bot]", "Bot"),
            "commit": {"author": {
                "name": "dependabot[bot]",
                "email": "49699333+dependabot[bot]@users.noreply.github.com",
                "date": "2025-03-01T08:55:00Z"},
                "committer": {"name": "GitHub",
                              "date": "2025-03-01T08:55:00Z"},
                "message": "Sube la versión"}})
        self.repo.timeline[3] = [{
            "event": "cross-referenced",
            "actor": _user("github-actions[bot]", "Bot"),
            "created_at": "2025-02-02T00:00:00Z",
            "source": {"type": "issue", "issue": {
                "number": 4, "title": "Arreglo desde una bifurcación",
                "state": "closed", "pull_request": {"url": "x"},
                "repository": {"full_name": "ana/app"}}}}]
        base = self.rest_text()
        texto = self.text(self.fast())
        self.assertEqual(texto, base)
        self.assertIn("@renovate[bot] (bot)", texto)
        self.assertIn("@dependabot[bot] añadió el commit 4444444", texto)
        self.assertIn("@github-actions[bot] lo mencionó", texto)

    def test_what_does_not_fit_goes_by_rest(self):
        self.hub.graphql_cap = 1           # el PR 2 tiene 4 revisiones
        base = self.rest_text()
        r = self.fast()
        self.assertEqual(self.text(r), base)
        self.assertEqual(r.notice, "", "no caber no es un fallo")
        por_rest = [p for p in self.hub.paths() if PER_ITEM_REST.search(p)]
        self.assertIn("/pulls/2/reviews", " ".join(por_rest))
        self.assertIn("/pulls/2/commits", " ".join(por_rest))
        # Lo que sí cabía no se vuelve a pedir.
        self.assertNotIn("/pulls/4/", " ".join(por_rest))
        self.assertNotIn("/timeline", " ".join(por_rest))

    def test_a_batch_that_fails_is_split_and_the_rest_by_rest(self):
        base = self.rest_text()
        self.hub.fault("graphql_error", match="p4:", times=50)
        r = self.fast()
        self.assertEqual(self.text(r), base)
        # Uno que falla no es que GraphQL no funcione: sin aviso.
        self.assertEqual(r.notice, "")
        # [1..7] falla → [1,2,3] bien · [4..7] falla → [4,5] falla →
        # [4] falla y va por REST · [5] bien · [6,7] bien.
        con_el_4 = [q for q in self.hub.graphql_queries if "p4:" in q]
        self.assertEqual(len(con_el_4), 4)
        por_rest = " ".join(p for p in self.hub.paths()
                            if PER_ITEM_REST.search(p))
        for parte in ("/pulls/4/reviews", "/pulls/4/commits",
                      "/issues/4/timeline"):
            self.assertIn(parte, por_rest)
        self.assertNotIn("/pulls/2/", por_rest)
        self.assertNotIn("/issues/5/", por_rest)

    def test_a_server_error_is_retried_once_then_split(self):
        base = self.rest_text()
        self.hub.fault("error", match="p4:", times=50)
        self.assertEqual(self.text(self.fast()), base)
        # Cada 502 se repite UNA vez (no seis) antes de partir el lote.
        from collections import Counter
        veces = Counter(q for q in self.hub.graphql_queries if "p4:" in q)
        self.assertEqual(len(veces), 4)
        self.assertEqual(set(veces.values()), {2})

    def test_graphql_not_allowed_means_rest_at_once(self):
        base = self.rest_text()
        self.hub.fault("graphql_unauth", match="/graphql", times=99)
        r = self.fast()
        self.assertEqual(self.text(r), base)
        self.assertEqual(len(self.hub.graphql_queries), 1,
                         "ni se reintenta ni se parte")
        self.assertTrue([p for p in self.hub.paths()
                         if PER_ITEM_REST.search(p)])
        # Y se dice, con el porqué: va a ir mucho más lento.
        self.assertIn("GraphQL no se pudo usar", r.notice)
        self.assertIn("(401)", r.notice)
        self.assertIn("uno a uno por REST", r.notice)

    def test_a_repository_graphql_cannot_see_means_rest_at_once(self):
        base = self.rest_text()
        self.hub.fault("graphql_norepo", match="/graphql", times=99)
        r = self.fast()
        self.assertEqual(self.text(r), base)
        self.assertEqual(len(self.hub.graphql_queries), 1)
        self.assertIn("no ve este repositorio", r.notice)

    def test_lists_that_do_not_fit_never_make_graphql_give_up(self):
        # Muchos lotes seguidos en los que NADA cabe (cada issue tiene más
        # menciones de las que da una consulta) no son un fallo: GraphQL
        # sigue con los siguientes, que sí caben.
        self.hub.graphql_cap = 1
        largos = list(range(8, 24))        # 2 menciones: no caben
        cortos = list(range(24, 32))       # 1 mención: caben
        self.add_fillers(len(largos) + len(cortos), first=8)
        for numero in largos + cortos:
            veces = 2 if numero in largos else 1
            self.repo.timeline[numero] = [{
                "event": "cross-referenced", "actor": _user("luis"),
                "created_at": f"2025-06-{1 + k:02d}T00:00:00Z",
                "source": {"type": "issue", "issue": {
                    "number": 1, "title": "Login roto", "state": "open",
                    "repository": {"full_name": "ana/app"}}}}
                for k in range(veces)]
        base = self.rest_text()
        pequeno = ghgraphql.BatchSizer
        with mock.patch.object(ghgraphql, "BatchSizer",
                               lambda: pequeno(initial=4, minimum=2,
                                               maximum=4, max_aliases=4)):
            r = self.fast(options=DumpOptions(mentions=True, workers=1))
        self.assertEqual(self.text(r), base)
        self.assertEqual(r.notice, "")
        por_rest = " ".join(p for p in self.hub.paths()
                            if PER_ITEM_REST.search(p))
        for numero in largos:
            self.assertIn(f"/issues/{numero}/timeline", por_rest)
        for numero in cortos:
            self.assertNotIn(f"/issues/{numero}/timeline", por_rest)

    def test_graphql_that_fails_with_everything_is_left_with_a_notice(self):
        # Si GitHub falla con TODO lote tras lote, lo que queda va por REST
        # (sin seguir partiendo lotes para nada) y se dice por qué.
        self.add_fillers(40, first=8)
        base = self.rest_text()
        self.hub.fault("graphql_error", match="/graphql", times=10000)
        pequeno = ghgraphql.BatchSizer
        with mock.patch.object(ghgraphql, "BatchSizer",
                               lambda: pequeno(initial=4, minimum=2,
                                               maximum=4, max_aliases=4)):
            r = self.fast(options=DumpOptions(mentions=True, workers=1))
        self.assertEqual(self.text(r), base)
        self.assertIn("GraphQL no se pudo usar", r.notice)
        self.assertIn("lotes seguidos", r.notice)
        self.assertIn("Something went wrong", r.notice)
        # Tres lotes de 4 partidos hasta el final (7 consultas cada uno,
        # como mucho), no los 47 issues y PR.
        self.assertLessEqual(len(self.hub.graphql_queries), 3 * 7)

    def test_a_query_github_rejects_goes_by_rest_at_once(self):
        # Como GitHub: `state` sin alias en Issue y en PullRequest a la vez
        # es una consulta inválida (ni se ejecuta). Partir no arreglaría
        # nada: lo que falte va por REST a la primera.
        base = self.rest_text()
        roto = ghgraphql._TIMELINE.replace("issueState: state", "state") \
            .replace("prState: state", "state")
        with mock.patch.object(ghgraphql, "_TIMELINE", roto):
            r = self.fast()
        self.assertEqual(self.text(r), base)
        self.assertIn("no acepta la consulta", r.notice)
        menciones = [q for q in self.hub.graphql_queries
                     if "issueOrPullRequest" in q]
        self.assertEqual(len(menciones), 1, "ni se parte ni se repite")
        self.assertIn("/issues/1/timeline", " ".join(self.hub.paths()))

    def test_without_a_token_never_graphql(self):
        base = self.rest_text()
        self.assertEqual(self.text(self.fast(token="")), base)
        self.assertEqual(self.hub.graphql_queries, [])
        self.assertFalse([p for p in self.hub.paths() if "graphql" in p])

    def test_the_graphql_quota_is_waited_for(self):
        base = self.rest_text()
        self.hub.fault("graphql_rate", match="/graphql", seconds=40)
        avisos = []
        r = self.fast(on_wait=lambda why, until: avisos.append(why))
        self.assertEqual(self.text(r), base)
        self.assertTrue(any("agotó las consultas" in a for a in avisos),
                        avisos)
        self.assertTrue(self.slept and 30 <= max(self.slept) <= 45,
                        self.slept)


# ------------------------------------------------------------------ #
#  Páginas a la vez                                                   #
# ------------------------------------------------------------------ #
class PagesAtOnceTests(FastCase):

    def test_pages_go_at_once_and_each_one_once(self):
        self.add_fillers(30)                # 37 issues: 19 páginas de 2
        base = self.rest_text()
        for n in range(2, 20):
            self.hub.delay(f"direction=asc&per_page=100&page={n}", 0.05)
        self.assertEqual(self.text(self.fast()), base)
        paginas = self.issue_pages()
        # Cada página una vez; la última, dos (la cola).
        self.assertEqual(sorted(paginas), list(range(1, 20)) + [19])
        self.assertGreaterEqual(self.hub.max_active, 3)

    def test_new_issues_while_downloading_are_not_lost(self):
        self.add_fillers(12)                # 19 issues: 10 páginas
        nuevos = [filler(100 + k, 28) for k in range(3)]
        self.serve_hook(
            lambda what: what.startswith("/repos/ana/app/issues?state=all")
            and numbered(what) == 3,
            lambda: self.repo.issues.extend(nuevos))
        texto = self.text(self.fast())
        for n in (100, 101, 102):
            self.assertIn(f"ISSUE #{n} ·", texto)
        self.hub.on_served = None
        self.assertEqual(texto, self.rest_text())

    def test_events_newest_first_with_news_in_between_miss_nothing(self):
        # Como en GitHub: los eventos del más reciente al más antiguo, por
        # número de página. A mitad, llegan tres novedades (que corren
        # todo tres puestos) y la página 3 se sirve DESPUÉS que la 4.
        self.repo.events_paged = True
        base = self.rest_text()
        self.hub.delay("events?per_page=100&page=3", 0.4)

        def novedades():
            for k in range(3):
                self.repo.events.append({
                    "id": 900 + k, "event": "subscribed",
                    "actor": _user("eva"), "commit_id": None,
                    "created_at": "2025-06-01T10:00:00Z",
                    "issue": {"number": 1, "title": "x"}})
        self.serve_hook(lambda what: "/issues/events?" in what
                        and numbered(what) == 4, novedades)
        self.assertEqual(self.text(self.fast()), base,
                         "ningún evento de antes se pierde")
        # Se notó y la tanda se volvió a pedir en orden.
        de_eventos = [numbered(p) for p in self.hub.paths()
                      if "/issues/events?per_page=100" in p]
        self.assertGreater(de_eventos.count(3), 1)

    def test_a_very_busy_repository_goes_one_by_one(self):
        # Novedades con CADA página servida: tras dos tandas estropeadas,
        # el resto de los eventos va en orden, sin más comprobaciones (en
        # paralelo se repetiría casi todo).
        self.repo.events_paged = True
        for k in range(40):
            self.repo.events.append({
                "id": 500 + k, "event": "labeled", "actor": _user("ana"),
                "label": {"name": f"e{k}"}, "commit_id": None,
                "created_at": f"2025-04-{1 + k % 28:02d}T09:00:00Z",
                "issue": {"number": 5 + k % 3, "title": "x"}})
        base = self.rest_text()
        siguiente = [1000]

        def novedad(what: str) -> None:
            if "/issues/events?per_page=100" in what:
                siguiente[0] += 1
                self.repo.events.append({
                    "id": siguiente[0], "event": "subscribed",
                    "actor": _user("eva"), "commit_id": None,
                    "created_at": "2025-06-01T10:00:00Z",
                    "issue": {"number": 1, "title": "x"}})
        self.hub.on_served = novedad
        # Con 2 conexiones, tandas de 8 páginas (hay 27).
        self.assertEqual(self.text(self.fast(options=DumpOptions(
            mentions=True, workers=2))), base)
        sondas = [p for p in self.hub.paths()
                  if "/issues/events?per_page=1" in p
                  and "per_page=100" not in p]
        # La inicial, tras la 1.ª tanda, otra tras rehacerla y tras la 2.ª.
        self.assertEqual(len(sondas), 4)

    def test_events_newest_first_without_news_go_at_once(self):
        self.repo.events_paged = True
        base = self.rest_text()
        self.assertEqual(self.text(self.fast()), base)
        de_eventos = [numbered(p) for p in self.hub.paths()
                      if "/issues/events?per_page=100" in p]
        self.assertEqual(sorted(de_eventos), list(range(1, 8)) + [7])
        sondas = [p for p in self.hub.paths()
                  if "/issues/events?per_page=1" in p
                  and "per_page=100" not in p]
        self.assertEqual(len(sondas), 2, "antes y después de la tanda")

    def test_stop_in_the_middle_and_resume(self):
        self.add_fillers(30)                # 19 páginas
        base = self.rest_text()
        cancel = threading.Event()
        # La 5 llega tarde: otras seis (de la 2 a la 9, sin ella) llegan
        # antes, y entonces se pulsa «Detener». Lo completo desde el
        # principio es solo hasta la 4.
        self.hub.delay("direction=asc&per_page=100&page=5", 0.8)
        guardar = ghstore.Store.save
        paginas_guardadas = []

        def contar(almacen, stage, *args, **kw):
            guardar(almacen, stage, *args, **kw)
            if stage == "issues" and kw.get("items"):
                paginas_guardadas.append(kw.get("cursor"))
                # La página 1 (en orden) y seis más a la vez.
                if len(paginas_guardadas) == 1 + 6:
                    cancel.set()
        with mock.patch.object(ghstore.Store, "save", contar), \
                self.assertRaises(Cancelled):
            self.fast(cancel=cancel)
        cursor, done = self.stages("issues")["issues"]
        self.assertFalse(done)
        self.assertTrue(cursor.startswith(PARALLEL_PREFIX), cursor)
        primera, _ultima = parse_parallel_cursor(cursor)
        self.assertEqual(primera, 5, "la 5 no llegó: el cursor no la salta")
        self.hub.delays.clear()
        self.hub.on_served = None
        self.hub.requests.clear()
        r = self.fast()
        self.assertTrue(r.resumed)
        self.assertEqual(self.text(r), base)
        paginas = self.issue_pages()
        self.assertEqual(min(paginas), 4,
                         "se sigue desde la anterior a la que faltaba")
        self.assertNotIn(3, paginas, "lo completo no se vuelve a pedir")

    def test_a_last_page_on_another_server_is_refused(self):
        self.hub.fault("foreign_last", "/issues?")
        with self.assertRaises(StepError) as e:
            self.fast()
        self.assertIn("no la sigo", str(e.exception))
        self.assertFalse(self.issue_pages()[1:],
                         "ni una página más tras ese enlace")


# ------------------------------------------------------------------ #
#  Los límites de GitHub, para todas las conexiones                   #
# ------------------------------------------------------------------ #
class SharedLimitsTests(Case):

    def two_sessions(self, shared: bool):
        comunes = SharedLimits()
        esperas = {"a": [], "b": []}
        sesiones = []
        for nombre in ("a", "b"):
            sesion = ApiSession(token=self.token, root=self.hub.root,
                                sleep=esperas[nombre].append,
                                limits=comunes if shared else None)
            self.addCleanup(sesion.close)
            sesiones.append(sesion)
        return sesiones, esperas

    def test_slow_down_reaches_every_connection(self):
        (a, b), esperas = self.two_sessions(shared=True)
        self.hub.fault("secondary", "/repos/ana/app/issues", after=7)
        a.get("/repos/ana/app/issues")
        antes = len(self.hub.requests)
        b.get("/repos/ana/app/pulls")
        self.assertEqual(esperas["a"], [7.0])
        self.assertEqual(len(esperas["b"]), 1, "la otra también espera")
        self.assertGreater(esperas["b"][0], 6)
        self.assertEqual(len(self.hub.requests), antes + 1,
                         "y no lanza nada que GitHub fuera a rechazar")
        b.get("/repos/ana/app/pulls")
        self.assertEqual(len(esperas["b"]), 1, "solo una vez")

    def test_an_exhausted_quota_stops_every_connection(self):
        (a, b), esperas = self.two_sessions(shared=True)
        self.hub.fault("last", "/repos/ana/app/issues", seconds=30)
        a.get("/repos/ana/app/issues")
        self.assertEqual(a.quota("core")[0], 0)
        b.get("/repos/ana/app/pulls")
        self.assertEqual(len(esperas["b"]), 1)
        self.assertTrue(25 <= esperas["b"][0] <= 35, esperas)
        self.assertEqual(b.quota("core")[0], 5000)

    def test_separate_sessions_know_nothing_of_each_other(self):
        (a, b), esperas = self.two_sessions(shared=False)
        self.hub.fault("secondary", "/repos/ana/app/issues", after=7)
        a.get("/repos/ana/app/issues")
        b.get("/repos/ana/app/pulls")
        self.assertEqual(esperas["b"], [])

    def test_the_connections_of_a_download_share_what_they_know(self):
        from gitpush_studio.core.ghpool import make_pool
        pool = make_pool(self.token, self.hub.root, None, workers=3)
        try:
            aqui = pool.session().limits
            otras = {id(r) for _n, r in pool.run(
                lambda session, n: (time.sleep(0.05), session.limits)[1],
                range(6))}
        finally:
            pool.close()
        self.assertEqual(otras, {id(aqui)})

    def test_the_graphql_quota_does_not_stop_rest(self):
        comunes = SharedLimits()
        comunes.update({"x-ratelimit-resource": "graphql",
                        "x-ratelimit-remaining": "0",
                        "x-ratelimit-reset": str(int(time.time()) + 600)})
        esperas = []
        s = ApiSession(token=self.token, root=self.hub.root,
                       sleep=esperas.append, limits=comunes)
        self.addCleanup(s.close)
        s.get("/repos/ana/app/issues")
        self.assertEqual(esperas, [], "REST tiene su propia cuota")


# ------------------------------------------------------------------ #
#  Seguir una descarga de la versión anterior                         #
# ------------------------------------------------------------------ #
class OldStoreTests(FastCase):

    def test_a_rest_download_half_done_goes_on_by_graphql(self):
        base = self.rest_text()
        cancel = threading.Event()
        self.serve_hook(lambda what: "/pulls/4/reviews" in what, cancel.set)
        with self.assertRaises(Cancelled):
            self.dump(self.tmp / "viejo.txt", options=REST, cancel=cancel)
        hechos = {n for n, (_c, d) in self.stages("").items()
                  if d and ":" in n}
        self.assertEqual(hechos, {"reviews:2", "reviews:4"})
        self.hub.on_served = None
        self.hub.requests.clear()
        self.hub.graphql_queries.clear()
        r = self.fast()
        self.assertTrue(r.resumed)
        self.assertEqual(self.text(r), base)
        paths = self.hub.paths()
        self.assertFalse([p for p in paths if "/issues?state=all" in p],
                         "los listados ya estaban")
        self.assertFalse([p for p in paths if PER_ITEM_REST.search(p)])
        de_pulls = [q for q in self.hub.graphql_queries if "pullRequest(" in q]
        self.assertEqual(len(de_pulls), 1)
        self.assertIn("commits(first:", de_pulls[0])
        self.assertNotIn("reviews(first:", de_pulls[0],
                         "las revisiones ya estaban")

    def test_what_was_done_is_never_asked_again(self):
        # Por REST, de uno en uno, hasta tener las revisiones y los
        # commits de los dos PR; al seguir por GraphQL solo faltan las
        # menciones: ni revisiones ni commits otra vez.
        base = self.rest_text()
        cancel = threading.Event()
        self.serve_hook(lambda what: "/pulls/4/commits" in what, cancel.set)
        with self.assertRaises(Cancelled):
            self.dump(self.tmp / "viejo.txt", options=REST, cancel=cancel)
        hechos = {n for n, (_c, d) in self.stages("").items()
                  if d and n.startswith(("reviews:", "commits:"))}
        self.assertEqual(hechos, {"reviews:2", "reviews:4", "commits:2",
                                  "commits:4"})
        self.hub.on_served = None
        self.hub.graphql_queries.clear()
        r = self.fast()
        self.assertEqual(self.text(r), base)
        for consulta in self.hub.graphql_queries:
            self.assertNotIn("...Cm", consulta, "los commits ya estaban")
            self.assertNotIn("...Rv", consulta, "las revisiones también")


# ------------------------------------------------------------------ #
#  Un listado, paso a paso (sin red)                                  #
# ------------------------------------------------------------------ #
class ListingStepsTests(unittest.TestCase):
    """El listado de eventos (lo más reciente primero) movido a mano:
    qué pide y qué cursor deja en cada momento."""

    LAST = "https://api.github.com/repos/a/b/issues/events?per_page=100&page=20"

    def setUp(self):
        from gitpush_studio.core.ghfetch import (LISTINGS, ListingSource,
                                                 _Tracker)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = ghstore.Store(self.tmp / "a.sqlite3", {"format": 1})
        self.addCleanup(self.store.close)

        class _Contador:
            requests = 0
        tracker = _Tracker(_Contador(), self.store, None)
        eventos = [l for l in LISTINGS if l.name == "events"][0]
        self.source = ListingSource(eventos, self.store, "/repos/a/b",
                                    tracker, workers=2)

    def page(self, number: int, last_page: int = 20):
        from gitpush_studio.core.ghsession import Page
        url = page_url(self.LAST, number)
        siguiente = page_url(self.LAST, number + 1) \
            if number < last_page else ""
        return Page(url=url, items=[], next_url=siguiente,
                    last_page=last_page, last_url=self.LAST), {"entries": []}

    def answer(self, result):
        """La siguiente consulta (una sola) y su respuesta."""
        task = self.source.next_task()
        self.assertIsNotNone(task)
        self.source.on_result(task, result)
        return task

    def cursor(self):
        return self.store.stage("events")[0]

    def batch(self, numbers):
        tareas = []
        for _ in numbers:
            tarea = self.source.next_task()
            tareas.append(tarea)
            self.source.running += 1
        self.assertIsNone(self.source.next_task(), "la tanda entera, y nada más")
        for tarea in tareas:
            self.source.running -= 1
            self.source.on_result(tarea, self.page(int(tarea.data[1])))
        return [int(t.data[1]) for t in tareas]

    def test_a_batch_moves_the_cursor_exactly_past_itself(self):
        self.answer(self.page(1))                 # en orden: la primera
        self.assertEqual(self.answer("H").data[0], "sonda")
        self.assertEqual(self.batch(range(8)), list(range(2, 10)))
        self.assertEqual(self.cursor(), parallel_cursor(2, self.LAST),
                         "sin comprobar, no avanza")
        self.answer("H")                          # nada cambió
        self.assertEqual(self.cursor(), parallel_cursor(10, self.LAST))
        self.assertEqual(self.batch(range(8)), list(range(10, 18)))
        self.answer("H")
        self.assertEqual(self.cursor(), parallel_cursor(18, self.LAST))

    def test_news_in_between_redo_the_batch_in_order(self):
        self.answer(self.page(1))
        self.answer("H")
        self.batch(range(8))
        self.answer("NUEVO")                      # llegó algo delante
        self.assertEqual(self.cursor(), parallel_cursor(2, self.LAST))
        for esperada in range(2, 10):             # otra vez, de una en una
            tarea = self.answer(self.page(esperada))
            self.assertEqual(int(tarea.data[1]), esperada)
            self.assertEqual(self.cursor(),
                             parallel_cursor(esperada + 1, self.LAST))
        self.assertEqual(self.source.next_task().data[0], "sonda",
                         "y se vuelve a mirar lo más reciente")


# ------------------------------------------------------------------ #
#  Piezas                                                             #
# ------------------------------------------------------------------ #
class PiecesTests(unittest.TestCase):

    def test_page_url(self):
        ultima = ("https://api.github.com/repositories/1/issues?state=all"
                  "&per_page=100&page=86")
        url = page_url(ultima, 7)
        self.assertTrue(url.startswith("https://api.github.com/"
                                       "repositories/1/issues?"))
        self.assertIn("state=all", url)
        self.assertIn("per_page=100", url)
        self.assertEqual(page_number(url), 7)
        self.assertNotIn("page=86", url)

    def test_parallel_cursor(self):
        ultima = "https://api.github.com/x?page=9"
        self.assertEqual(parse_parallel_cursor(parallel_cursor(4, ultima)),
                         (4, ultima))
        for roto in ("", "https://api.github.com/x?page=2", "paralelo:",
                     "paralelo:x|" + ultima, "paralelo:3",
                     "paralelo:3|https://api.github.com/x"):
            self.assertIsNone(parse_parallel_cursor(roto), roto)

    def test_bots_and_states_as_rest(self):
        self.assertEqual(ghgraphql._person({"login": "dependabot",
                                            "__typename": "Bot"}),
                         {"login": "dependabot[bot]", "type": "Bot"})
        self.assertEqual(ghgraphql._person({"login": "ana",
                                            "__typename": "User"}),
                         {"login": "ana", "type": "User"})
        self.assertEqual(ghgraphql._person({"login": "acme",
                                            "__typename": "Organization"}),
                         {"login": "acme", "type": "Organization"})
        self.assertIsNone(ghgraphql._person(None))
        self.assertIsNone(ghgraphql._person({"login": ""}))
        bot = ghgraphql._commit_author({
            "user": None,
            "email": "49699333+dependabot[bot]@users.noreply.github.com"})
        self.assertEqual(bot, {"login": "dependabot[bot]", "type": "Bot"})
        self.assertIsNone(ghgraphql._commit_author(
            {"user": None, "email": "ana@ejemplo.org"}))
        self.assertIsNone(ghgraphql._commit_author(
            {"user": None, "email": "1+ana@users.noreply.github.com"}))
        _tipo, _id, _n, _at, datos = ghgraphql.mention_from(1, {
            "createdAt": "2025-01-01T00:00:00Z",
            "actor": {"login": "ana", "__typename": "User"},
            "source": {"__typename": "PullRequest", "number": 2,
                       "title": "t", "prState": "MERGED",
                       "repository": {"nameWithOwner": "ana/app"}}})
        self.assertEqual(datos["source_state"], "closed")
        self.assertTrue(datos["source_is_pr"])

    def test_what_does_not_fit_is_marked(self):
        from gitpush_studio.core.ghgraphql import BatchItem, items_results
        hay_mas = {"hasNextPage": True}
        entera = {"hasNextPage": False}
        datos = {"p1": {"reviews": {"pageInfo": hay_mas,
                                    "nodes": [{}] * 100},
                        "commits": {"pageInfo": entera, "nodes": []},
                        "timelineItems": {"pageInfo": entera, "nodes": []}},
                 "p2": None,
                 "n3": {"__typename": "Issue",
                        "timelineItems": {"pageInfo": hay_mas,
                                          "nodes": [{}] * 100}}}
        lote = [BatchItem(1, True, ("reviews", "commits", "timeline")),
                BatchItem(2, True, ("reviews",)),
                BatchItem(3, False, ("timeline",))]
        resultados = items_results(datos, lote)
        self.assertEqual(resultados[1].fallback, {"reviews"})
        self.assertEqual(resultados[2].fallback, set())
        self.assertEqual(resultados[2].entries, [])
        self.assertEqual(resultados[3].fallback, {"timeline"})
        # No caber no es fallar.
        self.assertFalse(any(r.failed for r in resultados.values()))

    def test_completeness_comes_from_has_next_page(self):
        # GitHub cuenta en `totalCount` de la historia TODOS sus eventos
        # aunque se pidan solo las menciones: 150 con 3 menciones es una
        # lista ENTERA si no hay página siguiente. Sin esa señal clara,
        # nada se da por entero (mejor pedirlo otra vez por REST).
        from gitpush_studio.core.ghgraphql import (BatchItem, complete,
                                                   items_results)

        def mencion(numero: int) -> dict:
            return {"createdAt": "2025-01-01T00:00:00Z",
                    "actor": {"login": "ana", "__typename": "User"},
                    "source": {"__typename": "Issue", "number": numero,
                               "title": "t", "issueState": "OPEN",
                               "repository": {"nameWithOwner": "ana/app"}}}
        datos = {
            "n3": {"__typename": "Issue", "timelineItems": {
                "totalCount": 150, "pageInfo": {"hasNextPage": False},
                "nodes": [mencion(9), mencion(10), mencion(11)]}},
            "n4": {"__typename": "Issue", "timelineItems": {
                "totalCount": 0, "nodes": []}},
            "n5": {"__typename": "Issue", "timelineItems": {
                "pageInfo": {}, "nodes": []}},
            "n6": {"__typename": "Issue", "timelineItems": {
                "pageInfo": {"hasNextPage": None}, "nodes": []}},
        }
        lote = [BatchItem(n, False, ("timeline",)) for n in (3, 4, 5, 6)]
        resultados = items_results(datos, lote)
        self.assertEqual(resultados[3].fallback, set())
        self.assertEqual(len(resultados[3].entries), 3)
        for numero in (4, 5, 6):
            self.assertEqual(resultados[numero].fallback, {"timeline"})
            self.assertEqual(resultados[numero].entries, [])
        self.assertTrue(complete({"pageInfo": {"hasNextPage": False}}))
        self.assertFalse(complete({"pageInfo": {"hasNextPage": True}}))
        self.assertFalse(complete({"pageInfo": {"hasNextPage": "false"}}))
        self.assertFalse(complete({"pageInfo": None}))
        self.assertFalse(complete({}))
        self.assertFalse(complete(None))

    def test_only_a_real_failure_counts_as_failed(self):
        # Un elemento que GraphQL no puede traer ni solo queda «fallido»
        # (con el error de GitHub, para explicarlo); un lote que fue bien,
        # no, aunque algo no cupiera.
        from gitpush_studio.core.ghgraphql import BatchItem, fetch_items
        item = BatchItem(7, False, ("timeline",))
        with mock.patch.object(ghgraphql, "run_query",
                               side_effect=ghgraphql.GraphQLBatchError(
                                   "GraphQL respondió 502.")):
            outcome = fetch_items(None, "ana", "app", [item])
        self.assertTrue(outcome.results[7].failed)
        self.assertEqual(outcome.results[7].fallback, {"timeline"})
        self.assertEqual(outcome.error, "GraphQL respondió 502.")
        # Partido en dos y fallando cada mitad: lo que dijo GitHub llega
        # hasta arriba (es lo que explica el aviso si se deja GraphQL).
        otro = BatchItem(8, False, ("timeline",))
        with mock.patch.object(ghgraphql, "run_query",
                               side_effect=ghgraphql.GraphQLBatchError(
                                   "GraphQL respondió 502.")):
            outcome = fetch_items(None, "ana", "app", [item, otro])
        self.assertTrue(outcome.split)
        self.assertTrue(all(r.failed for r in outcome.results.values()))
        self.assertEqual(outcome.error, "GraphQL respondió 502.")
        with mock.patch.object(ghgraphql, "run_query", return_value={
                "n7": {"__typename": "Issue", "timelineItems": {
                    "pageInfo": {"hasNextPage": True}, "nodes": []}}}):
            outcome = fetch_items(None, "ana", "app", [item])
        self.assertFalse(outcome.results[7].failed)
        self.assertEqual(outcome.error, "")

    def test_only_failures_in_a_row_make_graphql_give_up(self):
        # Fallos sueltos (GitHub tiene un mal momento) no hacen dejar
        # GraphQL para el resto de una descarga larga: solo
        # MAX_FAILED_BATCHES lotes SEGUIDOS en los que falla todo.
        from collections import deque
        from gitpush_studio.core import ghfetch
        from gitpush_studio.core.ghgraphql import (BatchItem, BatchOutcome,
                                                   ItemResult)
        from gitpush_studio.core.ghsched import Task

        class Apunta:
            def __init__(self):
                self.recibido = []

            def add(self, cosas):
                self.recibido.extend(cosas)

            def save_many(self, etapas, entradas):
                pass

        rest = Apunta()
        fuente = ghfetch.GraphQLSource(Apunta(), "ana", "app",
                                       ghfetch.FetchOptions(mentions=True),
                                       Apunta(), rest, [])
        fuente._queue = deque([BatchItem(99, False, ("timeline",))])

        def lote(numero: int, fallo: bool) -> None:
            item = BatchItem(numero, False, ("timeline",))
            resultado = ItemResult(fallback={"timeline"}, failed=fallo)
            salida = BatchOutcome({numero: resultado}, split=fallo,
                                  error="GraphQL respondió 502."
                                  if fallo else "")
            fuente.on_result(Task(fuente, None, [item]), (salida, 0.1))

        tope = ghfetch.MAX_FAILED_BATCHES
        for numero in range(1, tope):
            lote(numero, fallo=True)
        lote(10, fallo=False)          # uno bien (aunque no cupiera)
        for numero in range(11, 11 + tope - 1):
            lote(numero, fallo=True)
        self.assertFalse(fuente._unavailable)
        self.assertEqual(fuente.notice, "")
        lote(50, fallo=True)           # el tercero seguido
        self.assertTrue(fuente._unavailable)
        self.assertIn("GraphQL respondió 502", fuente.notice)
        # Lo que quedaba en la cola va por REST, entero.
        self.assertIn(("timeline", 99), rest.recibido)

    def test_entries_without_an_id_never_collide(self):
        # GitHub siempre manda el id; si faltara, dos entradas distintas
        # no deben pisarse en el almacén (una desaparecería del TXT).
        from gitpush_studio.core.ghrecords import comment_entry, review_entry
        base = {"issue_url": "https://api.github.com/repos/ana/app/issues/1",
                "user": {"login": "ana"}, "created_at": "2025-01-01T00:00:00Z"}
        uno = comment_entry(dict(base, body="uno"))
        dos = comment_entry(dict(base, body="dos"))
        self.assertNotEqual(uno[1], dos[1])
        self.assertEqual(comment_entry(dict(base, id=7, body="x"))[1], "7")
        revision = {"submitted_at": "2025-01-01T00:00:00Z",
                    "state": "APPROVED", "user": {"login": "ana"}}
        self.assertNotEqual(review_entry(2, dict(revision, body="a"))[1],
                            review_entry(2, dict(revision, body="b"))[1])

    def test_batch_sizes_follow_how_long_github_takes(self):
        from collections import deque
        from gitpush_studio.core.ghgraphql import BatchItem, BatchSizer
        tamano = BatchSizer(initial=60, target=3.0, maximum=300)
        cola = deque(BatchItem(n, n % 2 == 0, ("reviews", "commits",
                                               "timeline")
                               if n % 2 == 0 else ("timeline",))
                     for n in range(1, 400))
        lote = tamano.take(cola)
        self.assertLessEqual(sum(i.units for i in lote), 60)
        self.assertGreater(sum(i.units for i in lote), 55)
        # Rápido: crece, pero nunca más del doble de una vez.
        tamano.observe(60, 0.6, split=False)
        self.assertEqual(tamano.units, 120)
        tamano.observe(120, 1.2, split=False)
        self.assertEqual(tamano.units, 240)
        # Lento: baja hacia lo que cabe en ~3 s.
        tamano.observe(240, 24.0, split=False)
        self.assertLess(tamano.units, 240)
        # Hubo que partir: la mitad.
        antes = tamano.units
        tamano.observe(antes, 1.0, split=True)
        self.assertEqual(tamano.units, max(8, antes // 2))
        # Nunca más de 100 elementos por consulta.
        tamano.units = 300
        lote = tamano.take(deque(BatchItem(n, False, ("timeline",))
                                 for n in range(1, 1000)))
        self.assertEqual(len(lote), 100)

    def test_pool_runs_at_once_and_stops_on_error(self):
        hilos = set()
        en_curso = []
        maximo = [0]
        candado = threading.Lock()

        def tarea(_session, n):
            with candado:
                en_curso.append(n)
                maximo[0] = max(maximo[0], len(en_curso))
            time.sleep(0.05)
            with candado:
                en_curso.remove(n)
            hilos.add(threading.get_ident())
            if n == 5:
                raise StepError("falla la cinco")
            return n * 10

        pool = SessionPool(lambda cancel: _Dummy(), workers=4)
        try:
            vistos = []
            with self.assertRaises(StepError):
                for n, r in pool.run(tarea, range(1, 40)):
                    vistos.append(r)
            self.assertGreaterEqual(maximo[0], 2)
            self.assertGreaterEqual(len(hilos), 2, "en varios hilos")
            self.assertNotIn(threading.get_ident(), hilos)
            self.assertLess(len(vistos), 38, "las demás se paran")
            # Tras un error, el siguiente `run` funciona.
            self.assertEqual(sorted(r for _n, r in pool.run(
                lambda s, n: n, [1, 2, 3])), [1, 2, 3])
        finally:
            pool.close()

    def test_a_single_worker_runs_here(self):
        aqui = threading.get_ident()
        pool = SessionPool(lambda cancel: _Dummy(), workers=1)
        try:
            hilos = {r for _n, r in pool.run(
                lambda s, n: threading.get_ident(), range(5))}
        finally:
            pool.close()
        self.assertEqual(hilos, {aqui})

    def test_stop_reaches_every_worker(self):
        cancel = threading.Event()
        pool = SessionPool(lambda combined: _Dummy(combined), workers=3,
                           cancel=cancel)

        def espera(session, n):
            if n == 0:
                cancel.set()
            session.cancel.wait(5)
            session.check_cancel()
            return n
        inicio = time.monotonic()
        try:
            with self.assertRaises(Cancelled):
                list(pool.run(espera, range(10)))
        finally:
            pool.close()
        self.assertLess(time.monotonic() - inicio, 3)

    def test_any_event(self):
        a, b = threading.Event(), threading.Event()
        juntos = AnyEvent(a, b)
        self.assertFalse(juntos.is_set())
        self.assertFalse(juntos.wait(0.01))
        b.set()
        self.assertTrue(juntos.is_set())
        self.assertTrue(juntos.wait(1))


class _Dummy:
    """Una «sesión» para probar el reparto sin red."""

    def __init__(self, cancel=None) -> None:
        self.cancel = cancel or threading.Event()
        self.requests = 0
        self.connections = 0

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled("Descarga detenida.")

    def close(self) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
