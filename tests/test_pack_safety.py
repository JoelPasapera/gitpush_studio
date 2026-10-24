# -*- coding: utf-8 -*-
"""El TXT del proyecto no puede escribir fuera de su sitio, ni copiar lo
que no es del proyecto, ni colgarse, ni romperse a medias.

  · un enlace (aunque apunte a algo que no existe) con el nombre del TXT
    nunca se sigue: escribir a través de él podía crear o pisar
    ~/.ssh/authorized_keys al empaquetar un repositorio descargado;
  · un enlace a un archivo de fuera se describe, no se copia (una clave
    privada tuya no puede acabar dentro del TXT que pegas en un chat);
  · una tubería (FIFO) en el proyecto no deja el empaquetado esperando
    para siempre;
  · un nombre que no es UTF-8 no tumba ni el TXT ni la huella;
  · `pack` (CLI) y «Generar TXT» no pisan un .txt tuyo;
  · si algo falla a mitad, el TXT anterior sigue entero.
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from gitpush_studio import cli
from gitpush_studio.core import pack
from gitpush_studio.core.pack import (consolidate_each, consolidate_many,
                                      consolidate_project, free_txt_path,
                                      write_modules)
from gitpush_studio.core.walk import folder_fingerprint

POSIX = os.name == "posix"


def write(folder, rel, content="x\n"):
    p = Path(folder) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode("utf-8")
    p.write_bytes(content)
    return p


def links_allowed(tmp):
    try:
        os.symlink("x", os.path.join(tmp, ".prueba-enlace"))
        os.unlink(os.path.join(tmp, ".prueba-enlace"))
        return True
    except (OSError, NotImplementedError):
        return False


class TheTxtStaysWhereItBelongs(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.proj = self.tmp / "tool"
        write(self.proj, "main.py", "print('hola')\n")
        if not links_allowed(str(self.tmp)):
            self.skipTest("este sistema no permite crear enlaces")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_dangling_link_with_the_txt_name_is_never_followed(self):
        fuera = self.tmp / "fuera" / "authorized_keys"
        fuera.parent.mkdir()
        os.symlink(str(fuera), str(self.proj / "tool.txt"))
        res = consolidate_each([self.proj])
        self.assertFalse(fuera.exists(), "se escribió fuera del proyecto")
        self.assertTrue(os.path.islink(self.proj / "tool.txt"))
        self.assertEqual(res["count"], 1, res["failed"])
        self.assertEqual(Path(res["paths"][0]).name, "tool-unificado.txt")

    def test_a_link_to_a_txt_of_ours_is_still_not_ours(self):
        """Un enlace nunca es «nuestro TXT», aunque apunte a uno: rehacerlo
        cambiaría tu enlace por un archivo."""
        otro = self.tmp / "fuera" / "viejo.txt"
        otro.parent.mkdir()
        consolidate_project(self.proj, otro)          # un TXT nuestro, fuera
        antes = otro.read_bytes()
        os.symlink(str(otro), str(self.proj / "tool.txt"))
        res = consolidate_each([self.proj])
        self.assertEqual(Path(res["paths"][0]).name, "tool-unificado.txt")
        self.assertTrue(os.path.islink(self.proj / "tool.txt"))
        self.assertEqual(otro.read_bytes(), antes)

    def test_publishing_with_the_txt_never_follows_a_link(self):
        """El paso del TXT de PUBLICAR resolvía la ruta entera
        (`realpath`), enlace incluido, y escribía donde apuntara."""
        from tests.gitenv import HAVE_GIT, init_bare
        if not HAVE_GIT:
            self.skipTest("git no disponible")
        from gitpush_studio.core.publish import PublishJob, publish
        fuera = self.tmp / "fuera" / "authorized_keys"
        fuera.parent.mkdir()
        os.symlink(str(fuera), str(self.proj / "tool.txt"))
        bare = init_bare(self.tmp, "remoto")
        r = publish(PublishJob(folder=str(self.proj), url=bare, user="T",
                               email="t@x.y", preview=False, check=False,
                               consolidate=True))
        self.assertEqual(r.status, "ok", r.message)
        self.assertFalse(fuera.exists(), "se escribió fuera del proyecto")
        self.assertEqual(Path(r.txt_path).name, "tool-unificado.txt")
        self.assertTrue(os.path.islink(self.proj / "tool.txt"))

    def test_an_explicit_destination_that_is_a_link_is_not_followed(self):
        fuera = self.tmp / "fuera.txt"
        destino = self.tmp / "salida.txt"
        os.symlink(str(fuera), str(destino))
        real, renombrado = free_txt_path(destino)
        self.assertTrue(renombrado)
        consolidate_project(self.proj, real)
        self.assertFalse(fuera.exists())
        # Y aunque alguien pidiera escribir justo ahí, se reemplaza el
        # enlace, no lo que hay al otro lado.
        consolidate_project(self.proj, destino)
        self.assertFalse(fuera.exists())
        self.assertFalse(os.path.islink(destino))

    def test_links_to_files_outside_are_described_not_copied(self):
        secreto = self.tmp / "id_ed25519"
        secreto.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nSECRETO\n")
        os.symlink(str(secreto), str(self.proj / "notes.md"))
        os.symlink(str(self.tmp / "no-existe"), str(self.proj / "roto.md"))
        for use_git in (False, True):
            if use_git:
                subprocess.run(["git", "init", "-q", str(self.proj)],
                               check=True, capture_output=True)
            out = self.tmp / f"txt-{use_git}.txt"
            consolidate_project(self.proj, out, use_git=use_git)
            texto = out.read_text(encoding="utf-8")
            self.assertNotIn("SECRETO", texto)
            self.assertIn("MODULO: notes.md", texto)
            self.assertIn("<ENLACE SIMBÓLICO", texto)
            self.assertIn("MODULO: roto.md", texto)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "sin tuberías con nombre")
    def test_a_fifo_does_not_hang_the_txt(self):
        os.mkfifo(str(self.proj / "tuberia"))
        out = self.tmp / "salida.txt"
        hilo = threading.Thread(target=consolidate_project,
                                args=(self.proj, out), daemon=True)
        hilo.start()
        hilo.join(20)
        self.assertFalse(hilo.is_alive(), "el empaquetado se quedó colgado")
        texto = out.read_text(encoding="utf-8")
        self.assertIn("MODULO: tuberia", texto)
        self.assertIn("OMITIDO", texto)


class NamesAndFailures(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.proj = self.tmp / "proj"
        write(self.proj, "a.txt", "A\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "nombres no UTF-8: solo donde el sistema los admite")
    def test_names_that_are_not_utf8(self):
        with open(os.path.join(os.fsencode(self.proj), b"caf\xe9.txt"),
                  "wb") as fh:
            fh.write(b"contenido\n")
        out = self.tmp / "salida.txt"
        st = consolidate_project(self.proj, out)
        self.assertEqual(st["files"], 2)
        self.assertIn("MODULO: caf�.txt",
                      out.read_text(encoding="utf-8"))
        st = consolidate_many("ana", [self.proj], self.tmp / "perfil.txt")
        self.assertEqual(st["files"], 2)
        self.assertTrue(folder_fingerprint(self.proj, []))

    def test_a_file_that_grows_after_listing_is_marked_as_cut(self):
        grande = write(self.proj, "crece.log", "x" * 500)
        out = io.BytesIO()
        contadores = {"binaries": 0, "big": 0}
        write_modules(out, [("crece.log", str(grande), 10)], contadores,
                      max_file_bytes=100, max_total_bytes=10 ** 6)
        texto = out.getvalue().decode("utf-8")
        self.assertIn("ARCHIVO CORTADO", texto)
        self.assertEqual(texto.count("x"), 100)

    def test_a_failure_halfway_keeps_the_previous_txt_whole(self):
        out = self.tmp / "perfil.txt"
        consolidate_many("ana", [self.proj], out)
        antes = out.read_bytes()
        otro = self.tmp / "otro"
        write(otro, "b.txt", "B\n")
        reales = pack.write_modules
        llamadas = []

        def falla_al_segundo(*a, **kw):
            llamadas.append(1)
            if len(llamadas) == 2:
                raise OSError(28, "No space left on device")
            return reales(*a, **kw)

        with mock.patch.object(pack, "write_modules", falla_al_segundo):
            with self.assertRaises(OSError):
                consolidate_many("ana", [self.proj, otro], out)
        self.assertEqual(out.read_bytes(), antes)
        self.assertEqual([p.name for p in self.tmp.iterdir()
                          if p.name.startswith(".perfil")], [],
                         "quedó un temporal a medias")

    def test_cli_pack_never_overwrites_your_txt(self):
        notas = self.tmp / "proj.txt"
        notas.write_text("MIS APUNTES\n", encoding="utf-8")
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            rc = cli.main(["pack", str(self.proj)])
        self.assertEqual(rc, 0)
        self.assertEqual(notas.read_text(encoding="utf-8"), "MIS APUNTES\n")
        self.assertTrue((self.tmp / "proj-unificado.txt").exists())
        self.assertIn("no lo toco", salida.getvalue())

    def test_quoting_our_header_does_not_make_a_file_ours(self):
        notas = self.tmp / "proj.txt"
        notas.write_text("Mis notas sobre el formato:\nMODULO RAIZ: x\n",
                         encoding="utf-8")
        self.assertEqual(free_txt_path(notas)[1], True)
        propio = self.tmp / "nuestro.txt"
        consolidate_project(self.proj, propio)
        self.assertEqual(free_txt_path(propio), (propio, False))


if __name__ == "__main__":
    unittest.main()
