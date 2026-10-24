# -*- coding: utf-8 -*-
"""Analíticas del historial de git (core.gitstats).

  · el parser lee lo que git escribe con `--numstat -z`, también los
    renombrados, los binarios y las rutas con espacios o tildes, y no se
    rompe si un registro llega cortado;
  · la lectura en flujo da los mismos registros se corte donde se corte;
  · los renombrados se siguen: el historial de un archivo movido no se
    parte en dos;
  · las personas se unen (mismo nombre o mismo correo) sin fundir a
    desconocidos por un correo de relleno;
  · hora y día salen en la zona horaria del AUTOR.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.gitenv import HAVE_GIT, git, init_repo

from gitpush_studio.core.gitstats import (aggregate, analyze_history,
                                          author_name_key, bus_factor,
                                          iter_records, parse_record)

SEP = b"\x1e"
F = b"\x1f"


def registro(sha, nombre, correo, marca, iso, entradas):
    """Un commit tal como lo escribe git log --numstat -z."""
    cab = SEP + F.join([sha, nombre.encode("utf-8"), correo.encode(),
                        str(marca).encode(), iso.encode()]) + b"\x00\n"
    cuerpo = b""
    for e in entradas:
        if len(e) == 4:                                  # renombrado
            a, d, viejo, nuevo = e
            cuerpo += (b"%s\t%s\t\x00" % (a, d) + viejo.encode("utf-8")
                       + b"\x00" + nuevo.encode("utf-8") + b"\x00")
        else:
            a, d, ruta = e
            cuerpo += b"%s\t%s\t" % (a, d) + ruta.encode("utf-8") + b"\x00"
    return cab + cuerpo


def parsea(raw):
    return [parse_record(r) for r in iter_records([raw])]


class ParserTests(unittest.TestCase):

    def test_normal_binary_and_rename(self):
        raw = registro(b"a" * 40, "Ana Pérez", "ana@x.y", 100,
                       "2026-10-16 09:30:00 -0500",
                       [(b"3", b"1", "src/con espacio.py"),
                        (b"-", b"-", "img/logo.png"),
                        (b"2", b"0", "viejo nombre.txt", "nuevo/ñandú.txt")])
        (sha, nombre, correo, marca, iso, cambios), = parsea(raw)
        self.assertEqual((nombre, correo, marca), ("Ana Pérez", "ana@x.y", 100))
        self.assertEqual(cambios, [
            (3, 1, None, "src/con espacio.py"),
            (0, 0, None, "img/logo.png"),
            (2, 0, "viejo nombre.txt", "nuevo/ñandú.txt")])

    def test_a_commit_without_files(self):
        raw = registro(b"b" * 40, "X", "x@y", 1, "2026-01-01 00:00:00 +0000",
                       [])
        self.assertEqual(parsea(raw)[0][5], [])

    def test_truncated_or_garbage_records_do_not_crash(self):
        bueno = registro(b"c" * 40, "X", "x@y", 1,
                         "2026-01-01 00:00:00 +0000",
                         [(b"1", b"0", "a.txt", "b.txt")])
        cortado = bueno[:-10]                     # renombrado a medias
        self.assertIsNotNone(parsea(cortado)[0])
        self.assertIsNone(parse_record(b"basura sin campos"))
        self.assertIsNone(parse_record(F.join([b"s", b"n", b"e", b"NaN",
                                               b"iso"]) + b"\x00"))

    def test_streaming_gives_the_same_records_wherever_it_is_cut(self):
        raw = b"".join(registro(bytes([65 + i]) * 40, "Autor %d" % i,
                                "a%d@x.y" % i, i, "2026-01-01 10:00:00 +0000",
                                [(b"1", b"0", "f%d.txt" % i)])
                       for i in range(5))
        entero = list(iter_records([raw]))
        self.assertEqual(len(entero), 5)
        for corte in range(1, len(raw)):
            with self.subTest(corte=corte):
                self.assertEqual(list(iter_records([raw[:corte],
                                                    raw[corte:]])), entero)
        # Y troceado de byte en byte.
        self.assertEqual(list(iter_records(raw[i:i + 1]
                                           for i in range(len(raw)))), entero)


class AggregateTests(unittest.TestCase):

    def agrega(self, *regs, existing=None):
        return aggregate([parse_record(r) for r in
                          iter_records([b"".join(regs)])], existing)

    def test_renames_are_followed_to_today_s_name(self):
        # git log va del más nuevo al más viejo.
        rep = self.agrega(
            registro(b"3" * 40, "A", "a@x", 300, "2026-03-01 10:00:00 +0000",
                     [(b"1", b"1", "c.txt")]),
            registro(b"2" * 40, "A", "a@x", 200, "2026-02-01 10:00:00 +0000",
                     [(b"0", b"0", "b.txt", "c.txt")]),
            registro(b"1" * 40, "A", "a@x", 100, "2026-01-01 10:00:00 +0000",
                     [(b"5", b"0", "a.txt", "b.txt")]),
            registro(b"0" * 40, "A", "a@x", 50, "2025-12-01 10:00:00 +0000",
                     [(b"9", b"0", "a.txt")]),
            existing={"c.txt"})
        self.assertEqual([(c.path, c.commits, c.added) for c in rep.churn],
                         [("c.txt", 4, 15)])
        self.assertTrue(rep.churn[0].exists)

    def test_months_are_continuous_even_with_gaps(self):
        rep = self.agrega(
            registro(b"2" * 40, "A", "a@x", 2, "2026-03-15 10:00:00 +0000", []),
            registro(b"1" * 40, "A", "a@x", 1, "2025-12-02 10:00:00 +0000", []))
        self.assertEqual(rep.months, [("2025-12", 1), ("2026-01", 0),
                                      ("2026-02", 0), ("2026-03", 1)])

    def test_hour_and_weekday_are_the_author_s(self):
        # 2026-10-16 es viernes; 23:50 en Lima es sábado 04:50 en UTC.
        rep = self.agrega(registro(b"1" * 40, "A", "a@x", 1,
                                   "2026-10-16 23:50:00 -0500", []))
        self.assertEqual(rep.heatmap[4][23], 1)           # vie, 23 h
        self.assertEqual(sum(map(sum, rep.heatmap)), 1)

    def test_people_are_merged_by_name_or_email(self):
        rep = self.agrega(
            registro(b"4" * 40, "☿ Kenneth Reitz", "me@kr.com", 400,
                     "2026-01-01 10:00:00 +0000", [(b"1", b"0", "a")]),
            registro(b"3" * 40, "Kenneth Reitz", "otro@kr.com", 300,
                     "2025-01-01 10:00:00 +0000", [(b"1", b"0", "a")]),
            registro(b"2" * 40, "kennethreitz", "me@kr.com", 200,
                     "2024-01-01 10:00:00 +0000", [(b"1", b"0", "a")]),
            registro(b"1" * 40, "Ana Pérez", "ana@x.y", 100,
                     "2023-01-01 10:00:00 +0000", [(b"1", b"0", "a")]),
            registro(b"0" * 40, "Ana Perez", "ana@trabajo.y", 50,
                     "2022-01-01 10:00:00 +0000", [(b"1", b"0", "a")]))
        gente = {a.name: a.commits for a in rep.authors}
        # El nombre que se enseña es el más reciente de cada persona.
        self.assertEqual(gente, {"☿ Kenneth Reitz": 3, "Ana Pérez": 2})

    def test_placeholder_emails_and_generic_names_do_not_merge_strangers(self):
        rep = self.agrega(
            registro(b"3" * 40, "Ana", "you@example.com", 3,
                     "2026-01-01 10:00:00 +0000", []),
            registro(b"2" * 40, "Luis", "you@example.com", 2,
                     "2026-01-01 10:00:00 +0000", []),
            registro(b"1" * 40, "root", "a@uno.pe", 1,
                     "2026-01-01 10:00:00 +0000", []),
            registro(b"0" * 40, "root", "b@dos.pe", 0,
                     "2026-01-01 10:00:00 +0000", []))
        self.assertEqual(len(rep.authors), 4)

    def test_bus_factor(self):
        self.assertEqual(bus_factor([]), 0)
        self.assertEqual(bus_factor([10]), 1)
        self.assertEqual(bus_factor([5, 5]), 2)            # nadie > 50 %
        self.assertEqual(bus_factor([3, 3, 3]), 2)
        self.assertEqual(bus_factor([90, 5, 5]), 1)

    def test_name_keys(self):
        self.assertEqual(author_name_key("☿ Kenneth  Reitz "), "kenneth reitz")
        self.assertEqual(author_name_key("Ana PÉREZ"), "ana perez")
        self.assertEqual(author_name_key("***"), "")


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class RealRepositoryTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = Path(init_repo(self.tmp / "r"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def commit(self, nombre, correo, fecha, mensaje):
        env = dict(os.environ, GIT_AUTHOR_NAME=nombre,
                   GIT_AUTHOR_EMAIL=correo, GIT_AUTHOR_DATE=fecha,
                   GIT_COMMITTER_DATE=fecha)
        subprocess.run(["git", "-c", "core.autocrlf=false", "add", "-A"],
                       cwd=self.repo, check=True, env=env,
                       capture_output=True)
        subprocess.run(["git", "-c", "core.autocrlf=false", "commit", "-q",
                        "-m", mensaje], cwd=self.repo, check=True, env=env,
                       capture_output=True)

    def test_history_of_a_real_repository(self):
        (self.repo / "carpeta vieja").mkdir()
        (self.repo / "carpeta vieja" / "módulo.py").write_text(
            "\n".join("linea %d" % i for i in range(30)) + "\n",
            encoding="utf-8")
        self.commit("Ana Pérez", "ana@x.y", "2026-01-05T10:00:00-05:00", "1")
        git(self.repo, "mv", "carpeta vieja", "nueva")
        self.commit("Ana Pérez", "ana@x.y", "2026-02-05T11:00:00-05:00", "2")
        with open(self.repo / "nueva" / "módulo.py", "a",
                  encoding="utf-8") as fh:
            fh.write("otra\n")
        self.commit("Luis", "luis@x.y", "2026-04-07T23:30:00+01:00", "3")

        rep = analyze_history(str(self.repo), existing={"nueva/módulo.py"})
        self.assertEqual(rep.commits, 3)
        self.assertEqual({a.name: a.commits for a in rep.authors},
                         {"Ana Pérez": 2, "Luis": 1})
        # El movimiento de carpeta no parte el historial del archivo.
        self.assertEqual([(c.path, c.commits) for c in rep.churn],
                         [("nueva/módulo.py", 3)])
        self.assertEqual([m for m, _ in rep.months],
                         ["2026-01", "2026-02", "2026-03", "2026-04"])
        self.assertEqual(rep.heatmap[1][23], 1)         # martes 23 h (Luis)
        self.assertEqual(rep.bus_factor, 1)

    def test_no_repository_or_no_commits_gives_none(self):
        otra = self.tmp / "sin_git"
        otra.mkdir()
        self.assertIsNone(analyze_history(str(otra)))
        self.assertIsNone(analyze_history(str(self.repo)))      # sin commits


if __name__ == "__main__":
    unittest.main()
