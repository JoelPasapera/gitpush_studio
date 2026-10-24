# -*- coding: utf-8 -*-
"""La red y los procesos: que nada se quede colgado ni filtre el token.

  · el token de GitHub solo viaja a github.com (git y HTTP);
  · una redirección a OTRO servidor no lleva la cabecera Authorization,
    y nunca se baja de https a http;
  · respuestas rotas (no JSON, enormes, cortadas) se convierten en
    errores explicados, no en excepciones sueltas;
  · un git colgado SIN escribir nada también respeta el tiempo máximo;
  · un repositorio raro no tumba una descarga en lote;
  · una instantánea que no se pudo escribir entera no queda marcada
    como completa, y la anterior vuelve a su sitio pase lo que pase.
"""

import errno
import http.server
import io
import json
import shutil
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from gitpush_studio.core import github, net, snapshot
from gitpush_studio.core.errors import StepError
from gitpush_studio.core.git import BulkCloner, Git, auth_env, github_origin


class _Servidor:
    """Servidor HTTP local que responde según una función."""

    def __init__(self, responder):
        vistos = self.vistos = []

        class Manejador(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                vistos.append((self.path, dict(self.headers)))
                codigo, cabeceras, cuerpo = responder(self)
                self.send_response(codigo)
                for k, v in cabeceras.items():
                    self.send_header(k, v)
                self.end_headers()
                if cuerpo:
                    try:
                        self.wfile.write(cuerpo)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            do_POST = do_GET

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                     Manejador)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever,
                         daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TokenOnlyGoesToGitHub(unittest.TestCase):

    def test_git_header_only_for_github_https(self):
        self.assertEqual(github_origin("https://github.com/a/b.git"),
                         "https://github.com/")
        self.assertEqual(github_origin("https://GitHub.com/a/b"),
                         "https://github.com/")
        for url in ("https://github.com.malo.com/a/b",
                    "https://malo.com/github.com/a/b",
                    "https://usuario@github.com/a/b",
                    "https://github.com:8443/a/b",
                    "http://github.com/a/b",
                    "git@github.com:a/b.git",
                    "/ruta/local.git", "", "https://[::1"):
            with self.subTest(url=url):
                self.assertEqual(github_origin(url), "")
                self.assertEqual(auth_env(url, "ghp_token"), {})

    def test_redirect_to_another_server_drops_authorization(self):
        destino = _Servidor(lambda h: (200, {"Content-Type":
                                             "application/json"}, b"{}"))
        origen = _Servidor(lambda h: (302, {
            "Location": f"http://localhost:{destino.port}/final"}, b""))
        try:
            with net.open_url(f"http://127.0.0.1:{origen.port}/inicio",
                              {"Authorization": "Bearer SECRETO",
                               "User-Agent": "prueba"}) as r:
                r.read()
            self.assertEqual(origen.vistos[0][1].get("Authorization"),
                             "Bearer SECRETO")
            cabeceras = {k.lower(): v for k, v in destino.vistos[0][1].items()}
            self.assertNotIn("authorization", cabeceras)
            self.assertEqual(cabeceras.get("user-agent"), "prueba")
        finally:
            origen.close()
            destino.close()

    def test_a_redirect_from_https_to_http_is_refused(self):
        """Bajar a http mandaría el token (y todo lo demás) en claro."""
        import urllib.request
        manejador = net.SafeRedirectHandler()
        req = urllib.request.Request("https://api.github.com/x",
                                     headers={"Authorization": "Bearer S"})
        with self.assertRaises(net.urllib.error.HTTPError):
            manejador.redirect_request(req, io.BytesIO(b""), 302, "Found",
                                       {}, "http://api.github.com/y")
        nueva = manejador.redirect_request(req, io.BytesIO(b""), 302,
                                           "Found", {},
                                           "https://api.github.com/y")
        self.assertEqual(nueva.full_url, "https://api.github.com/y")

    def test_redirect_on_the_same_server_keeps_authorization(self):
        servidor = _Servidor(lambda h: (
            (302, {"Location": "/final"}, b"") if h.path == "/inicio"
            else (200, {}, b"ok")))
        try:
            with net.open_url(f"http://127.0.0.1:{servidor.port}/inicio",
                              {"Authorization": "Bearer X"}) as r:
                self.assertEqual(r.read(), b"ok")
            self.assertEqual(servidor.vistos[1][1].get("Authorization"),
                             "Bearer X")
        finally:
            servidor.close()


class BrokenResponsesAreExplained(unittest.TestCase):

    def api_contra(self, responder, **kw):
        servidor = _Servidor(responder)
        try:
            with mock.patch.object(github, "GITHUB_API",
                                   f"http://127.0.0.1:{servidor.port}"), \
                    mock.patch.object(github.time, "sleep"):
                return github.api("GET", "/x", **kw)
        finally:
            servidor.close()

    def test_a_200_that_is_not_json(self):
        codigo, cuerpo = self.api_contra(
            lambda h: (200, {}, b"<html>proxy</html>"))
        self.assertEqual(codigo, 200)
        self.assertIn("proxy", cuerpo["message"])

    def test_a_huge_response_is_not_read_into_memory(self):
        with mock.patch.object(net, "MAX_BODY", 1000), \
                mock.patch.object(github, "read_limited",
                                  lambda r: net.read_limited(r, 1000)):
            with self.assertRaises(StepError):
                self.api_contra(lambda h: (200, {}, b"x" * 5000))

    def test_no_server_at_all_is_a_step_error(self):
        with mock.patch.object(github, "GITHUB_API", "http://127.0.0.1:9"), \
                mock.patch.object(github.time, "sleep"):
            with self.assertRaises(StepError) as cm:
                github.api("GET", "/x", timeout=2)
        self.assertIn("conectar", str(cm.exception))

    def test_secondary_rate_limit_is_explained(self):
        with self.assertRaises(StepError) as cm:
            self.api_contra(lambda h: (403, {}, json.dumps(
                {"message": "You have exceeded a secondary rate limit"}
            ).encode()))
        self.assertIn("limitó", str(cm.exception))

    def test_odd_bodies_do_not_crash_the_helpers(self):
        with mock.patch.object(github, "api",
                               lambda *a, **k: (200, ["no", "es", "dict"])):
            self.assertEqual(github.whoami("t"), "")
            with self.assertRaises(StepError):
                github.get_repo("a", "b")
            with self.assertRaises(StepError):
                github.create_repo("t", "nombre")
        self.assertEqual(github._int("999999999999999999999999"),
                         999999999999999999999999)
        self.assertEqual(github._int("abc"), 0)
        self.assertEqual(github._int(-5), 0)

    def test_retry_after(self):
        self.assertEqual(net.retry_after({"Retry-After": "7"}), 7.0)
        self.assertEqual(net.retry_after({"Retry-After": "1e9"}), 120.0)
        self.assertEqual(net.retry_after({"Retry-After": "nan"}, 3), 3)
        self.assertEqual(net.retry_after({"Retry-After": "-1"}, 3), 3)
        self.assertEqual(net.retry_after({}, 2), 2)
        self.assertEqual(net.retry_after(None, 2), 2)


class ProcessesNeverHang(unittest.TestCase):

    def test_stream_times_out_even_without_output(self):
        g = Git(exe=sys.executable)
        lineas = []
        t0 = time.time()
        rc = g.stream(["-c", "import time; time.sleep(30)"], cwd=None,
                      on_line=lineas.append, timeout=1)
        self.assertLess(time.time() - t0, 15)
        self.assertEqual(rc, 1)
        self.assertIn("Tiempo agotado.", lineas)

    def test_stream_still_returns_the_real_exit_code(self):
        g = Git(exe=sys.executable)
        lineas = []
        rc = g.stream(["-c", "print('hola'); raise SystemExit(3)"], cwd=None,
                      on_line=lineas.append, timeout=30)
        self.assertEqual((rc, lineas), (3, ["hola"]))


class BulkDownloadsSurviveOddRepositories(unittest.TestCase):

    def test_one_exploding_repository_does_not_stop_the_rest(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            def clonar(git, url, carpeta, **kw):
                if "malo" in url:
                    raise RuntimeError("boom")
                Path(carpeta).mkdir(parents=True)
                (Path(carpeta) / "f").write_text("x")
                return "ok", 0.1, ""
            repos = [{"name": n, "clone_url": f"https://github.com/u/{n}"}
                     for n in ("bueno1", "malo", "bueno2")]
            repos.append({"name": "../fuera", "clone_url": "x"})
            eventos = []
            with mock.patch("gitpush_studio.core.git.clone_repo", clonar):
                res = BulkCloner(workers=2).run(
                    repos, tmp, False, "", lambda k, v: eventos.append((k, v)))
            self.assertEqual((res["ok"], res["fail"]), (2, 2))
            self.assertTrue(any("boom" in f for f in res["fails"]))
            self.assertFalse((tmp.parent / "fuera").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _targz(archivos):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for nombre, datos in archivos:
            info = tarfile.TarInfo("repo-sha/" + nombre)
            info.size = len(datos)
            tar.addfile(info, io.BytesIO(datos))
    return buf.getvalue()


class SnapshotsAreAllOrNothing(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.datos = _targz([("a.txt", b"A"), ("sub/b.txt", b"B")])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def opener(self, url, headers, timeout):
        return io.BytesIO(self.datos)

    def test_a_full_disk_is_a_failure_not_a_skipped_file(self):
        destino = self.tmp / "repo"
        abrir = open

        def lleno(ruta, modo="r", *a, **kw):
            if "w" in modo and str(ruta).endswith("b.txt"):
                raise OSError(errno.ENOSPC, "No space left on device")
            return abrir(ruta, modo, *a, **kw)

        with mock.patch("builtins.open", lleno):
            estado, _, error = snapshot.fetch_snapshot(
                "u", "r", destino, branch="main", opener=self.opener,
                retries=0)
        self.assertEqual(estado, "fail")
        self.assertIn("espacio", error)
        self.assertFalse((destino / snapshot.MARKER).exists())
        self.assertFalse(destino.exists() and any(destino.iterdir()))

    def test_the_previous_version_comes_back_whatever_happens(self):
        destino = self.tmp / "repo"
        estado, _, _ = snapshot.fetch_snapshot("u", "r", destino,
                                               branch="main",
                                               opener=self.opener, sha="v1")
        self.assertEqual(estado, "ok")
        (destino / "mio.txt").write_text("lo que añadí yo")
        with mock.patch.object(snapshot, "fetch_snapshot",
                               side_effect=RuntimeError("inesperado")):
            with self.assertRaises(RuntimeError):
                snapshot.update_snapshot("u", "r", destino, branch="main",
                                         sha_remoto="v2")
        self.assertEqual((destino / "mio.txt").read_text(), "lo que añadí yo")
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["repo"])

    def test_branch_names_cannot_break_the_url(self):
        urls = [u for u, _ in snapshot.snapshot_urls("u", "r", "a#b?c d")]
        self.assertTrue(all("a%23b%3Fc%20d" in u for u in urls), urls)

    def test_names_windows_reserves_are_recognised(self):
        for nombre in ("aux", "AUX.txt", "con", "nul.tar.gz", "COM1",
                       "lpt9.md", "fin.", "fin "):
            self.assertTrue(snapshot._reserved_on_windows(nombre), nombre)
        for nombre in ("auxiliar.txt", "console.log", "null", "a.txt"):
            self.assertFalse(snapshot._reserved_on_windows(nombre), nombre)


if __name__ == "__main__":
    unittest.main()
