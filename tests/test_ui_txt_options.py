# -*- coding: utf-8 -*-
"""La ventana «📄 Consolidar a TXT» con las opciones para una IA: qué se
elige por defecto, que llegan al TXT, y el botón de copiar (entero o
parte a parte)."""

from pathlib import Path

import tests.tkstub as tkstub

tkstub.install()

from tests.test_ui_headless import HeadlessCase                  # noqa: E402

from gitpush_studio.core.redact import MASK                      # noqa: E402
from gitpush_studio.ui.dialogs import ConsolidateDialog          # noqa: E402

TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


class ConsolidateOptionsTests(HeadlessCase):

    def setUp(self):
        super().setUp()
        self.fill_form()

    def generate(self, dlg):
        dlg._generate()
        tkstub.Scheduler.pump(lambda: not dlg.running)

    def test_secrets_are_crossed_out_by_default(self):
        (self.proj / "conf.py").write_text(f'TOKEN = "{TOKEN}"\n',
                                           encoding="utf-8")
        dlg = ConsolidateDialog(self.app)
        options = dlg.options()
        self.assertTrue(options.redact)
        self.assertFalse(options.for_ai)
        self.assertEqual(options.changes, "")
        self.assertEqual(options.split_tokens, 0)
        self.generate(dlg)
        text = Path(dlg.out_path).read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, text)
        self.assertIn(MASK, text)
        self.assertIn("tokens", dlg.lbl_status.text)
        self.assertIn("secreto", dlg.lbl_status.text)
        dlg.destroy()

    def test_the_choices_reach_the_options(self):
        dlg = ConsolidateDialog(self.app)
        dlg.seg_content.set("Solo las diferencias")
        dlg.opt_split.set("128.000 tokens")
        dlg.v_ai.set(True)
        dlg.v_redact.set(False)
        options = dlg.options()
        self.assertEqual(options.changes, "diferencias")
        self.assertEqual(options.split_tokens, 128000)
        self.assertTrue(options.for_ai)
        self.assertFalse(options.redact)
        dlg.destroy()

    def test_copy_the_whole_txt(self):
        dlg = ConsolidateDialog(self.app)
        self.assertEqual(dlg.btn_copy.cget("state"), "disabled")
        self.generate(dlg)
        self.assertEqual(dlg.btn_copy.cget("state"), "normal")
        tkstub.Clipboard.text = ""
        dlg._copy()
        self.assertEqual(tkstub.Clipboard.text,
                         Path(dlg.out_path).read_bytes().decode("utf-8"))
        self.assertIn("Copiado el TXT", dlg.lbl_status.text)
        dlg.destroy()

    def test_copy_part_by_part(self):
        for i in range(12):
            (self.proj / f"datos{i}.txt").write_text(
                "".join(f"fila {i}-{k} con algo de texto\n"
                        for k in range(1200)), encoding="utf-8")
        dlg = ConsolidateDialog(self.app)
        dlg.opt_split.set("32.000 tokens")
        # Dentro del proyecto, para comprobar que las partes no se publican.
        dlg.e_out.delete(0, "end")
        dlg.e_out.insert(0, str(self.proj / "proj.txt"))
        self.generate(dlg)
        self.assertGreater(len(dlg.parts), 1, dlg.lbl_status.text)
        self.assertIn("Repartido en", dlg.lbl_status.text)
        self.assertEqual(dlg.btn_copy.cget("text"),
                         f"📋 Parte 1/{len(dlg.parts)}")
        for number, part in enumerate(dlg.parts, 1):
            dlg._copy()
            self.assertEqual(tkstub.Clipboard.text,
                             Path(part).read_bytes().decode("utf-8"))
            self.assertIn(f"la parte {number} de", dlg.lbl_status.text)
        self.assertIn("Era la última", dlg.lbl_status.text)
        # El TXT y sus partes quedaron dentro del proyecto: no se publican.
        self.assertIn("/proj.txt", self.app.ignore_lines())
        self.assertIn("/proj-partes/", self.app.ignore_lines())
        dlg.destroy()

    def test_changes_need_a_repository(self):
        dlg = ConsolidateDialog(self.app)
        dlg.seg_content.set("Solo lo que cambió")
        self.generate(dlg)
        self.assertIn("repositorio", dlg.lbl_status.text)
        self.assertEqual(dlg.btn_go.cget("state"), "normal")
        dlg.destroy()
