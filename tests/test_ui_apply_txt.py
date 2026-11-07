# -*- coding: utf-8 -*-
"""La ventana «📥 Aplicar TXT» sin pantalla: pegar, revisar, ver las
diferencias y aplicar (o no) con la confirmación."""

from pathlib import Path
from unittest import mock

import tests.tkstub as tkstub

tkstub.install()

from tests.test_ui_headless import HeadlessCase                  # noqa: E402

from gitpush_studio.core import apply_txt as apply_module        # noqa: E402
from gitpush_studio.ui.diffview import DiffDialog                 # noqa: E402

CAMBIO = "MODULO: app.js\n```js\nconsole.log(2)\n```\n"


class ApplyTxtDialogTests(HeadlessCase):

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(apply_module, "BACKUP_ROOT",
                                  Path(self.tmp) / "respaldos")
        patch.start()
        self.addCleanup(patch.stop)
        self.fill_form()

    def open_dialog(self):
        self.app.open_apply_txt()
        return self.app._dialogs["apply_txt"]

    def console(self):
        return self.app.log_box.all_text()

    def review(self, dlg, text=CAMBIO):
        tkstub.Clipboard.text = text
        dlg._paste()
        dlg._review()
        tkstub.Scheduler.pump(lambda: not dlg.running)

    def test_paste_review_and_apply(self):
        dlg = self.open_dialog()
        self.assertEqual(dlg.e_proj.get(), str(self.proj))
        self.review(dlg)
        self.assertIn("Nada se ha escrito", dlg.lbl_status.text)
        self.assertEqual(dlg.selected_paths(), ["app.js"])
        self.assertEqual(dlg.btn_apply.cget("state"), "normal")
        self.assertIn("Aplicar 1 archivo", dlg.btn_apply.cget("text"))
        self.assertEqual((self.proj / "app.js").read_text(), "console.log(1)\n")
        tkstub.Messagebox.ANSWER = True
        dlg._apply()
        tkstub.Scheduler.pump(lambda: not dlg.running)
        self.assertEqual((self.proj / "app.js").read_text(), "console.log(2)\n")
        self.assertIn("Aplicado", dlg.lbl_status.text)
        self.assertIn("TXT aplicado", self.console())
        backups = list((Path(self.tmp) / "respaldos").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "app.js").read_text(),
                         "console.log(1)\n")
        # Lo revisado ya no vale: para aplicar más hay que revisar otra vez.
        self.assertEqual(dlg.btn_apply.cget("state"), "disabled")
        dlg.destroy()

    def test_saying_no_writes_nothing(self):
        dlg = self.open_dialog()
        self.review(dlg)
        tkstub.Messagebox.ANSWER = False
        dlg._apply()
        self.assertFalse(dlg.running)
        self.assertEqual((self.proj / "app.js").read_text(), "console.log(1)\n")
        self.assertFalse((Path(self.tmp) / "respaldos").exists())
        dlg.destroy()

    def test_rejected_and_suspicious_files_are_not_checked(self):
        cuerpo = "\n".join(f"paso{i}();" for i in range(12))
        (self.proj / "main.js").write_text(cuerpo + "\n", encoding="utf-8")
        texto = (CAMBIO
                 + "MODULO: ../fuera.txt\n```\nx\n```\n"
                 + "MODULO: main.js\n```\n// ... resto del código igual ...\n"
                   "fin();\n```\n")
        dlg = self.open_dialog()
        self.review(dlg, texto)
        self.assertEqual(sorted(dlg.checks), ["app.js", "main.js"])
        self.assertEqual(dlg.selected_paths(), ["app.js"])
        textos = " ".join(w.cget("text") or "" for w in self.widgets(dlg))
        self.assertIn("se saldría del proyecto", textos)
        self.assertIn("parece un resumen", textos)
        dlg.destroy()

    def widgets(self, root):
        seen, stack = [], [root]
        while stack:
            widget = stack.pop()
            seen.append(widget)
            stack.extend(getattr(widget, "_children", []))
        return seen

    def test_see_changes_opens_the_diff(self):
        dlg = self.open_dialog()
        self.review(dlg)
        change = dlg.plan.changes[0]
        dlg._show(change)
        diffs = [w for w in self.widgets(self.app)
                 if isinstance(w, DiffDialog)]
        self.assertEqual(len(diffs), 1)
        view = diffs[0]
        tkstub.Scheduler.pump(lambda: not view.running)
        text = view.view.all_text()
        self.assertIn("-console.log(1)", text)
        self.assertIn("+console.log(2)", text)
        self.assertIn("+1 −1", view.lbl_summary.cget("text"))
        view.destroy()
        dlg.destroy()

    def test_a_txt_file_can_be_used_instead_of_pasting(self):
        archivo = Path(self.tmp) / "respuesta.txt"
        archivo.write_text(CAMBIO, encoding="utf-8")
        tkstub.Filedialog.OPEN_FILE = str(archivo)
        dlg = self.open_dialog()
        dlg._open_file()
        self.assertIn("respuesta.txt", dlg.lbl_source.cget("text"))
        dlg._review()
        tkstub.Scheduler.pump(lambda: not dlg.running)
        self.assertEqual(dlg.selected_paths(), ["app.js"])
        tkstub.Filedialog.OPEN_FILE = ""
        dlg.destroy()

    def test_a_huge_paste_is_used_whole_but_not_shown(self):
        relleno = "MODULO: grande.txt\n```\n" + ("x" * 100 + "\n") * 30000 \
            + "```\n"
        dlg = self.open_dialog()
        tkstub.Clipboard.text = relleno + CAMBIO
        dlg._paste()
        self.assertEqual(dlg.txt.get("1.0", "end"), "")
        self.assertIn("se usa entero", dlg.lbl_source.cget("text"))
        dlg._review()
        tkstub.Scheduler.pump(lambda: not dlg.running)
        self.assertEqual(sorted(dlg.checks), ["app.js", "grande.txt"])
        dlg.destroy()

    def test_nothing_pasted_says_so(self):
        dlg = self.open_dialog()
        dlg._review()
        self.assertFalse(dlg.running)
        self.assertIn("Pega primero", dlg.lbl_status.text)
        dlg.destroy()
