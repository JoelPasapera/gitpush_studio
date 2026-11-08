# -*- coding: utf-8 -*-
"""La ventana «Issues y PR a TXT», contra el GitHub falso.

  · propone el repositorio del formulario y un destino FUERA del
    proyecto (lo que alguien pegó en un issue no debe acabar publicado);
  · descarga, escribe y lo cuenta; «Detener» guarda lo descargado y la
    siguiente vez sigue desde ahí;
  · si el usuario lo guarda dentro del proyecto, va al cuadro de ignorar;
  · avisa de que sin token GitHub da muy pocas consultas;
  · si GraphQL no se puede usar, lo dice (con el porqué) en la ventana y
    UNA vez en la consola; una espera que pidió GitHub sigue a la vista
    aunque otras partes avancen;
  · una descarga a medias de la versión anterior dice qué se vuelve a
    pedir.
"""

import json
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import tests.tkstub as tkstub

tkstub.install()

from tests.fakegithub import FakeGitHub, standard_repo          # noqa: E402
from tests.test_ui_headless import HeadlessCase                  # noqa: E402

from gitpush_studio.core import ghsession, ghstore              # noqa: E402
from gitpush_studio.core.ghfetch import Progress                # noqa: E402
from gitpush_studio.ui.dialogs import issues_txt                # noqa: E402
from gitpush_studio.ui.theme import AMBER, MUTED                # noqa: E402


class IssuesTxtDialogTests(HeadlessCase):

    def setUp(self):
        super().setUp()
        self.hub = FakeGitHub()
        self.addCleanup(self.hub.close)
        standard_repo(self.hub)
        self.alm = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.alm, True)
        for patch in (mock.patch.object(ghsession, "API_ROOT", self.hub.root),
                      mock.patch.object(ghstore, "STORE_DIR", self.alm)):
            patch.start()
            self.addCleanup(patch.stop)
        self.fill_form(url="https://github.com/ana/app.git")

    def open(self):
        self.app.open_issues_txt()
        return self.app._dialogs["issues_txt"]

    def run_dialog(self, dlg):
        dlg._start()
        tkstub.Scheduler.pump(lambda: not dlg.running, timeout=60)
        tkstub.Scheduler.run_due()

    def test_proposes_the_repository_and_a_place_outside_the_project(self):
        dlg = self.open()
        self.assertEqual(dlg.e_repo.get(), "ana/app")
        destino = Path(dlg.e_out.get())
        self.assertEqual(destino.name, "app-historial-github.txt")
        self.assertEqual(destino.parent, self.proj.resolve().parent)
        self.assertIn("Sin token", dlg.lbl_token.cget("text"))
        dlg.destroy()

    def test_downloads_writes_and_says_so(self):
        dlg = self.open()
        self.run_dialog(dlg)
        texto = dlg.lbl_status.cget("text")
        self.assertIn("✔ ana/app: 5 issues y 2 pull requests", texto)
        self.assertTrue(Path(dlg.out_path).is_file())
        self.assertEqual(dlg.btn_reveal.cget("state"), "normal")
        self.assertIn("🗂 Historial de ana/app", self.console())
        dlg.destroy()

    def test_stop_keeps_what_was_downloaded(self):
        dlg = self.open()
        real = issues_txt.dump_repository

        def parar_en_comentarios(*args, **kw):
            cancel = kw["cancel"]
            avisar = kw["on_progress"]

            def progreso(p):
                avisar(p)
                # Parar en cuanto haya algo guardado.
                if p.counts.get("items"):
                    cancel.set()
            kw["on_progress"] = progreso
            return real(*args, **kw)
        with mock.patch.object(issues_txt, "dump_repository",
                               parar_en_comentarios):
            self.run_dialog(dlg)
        self.assertIn("Detenido", dlg.lbl_status.cget("text"))
        self.assertFalse(Path(dlg.e_out.get()).exists())
        self.assertTrue(list(self.alm.glob("*.sqlite3")))
        self.run_dialog(dlg)
        self.assertIn("siguió la descarga anterior",
                      dlg.lbl_status.cget("text"))
        dlg.destroy()

    def test_inside_the_project_it_is_never_published(self):
        dlg = self.open()
        dlg.e_out.delete(0, "end")
        dlg.e_out.insert(0, str(self.proj / "historial.txt"))
        self.run_dialog(dlg)
        self.assertIn("/historial.txt", self.app.ignore_lines())
        self.assertIn("lo añadí al cuadro de ignorar", self.console())
        dlg.destroy()

    def test_a_wrong_repository_name(self):
        dlg = self.open()
        dlg.e_repo.delete(0, "end")
        dlg.e_repo.insert(0, "esto no es un repositorio")
        dlg._start()
        self.assertFalse(dlg.running)
        self.assertIn("usuario/repositorio", dlg.lbl_status.cget("text"))
        dlg.destroy()

    def half_done(self, commits: bool, mentions: bool) -> None:
        """Una descarga a medias de ana/app, como la deja «Detener»."""
        identidad = {"format": ghstore.FORMAT, "repo_id": 4242,
                     "commits": commits, "mentions": mentions}
        almacen = ghstore.open_store("ana", "app", identidad, base=self.alm)
        almacen.save("issues", cursor="",
                     items=[(1, {"number": 1, "kind": "issue"}),
                            (2, {"number": 2, "kind": "pr"})])
        almacen.close()

    def test_a_half_done_download_keeps_its_options(self):
        self.half_done(commits=False, mentions=True)
        dlg = self.open()
        self.assertFalse(dlg.v_commits.get())
        self.assertTrue(dlg.v_mentions.get())
        aviso = dlg.lbl_partial.cget("text")
        self.assertIn("a medias de ana/app (2 issues y PR", aviso)
        self.assertIn("sin los commits y con las menciones", aviso)
        self.run_dialog(dlg)
        self.assertIn("siguió la descarga anterior",
                      dlg.lbl_status.cget("text"))
        dlg.destroy()

    def test_another_repository_says_nothing(self):
        self.half_done(commits=False, mentions=True)
        dlg = self.open()
        dlg.e_repo.delete(0, "end")
        dlg.e_repo.insert(0, "luis/otro")
        dlg._repo_changed()
        self.assertEqual(dlg.lbl_partial.cget("text"), "")
        self.assertIsNone(dlg.partial)
        dlg.destroy()

    def test_peeking_never_changes_the_store(self):
        self.half_done(commits=True, mentions=False)
        ruta = ghstore.store_path("ana", "app", base=self.alm)
        antes = ruta.read_bytes()
        dlg = self.open()
        for spec in ("ana/app", "ANA/App", "https://github.com/ana/app"):
            dlg.e_repo.delete(0, "end")
            dlg.e_repo.insert(0, spec)
            dlg._repo_changed()
            self.assertIsNotNone(dlg.partial, spec)
        dlg.destroy()
        self.assertEqual(ruta.read_bytes(), antes, "solo se lee")

    def test_with_a_token(self):
        with mock.patch.object(type(self.app), "connection_token",
                               lambda self, profile=None: "ghp_x"):
            dlg = self.open()
        self.assertIn("5.000 consultas", dlg.lbl_token.cget("text"))
        dlg.destroy()

    def test_graphql_that_cannot_be_used_is_said_and_logged_once(self):
        self.hub.fault("graphql_unauth", match="/graphql", times=99)
        with mock.patch.object(type(self.app), "connection_token",
                               lambda self, profile=None: "ghp_x"):
            dlg = self.open()
            self.run_dialog(dlg)
        texto = dlg.lbl_status.cget("text")
        self.assertIn("✔ ana/app", texto, "la descarga termina igual")
        self.assertIn("⚠ GraphQL no se pudo usar", texto)
        self.assertIn("(401)", texto)
        self.assertEqual(self.console().count("GraphQL no se pudo usar"), 1)
        dlg.destroy()

    def test_a_wait_stays_in_sight_while_other_parts_go_on(self):
        dlg = self.open()
        dlg.running = True
        dlg.handle(("wait", "GitHub agotó las consultas de esta hora",
                    time.time() + 600))
        dlg.handle(("progress", Progress(
            "Descargando revisiones, commits y menciones (GraphQL)", 40, 90,
            {"items": 7})))
        texto = dlg.lbl_status.cget("text")
        self.assertIn("Descargando revisiones", texto)
        self.assertIn("⏳ GitHub agotó las consultas de esta hora", texto)
        self.assertEqual(dlg.lbl_status.cget("text_color"), AMBER)
        # Pasada la hora, ya no se dice.
        dlg._wait = ("GitHub agotó las consultas", time.time() - 1)
        dlg.handle(("progress", Progress("Descargando eventos", 41, 90)))
        self.assertNotIn("⏳", dlg.lbl_status.cget("text"))
        self.assertEqual(dlg.lbl_status.cget("text_color"), MUTED)
        dlg.running = False
        dlg.destroy()

    def test_a_half_done_download_of_the_previous_version(self):
        self.half_done(commits=True, mentions=True)
        ruta = ghstore.store_path("ana", "app", base=self.alm)
        db = sqlite3.connect(str(ruta))
        with db:
            fila = db.execute("SELECT value FROM meta WHERE key = "
                              "'identity'").fetchone()
            db.execute("UPDATE meta SET value = ? WHERE key = 'identity'",
                       (json.dumps(dict(json.loads(fila[0]), format=1)),))
        db.close()
        dlg = self.open()
        aviso = dlg.lbl_partial.cget("text")
        self.assertIn("a medias de ana/app (2 issues y PR", aviso)
        self.assertIn("versión anterior", aviso)
        self.assertIn("se vuelven a pedir", aviso)
        dlg.destroy()


if __name__ == "__main__":
    unittest.main()
