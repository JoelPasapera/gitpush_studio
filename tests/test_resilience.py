# -*- coding: utf-8 -*-
"""Aguante de la descarga masiva.

Bajar muchos repositorios a la vez choca con la realidad: cortes de red,
servidores ocupados y el «baja el ritmo» de GitHub. Un fallo pasajero no
puede costar un repositorio entero, y un límite de peticiones tiene que
frenar la cola completa, no solo al que lo recibió.
"""

import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tests.gitenv import HAVE_GIT, commit_all, init_repo

from gitpush_studio.core.git import (BulkCloner, clone_error_message,
                                     clone_repo, http_code,
                                     push_error_message,
                                     looks_like_auth_error,
                                     looks_rate_limited, looks_transient)


class ClassificationTests(unittest.TestCase):

    def test_what_deserves_another_try(self):
        for salida in ("the requested url returned error: 429",
                       "error: rpc failed; curl 56 recv failure: "
                       "connection reset by peer",
                       "fatal: early eof",
                       "fatal: unable to access: could not resolve host",
                       "the remote end hung up unexpectedly",
                       "server error: 503"):
            self.assertTrue(looks_transient(salida), salida)

    def test_what_does_not_get_another_try(self):
        for salida in ("fatal: authentication failed",
                       "remote: repository not found",
                       "fatal: could not read username",
                       "error: 403 forbidden",
                       "fatal: destination path already exists"):
            self.assertFalse(looks_transient(salida), salida)

    def test_a_number_inside_a_path_is_not_an_http_code(self):
        """Una carpeta temporal puede llamarse /tmp/tmp.k429xq: eso no es
        un límite de peticiones. Confundirlo daba un mensaje falso y
        reintentaba tres veces un error definitivo."""
        rutas = ["error: src refspec main does not match any\n"
                 "failed to push to '/tmp/tmp.k429xq/r.git'",
                 "fatal: '/tmp/tmp403ab/remoto.git' no existe",
                 "error: no such file '/home/denied/proyecto/500.txt'"]
        for salida in rutas:
            with self.subTest(salida=salida[:40]):
                self.assertEqual(http_code(salida), 0)
                self.assertFalse(looks_rate_limited(salida))
                self.assertFalse(looks_like_auth_error(salida))
                self.assertFalse(looks_transient(salida))
        self.assertIn("nada que publicar", push_error_message(rutas[0]))

    def test_a_real_code_is_recognised_where_git_puts_it(self):
        self.assertEqual(
            http_code("the requested url returned error: 429"), 429)
        self.assertEqual(http_code("error: rpc failed; http 500 curl 22"), 500)
        self.assertTrue(looks_rate_limited("returned error: 429"))
        self.assertTrue(looks_transient("returned error: 429"))   # se reintenta
        self.assertTrue(looks_like_auth_error("returned error: 403"))
        self.assertFalse(looks_transient("returned error: 403"))

    def test_denied_needs_company_to_count(self):
        self.assertTrue(looks_like_auth_error(
            "remote: permission to ana/p.git denied to otro."))
        self.assertTrue(looks_like_auth_error("permission denied (publickey)"))
        self.assertFalse(looks_like_auth_error("'/home/denied/proyecto'"))

    def test_rate_limit_is_recognised_and_explained(self):
        self.assertTrue(looks_rate_limited("http 429 too many requests"))
        self.assertFalse(looks_rate_limited("connection reset"))
        self.assertIn("bajar el ritmo",
                      clone_error_message("returned error: 429"))


class FakeGit:
    """Devuelve resultados preparados para cada intento de clonación."""

    def __init__(self, salidas):
        self.salidas = list(salidas)     # (codigo, texto)
        self.intentos = 0

    def run(self, args, cwd=None, check=True, timeout=None, env_extra=None,
            echo=True, register=None):
        self.intentos += 1
        code, out = self.salidas.pop(0) if self.salidas else (0, "")
        return subprocess.CompletedProcess(args, code, out, "")


class RetryTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.esperas = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def clonar(self, salidas, **kw):
        avisos = []
        git = FakeGit(salidas)
        estado, _, error = clone_repo(
            git, "https://github.com/a/b.git", self.tmp / "destino",
            on_retry=lambda e, lim, err: avisos.append((e, lim)),
            sleep=self.esperas.append, **kw)
        return git, estado, error, avisos

    def test_a_network_blip_is_retried_and_succeeds(self):
        git, estado, error, avisos = self.clonar(
            [(128, "error: RPC failed; curl 56 recv failure"), (0, "")])
        self.assertEqual(estado, "ok", error)
        self.assertEqual(git.intentos, 2)
        self.assertEqual(self.esperas, [3])          # espera creciente
        self.assertEqual(avisos, [(3, False)])

    def test_it_gives_up_after_the_last_try(self):
        git, estado, _, _ = self.clonar([(128, "fatal: early EOF")] * 5)
        self.assertEqual(estado, "fail")
        self.assertEqual(git.intentos, 3)            # 1 + 2 reintentos
        self.assertEqual(self.esperas, [3, 6])       # 3 s y luego 6 s

    def test_a_bad_token_fails_immediately(self):
        git, estado, error, avisos = self.clonar(
            [(128, "fatal: Authentication failed")] * 3)
        self.assertEqual(estado, "fail")
        self.assertEqual(git.intentos, 1)            # insistir no arregla esto
        self.assertEqual(avisos, [])
        self.assertIn("credenciales", error)

    def test_a_missing_repository_fails_immediately(self):
        git, estado, error, _ = self.clonar(
            [(128, "remote: Repository not found")] * 3)
        self.assertEqual(git.intentos, 1)
        self.assertIn("no encontrado", error)

    def test_a_rate_limit_is_flagged_to_the_caller(self):
        _, estado, _, avisos = self.clonar(
            [(128, "The requested URL returned error: 429"), (0, "")])
        self.assertEqual(estado, "ok")
        self.assertEqual(avisos, [(3, True)])        # True: es un 429

    def test_retries_can_be_disabled(self):
        git, estado, _, _ = self.clonar([(128, "fatal: early EOF")] * 3,
                                        retries=0)
        self.assertEqual(estado, "fail")
        self.assertEqual(git.intentos, 1)


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class QueuePauseTests(unittest.TestCase):
    """Con git de verdad, para que la cola completa se comporte."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repos = []
        for i in range(4):
            origen = self.tmp / f"origen{i}"
            init_repo(origen)
            (origen / "f.txt").write_text(f"{i}\n", encoding="utf-8")
            commit_all(origen, "inicial")
            self.repos.append({"name": f"repo{i}",
                               "clone_url": str(origen), "private": False})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_normal_run_downloads_everything(self):
        eventos = []
        bc = BulkCloner(workers=4)
        s = bc.run(self.repos, self.tmp / "salida", True, "",
                   lambda k, p: eventos.append(k))
        self.assertEqual((s["ok"], s["fail"]), (4, 0))
        self.assertEqual(sorted(p.name for p in
                                (self.tmp / "salida").iterdir()),
                         ["repo0", "repo1", "repo2", "repo3"])

    def test_a_rate_limit_pauses_the_whole_queue_once(self):
        bc = BulkCloner(workers=4)
        bc.PAUSE_SECONDS = 0.3
        eventos = []
        # Dos trabajadores reciben el «429» casi a la vez: la pausa se
        # anuncia UNA sola vez y frena a todos, no solo a quien la recibió.
        bc._slow_down(lambda k, p: eventos.append((k, p)))
        bc._slow_down(lambda k, p: eventos.append((k, p)))
        self.assertEqual(eventos, [("slow", 0.3)])
        t = time.time()
        bc._wait_if_busy()
        self.assertGreaterEqual(time.time() - t, 0.2, "la cola no esperó")

    def test_stopping_interrupts_the_pause_immediately(self):
        bc = BulkCloner(workers=2)
        bc.PAUSE_SECONDS = 30
        bc._slow_down(lambda k, p: None)
        bc.stop()
        t = time.time()
        bc._wait_if_busy()                      # no se queda colgada
        self.assertLess(time.time() - t, 1.0)


if __name__ == "__main__":
    unittest.main()
