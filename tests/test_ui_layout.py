# -*- coding: utf-8 -*-
"""Las ventanas caben en la pantalla y sus botones nunca quedan cortados.

Un portátil de 1366×768 deja unos 650 píxeles de alto para una ventana:
«Issues y PR a TXT» y «Consolidar a TXT» no cabían enteros y sus botones
quedaban fuera de vista, sin forma de llegar a ellos. Ahora:

  · el tamaño se acota a la pantalla también con el escalado de Windows
    (125 %, 150 %…), y el mínimo nunca es más alto que la pantalla;
  · esas ventanas tienen cabecera y pie fijos (estado y botones) y el
    formulario en medio se desplaza: el pie no está DENTRO de lo que se
    desplaza.
"""

import unittest

import tests.tkstub as tkstub

tkstub.install()

from tests.test_ui_headless import HeadlessCase                    # noqa: E402

from gitpush_studio.ui.widgets import fit_to_screen                # noqa: E402


class FitToScreenTests(unittest.TestCase):

    def test_a_laptop_at_100_percent(self):
        self.assertEqual(fit_to_screen(760, 680, 640, 420, 1366, 768),
                         (760, 648, 640, 420))

    def test_with_windows_scaling(self):
        ancho, alto, min_ancho, min_alto = fit_to_screen(
            760, 680, 640, 560, 1366, 768, scaling=1.25)
        self.assertLessEqual(alto * 1.25, 768 - 100)
        self.assertLessEqual(min_alto, alto)
        self.assertLessEqual(ancho * 1.25, 1366)

    def test_a_big_screen_changes_nothing(self):
        self.assertEqual(fit_to_screen(760, 720, 640, 420, 1920, 1080),
                         (760, 720, 640, 420))

    def test_a_tiny_screen_never_gets_a_minimum_bigger_than_itself(self):
        ancho, alto, min_ancho, min_alto = fit_to_screen(
            860, 760, 700, 560, 800, 600)
        self.assertLessEqual(ancho, 760)
        self.assertLessEqual(alto, 480)
        self.assertLessEqual(min_ancho, ancho)
        self.assertLessEqual(min_alto, alto)

    def test_an_unknown_screen(self):
        self.assertEqual(fit_to_screen(760, 680, 640, 420, 0, 0),
                         (760, 600, 640, 420))


def _ancestros(widget):
    while widget is not None:
        yield widget
        widget = getattr(widget, "master", None)


class FixedFooterTests(HeadlessCase):

    def assert_footer(self, dlg, widgets):
        self.assertIsNotNone(dlg.body)
        self.assertIsNotNone(dlg.footer)
        for widget in widgets:
            linaje = list(_ancestros(widget))
            self.assertIn(dlg.footer, linaje)
            self.assertNotIn(dlg.body, linaje,
                             "el pie no puede ir dentro de lo que se "
                             "desplaza")

    def test_issues_txt(self):
        self.app.open_issues_txt()
        dlg = self.app._dialogs["issues_txt"]
        self.assert_footer(dlg, (dlg.btn_go, dlg.btn_stop, dlg.btn_reveal,
                                 dlg.lbl_status, dlg.pbar))
        self.assertIn(dlg.body, list(_ancestros(dlg.e_repo)))
        dlg.destroy()

    def test_consolidate(self):
        self.app.open_consolidate()
        dlg = self.app._dialogs["pack"]
        self.assert_footer(dlg, (dlg.btn_go, dlg.btn_stop, dlg.btn_copy,
                                 dlg.lbl_status, dlg.pbar))
        self.assertIn(dlg.body, list(_ancestros(dlg.e_proj)))
        dlg.destroy()


if __name__ == "__main__":
    unittest.main()


class SidebarTests(HeadlessCase):

    def test_tools_scroll_and_profiles_never_vanish(self):
        """Las herramientas van en una lista desplazable (añadir más solo
        alarga lo que se desplaza) y las dos listas tienen un mínimo."""
        from gitpush_studio.ui import app as app_module
        botones = [w for w in self.app.tools_list.winfo_children()]
        self.assertEqual(len(botones), len(app_module.SIDEBAR_TOOLS))
        self.assertGreater(app_module.SIDEBAR_TOOLS_MIN, 100)
        self.assertGreater(app_module.SIDEBAR_PROFILES_MIN, 40)
        for boton in botones:
            self.assertIs(boton.master, self.app.tools_list)
