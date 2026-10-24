# -*- coding: utf-8 -*-
"""Consola de actividad: se puede leer, seleccionar y copiar, pero no
escribir en ella.

Un cuadro de texto en estado «disabled» no admite copiar en Tk: no
recibe el foco, así que Ctrl+C no llega. Por eso se deja en estado
normal y se bloquean las teclas que modificarían el contenido, dejando
pasar las de copiar, seleccionar y moverse.
"""

import customtkinter as ctk

from .theme import BORDER, CONSOLE_BG, CONSOLE_FG, LOG_COLORS, MUTED

# Teclas que solo mueven el cursor o la vista: no cambian nada.
NAVIGATION = frozenset({
    "Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next",
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    "Escape", "Tab", "ISO_Left_Tab",
})
COPY_KEYS = frozenset({"c", "a", "insert"})      # con Control
CONTROL_MASK = 0x4


class Console(ctk.CTkTextbox):

    def __init__(self, master, fonts, height=180, **kw):
        super().__init__(master, height=height, font=fonts.mono,
                         corner_radius=8, fg_color=CONSOLE_BG,
                         text_color=CONSOLE_FG, border_width=0, wrap="word",
                         scrollbar_button_color=BORDER,
                         scrollbar_button_hover_color=MUTED, **kw)
        for tag, color in LOG_COLORS.items():
            self._safe(lambda t=tag, c=color: self.tag_config(t,
                                                              foreground=c))
        self._listen("<Key>", self._on_key)
        self._listen("<Control-a>", self._select_all)
        self._listen("<Control-A>", self._select_all)
        self._listen("<Button-2>", lambda e: "break")   # pegar con la rueda
        self._listen("<<Paste>>", lambda e: "break")

    # ---- utilidades internas ------------------------------------------
    @staticmethod
    def _safe(fn, default=None):
        """Las versiones de customtkinter difieren en qué reenvían al
        widget interno; un método ausente no debe tumbar la app."""
        try:
            return fn()
        except Exception:                                   # noqa: BLE001
            return default

    def _listen(self, sequence, command):
        """Ojo con el nombre: `_bind` es un método privado de
        tkinter.Misc (`_bind(what, sequence, func, add)`), y llamarlo
        así lo tapaba; tkinter lo invocaba con cuatro argumentos y la
        app moría con «takes 3 positional arguments but 5 were given»."""
        self._safe(lambda: self.bind(sequence, command, add="+"))

    def _on_key(self, event):
        if event.state & CONTROL_MASK and event.keysym.lower() in COPY_KEYS:
            return None
        if event.keysym in NAVIGATION:
            return None
        return "break"

    def _select_all(self, _event=None):
        self._safe(lambda: self.tag_add("sel", "1.0", "end-1c"))
        return "break"

    def _at_bottom(self):
        """¿La vista está abajo del todo? Si el usuario ha subido a leer
        o a seleccionar, no se le arrastra hacia abajo."""
        view = self._safe(self.yview)
        return True if not view else view[1] >= 0.999

    # ---- escritura -----------------------------------------------------
    def write(self, text, tag="out"):
        self.write_parts(((text, tag),))

    def write_parts(self, parts):
        """Una línea con varios tramos: [(texto, etiqueta), …]."""
        stick = self._at_bottom()
        for text, tag in parts:
            try:
                self.insert("end", text, tag)
            except Exception:                               # noqa: BLE001
                self.insert("end", text)      # sin colores, pero se lee
        self.insert("end", "\n")
        if stick:
            self._safe(lambda: self.see("end"))

    def clear(self):
        self.delete("1.0", "end")

    # ---- copiar --------------------------------------------------------
    def selected_text(self):
        return self._safe(lambda: self.get("sel.first", "sel.last"), "") or ""

    def all_text(self):
        return self._safe(lambda: self.get("1.0", "end-1c"), "") or ""

    def copy(self):
        """Copia la selección, o todo si no hay nada seleccionado.
        Devuelve «selección», «todo» o cadena vacía si no había texto."""
        text = self.selected_text()
        kind = "selección" if text.strip() else ""
        if not kind:
            text, kind = self.all_text(), "todo"
        if not text.strip():
            return ""
        self.clipboard_clear()
        self.clipboard_append(text)
        return kind
