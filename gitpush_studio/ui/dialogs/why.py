# -*- coding: utf-8 -*-
"""«¿Por qué no se sube este archivo?»: eliges un archivo o una carpeta
del proyecto y la ventana dice si se publica y, si no, qué regla exacta
lo deja fuera (y dónde está escrita)."""

from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from ...core.fs import normalize_path
from ...core.why import NOT_PUBLISHED, PUBLISHED, explain
from ..base import WorkerDialog
from ..theme import AMBER, GREEN_T, MUTED, RED, TEXT
from ..widgets import browse_folder_into, button, entry, panel, set_text


class WhyDialog(WorkerDialog):
    TITLE = "¿Por qué no se sube?"
    SIZE = (760, 520)
    MIN_SIZE = (620, 420)

    def __init__(self, app):
        super().__init__(app)
        f = self.fonts
        pad = {"padx": 22}
        ctk.CTkLabel(self, text="❓  ¿Por qué no se sube?", font=f.title,
                     text_color=TEXT).pack(anchor="w", pady=(18, 0), **pad)
        ctk.CTkLabel(self, text="Elige un archivo (o una carpeta) del "
                                "proyecto y te digo si se publica y, si no, "
                                "qué regla lo deja fuera.", font=f.small,
                     text_color=MUTED, justify="left", wraplength=700
                     ).pack(anchor="w", pady=(2, 10), **pad)

        top = panel(self, f, "📁  Proyecto")
        row = ctk.CTkFrame(top, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(6, 12))
        row.grid_columnconfigure(0, weight=1)
        self.e_folder = entry(row, f, "Carpeta del proyecto")
        self.e_folder.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        button(row, f, "Examinar…",
               lambda: browse_folder_into(self.e_folder, "Proyecto"),
               width=110).grid(row=0, column=1)

        target = panel(self, f, "📄  Archivo o carpeta", pady=(10, 0))
        row2 = ctk.CTkFrame(target, fg_color="transparent")
        row2.pack(fill="x", padx=16, pady=(6, 12))
        row2.grid_columnconfigure(0, weight=1)
        self.e_target = entry(row2, f, "ruta/dentro/del/proyecto.txt")
        self.e_target.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.e_target.bind("<Return>", lambda _e: self._explain())
        button(row2, f, "Archivo…", self._pick_file, width=96
               ).grid(row=0, column=1, padx=(0, 6))
        button(row2, f, "Carpeta…", self._pick_folder, width=96
               ).grid(row=0, column=2)

        self.btn_go = button(self, f, "Explicar", self._explain,
                             kind="primary", height=40)
        self.btn_go.pack(fill="x", pady=(12, 0), **pad)
        self.lbl_answer = ctk.CTkLabel(self, text="", font=f.body,
                                       text_color=TEXT, justify="left",
                                       wraplength=700, anchor="w")
        self.lbl_answer.pack(anchor="w", fill="x", pady=(12, 0), **pad)
        self.lbl_status = ctk.CTkLabel(self, text="", font=f.small,
                                       text_color=MUTED)
        self.lbl_status.pack(anchor="w", pady=(4, 0), **pad)
        button(self, f, "Cerrar", self.close, kind="ghost", width=100,
               height=36).pack(anchor="e", pady=(10, 16), **pad)

        folder = normalize_path(app.e_folder.get())
        if folder and Path(folder).is_dir():
            self.e_folder.insert(0, folder)

    def _pick_file(self):
        folder = normalize_path(self.e_folder.get())
        chosen = filedialog.askopenfilename(
            title="¿Qué archivo?", initialdir=folder or str(Path.home()))
        if chosen:
            set_text(self.e_target, str(Path(chosen)))
            self._explain()

    def _pick_folder(self):
        if browse_folder_into(self.e_target, "¿Qué carpeta?"):
            self._explain()

    def _explain(self):
        if self.running:
            return
        folder = normalize_path(self.e_folder.get())
        if not folder or not Path(folder).is_dir():
            self.status("Indica una carpeta de proyecto que exista.", RED)
            return
        target = self.e_target.get().strip()
        if not target:
            self.status("Elige o escribe un archivo del proyecto.", AMBER)
            return
        self.running = True
        self.btn_go.configure(state="disabled", text="Mirando…")
        self.status("", MUTED)
        self.run_in_thread(self._work, folder, target,
                           self.app.ignore_lines())

    def _work(self, folder, target, box):
        self.post("answer", explain(folder, target, box))

    def handle(self, msg):
        if msg[0] == "answer":
            self.running = False
            self.btn_go.configure(state="normal", text="Explicar")
            answer = msg[1]
            color = {PUBLISHED: GREEN_T, NOT_PUBLISHED: AMBER}.get(
                answer.verdict, RED)
            self.lbl_answer.configure(text="\n".join(answer.lines()),
                                      text_color=color)
            self.app.log("❓ " + " ".join(answer.lines()[0].split()), "dim")
        elif msg[0] == "err":
            self.running = False
            self.btn_go.configure(state="normal", text="Explicar")
            self.status("✖ " + msg[1], RED)
