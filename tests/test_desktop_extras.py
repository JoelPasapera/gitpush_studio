# -*- coding: utf-8 -*-
"""Comodidades del escritorio: atajos, soltar carpetas, bandeja (en
Windows; aquí se comprueba que fuera de Windows no hacen nada), abrir al
iniciar Windows, avisos de versiones nuevas y el icono."""

import io
import json
import struct
import sys
import zlib
from pathlib import Path
from unittest import mock

import tests.tkstub as tkstub

tkstub.install()

from tests.test_ui_headless import HeadlessCase, write            # noqa: E402

import unittest                                                     # noqa: E402

from gitpush_studio.core import startup, updates                  # noqa: E402
from gitpush_studio.ui import help as help_module, winshell      # noqa: E402

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
from tools import crear_icono                                     # noqa: E402


class ShortcutAndDropTests(HeadlessCase):

    def test_keyboard_shortcuts(self):
        llamados = []
        for metodo in ("start_publish", "show_help", "save_profile",
                       "start_scan", "open_consolidate", "open_apply_txt"):
            setattr(self.app, metodo,
                    lambda m=metodo: llamados.append(m))
        for secuencia in ("<Control-Return>", "<F1>", "<Control-s>",
                          "<F5>", "<Control-t>", "<Control-r>"):
            self.assertEqual(self.app.event(secuencia), "break")
        self.assertEqual(llamados, ["start_publish", "show_help",
                                    "save_profile", "start_scan",
                                    "open_consolidate", "open_apply_txt"])

    def test_every_shortcut_points_to_a_real_action_and_is_explained(self):
        ayuda = self.app.log_box.all_text()
        for _secuencia, metodo, tecla, que in help_module.SHORTCUTS:
            self.assertTrue(callable(getattr(self.app, metodo, None)), metodo)
            if tecla:
                self.assertIn(f"{tecla}: {que}", ayuda)

    def test_dropping_a_folder_sets_the_project(self):
        otra = Path(self.tmp) / "otro proyecto"
        write(otra, "a.txt", "a\n")
        self.app.on_paths_dropped([str(otra)])
        self.assertEqual(Path(self.app.e_folder.get()), otra)
        self.assertIn("Carpeta del proyecto", self.app.log_box.all_text())

    def test_dropping_a_txt_opens_apply(self):
        txt = Path(self.tmp) / "cambios.txt"
        txt.write_text("MODULO: a.txt\n```\nb\n```\n", encoding="utf-8")
        self.fill_form()
        self.app.on_paths_dropped([str(txt)])
        dialog = self.app._dialogs["apply_txt"]
        self.assertEqual(dialog.source_file, str(txt))
        dialog.destroy()

    def test_dropping_something_else_explains(self):
        archivo = Path(self.tmp) / "foto.png"
        archivo.write_bytes(b"\x89PNG")
        self.app.on_paths_dropped([str(archivo)])
        self.assertIn("Suelta una carpeta", self.app.log_box.all_text())

    def test_outside_windows_the_shell_does_nothing(self):
        shell = self.app.shell
        if sys.platform.startswith("win"):
            self.skipTest("en Windows sí hace cosas")
        self.assertFalse(shell.install())
        self.assertFalse(shell.enable_drop(print))
        self.assertFalse(shell.tray_show("x", print))
        self.assertFalse(shell.notify("t", "x"))
        shell.tray_hide()
        shell.uninstall()

    def test_update_notice(self):
        self.app.config_data["update_repo"] = "ana/gitpush_studio"
        self.app.config_data["update_checked_at"] = 0
        with mock.patch.object(updates, "newer_than",
                               lambda current, repo: ("v9.0.0",
                                                      "https://github.com/"
                                                      "ana/gitpush_studio/"
                                                      "releases/v9.0.0")):
            self.app.check_for_updates()
            tkstub.Scheduler.pump(
                lambda: "versión nueva" in self.app.log_box.all_text())
        self.assertIn("v9.0.0", self.app.log_box.all_text())
        self.assertGreater(self.app.config_data["update_checked_at"], 0)

    def test_no_repository_means_no_question(self):
        self.app.config_data["update_repo"] = ""
        with mock.patch.object(updates, "newer_than",
                               lambda *a: self.fail("no debía preguntar")):
            self.app.check_for_updates()


class DropPathsTests(unittest.TestCase):

    def test_reading_a_drop(self):
        rutas = ["C:\\proyectos\\web", "D:\\notas.txt"]

        def query(hdrop, index, buffer, size):
            self.assertEqual(hdrop, 1234)
            if index == 0xFFFFFFFF:
                return len(rutas)
            if buffer is None:
                return len(rutas[index])
            buffer.value = rutas[index]
            return len(rutas[index])
        self.assertEqual(winshell.drop_paths(query, 1234), rutas)


class FakeKey:
    def __init__(self, store, writable):
        self.store = store
        self.writable = writable

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeWinreg:
    """Lo justo de `winreg` para las pruebas."""
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    def OpenKey(self, root, path, reserved, access):
        return FakeKey(self.values, access == self.KEY_SET_VALUE)

    def CreateKeyEx(self, root, path, reserved, access):
        return FakeKey(self.values, True)

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


class StartupTests(unittest.TestCase):

    def test_enable_and_disable(self):
        fake = FakeWinreg()
        self.assertFalse(startup.is_enabled(fake))
        startup.enable('"C:\\app.exe" --al-iniciar', fake)
        self.assertTrue(startup.is_enabled(fake))
        self.assertEqual(fake.values["GitPush Studio"],
                         '"C:\\app.exe" --al-iniciar')
        startup.disable(fake)
        self.assertFalse(startup.is_enabled(fake))
        startup.disable(fake)                   # sin valor: no pasa nada

    def test_the_command(self):
        exe = startup.launch_command("C:\\Apps\\GitPush Studio.exe",
                                     frozen=True)
        self.assertEqual(exe, '"C:\\Apps\\GitPush Studio.exe" --al-iniciar')
        py = startup.launch_command("/usr/bin/python3", "/x/GitPushStudio.pyw",
                                    frozen=False)
        self.assertEqual(py, '"/usr/bin/python3" "/x/GitPushStudio.pyw" '
                             '--al-iniciar')
        self.assertTrue(startup.started_at_login(["app", "--al-iniciar"]))
        self.assertFalse(startup.started_at_login(["app"]))


class UpdatesTests(unittest.TestCase):

    def opener(self, payload):
        def opener(url, headers=None, timeout=10):
            self.assertIn("/repos/ana/gitpush_studio/releases/latest", url)
            return io.BytesIO(json.dumps(payload).encode())
        return opener

    def test_versions(self):
        self.assertEqual(updates.parse_version("v3.4.0"), (3, 4, 0))
        self.assertEqual(updates.parse_version("GitPush 3.4"), (3, 4, 0))
        self.assertIsNone(updates.parse_version("sin número"))
        self.assertTrue(updates.valid_repo("ana/gitpush_studio"))
        for malo in ("", "ana", "ana/", "../x", "a/b/c", "ana/gitpush studio"):
            self.assertFalse(updates.valid_repo(malo), malo)

    def test_once_a_day(self):
        self.assertTrue(updates.due(0, now=1_000_000))
        self.assertFalse(updates.due(1_000_000, now=1_000_000 + 3600))
        self.assertTrue(updates.due(1_000_000, now=1_000_000 + 90_000))
        self.assertTrue(updates.due(2_000_000, now=1_000_000))   # reloj atrás
        self.assertTrue(updates.due("basura", now=1_000_000))

    def test_newer_or_not(self):
        nueva = {"tag_name": "v3.4.0",
                 "html_url": "https://github.com/ana/gitpush_studio/"
                             "releases/tag/v3.4.0"}
        found = updates.newer_than("3.3.0", "ana/gitpush_studio",
                                   self.opener(nueva))
        self.assertEqual(found[0], "v3.4.0")
        self.assertIsNone(updates.newer_than("3.4.0", "ana/gitpush_studio",
                                             self.opener(nueva)))
        rara = {"tag_name": "v9.0.0", "html_url": "https://malo.example/x"}
        self.assertIsNone(updates.newer_than("3.3.0", "ana/gitpush_studio",
                                             self.opener(rara)))

    def test_network_problems_are_silent(self):
        def broken(url, headers=None, timeout=10):
            raise OSError("sin red")
        self.assertIsNone(updates.newer_than("3.3.0", "ana/gitpush_studio",
                                             broken))


class IconTests(unittest.TestCase):

    def test_the_ico_holds_valid_pngs(self):
        data = (RAIZ / "gitpush_studio" / "assets" / "gitpush.ico").read_bytes()
        reserved, kind, count = struct.unpack("<HHH", data[:6])
        self.assertEqual((reserved, kind), (0, 1))
        self.assertEqual(count, len(crear_icono.SIZES))
        for index, size in enumerate(crear_icono.SIZES):
            entry = data[6 + 16 * index:6 + 16 * (index + 1)]
            width, _h, _c, _r, _planes, bits, length, offset = \
                struct.unpack("<BBBBHHII", entry)
            self.assertEqual(width, 0 if size == 256 else size)
            self.assertEqual(bits, 32)
            png = data[offset:offset + length]
            self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
            ihdr_width, ihdr_height = struct.unpack(">II", png[16:24])
            self.assertEqual((ihdr_width, ihdr_height), (size, size))
            idat_length = struct.unpack(">I", png[33:37])[0]
            raw = zlib.decompress(png[41:41 + idat_length])
            self.assertEqual(len(raw), size * (size * 4 + 1))

    def test_the_drawing(self):
        self.assertTrue(crear_icono.inside_arrow(0.5, 0.3))
        self.assertTrue(crear_icono.inside_arrow(0.5, 0.8))
        self.assertFalse(crear_icono.inside_arrow(0.2, 0.8))
        self.assertFalse(crear_icono.inside_rounded_square(0.01, 0.01))
        self.assertTrue(crear_icono.inside_rounded_square(0.5, 0.5))
