# -*- coding: utf-8 -*-
"""La interfaz y el repositorio equivocado (ver `test_remotes`).

  · publicar una carpeta conectada a otro repositorio pregunta, y cada
    respuesta hace exactamente lo que dice (y cerrar = no publicar);
  · la auto-publicación no pregunta: se pone en pausa sin subir nada;
  · al abrir una carpeta conectada, una URL vacía se rellena con la suya
    y una distinta se avisa (una sola vez);
  · cambiar la carpeta del formulario con la auto-publicación en marcha
    la pone en pausa: nunca pasa a vigilar y publicar otra carpeta en el
    repositorio de la anterior.
"""

import unittest
from pathlib import Path
from unittest import mock

import tests.tkstub as tkstub

tkstub.install()

from tests.gitenv import HAVE_GIT, git                            # noqa: E402
from tests.test_ui_headless import (HeadlessCase, make_bare,      # noqa: E402
                                    remote_files, write)

from gitpush_studio.core import remotes                           # noqa: E402
from gitpush_studio.core.git import Git                           # noqa: E402
from gitpush_studio.core.publish import PublishJob, publish       # noqa: E402
from gitpush_studio.ui import app_auto                            # noqa: E402
from gitpush_studio.ui.dialogs import RemoteDialog                # noqa: E402


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class RemoteGuardUITests(HeadlessCase):
    """El proyecto ya publica en `self.bare`; `self.otro` está vacío."""

    def setUp(self):
        super().setUp()
        self.otro = make_bare(self.tmp, "otro")
        primera = publish(PublishJob(folder=str(self.proj), url=self.bare,
                                     user="Test", email="t@x.y", check=False,
                                     preview=False))
        self.assertEqual(primera.status, "ok", primera.message)
        write(self.proj, "nuevo.txt", "nuevo\n")

    def remote_question(self):
        """Publica con la URL de `self.otro` y devuelve la pregunta."""
        self.fill_form(url=self.otro)
        self.app.start_publish()
        tkstub.Scheduler.pump(lambda: isinstance(self.app.controller._dialog,
                                                 RemoteDialog))
        return self.app.controller._dialog

    def origin(self):
        return Git().remote_url(str(self.proj))

    def test_the_question_shows_both_repositories(self):
        dialog = self.remote_question()
        self.assertEqual(dialog.conflict.current, self.bare)
        self.assertEqual(dialog.conflict.wanted, self.otro)
        dialog.close()
        self.run_until_idle()

    def test_publish_in_the_folders_repository(self):
        self.remote_question().finish(remotes.USE_FOLDER)
        self.run_until_idle()
        self.assertIn("nuevo.txt", remote_files(self.bare))
        self.assertEqual(remote_files(self.otro), [])
        self.assertEqual(self.origin(), self.bare)
        # El formulario dice ya el de la carpeta: la próxima no pregunta.
        self.assertEqual(self.app.e_url.get(), self.bare)

    def test_publish_in_the_forms_repository(self):
        self.remote_question().finish(remotes.USE_FORM)
        self.run_until_idle()
        self.assertIn("nuevo.txt", remote_files(self.otro))
        self.assertNotIn("nuevo.txt", remote_files(self.bare))
        self.assertEqual(self.origin(), self.otro)

    def test_closing_the_question_publishes_nothing(self):
        antes = git(self.bare, "rev-parse", "main").strip()
        self.remote_question().close()
        self.run_until_idle()
        self.assertEqual(git(self.bare, "rev-parse", "main").strip(), antes)
        self.assertEqual(remote_files(self.otro), [])
        self.assertEqual(self.origin(), self.bare)
        self.assertIn("No se publicó nada", self.console())

    def test_auto_publish_pauses_instead_of_asking(self):
        a = self.app
        self.fill_form(url=self.otro)
        a.v_auto.set(True)
        with mock.patch.object(app_auto, "SETTLE_SECONDS", 0):
            a.toggle_auto()
            a._launch_check()
            tkstub.Scheduler.pump(lambda: not a.v_auto.get(), timeout=30,
                                  step=lambda: self.assertNotIsInstance(
                                      a.controller._dialog, RemoteDialog))
        self.run_until_idle()
        self.assertFalse(a.v_auto.get())
        # La frase del motivo, no una cualquiera: la ayuda que se enseña al
        # abrir también habla de repositorios.
        self.assertIn("nunca publica en otro repositorio", self.console())
        self.assertEqual(remote_files(self.otro), [])
        self.assertEqual(self.origin(), self.bare)


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class FolderChangeTests(HeadlessCase):

    def setUp(self):
        super().setUp()
        publish(PublishJob(folder=str(self.proj), url=self.bare,
                           user="Test", email="t@x.y", check=False,
                           preview=False))
        self.otro = make_bare(self.tmp, "otro")

    def test_an_empty_url_takes_the_folders_repository(self):
        a = self.app
        a.e_url.delete(0, "end")
        a.e_folder.delete(0, "end")
        a.e_folder.insert(0, str(self.proj))
        a.folder_changed()
        self.assertEqual(a.e_url.get(), self.bare)
        self.assertIn("tomada de la carpeta", self.console())

    def test_another_url_is_warned_once(self):
        a = self.app
        self.fill_form(url=self.otro)
        a.folder_changed()
        a.folder_changed()                 # salir del campo otra vez
        self.assertEqual(self.console().count("Al publicar te preguntaré"),
                         1)
        self.assertEqual(a.e_url.get(), self.otro)       # no se cambia sola

    def test_a_token_in_the_origin_never_reaches_the_form(self):
        token = "ghp_" + "".join(chr(ord("a") + i % 26) for i in range(36))
        git(self.proj, "remote", "set-url", "origin",
            f"https://x-access-token:{token}@github.com/ana/app.git")
        a = self.app
        a.e_url.delete(0, "end")
        a.e_folder.delete(0, "end")
        a.e_folder.insert(0, str(self.proj))
        a.folder_changed()
        self.assertEqual(a.e_url.get(), "https://github.com/ana/app.git")
        self.assertNotIn(token, self.console())

    def test_changing_the_folder_pauses_auto_publish(self):
        a = self.app
        self.fill_form()
        a.v_auto.set(True)
        a.toggle_auto()
        self.assertTrue(a.v_auto.get())
        otra = Path(self.tmp) / "otra carpeta"
        write(otra, "b.txt", "b\n")
        a.on_paths_dropped([str(otra)])
        self.assertFalse(a.v_auto.get())
        self.assertIn("Cambiaste la carpeta", self.console())

    def test_a_changed_form_never_publishes_the_new_folder_on_its_own(self):
        """Aunque la carpeta se cambie sin salir del campo (sin aviso de
        la interfaz), la ronda siguiente no publica la carpeta nueva."""
        a = self.app
        self.fill_form()
        a.v_auto.set(True)
        a.toggle_auto()
        otra = Path(self.tmp) / "otra"
        write(otra, "b.txt", "b\n")
        a.e_folder.delete(0, "end")
        a.e_folder.insert(0, str(otra))
        with mock.patch.object(app_auto, "SETTLE_SECONDS", 0):
            a._launch_check()
            # Ni siquiera se empieza a mirar la carpeta nueva.
            self.assertFalse(a._auto_checking)
            self.assertFalse(a.v_auto.get())
        self.run_until_idle()
        self.assertFalse((otra / ".git").exists())
        self.assertNotIn("b.txt", remote_files(self.bare))

    def test_a_folder_changed_during_the_check_is_not_published(self):
        """La carpeta cambia mientras la comprobación espera a que los
        cambios se asienten: su resultado ya no vale para la nueva."""
        a = self.app
        self.fill_form()
        a.v_auto.set(True)
        a.toggle_auto()
        otra = Path(self.tmp) / "otra"
        write(otra, "b.txt", "b\n")
        a.e_folder.delete(0, "end")
        a.e_folder.insert(0, str(otra))
        a._auto_checking = True
        a._handle_auto_result("publish", None)
        self.assertFalse(a.controller.running)
        self.assertFalse(a.v_auto.get())
        self.run_until_idle()
        self.assertFalse((otra / ".git").exists())


if __name__ == "__main__":
    unittest.main()
