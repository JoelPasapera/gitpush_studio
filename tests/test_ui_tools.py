# -*- coding: utf-8 -*-
"""Las herramientas nuevas de la barra lateral, sin pantalla: revisar el
historial y «¿por qué no se sube?»."""

import unittest
from pathlib import Path

import tests.tkstub as tkstub

tkstub.install()

from tests.gitenv import HAVE_GIT, commit_all, init_repo         # noqa: E402
from tests.test_ui_headless import HeadlessCase, write            # noqa: E402

TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class HistoryScanDialogTests(HeadlessCase):

    def test_finds_a_deleted_secret_and_offers_to_leave_out_a_present_one(self):
        init_repo(self.proj)
        write(self.proj, "viejo.py", f'TOKEN = "{TOKEN}"\n')
        commit_all(self.proj, "con token")
        (self.proj / "viejo.py").unlink()
        write(self.proj, "actual.py", f'OTRO = "{TOKEN[:-1]}X"\n')
        commit_all(self.proj, "otro")
        self.fill_form()
        self.app.open_history_scan()
        dlg = self.app._dialogs["history_scan"]
        dlg._start()
        tkstub.Scheduler.pump(lambda: not dlg.running, timeout=30)
        self.assertIn("hallazgo", dlg.lbl_status.text)
        textos = " ".join((w.cget("text") or "") for w in self.walk(dlg))
        self.assertIn("viejo.py", textos)
        self.assertIn("en el historial", textos)
        self.assertIn("rotarla", textos)
        present = [f for f in dlg.report.findings if f.still_present]
        self.assertEqual([f.path for f in present], ["actual.py"])
        dlg._exclude(present[0])
        self.assertIn("/actual.py", self.app.ignore_lines())
        dlg.destroy()

    def test_a_folder_without_git(self):
        self.fill_form()
        self.app.open_history_scan()
        dlg = self.app._dialogs["history_scan"]
        dlg._start()
        self.assertFalse(dlg.running)
        self.assertIn("repositorio", dlg.lbl_status.text)
        dlg.destroy()

    def walk(self, root):
        seen, stack = [], [root]
        while stack:
            widget = stack.pop()
            seen.append(widget)
            stack.extend(getattr(widget, "_children", []))
        return seen


class WhyDialogTests(HeadlessCase):

    def test_explains_the_box_rule(self):
        self.fill_form()
        self.app.add_ignore_patterns(["/app.js"])
        self.app.open_why()
        dlg = self.app._dialogs["why"]
        dlg.e_target.insert(0, "app.js")
        dlg._explain()
        tkstub.Scheduler.pump(lambda: not dlg.running)
        self.assertIn("No se sube", dlg.lbl_answer.cget("text"))
        self.assertIn("cuadro", dlg.lbl_answer.cget("text"))
        dlg.e_target.delete(0, "end")
        dlg.e_target.insert(0, str(Path(self.proj) / "index.html"))
        dlg._explain()
        tkstub.Scheduler.pump(lambda: not dlg.running)
        self.assertIn("Se sube", dlg.lbl_answer.cget("text"))
        dlg.destroy()

    def test_needs_a_target(self):
        self.fill_form()
        self.app.open_why()
        dlg = self.app._dialogs["why"]
        dlg._explain()
        self.assertFalse(dlg.running)
        self.assertIn("Elige", dlg.lbl_status.text)
        dlg.destroy()


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class PreviewDiffTests(HeadlessCase):
    """«Ver» en la vista previa: qué cambia exactamente en cada archivo
    (lo de GitHub hoy frente a lo que se va a subir)."""

    def diff_text(self, dlg, kind, path):
        from gitpush_studio.ui.diffview import DiffDialog
        from gitpush_studio.ui.dialogs import PreviewDialog  # noqa: F401
        before = set(id(w) for w in self.walk(self.app))
        dlg.show_diff(kind, path)
        nuevos = [w for w in self.walk(self.app)
                  if isinstance(w, DiffDialog) and id(w) not in before]
        self.assertEqual(len(nuevos), 1)
        view = nuevos[0]
        tkstub.Scheduler.pump(lambda: not view.running, timeout=20)
        text = view.view.all_text()
        view.destroy()
        return text

    def walk(self, root):
        seen, stack = [], [root]
        while stack:
            widget = stack.pop()
            seen.append(widget)
            stack.extend(getattr(widget, "_children", []))
        return seen

    def test_each_file_shows_its_lines(self):
        from gitpush_studio.ui.dialogs import PreviewDialog
        self.fill_form()
        self.app.start_publish()
        self.run_until_idle()
        write(self.proj, "index.html", "<h1>editado</h1>\n")
        write(self.proj, "nuevo.txt", "n\n")
        (self.proj / "app.js").unlink()
        self.app.start_publish()
        tkstub.Scheduler.pump(lambda: isinstance(self.app.controller._dialog,
                                                 PreviewDialog))
        dlg = self.app.controller._dialog
        self.assertEqual(dlg.folder, str(self.proj))
        modificado = self.diff_text(dlg, "modified", "index.html")
        self.assertIn("-<h1>hola</h1>", modificado)
        self.assertIn("+<h1>editado</h1>", modificado)
        self.assertIn("+n", self.diff_text(dlg, "added", "nuevo.txt"))
        self.assertIn("-console.log(1)",
                      self.diff_text(dlg, "deleted", "app.js"))
        dlg._publish()
        tkstub.Scheduler.pump(lambda: not self.app.controller.running)
