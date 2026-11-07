# -*- coding: utf-8 -*-
"""Ver las diferencias de un archivo, línea a línea y con colores.

`DiffText` es un cuadro de solo lectura (se puede seleccionar y copiar,
como la consola) que pinta un diff: en verde lo que entra, en rojo lo
que sale, en azul la cabecera de cada trozo, y los números de línea de
los dos lados a la izquierda.

`DiffDialog` lo abre en su propia ventana. El contenido lo prepara una
función (`loader`) que se ejecuta en un hilo: leer dos versiones de git
o comparar un archivo grande no congela la ventana.
"""

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

import customtkinter as ctk

from ..core import diff
from .base import WorkerDialog
from .console import Console
from .theme import BLUE, GREEN_T, MUTED, RED, TEXT
from .widgets import button

# Más allá de esto no se pinta (un cuadro de texto de Tk con cientos de
# miles de líneas se arrastra): se avisa de cuántas faltan.
MAX_RENDER_LINES = 4000

DIFF_COLORS = {
    "add": ("#aff5b4", "#12261e"),
    "del": ("#ffdcd7", "#3d1d1f"),
    "hunk": (BLUE, None),
    "num": (MUTED, None),
    "ctx": ("#c9d1d9", None),
}


@dataclass
class DiffContent:
    """Lo que hay que enseñar: las dos versiones (líneas sin su salto) y,
    si se tienen, los bloques ya calculados. `note` sustituye al diff
    cuando no hay texto que comparar (un binario, un archivo nuevo
    enorme…)."""
    old_lines: List[str] = field(default_factory=list)
    new_lines: List[str] = field(default_factory=list)
    codes: Optional[Sequence[diff.Opcode]] = None
    note: str = ""


class DiffText(Console):
    """Cuadro de solo lectura con el diff pintado."""

    def __init__(self, master, fonts, height=420, **kw):
        super().__init__(master, fonts, height=height, **kw)
        self.configure(wrap="none")
        for tag, (fg, bg) in DIFF_COLORS.items():
            options = {"foreground": fg}
            if bg:
                options["background"] = bg
            self._safe(lambda t=tag, o=options: self.tag_config(t, **o))

    def render(self, content: DiffContent, context: int = 3) -> str:
        """Pinta `content` y devuelve el resumen («+3 −1 líneas…»)."""
        self.clear()
        if content.note:
            self.write(content.note, "num")
            return content.note
        codes = content.codes
        if codes is None:
            codes = diff.opcodes(content.old_lines, content.new_lines)
        if not diff.has_changes(codes):
            self.write("No hay diferencias.", "num")
            return "Sin diferencias"
        added, removed = diff.change_counts(codes)
        hunks = diff.hunks(content.old_lines, content.new_lines, context,
                           codes)
        width = len(str(max(len(content.old_lines),
                            len(content.new_lines), 1)))
        shown = 0
        total = sum(len(h.lines) + 1 for h in hunks)
        for hunk in hunks:
            if shown >= MAX_RENDER_LINES:
                break
            self.write(diff.hunk_header(hunk), "hunk")
            shown += 1
            for line in hunk.lines:
                if shown >= MAX_RENDER_LINES:
                    break
                self.write_parts((
                    (_number(line.old_number, width) + " "
                     + _number(line.new_number, width) + " │", "num"),
                    (line.tag + line.text, _TAGS[line.tag])))
                shown += 1
        if shown < total:
            self.write(f"… y {total - shown} líneas más que no se pintan "
                       "aquí (el cambio completo sí se aplica).", "num")
        summary = (f"+{added} −{removed} líneas · {len(hunks)} "
                   f"{'trozo' if len(hunks) == 1 else 'trozos'} con cambios")
        self._safe(lambda: self.see("1.0"))
        return summary


_TAGS = {" ": "ctx", "-": "del", "+": "add"}


def _number(value: Optional[int], width: int) -> str:
    if value is None:
        return " " * width
    return str(value).rjust(width)


class DiffDialog(WorkerDialog):
    """Ventana con las diferencias de un archivo."""
    TITLE = "Diferencias"
    SIZE = (940, 660)
    MIN_SIZE = (640, 420)

    def __init__(self, app, title: str, loader: Callable[[], DiffContent],
                 subtitle: str = ""):
        super().__init__(app, title=f"Diferencias · {title}")
        f = self.fonts
        pad = {"padx": 20}
        ctk.CTkLabel(self, text="🔍  " + title, font=f.title,
                     text_color=TEXT, anchor="w"
                     ).pack(anchor="w", pady=(16, 0), **pad)
        self.lbl_summary = ctk.CTkLabel(self, text=subtitle or "Cargando…",
                                        font=f.small, text_color=MUTED,
                                        justify="left", wraplength=880)
        self.lbl_summary.pack(anchor="w", pady=(2, 8), **pad)
        legend = ctk.CTkFrame(self, fg_color="transparent")
        legend.pack(anchor="w", **pad)
        ctk.CTkLabel(legend, text="−  lo que sale", font=f.small,
                     text_color=RED).pack(side="left", padx=(0, 16))
        ctk.CTkLabel(legend, text="+  lo que entra", font=f.small,
                     text_color=GREEN_T).pack(side="left")
        self.view = DiffText(self, f)
        self.view.pack(fill="both", expand=True, pady=(6, 8), **pad)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", pady=(0, 14), **pad)
        row.grid_columnconfigure(0, weight=1)
        self.btn_copy = button(row, f, "📋 Copiar", self._copy, kind="ghost",
                               width=110, height=32)
        self.btn_copy.grid(row=0, column=1, padx=(0, 8))
        button(row, f, "Cerrar", self.close, kind="ghost", width=100,
               height=32).grid(row=0, column=2)
        self.subtitle = subtitle
        self.content: Optional[DiffContent] = None
        self.running = True
        self.run_in_thread(self._load, loader)

    def _load(self, loader: Callable[[], DiffContent]) -> None:
        self.post("diff", loader())

    def _copy(self) -> None:
        kind = self.view.copy()
        self.btn_copy.configure(text="✔ Copiado" if kind else "Nada")

    def handle(self, msg) -> None:
        if msg[0] == "diff":
            self.running = False
            self.content = msg[1]
            summary = self.view.render(self.content)
            text = summary if not self.subtitle else \
                f"{self.subtitle}  ·  {summary}"
            self.lbl_summary.configure(text=text)
        elif msg[0] == "err":
            self.running = False
            self.lbl_summary.configure(text="✖ " + msg[1], text_color=RED)
