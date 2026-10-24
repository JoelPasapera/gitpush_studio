# -*- coding: utf-8 -*-
"""Ningún widget nuestro puede tapar un nombre de tkinter.

La consola dejó de abrirse porque `Console._bind` tapaba
`tkinter.Misc._bind(what, sequence, func, add)`, un método privado que
tkinter llama por dentro con cuatro argumentos: «_bind() takes 3
positional arguments but 5 were given». El mismo riesgo tenía
`QueuePanel.update`, que tapaba `Misc.update()`.

Este archivo revisa el código fuente (no hace falta tkinter instalado) y
falla si alguna clase que herede de un widget define un método o un
atributo con un nombre reservado. Así el fallo no puede volver por otra
puerta.
"""

import ast
import glob
import unittest
from pathlib import Path

# Nombres que tkinter define en Misc, Text, Frame, Toplevel y Wm, y que
# por tanto heredan todos los widgets de customtkinter.
TKINTER_NAMES = frozenset("""
after after_cancel after_idle anchor aspect attributes bbox bell bind
bindtags bind_all bind_class cget children clipboard_append
clipboard_clear clipboard_get colormodel columnconfigure compare config
configure count debug deiconify delete deletecommand destroy dlineinfo
dump edit edit_modified edit_redo edit_reset edit_separator edit_undo
event_add event_delete event_generate event_info focus focus_displayof
focus_force focus_get focus_lastfor focus_set forget frame geometry get
getboolean getdouble getint getvar grab_current grab_release grab_set
grab_set_global grab_status grid grid_anchor grid_bbox grid_columnconfigure
grid_configure grid_forget grid_info grid_location grid_propagate
grid_remove grid_rowconfigure grid_size grid_slaves group iconbitmap
iconify iconmask iconname iconphoto iconposition iconwindow image_cget
image_configure image_create image_names image_types index info insert
keys lift location lower mainloop manage mark_gravity mark_names
mark_next mark_previous mark_set mark_unset master maxsize minsize
nametowidget option_add option_clear option_get option_readfile
overrideredirect pack pack_configure pack_forget pack_info pack_propagate
pack_slaves peer_create peer_names place place_configure place_forget
place_info place_slaves positionfrom propagate protocol quit register
replace resizable rowconfigure scan_dragto scan_mark search see
selection_clear selection_get selection_handle selection_own
selection_own_get send setvar size sizefrom slaves state tag_add tag_bind
tag_cget tag_config tag_configure tag_delete tag_lower tag_names
tag_nextrange tag_prevrange tag_raise tag_ranges tag_remove tag_unbind
title tk tkraise transient unbind unbind_all unbind_class update
update_idletasks wait_variable wait_visibility wait_window waitvar
window_cget window_configure window_create window_names winfo_children
winfo_exists winfo_height winfo_width winfo_x winfo_y withdraw xview
xview_moveto xview_scroll yview yview_moveto yview_pickplace yview_scroll
_bind _configure _displayof _getboolean _getconfigure _getconfigure1
_getdoubles _getints _grid_configure _gridconvvalue _last_child_ids
_nametowidget _options _register _report_exception _root _substitute
_subst_format _subst_format_str _w _windowingsystem
""".split())

# Clases base de las que heredan nuestros widgets (por nombre, porque
# customtkinter puede no estar instalado al ejecutar las pruebas).
WIDGET_BASES = ("ctk.CTk", "ctk.CTkToplevel", "ctk.CTkFrame",
                "ctk.CTkTextbox", "ctk.CTkScrollableFrame",
                "BaseDialog", "DecisionDialog", "WorkerDialog",
                "GitPushStudio", "Console", "QueuePanel")


def base_names(node):
    out = []
    for b in node.bases:
        if isinstance(b, ast.Name):
            out.append(b.id)
        elif isinstance(b, ast.Attribute) and isinstance(b.value, ast.Name):
            out.append(f"{b.value.id}.{b.attr}")
    return out


def own_names(node):
    """Métodos y atributos de instancia que define la clase."""
    names = set()
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(item.name)
            for sub in ast.walk(item):
                if isinstance(sub, ast.Attribute) and \
                        isinstance(sub.value, ast.Name) and \
                        sub.value.id == "self" and \
                        isinstance(sub.ctx, ast.Store):
                    names.add(sub.attr)
        elif isinstance(item, ast.Assign):
            for t in item.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
    return names


class TkinterNameCollisions(unittest.TestCase):

    def test_no_ui_class_shadows_a_tkinter_name(self):
        problemas = []
        for path in sorted(glob.glob("gitpush_studio/ui/**/*.py",
                                     recursive=True)):
            tree = ast.parse(Path(path).read_text(encoding="utf-8"), path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                if not any(b in WIDGET_BASES for b in base_names(node)):
                    continue
                for name in sorted(own_names(node) & TKINTER_NAMES):
                    problemas.append(f"{path}: {node.name}.{name} tapa un "
                                     "nombre de tkinter")
        self.assertEqual(problemas, [], "\n" + "\n".join(problemas))

    def test_the_checker_actually_detects_a_collision(self):
        """La prueba anterior solo vale si sabe encontrar el fallo."""
        codigo = ("import customtkinter as ctk\n"
                  "class X(ctk.CTkTextbox):\n"
                  "    def _bind(self, a, b):\n        pass\n")
        tree = ast.parse(codigo)
        clase = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.ClassDef))
        self.assertIn("ctk.CTkTextbox", base_names(clase))
        self.assertIn("_bind", own_names(clase) & TKINTER_NAMES)


if __name__ == "__main__":
    unittest.main()
