# -*- coding: utf-8 -*-
"""El volcado de issues y pull requests a un TXT.

Contra un GitHub falso (`tests.fakegithub`), se exige:

  · la sesión: una sola conexión reutilizada, gzip con tope (una bomba
    comprimida no entra), paginación por «Link» (por página y por
    cursor), el token SOLO a la API (ni a una redirección ni a un
    «siguiente» de otro servidor), esperas por la cuota y por el límite
    secundario, reintentos, «Detener» durante una espera y el túnel por
    el proxy del sistema;
  · la descarga: todo lo que hay, y nada de lo que no es conversación;
    reanudar tras detenerse sin perder ni repetir nada (el TXT sale
    idéntico al de una descarga de un tirón); los issues desactivados;
    un commit que está en dos pull requests, o un comentario que menciona
    a dos issues a la vez, sale en LOS DOS (antes de 3.6.1 uno de los dos
    lo perdía); un almacén a medias del formato anterior se pone al día
    sin perder lo que estaba bien;
  · el TXT: inequívoco (lo que escribió alguien nunca empieza en la
    primera columna), sin caracteres de control, con las horas en la
    zona pedida, los hilos de código dentro de su revisión, los secretos
    tachados si se pide y una última línea que dice que está completo;
  · el destino: nunca pisa un archivo que no sea un historial nuestro,
    no sigue enlaces y un «Detener» lo deja como estaba.
"""

import base64
import json
import os
import re
import sqlite3
import shutil
import tempfile
import threading
import tracemalloc
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.fakegithub import (ConnectProxy, FakeGitHub, _user,
                              standard_repo, token_like)

from gitpush_studio.core import ghsession, ghstore
from gitpush_studio.core.errors import Cancelled, StepError
from gitpush_studio.core.ghdump import (DumpOptions, dump_repository,
                                        free_output_path, is_dump,
                                        parse_repository, partial_download)
from gitpush_studio.core.ghrender import (FOOTER_PREFIX, RULE, THIN,
                                          TITLE_PREFIX, clean)
from gitpush_studio.core.ghsession import (ApiSession, link_targets,
                                           page_number, system_proxy)
from gitpush_studio.core.redact import MASK

LIMA = timezone(timedelta(hours=-5))
NOW = datetime(2026, 11, 6, 14, 30, tzinfo=timezone.utc)


class Case(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.hub = FakeGitHub()
        self.addCleanup(self.hub.close)
        self.secret = token_like()
        self.repo = standard_repo(self.hub, secret=self.secret)
        self.token = token_like("ghp_")[:-4] + "TEST"
        self.slept = []
        self.waits = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def session(self, **kw):
        kw.setdefault("token", self.token)
        kw.setdefault("sleep", self.slept.append)
        kw.setdefault("on_wait", lambda why, until: self.waits.append(why))
        return ApiSession(root=self.hub.root, **kw)

    def dump(self, out=None, **kw):
        options = kw.pop("options", DumpOptions(mentions=True))
        out = out or self.tmp / "historial.txt"
        kw.setdefault("token", self.token)
        kw.setdefault("sleep", self.slept.append)
        return dump_repository("ana/app", str(out), options=options,
                               api_root=self.hub.root,
                               store_dir=self.tmp / "almacen", now=NOW,
                               tz=LIMA, **kw)

    def text(self, result):
        return Path(result.path).read_text(encoding="utf-8")


# ------------------------------------------------------------------ #
#  La sesión                                                          #
# ------------------------------------------------------------------ #
class SessionTests(Case):

    def test_one_connection_gzip_and_both_kinds_of_pages(self):
        with self.session() as s:
            issues = [i for p in s.pages("/repos/ana/app/issues",
                                         {"state": "all"}) for i in p.items]
            events = [e for p in s.pages("/repos/ana/app/issues/events")
                      for e in p.items]
        self.assertEqual([i["number"] for i in issues], list(range(1, 8)))
        self.assertEqual(len(events), len(self.repo.events))
        self.assertEqual(self.hub.connections, 1)
        for _path, headers in self.hub.requests:
            self.assertIn("gzip", headers.get("accept-encoding", ""))
            self.assertEqual(headers.get("authorization"),
                             "Bearer " + self.token)

    def test_pages_know_the_last_one(self):
        with self.session() as s:
            first = next(s.pages("/repos/ana/app/issues", {"state": "all"}))
        self.assertEqual(first.last_page, 4)
        self.assertEqual(page_number(first.next_url), 2)

    def test_a_next_page_on_another_server_is_refused(self):
        self.hub.fault("foreign_next", "/issues?")
        with self.session() as s, self.assertRaises(StepError) as e:
            list(s.pages("/repos/ana/app/issues", {"state": "all"}))
        self.assertIn("no la sigo", str(e.exception))
        self.assertEqual(len(self.hub.requests), 1)

    def test_redirects_only_inside_the_api(self):
        self.hub.fault("redirect", "/repos/ana/app",
                       to="/repos/ana/app?movido=1")
        with self.session() as s:
            r = s.get("/repos/ana/app")
        self.assertTrue(r.ok)
        self.assertEqual(r.data["full_name"], "ana/app")
        self.hub.fault("redirect", "/repos/ana/app",
                       to="https://evil.example/robar")
        with self.session() as s, self.assertRaises(StepError):
            s.get("/repos/ana/app")
        for path, _headers in self.hub.requests:
            self.assertTrue(path.startswith("/repos/"), path)

    def test_waits_for_the_quota_and_then_goes_on(self):
        self.hub.fault("rate", "/repos/ana/app", seconds=30)
        with self.session() as s:
            r = s.get("/repos/ana/app")
        self.assertTrue(r.ok)
        self.assertEqual(len(self.slept), 1)
        self.assertTrue(25 <= self.slept[0] <= 35, self.slept)
        self.assertIn("sigo a las", self.waits[0])

    def test_a_quota_reset_too_far_away_stops_with_an_explanation(self):
        self.hub.fault("rate", "/repos/ana/app", seconds=4 * 3600)
        with self.session() as s, self.assertRaises(StepError) as e:
            s.get("/repos/ana/app")
        self.assertIn("Vuelve a intentarlo", str(e.exception))
        self.assertEqual(self.slept, [])

    def test_secondary_limit_waits_retry_after(self):
        self.hub.fault("secondary", "/repos/ana/app", after=7)
        with self.session() as s:
            self.assertTrue(s.get("/repos/ana/app").ok)
        self.assertEqual(self.slept, [7.0])

    def test_server_errors_and_cut_connections_are_retried(self):
        self.hub.fault("error", "/repos/ana/app")
        self.hub.fault("drop", "/repos/ana/app")
        with self.session() as s:
            self.assertTrue(s.get("/repos/ana/app").ok)
        self.assertGreaterEqual(self.hub.connections, 2)

    def test_stop_during_a_wait(self):
        cancel = threading.Event()
        self.hub.fault("rate", "/repos/ana/app", seconds=30)
        with ApiSession(token=self.token, root=self.hub.root, cancel=cancel,
                        on_wait=lambda *_a: cancel.set()) as s:
            with self.assertRaises(Cancelled):
                s.get("/repos/ana/app")

    def test_a_gzip_bomb_never_fills_the_memory(self):
        self.hub.fault("bomb", "/repos/ana/app")
        with self.session() as s, self.assertRaises(StepError) as e:
            s.get("/repos/ana/app")
        self.assertIn("demasiado grande", str(e.exception))

    def test_a_huge_answer_is_not_read_whole(self):
        self.hub.fault("huge", "/repos/ana/app")
        with self.session() as s:
            with self.assertRaises(StepError) as e:
                s.get("/repos/ana/app")
            self.assertIn("demasiado grande", str(e.exception))
            # La conexión con el resto del cuerpo pendiente se descarta:
            # lo que quedaba en el cable nunca se toma por otra respuesta.
            self.assertIsNone(s._connection)
            self.assertTrue(s.get("/repos/ana/app").ok)

    def test_a_bomb_is_never_inflated_whole(self):
        """El tope se aplica MIENTRAS se descomprime: mirar el tamaño al
        final ya sería tarde (la memoria se habría llenado)."""
        compresor = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        ceros = b"\0" * (1024 * 1024)
        bomba = b"".join(compresor.compress(ceros) for _ in range(160))
        bomba += compresor.flush()
        tracemalloc.start()
        try:
            with self.assertRaises(ghsession._TooBig):
                ghsession.gunzip_limited(bomba, 1024 * 1024)
            _actual, pico = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(pico, 16 * 1024 * 1024)

    def test_something_that_is_not_json(self):
        self.hub.fault("notjson", "/repos/ana/app")
        with self.session() as s, self.assertRaises(StepError):
            s.get("/repos/ana/app")

    def test_through_a_proxy_tunnel(self):
        with ConnectProxy() as proxy:
            auth = {"Proxy-Authorization": "Basic eDp5"}
            with self.session(proxy=("127.0.0.1", proxy.port, auth)) as s:
                self.assertTrue(s.get("/repos/ana/app").ok)
                self.assertTrue(s.get("/repos/ana/app").ok)
        self.assertEqual(len(proxy.seen), 1)              # un solo túnel
        target, headers = proxy.seen[0]
        self.assertEqual(target, f"127.0.0.1:{self.hub.port}")
        self.assertEqual(headers.get("proxy-authorization"), "Basic eDp5")
        # El token va DENTRO del túnel, nunca en el CONNECT.
        self.assertNotIn("authorization", headers)

    def test_the_system_proxy(self):
        with mock.patch("urllib.request.getproxies",
                        lambda: {"https": "http://ana:p%40ss@proxy.local:3128"}), \
                mock.patch("urllib.request.proxy_bypass", lambda h: False):
            host, port, headers = system_proxy("api.github.com")
        self.assertEqual((host, port), ("proxy.local", 3128))
        self.assertEqual(base64.b64decode(
            headers["Proxy-Authorization"].split()[1]), b"ana:p@ss")
        with mock.patch("urllib.request.getproxies",
                        lambda: {"https": "proxy.local:3128"}), \
                mock.patch("urllib.request.proxy_bypass", lambda h: True):
            self.assertIsNone(system_proxy("api.github.com"))

    def test_link_headers(self):
        links = link_targets('<https://a.x/r?page=2&q=a,b>; rel="next", '
                             '<https://a.x/r?page=9>; rel="last"')
        self.assertEqual(links["next"], "https://a.x/r?page=2&q=a,b")
        self.assertEqual(page_number(links["last"]), 9)
        self.assertEqual(link_targets(""), {})
        self.assertEqual(link_targets("basura; rel=next"), {})


# ------------------------------------------------------------------ #
#  La descarga y la reanudación                                       #
# ------------------------------------------------------------------ #
class FetchTests(Case):

    def test_everything_and_only_conversation(self):
        r = self.dump()
        self.assertEqual((r.items, r.issues, r.pulls), (7, 5, 2))
        self.assertEqual(r.counts, {"comment": 5, "code": 3, "event": 12,
                                    "review": 3, "commit": 3, "mention": 1})
        self.assertFalse(r.resumed)
        self.assertFalse(list((self.tmp / "almacen").glob("*")),
                         "el almacén se borra al terminar")

    def stop_when_served(self, cancel, fragment, times=1):
        """«Detener» en cuanto se sirve la `times`-ésima consulta que
        contiene `fragment` (la respuesta llega igual: lo que se pare es
        lo siguiente)."""
        vistas = []

        def hook(what):
            if fragment in what:
                vistas.append(what)
                if len(vistas) == times:
                    cancel.set()
        self.hub.on_served = hook

    def test_resuming_after_a_stop_gives_the_same_txt(self):
        # De una en una: los listados de issues y PR y lo de cada uno van
        # antes que los comentarios, así que pararse ahí es exacto.
        una = DumpOptions(mentions=True, workers=1)
        entero = self.text(self.dump(self.tmp / "entero.txt", options=una))
        primera = len(self.hub.requests)
        self.hub.requests.clear()
        cancel = threading.Event()
        self.stop_when_served(cancel, "/issues/comments?")
        destino = self.tmp / "reanudado.txt"
        with self.assertRaises(Cancelled):
            self.dump(destino, cancel=cancel, options=una)
        self.hub.on_served = None
        self.assertFalse(destino.exists())
        self.assertTrue(list((self.tmp / "almacen").glob("*.sqlite3")))
        antes = len(self.hub.requests)
        self.hub.requests.clear()
        r = self.dump(destino, options=una)
        self.assertTrue(r.resumed)
        self.assertEqual(self.text(r), entero)
        # Lo ya descargado no se vuelve a pedir: entre las dos tandas, como
        # mucho la página en curso y la consulta del repositorio de más.
        self.assertLessEqual(antes + len(self.hub.requests), primera + 3)
        paths = self.hub.paths()
        self.assertFalse(any("/issues?state=all" in p for p in paths),
                         "el listado de issues ya estaba terminado")

    def test_resuming_with_pages_at_once_gives_the_same_txt(self):
        # Lo mismo con varias conexiones: lo que estuviera a medias en ese
        # momento (cualquier listado o lote) se sigue sin perder nada.
        entero = self.text(self.dump(self.tmp / "entero.txt",
                                     options=DumpOptions(mentions=True,
                                                         workers=1)))
        for veces in (1, 3, 6, 10):
            with self.subTest(parar_tras=veces):
                cancel = threading.Event()
                self.stop_when_served(cancel, "/repos/ana/app/", veces)
                destino = self.tmp / f"reanudado-{veces}.txt"
                with self.assertRaises(Cancelled):
                    self.dump(destino, cancel=cancel)
                self.hub.on_served = None
                # Lo que haya quedado (según cuándo se paró, quizá nada).
                previo = ghstore.peek("ana", "app", base=self.tmp / "almacen")
                r = self.dump(destino)
                self.assertEqual(r.resumed, previo is not None)
                self.assertEqual(self.text(r), entero)

    def test_resuming_asks_again_for_the_page_it_was_on(self):
        # De una en una (con varias conexiones, `test_ghfast` lo prueba
        # con las páginas a la vez).
        una = DumpOptions(mentions=True, workers=1)
        cancel = threading.Event()
        self.stop_when_served(cancel, "/issues?state=all", times=2)
        with self.assertRaises(Cancelled):
            self.dump(cancel=cancel, options=una)
        self.hub.on_served = None
        self.hub.requests.clear()
        self.dump(options=una)
        primera_de_issues = next(p for p in self.hub.paths()
                                 if "/issues?" in p)
        self.assertIn("page=2", primera_de_issues)

    def test_other_options_start_from_zero(self):
        cancel = threading.Event()
        with self.assertRaises(Cancelled):
            self.dump(cancel=cancel, on_progress=lambda p: cancel.set())
        r = self.dump(options=DumpOptions(commits=False))
        self.assertFalse(r.resumed)
        self.assertEqual(r.counts["commit"], 0)
        self.assertNotIn("añadió el commit", self.text(r))

    def section(self, texto: str, cabecera: str) -> str:
        """Lo de un issue/PR en el TXT (de su cabecera a la raya)."""
        return texto.split(cabecera)[1].split(RULE)[0]

    def test_a_commit_in_two_pull_requests_is_in_both(self):
        # El mismo commit en el PR 2 y en el 4 (un PR reabierto como otro,
        # ramas apiladas…): antes se guardaba UNA vez y uno lo perdía.
        compartido = dict(self.repo.commits[2][0])
        self.repo.commits[4].append(compartido)
        for opciones in (DumpOptions(mentions=True, workers=1,
                                     graphql=False),
                         DumpOptions(mentions=True)):
            with self.subTest(graphql=opciones.graphql):
                r = self.dump(options=opciones)
                texto = self.text(r)
                self.assertEqual(r.counts["commit"], 4)
                for cabecera in ("PULL REQUEST #2 · ", "PULL REQUEST #4 · "):
                    self.assertIn("añadió el commit 1111111",
                                  self.section(texto, cabecera), cabecera)

    def test_one_comment_mentioning_two_issues_is_in_both(self):
        # «Arregla #3 y #5» en el issue 1: GitHub anota la mención en los
        # dos, desde el mismo sitio y en el mismo segundo.
        mencion = {"event": "cross-referenced", "actor": _user("luis"),
                   "created_at": "2025-04-01T10:00:00Z",
                   "source": {"type": "issue", "issue": {
                       "number": 1, "title": "Error al compilar",
                       "state": "closed",
                       "repository": {"full_name": "ana/app"}}}}
        for numero in (3, 5):
            self.repo.timeline[numero] = [dict(mencion)] \
                + self.repo.timeline.get(numero, [])
        for opciones in (DumpOptions(mentions=True, workers=1,
                                     graphql=False),
                         DumpOptions(mentions=True)):
            with self.subTest(graphql=opciones.graphql):
                r = self.dump(options=opciones)
                texto = self.text(r)
                for numero in (3, 5):
                    trozo = texto.split(f"#{numero} · ", 1)[1].split(RULE)[0]
                    self.assertIn("@luis lo mencionó desde el issue #1",
                                  trozo, numero)

    def old_format(self, ruta: Path) -> None:
        """Deja el almacén como lo guardaba 3.6.0 (formato 1): los commits
        con el sha como id y las menciones sin el número del mencionado,
        guardados uno tras otro (el último pisa al anterior, como entonces:
        un commit compartido se queda solo en uno de sus PR)."""
        db = sqlite3.connect(str(ruta))
        with db:
            fila = db.execute("SELECT value FROM meta WHERE key = "
                              "'identity'").fetchone()
            identidad = dict(json.loads(fila[0]), format=1)
            db.execute("UPDATE meta SET value = ? WHERE key = 'identity'",
                       (json.dumps(identidad),))
            filas = db.execute("SELECT kind, number, at, data FROM entries "
                               "WHERE kind IN ('commit', 'mention') ORDER BY "
                               "number, at").fetchall()
            db.execute("DELETE FROM entries WHERE kind IN "
                       "('commit', 'mention')")
            for kind, number, at, data in filas:
                d = json.loads(data)
                if kind == "commit":
                    ident = d["sha"]
                else:
                    ident = (f"{d['source_repo']}#{d['source_number']}"
                             f"@{d['created_at']}")
                db.execute("INSERT OR REPLACE INTO entries (kind, id, "
                           "number, at, data) VALUES (?, ?, ?, ?, ?)",
                           (kind, ident, number, at, data))
        db.close()

    def test_a_store_of_the_previous_format_is_upgraded(self):
        # Una descarga a medias de 3.6.0 (formato 1), parada tras los
        # commits y las menciones, con un commit en dos PR que ese formato
        # dejó solo en uno. Al seguir, lo mal guardado (commits y
        # menciones) se tira y se vuelve a pedir; lo demás (issues,
        # revisiones…) se aprovecha. El TXT sale completo y sin repetidos.
        self.repo.commits[4].append(dict(self.repo.commits[2][0]))
        una = DumpOptions(mentions=True, workers=1, graphql=False)
        entero = self.text(self.dump(self.tmp / "entero.txt", options=una))
        cancel = threading.Event()
        self.stop_when_served(cancel, "/issues/comments?")
        destino = self.tmp / "reanudado.txt"
        with self.assertRaises(Cancelled):
            self.dump(destino, cancel=cancel, options=una)
        self.hub.on_served = None
        ruta = ghstore.store_path("ana", "app", base=self.tmp / "almacen")
        self.old_format(ruta)
        db = sqlite3.connect(str(ruta))
        perdido = db.execute("SELECT COUNT(*) FROM entries WHERE kind = "
                             "'commit' AND number = 2 AND id = "
                             "'1111111aaaa'").fetchone()[0]
        db.close()
        self.assertEqual(perdido, 0, "en el formato 1, el PR 2 lo perdió")
        previo = partial_download("ana/app", store_dir=self.tmp / "almacen")
        self.assertIsNotNone(previo, "se puede seguir")
        self.hub.requests.clear()
        r = self.dump(destino, options=una)
        self.assertTrue(r.resumed)
        self.assertEqual(self.text(r), entero)
        self.assertEqual(r.counts["commit"], 4)
        pedidas = " ".join(self.hub.paths())
        self.assertIn("/pulls/2/commits", pedidas)
        self.assertIn("/issues/3/timeline", pedidas)
        self.assertNotIn("/pulls/2/reviews", pedidas, "eso estaba bien")
        self.assertNotIn("/issues?state=all", pedidas)

    def test_disabled_issues(self):
        self.repo.issues_disabled = True
        r = self.dump()
        self.assertEqual((r.issues, r.pulls), (0, 2))
        self.assertIn("PULL REQUEST #2 · fusionado", self.text(r))

    def test_a_missing_repository(self):
        with self.assertRaises(StepError) as e:
            dump_repository("ana/no-existe", str(self.tmp / "x.txt"),
                            api_root=self.hub.root,
                            store_dir=self.tmp / "almacen")
        self.assertIn("no existe, o es privado", str(e.exception))


# ------------------------------------------------------------------ #
#  El TXT                                                             #
# ------------------------------------------------------------------ #
class TxtTests(Case):

    def test_only_the_dump_starts_at_column_zero(self):
        texto = self.text(self.dump())
        lineas = texto.split("\n")
        self.assertTrue(texto.startswith(TITLE_PREFIX + "ana/app\n"))
        self.assertTrue(lineas[-2].startswith(FOOTER_PREFIX), lineas[-2])
        cabeceras = [x for x in lineas
                     if re.match(r"(ISSUE|PULL REQUEST) #\d+ · ", x)]
        self.assertEqual(len(cabeceras), 7)
        # La trampa del issue 1 («=====», «ISSUE #99 · …») va sangrada.
        self.assertIn("    ISSUE #99 · falso · título", lineas)
        self.assertIn("    =====", lineas)
        self.assertEqual(lineas.count(RULE), 3 + 7)
        self.assertEqual(lineas.count(THIN), 2 * 7)
        estructura = re.compile(
            r"^(HISTORIAL DE GITHUB: |FIN DEL HISTORIAL: |={80}$|-{80}$|"
            r"ÍNDICE \(|  #\d|(ISSUE|PULL REQUEST) #\d+ · |"
            r"\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d\d:\d\d\] |"
            r"[A-ZÁÉÍÓÚ][a-záéíóúñ ]+: {2,}| {18}\S|    |$)")
        for linea in lineas:
            self.assertRegex(linea, estructura)

    def test_control_characters_and_reversed_text(self):
        texto = self.text(self.dump())
        self.assertNotIn("\x1b", texto)
        self.assertNotIn("\u202e", texto)
        self.assertIn("<U+202E>al revés", texto)
        self.assertNotIn("\r", texto)
        self.assertEqual(clean("a\x00b\u2066c\r\nd"), "a\ufffdb<U+2066>c\nd")

    def test_times_in_the_requested_zone(self):
        texto = self.text(self.dump())
        self.assertIn("[2025-01-03 10:22:05 -05:00] @luis (ya había "
                      "contribuido) abrió el issue:", texto)
        utc = self.text(self.dump(self.tmp / "utc.txt",
                                  options=DumpOptions(utc=True)))
        self.assertIn("[2025-01-03 15:22:05 +00:00] @luis", utc)
        self.assertIn("Horas:            en UTC", utc)

    def test_review_threads_and_hidden_reply_reviews(self):
        texto = self.text(self.dump())
        pr2 = texto.split("PULL REQUEST #2 · ")[1].split(RULE)[0]
        self.assertIn("revisó el pull request: pidió cambios (en el commit "
                      "1111111):\n    Faltan pruebas.\n    ┌─ src/arm.c · "
                      "línea 42\n", pr2)
        self.assertIn("    │ [2025-01-05 11:00:00 -05:00] @ana (propietario) "
                      "respondió:\n    │     No puede: es un contador.", pr2)
        # La revisión vacía que GitHub crea por una respuesta no sale sola,
        # y el borrador sin enviar tampoco.
        self.assertNotIn("revisó el pull request: comentó", pr2)
        self.assertNotIn("borrador", pr2)
        self.assertIn("aprobó los cambios", pr2)
        # Fusionado y cerrado a la vez: solo «fusionó».
        self.assertIn("@ana fusionó el pull request (commit abc1234).", pr2)
        self.assertNotIn("cerró el pull request", pr2)
        pr4 = texto.split("PULL REQUEST #4 · ")[1].split(RULE)[0]
        self.assertIn("comentó en el código:\n    ┌─ config.py · línea 3 · "
                      "ese código cambió después", pr4)
        self.assertIn("Ramas:            luis:fix → main (desde la "
                      "bifurcación luis/app)", pr4)
        self.assertIn("equipo «core»", pr4)

    def test_people(self):
        texto = self.text(self.dump())
        self.assertIn("@ghost (cuenta eliminada) comentó:", texto)
        self.assertIn("@dependabot[bot] (bot) comentó:", texto)
        self.assertIn("@luis (primera contribución) abrió el pull request:",
                      texto)
        self.assertIn("«Robot de CI» (sin cuenta de GitHub) añadió el commit "
                      "2222222: «Pruebas»", texto)
        self.assertNotIn("se suscribió", texto)
        self.assertIn("Participan:       @luis, @ana, @ghost", texto)

    def test_the_ghost_account_is_a_deleted_one(self):
        # REST da las cuentas borradas como «ghost»; GraphQL, a veces,
        # como nadie: el TXT dice lo mismo en los dos casos.
        from tests.fakegithub import _user
        self.repo.comments[1]["user"] = _user("ghost")
        texto = self.text(self.dump())
        self.assertEqual(texto.count("@ghost (cuenta eliminada) comentó:"),
                         2)

    def test_secrets(self):
        sin = self.text(self.dump())
        self.assertIn(self.secret, sin)              # el volcado es fiel
        con = self.text(self.dump(self.tmp / "tachado.txt",
                                  options=DumpOptions(redact=True)))
        self.assertNotIn(self.secret, con)
        self.assertNotIn(self.secret[4:20], con)
        self.assertEqual(con.count(MASK), 2 + 1)     # +1: la cabecera
        self.assertTrue(con.rstrip("\n").endswith("2 secretos tachados"))

    def test_never_the_token(self):
        for opciones in (DumpOptions(), DumpOptions(redact=True)):
            r = self.dump(self.tmp / f"t{int(opciones.redact)}.txt",
                          options=opciones)
            self.assertNotIn(self.token, self.text(r))


# ------------------------------------------------------------------ #
#  El destino                                                         #
# ------------------------------------------------------------------ #
class DestinationTests(Case):

    def test_a_foreign_file_is_never_overwritten(self):
        mio = self.tmp / "app.txt"
        mio.write_text("mis notas\n", encoding="utf-8")
        r = self.dump(mio)
        self.assertTrue(r.renamed)
        self.assertEqual(Path(r.path).name, "app-historial.txt")
        self.assertEqual(mio.read_text(encoding="utf-8"), "mis notas\n")
        # Rehacerlo pisa el historial NUESTRO, no busca otro nombre.
        r2 = self.dump(mio)
        self.assertEqual(r2.path, r.path)
        self.assertTrue(is_dump(Path(r.path)))

    @unittest.skipUnless(hasattr(os, "symlink") and os.name != "nt",
                         "enlaces de POSIX")
    def test_a_link_is_not_followed(self):
        fuera = self.tmp / "fuera.txt"
        fuera.write_text(TITLE_PREFIX + "parece nuestro\n", encoding="utf-8")
        enlace = self.tmp / "enlace.txt"
        os.symlink(str(fuera), str(enlace))
        destino, renombrado = free_output_path(enlace)
        self.assertTrue(renombrado)
        self.assertNotEqual(destino, enlace)
        self.dump(enlace)
        self.assertEqual(fuera.read_text(encoding="utf-8"),
                         TITLE_PREFIX + "parece nuestro\n")

    def test_stopping_while_writing_keeps_the_previous_txt(self):
        destino = self.tmp / "historial.txt"
        self.dump(destino)
        antes = destino.read_bytes()
        cancel = threading.Event()
        escritos = []

        def parar_al_escribir(hechos, total):
            escritos.append(hechos)
            if hechos == 3:
                cancel.set()
        with self.assertRaises(Cancelled):
            self.dump(destino, cancel=cancel, on_write=parar_al_escribir,
                      options=DumpOptions(mentions=False))
        self.assertEqual(destino.read_bytes(), antes)
        sobras = [p.name for p in self.tmp.iterdir()
                  if p.name.startswith(".historial.txt.")]
        self.assertEqual(sobras, [])

    def test_repository_names(self):
        for texto in ("ana/app", "https://github.com/ana/app",
                      "https://github.com/ana/app.git",
                      "git@github.com:ana/app.git",
                      "https://www.github.com/ana/app/pull/3",
                      "github.com/ana/app/issues?q=x", " ana/app "):
            self.assertEqual(parse_repository(texto), ("ana", "app"), texto)
        for malo in ("", "app", "ana/app/extra", "https://gitlab.com/ana/app",
                     "ana/..", "-ana/app", "ana/a b"):
            with self.assertRaises(StepError, msg=malo):
                parse_repository(malo)


class CommandLineTests(Case):

    def test_issues_txt(self):
        import contextlib
        import io
        from gitpush_studio import cli
        salida = self.tmp / "cli.txt"
        buffer = io.StringIO()
        with mock.patch.object(ghsession, "API_ROOT", self.hub.root), \
                mock.patch.object(ghstore, "STORE_DIR", self.tmp / "alm"), \
                contextlib.redirect_stdout(buffer):
            rc = cli.main(["issues-txt", "ana/app", "-o", str(salida),
                           "--token", self.token, "--redact", "--utc"])
        self.assertEqual(rc, 0, buffer.getvalue())
        texto = salida.read_text(encoding="utf-8")
        self.assertNotIn(self.secret, texto)
        self.assertIn("[2025-01-03 15:22:05 +00:00]", texto)
        self.assertIn("7 issues y pull requests", buffer.getvalue())
        self.assertTrue(self.hub.graphql_queries, "con token, GraphQL")
        self.assertNotIn("⚠", buffer.getvalue(), "sin avisos")

    def test_issues_txt_says_when_graphql_cannot_be_used(self):
        import contextlib
        import io
        from gitpush_studio import cli
        self.hub.fault("graphql_unauth", match="/graphql", times=99)
        buffer = io.StringIO()
        with mock.patch.object(ghsession, "API_ROOT", self.hub.root), \
                mock.patch.object(ghstore, "STORE_DIR", self.tmp / "alm"), \
                contextlib.redirect_stdout(buffer):
            rc = cli.main(["issues-txt", "ana/app", "-o",
                           str(self.tmp / "cli.txt"), "--token", self.token])
        self.assertEqual(rc, 0, buffer.getvalue())
        salida = buffer.getvalue()
        # Mientras va (una vez) y en el resumen final.
        self.assertEqual(salida.count("⚠ GraphQL no se pudo usar"), 2,
                         salida)
        self.assertIn("(401)", salida)

    def test_issues_txt_as_before(self):
        import contextlib
        import io
        from gitpush_studio import cli
        salida = self.tmp / "cli.txt"
        buffer = io.StringIO()
        with mock.patch.object(ghsession, "API_ROOT", self.hub.root), \
                mock.patch.object(ghstore, "STORE_DIR", self.tmp / "alm"), \
                contextlib.redirect_stdout(buffer):
            rc = cli.main(["issues-txt", "ana/app", "-o", str(salida),
                           "--token", self.token, "--workers", "1",
                           "--no-graphql"])
        self.assertEqual(rc, 0, buffer.getvalue())
        self.assertEqual(self.hub.graphql_queries, [])
        self.assertEqual(self.hub.connections, 1, "una sola conexión")

    def test_issues_txt_tells_about_a_half_done_download(self):
        import contextlib
        import io
        from gitpush_studio import cli
        identidad = {"format": ghstore.FORMAT, "repo_id": 4242,
                     "commits": True, "mentions": True}
        almacen = ghstore.open_store("ana", "app", identidad,
                                     base=self.tmp / "alm")
        almacen.save("issues", cursor="", items=[(1, {"number": 1})])
        almacen.close()

        def lanzar(*extra):
            buffer = io.StringIO()
            # Solo el aviso: la descarga en sí no llega a empezar.
            with mock.patch.object(ghsession, "API_ROOT", self.hub.root), \
                    mock.patch.object(ghstore, "STORE_DIR", self.tmp / "alm"), \
                    mock.patch("gitpush_studio.core.ghdump.dump_repository",
                               side_effect=Cancelled("parado")), \
                    contextlib.redirect_stdout(buffer):
                cli.main(["issues-txt", "ana/app", "--token", self.token,
                          "-o", str(self.tmp / "x.txt"), *extra])
            return buffer.getvalue()
        self.assertIn("↻ Sigo la descarga a medias de ana/app",
                      lanzar("--mentions"))
        aviso = lanzar()
        self.assertIn("⚠ Hay una descarga a medias de ana/app con los "
                      "commits y con las menciones", aviso)
        self.assertIn("(--mentions)", aviso)

    def test_issues_txt_workers_out_of_range(self):
        import contextlib
        import io
        from gitpush_studio import cli
        for malo in ("0", "9", "dos"):
            errores = io.StringIO()
            with contextlib.redirect_stderr(errores), \
                    self.assertRaises(SystemExit) as e:
                cli.main(["issues-txt", "ana/app", "--workers", malo])
            self.assertEqual(e.exception.code, 2)
            self.assertIn("--workers", errores.getvalue())
        self.assertEqual(self.hub.requests, [], "ni una consulta")


if __name__ == "__main__":
    unittest.main()
