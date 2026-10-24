# -*- coding: utf-8 -*-
"""Presentación y resumen de los TXT consolidados.

Quien abre el archivo (o se lo pasa a otra herramienta) tiene que poder
entender de qué va sin conocer la aplicación: qué contiene, cómo está
ordenado, qué significan las marcas y qué se quedó fuera.
"""

import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from gitpush_studio.core import preamble
from gitpush_studio.core.pack import (PACK_MAX_FILE, consolidate_many,
                                      consolidate_project)


def plano(texto):
    """Junta el texto en una sola línea: el preámbulo va envuelto a 78
    columnas, así que una frase puede estar partida entre dos líneas y no
    se encontraría buscándola tal cual."""
    return " ".join(texto.split())


def write(folder, rel, content=b"x\n"):
    p = Path(folder) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content if isinstance(content, bytes)
                  else content.encode("utf-8"))
    return p


class PreambleTextTests(unittest.TestCase):

    def test_profile_paragraph_answers_the_obvious_questions(self):
        texto = preamble.profile_text(
            "ana", "https://github.com/ana", 12, PACK_MAX_FILE,
            when=datetime(2026, 9, 17, 15, 40))
        self.assertIn("12 repositorios", plano(texto))
        self.assertIn("«ana»", plano(texto))
        self.assertIn("https://github.com/ana", plano(texto))
        self.assertIn("17 de septiembre de 2026 a las 15:40", plano(texto))
        llano = plano(texto)
        self.assertIn("foto fija", llano)                 # qué NO es
        self.assertIn("MODULO: ruta/relativa", llano)     # cómo leerlo
        self.assertIn(".gitignore", llano)                # qué falta
        self.assertIn("2 MB", llano)
        self.assertIn("no se confunda con un archivo vacío", llano)
        # Es prosa, no una lista: ninguna línea empieza por viñeta.
        for linea in texto.splitlines():
            self.assertFalse(linea.strip().startswith(("-", "*", "·", "•")),
                             linea)
        # Y va envuelto a un ancho legible.
        self.assertLessEqual(max(len(x) for x in texto.splitlines()),
                             preamble.WIDTH)

    def test_project_paragraph_is_the_same_idea_in_singular(self):
        texto = preamble.project_text("mi-app", 34, PACK_MAX_FILE,
                                      when=datetime(2026, 1, 3, 9, 5))
        llano = plano(texto)
        self.assertIn("«mi-app»", llano)
        self.assertIn("34 archivos", llano)
        self.assertIn("3 de enero de 2026 a las 09:05", llano)
        self.assertNotIn("repositorios del perfil", llano)

    def test_summary_counts_in_proper_spanish(self):
        uno = plano(preamble.summary_text(
            {"repos": 1, "files": 1, "binaries": 1, "big": 1, "ignored": 1,
             "bytes": 2048, "seconds": 0.4}))
        self.assertIn("1 repositorio,", uno)
        self.assertIn("1 archivo volcado", uno)
        self.assertIn("1 binario omitido", uno)
        self.assertIn("y 1 ignorado", uno)
        self.assertIn("2 KB", uno)
        self.assertIn("en menos de un segundo", uno)
        varios = plano(preamble.summary_text(
            {"repos": 9, "files": 120, "binaries": 3, "bytes": 5 << 20,
             "seconds": 95}))
        self.assertIn("9 repositorios", varios)
        self.assertIn("120 archivos volcados", varios)
        self.assertIn("en 1 min 35 s", varios)
        self.assertNotIn("ignorado", varios)     # cero no se menciona

    def test_summary_warns_when_the_dump_was_cut(self):
        texto = plano(preamble.summary_text({"files": 3, "bytes": 10,
                                             "seconds": 1}, truncated=True))
        self.assertIn("está cortado", texto)


class ConsolidatedFileTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.web = self.tmp / "web"
        write(self.web, "index.html", "<h1>hola</h1>\n")
        write(self.web, "src/estilo.css", "body{}\n")
        write(self.web, "logo.png", b"\x89PNG\x00binario")
        self.api = self.tmp / "api"
        write(self.api, "main.py", "print(1)\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_profile_txt_opens_with_the_description_and_ends_with_totals(self):
        out = self.tmp / "ana.txt"
        st = consolidate_many("ana", [self.web, self.api], out)
        texto = out.read_text(encoding="utf-8")
        cabecera, _, resto = texto.partition("PERFIL DE GITHUB: ana")
        self.assertTrue(cabecera.startswith("#"))
        # La descripción va antes que el árbol y que los repositorios.
        self.assertLess(resto.index("Este archivo reúne"),
                        resto.index("ESTRUCTURA DEL PERFIL"))
        self.assertLess(resto.index("ESTRUCTURA DEL PERFIL"),
                        resto.index("REPOSITORIO 1/2"))
        self.assertIn("2 repositorios del perfil de GitHub «ana»", plano(texto))
        # Cada repositorio dice cuántos archivos trae.
        self.assertEqual(texto.count("Archivos incluidos:"), 2)
        # Y el resumen cierra el archivo.
        self.assertTrue(texto.rstrip().endswith("un segundo."), texto[-120:])
        self.assertIn("RESUMEN", texto)
        resumen = plano(texto.split("RESUMEN")[-1])
        self.assertIn("2 repositorios", resumen)
        self.assertIn("1 binario omitido", resumen)
        self.assertEqual(st["bytes"], out.stat().st_size)
        self.assertEqual(st["files"], 4)   # 3 de texto + el binario listado

    def test_project_txt_also_explains_itself(self):
        out = self.tmp / "web.txt"
        st = consolidate_project(self.web, out)
        texto = out.read_text(encoding="utf-8")
        self.assertIn("contenido del proyecto «web»", plano(texto))
        self.assertLess(texto.index("Este archivo reúne"),
                        texto.index("ESTRUCTURA DEL PROYECTO"))
        self.assertLess(texto.index("ESTRUCTURA DEL PROYECTO"),
                        texto.index("MODULO: index.html"))
        self.assertIn("RESUMEN", texto)
        self.assertEqual(st["bytes"], out.stat().st_size)
        self.assertEqual(st["binaries"], 1)

    def test_the_description_never_mentions_a_file_that_is_not_there(self):
        out = self.tmp / "web.txt"
        consolidate_project(self.web, out)
        texto = out.read_text(encoding="utf-8")
        self.assertNotIn("web.txt", texto)      # el TXT no se incluye a sí mismo


if __name__ == "__main__":
    unittest.main()
