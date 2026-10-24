# -*- coding: utf-8 -*-
"""«Abrir ubicación del archivo»: la carpeta que contiene el TXT, con el
archivo seleccionado, en cada sistema.

Las llamadas reales dependen del escritorio (Explorador, Finder, el
gestor de archivos de Linux), así que aquí se comprueba QUÉ se le pide a
cada uno, con los procesos sustituidos:

  · Windows: primero la API del shell; si falla, `explorer /select,`
    con la ruta entre comillas (una coma en el nombre de una carpeta no
    puede partirla);
  · macOS: `open -R`;
  · Linux: `ShowItems` de FileManager1 por D-Bus, con la ruta como URI
    escapada; si no hay gestor que responda, la carpeta sin selección;
  · si el archivo ya no existe, se abre la carpeta donde estaba;
  · nunca lanza.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gitpush_studio.core import desktop


class Base(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.carpeta = self.tmp / "Notas, 2026 (copia)"
        self.carpeta.mkdir()
        self.txt = self.carpeta / "mi proyecto.txt"
        self.txt.write_text("MODULO RAIZ: x\n", encoding="utf-8")
        self.abiertos = []
        self._p = mock.patch.object(desktop, "open_path",
                                    lambda ruta: self.abiertos.append(
                                        os.fspath(ruta)) or True)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plataforma(self, nombre):
        return mock.patch.object(desktop, "platform_name",
                                 return_value=nombre)


class WindowsRevealsWithTheShell(Base):

    def test_the_shell_api_is_used_first(self):
        llamadas = []
        with self.plataforma("windows"), \
                mock.patch.object(desktop, "_shell_select",
                                  lambda r: llamadas.append(r) or True), \
                mock.patch.object(desktop.subprocess, "Popen") as popen:
            self.assertTrue(desktop.reveal_in_folder(self.txt))
        self.assertEqual(llamadas, [os.path.normpath(str(self.txt))])
        popen.assert_not_called()
        self.assertEqual(self.abiertos, [])

    def test_without_the_shell_api_explorer_selects_it(self):
        def falla(_ruta):
            raise OSError("sin COM")
        with self.plataforma("windows"), \
                mock.patch.object(desktop, "_shell_select", falla), \
                mock.patch.object(desktop.subprocess, "Popen") as popen:
            self.assertTrue(desktop.reveal_in_folder(self.txt))
        orden = popen.call_args[0][0]
        self.assertEqual(orden, 'explorer /select,"%s"'
                         % os.path.normpath(str(self.txt)))

    def test_a_comma_in_a_folder_name_cannot_split_the_path(self):
        """El Explorador separa sus argumentos por comas: sin comillas,
        «Notas, 2026» abría otra carpeta."""
        orden = desktop.explorer_select_command(r"C:\Users\PC\Notas, 2026\x.txt")
        self.assertEqual(orden, r'explorer /select,"C:\Users\PC\Notas, 2026\x.txt"')


class MacRevealsInFinder(Base):

    def test_open_minus_r(self):
        with self.plataforma("mac"), \
                mock.patch.object(desktop.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            self.assertTrue(desktop.reveal_in_folder(self.txt))
        self.assertEqual(run.call_args[0][0],
                         ["open", "-R", os.path.abspath(str(self.txt))])
        self.assertEqual(self.abiertos, [])


class LinuxAsksTheFileManager(Base):

    def test_show_items_by_dbus_with_an_escaped_uri(self):
        with self.plataforma("linux"), \
                mock.patch.object(desktop.shutil, "which",
                                  lambda n: "/usr/bin/" + n), \
                mock.patch.object(desktop.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            self.assertTrue(desktop.reveal_in_folder(self.txt))
        orden = run.call_args[0][0]
        self.assertEqual(orden[0], "dbus-send")
        self.assertIn("org.freedesktop.FileManager1.ShowItems", orden)
        lista = [a for a in orden if a.startswith("array:string:")][0]
        uri = lista[len("array:string:"):]
        # Ni la coma ni los espacios pueden partir la lista de dbus-send.
        self.assertNotIn(",", uri)
        self.assertNotIn(" ", uri)
        self.assertEqual(uri, Path(os.path.abspath(str(self.txt))).as_uri())
        self.assertEqual(self.abiertos, [])

    def test_gdbus_when_dbus_send_is_missing(self):
        with self.plataforma("linux"), \
                mock.patch.object(desktop.shutil, "which",
                                  lambda n: None if n == "dbus-send"
                                  else "/usr/bin/" + n), \
                mock.patch.object(desktop.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            self.assertTrue(desktop.reveal_in_folder(self.txt))
        orden = run.call_args[0][0]
        self.assertEqual(orden[:2], ["gdbus", "call"])
        self.assertNotIn("'", orden[-2][2:-2])      # la URI no rompe '…'

    def test_no_file_manager_answers_so_the_folder_opens(self):
        with self.plataforma("linux"), \
                mock.patch.object(desktop.shutil, "which",
                                  lambda n: "/usr/bin/" + n), \
                mock.patch.object(desktop.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 1)
            self.assertTrue(desktop.reveal_in_folder(self.txt))
        self.assertEqual(self.abiertos, [os.path.abspath(str(self.carpeta))])

    def test_a_hung_file_manager_does_not_hang_us(self):
        with self.plataforma("linux"), \
                mock.patch.object(desktop.shutil, "which",
                                  lambda n: "/usr/bin/" + n), \
                mock.patch.object(desktop.subprocess, "run",
                                  side_effect=subprocess.TimeoutExpired(
                                      "dbus-send", 5)):
            self.assertTrue(desktop.reveal_in_folder(self.txt))
        self.assertEqual(self.abiertos, [os.path.abspath(str(self.carpeta))])


class EdgeCases(Base):

    def test_a_txt_that_is_gone_opens_its_folder(self):
        self.txt.unlink()
        with self.plataforma("windows"), \
                mock.patch.object(desktop, "_shell_select") as shell:
            self.assertTrue(desktop.reveal_in_folder(self.txt))
        shell.assert_not_called()
        self.assertEqual(self.abiertos, [os.path.abspath(str(self.carpeta))])

    def test_nothing_left_to_open_says_so(self):
        shutil.rmtree(self.carpeta)
        self.assertFalse(desktop.reveal_in_folder(self.txt))
        self.assertEqual(self.abiertos, [])

    def test_it_never_raises(self):
        with self.plataforma("mac"), \
                mock.patch.object(desktop.subprocess, "run",
                                  side_effect=RuntimeError("raro")):
            self.assertTrue(desktop.reveal_in_folder(self.txt))
        # No pudo señalarlo: al menos, la carpeta.
        self.assertEqual(self.abiertos, [os.path.abspath(str(self.carpeta))])


class OpenPath(unittest.TestCase):

    def test_each_system_uses_its_own_opener(self):
        with mock.patch.object(desktop, "platform_name", return_value="mac"), \
                mock.patch.object(desktop.subprocess, "run") as run:
            self.assertTrue(desktop.open_path("/x/y.txt"))
        self.assertEqual(run.call_args[0][0], ["open", "/x/y.txt"])
        with mock.patch.object(desktop, "platform_name",
                               return_value="linux"), \
                mock.patch.object(desktop.subprocess, "run") as run:
            self.assertTrue(desktop.open_path("/x/y.txt"))
        self.assertEqual(run.call_args[0][0], ["xdg-open", "/x/y.txt"])

    def test_a_missing_opener_is_false_not_an_exception(self):
        with mock.patch.object(desktop, "platform_name",
                               return_value="linux"), \
                mock.patch.object(desktop.subprocess, "run",
                                  side_effect=FileNotFoundError("xdg-open")):
            self.assertFalse(desktop.open_path("/x/y.txt"))


if __name__ == "__main__":
    unittest.main()
