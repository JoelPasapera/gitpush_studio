# -*- coding: utf-8 -*-
"""Registro de diagnóstico: rota solo, nunca guarda secretos y nunca
rompe la aplicación."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from gitpush_studio.core.diaglog import DiagnosticLog, redact


class Redaction(unittest.TestCase):

    def test_credentials_in_urls_are_hidden(self):
        t = redact("$ git remote set-url origin "
                   "https://ana:ghp_secreto123@github.com/ana/p.git")
        self.assertNotIn("secreto", t)
        self.assertNotIn("ana:", t)
        self.assertIn("https://***@github.com/ana/p.git", t)

    def test_provider_tokens_and_headers_are_hidden(self):
        token = "ghp_" + "Ab3" * 12
        for texto in (f"usando {token} ahora",
                      "AUTHORIZATION: basic eC1hY2Nlc3MtdG9rZW46Z2hw",
                      "Authorization: Bearer abc.def.ghi"):
            with self.subTest(texto=texto):
                limpio = redact(texto)
                self.assertNotIn(token, limpio)
                self.assertNotIn("eC1hY2Nlc3M", limpio)
                self.assertNotIn("abc.def", limpio)

    def test_ordinary_text_is_untouched(self):
        for texto in ("✔ ¡Proyecto publicado en 3.2 s!",
                      "https://github.com/ana/proyecto",
                      "$ git push -u origin refs/heads/main"):
            self.assertEqual(redact(texto), texto)


class Rotation(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_it_rotates_and_keeps_a_bounded_number_of_files(self):
        log = DiagnosticLog(self.tmp, max_bytes=2048, backups=3)
        for i in range(400):
            log.write(f"línea {i:04d} " + "x" * 40)
        archivos = log.files()
        self.assertEqual(len(archivos), 4)          # actual + 3 copias
        self.assertTrue(all(p.stat().st_size <= 2048 for p in archivos))
        self.assertIn("línea 0399", archivos[0].read_text("utf-8"))
        self.assertNotIn("línea 0000",
                         "".join(p.read_text("utf-8") for p in archivos))

    def test_a_broken_disk_never_raises(self):
        bloqueo = self.tmp / "no-es-carpeta"
        bloqueo.write_text("archivo")
        log = DiagnosticLog(bloqueo / "registro")
        log.write("hola")                           # no lanza
        log.write("otra vez")
        self.assertEqual(log.files(), [])

    @unittest.skipUnless(os.name == "posix", "permisos POSIX")
    def test_secrets_never_reach_the_file(self):
        log = DiagnosticLog(self.tmp)
        log.write("https://u:clave-muy-secreta@github.com/x", "cmd")
        texto = log.path.read_text("utf-8")
        self.assertNotIn("clave-muy-secreta", texto)
        self.assertIn("[cmd]", texto)


if __name__ == "__main__":
    unittest.main()
