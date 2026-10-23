# -*- coding: utf-8 -*-
"""Consolidar un proyecto (árbol + contenido) en un único TXT."""

from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from ...core.fs import fmt_size, normalize_path, open_in_explorer
from ...core.pack import consolidate_project
from ..base import WorkerDialog
from ..theme import BLUE, GREEN_T, MUTED, RED, TEXT
from ..widgets import (browse_folder_into, button, checkbox, entry, hint,
                       panel, set_text)


def _inside(out, proj):
    try:
        return Path(out).resolve().is_relative_to(Path(proj).resolve())
    except AttributeError:                       # Python < 3.9
        return str(Path(out).resolve()).startswith(str(Path(proj).resolve()))


class ConsolidateDialog(WorkerDialog):
    TITLE = "Consolidar proyecto a TXT"
    SIZE = (600, 520)
    MIN_SIZE = (540, 440)

    def __init__(self, app):
        super().__init__(app)
        f = self.fonts
        self.out_path = ""
        pad = {"padx": 22}

        ctk.CTkLabel(self, text="📄  Consolidar a TXT", font=f.title,
                     text_color=TEXT).pack(anchor="w", pady=(18, 0), **pad)
        ctk.CTkLabel(self, text="Todo el proyecto (árbol + contenido) en un "
                                "solo archivo de texto.", font=f.small,
                     text_color=MUTED).pack(anchor="w", pady=(2, 12), **pad)

        c1 = panel(self, f, "📁  Proyecto a empaquetar")
        rowp = ctk.CTkFrame(c1, fg_color="transparent")
        rowp.pack(fill="x", padx=16, pady=(6, 12))
        rowp.grid_columnconfigure(0, weight=1)
        self.e_proj = entry(rowp, f, "Carpeta del proyecto")
        self.e_proj.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        button(rowp, f, "Examinar…", self._browse_proj, width=110
               ).grid(row=0, column=1)

        c2 = panel(self, f, "💾  Archivo TXT de salida", pady=(12, 0))
        rowo = ctk.CTkFrame(c2, fg_color="transparent")
        rowo.pack(fill="x", padx=16, pady=(6, 0))
        rowo.grid_columnconfigure(0, weight=1)
        self.e_out = entry(rowo, f, "ruta\\del\\proyecto.txt")
        self.e_out.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        button(rowo, f, "Elegir…", self._browse_out, width=110
               ).grid(row=0, column=1)
        self.v_ign = ctk.BooleanVar(value=True)
        checkbox(c2, f, "Respetar .gitignore y el cuadro de ignorar "
                        "(recomendado)", self.v_ign
                 ).pack(anchor="w", padx=16, pady=(8, 0))
        hint(c2, f, "Por defecto se llama como el proyecto y se guarda junto "
                    "a él. Los binarios y los archivos de más de 2 MB se "
                    "marcan pero no se vuelcan; el TXT nunca se incluye a sí "
                    "mismo.", wraplength=520, pady=(6, 12))

        self.lbl_status = ctk.CTkLabel(self, text="", font=f.small,
                                       text_color=MUTED, justify="left",
                                       wraplength=540)
        self.lbl_status.pack(anchor="w", pady=(12, 0), **pad)

        rowb = ctk.CTkFrame(self, fg_color="transparent")
        rowb.pack(fill="x", pady=(10, 18), **pad)
        rowb.grid_columnconfigure(0, weight=1)
        self.btn_go = button(rowb, f, "Generar TXT", self._generate,
                             kind="primary", height=42)
        self.btn_go.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.btn_open = button(rowb, f, "Abrir TXT",
                               lambda: open_in_explorer(self.out_path),
                               kind="ghost", width=110, height=42,
                               text_color=BLUE, state="disabled")
        self.btn_open.grid(row=0, column=1, padx=(0, 10))
        button(rowb, f, "Cerrar", self.close, kind="ghost", width=90,
               height=42).grid(row=0, column=2)

        proj = normalize_path(app.e_folder.get())
        if proj and Path(proj).is_dir():
            self.e_proj.insert(0, proj)
            self._suggest_output()

    # ------------------------------------------------------------------
    def _suggest_output(self):
        proj = normalize_path(self.e_proj.get())
        p = Path(proj)
        if proj and p.is_dir():
            set_text(self.e_out, str(p.parent / (p.name + ".txt")))

    def _browse_proj(self):
        if browse_folder_into(self.e_proj, "Proyecto a empaquetar"):
            self._suggest_output()

    def _browse_out(self):
        proj = Path(normalize_path(self.e_proj.get() or "."))
        chosen = filedialog.asksaveasfilename(
            title="¿Dónde guardar el TXT?", defaultextension=".txt",
            initialfile=(proj.name or "proyecto") + ".txt",
            initialdir=str(proj.parent if proj.parent.is_dir()
                           else Path.home()),
            filetypes=[("Archivo de texto", "*.txt")])
        if chosen:
            set_text(self.e_out, str(Path(chosen)))

    def _generate(self):
        if self.running:
            return
        proj = normalize_path(self.e_proj.get())
        if not proj or not Path(proj).is_dir():
            self.status("Indica una carpeta de proyecto que exista.", RED)
            return
        out = normalize_path(self.e_out.get())
        if not out:
            p = Path(proj)
            out = str(p.parent / (p.name + ".txt"))
            set_text(self.e_out, out)
        self.running = True
        self.btn_go.configure(state="disabled", text="Generando…")
        self.status("Empaquetando el proyecto…", MUTED)
        self.run_in_thread(self._work, proj, out, self.app.ignore_lines(),
                           bool(self.v_ign.get()))

    def _work(self, proj, out, patterns, respect):
        stats = consolidate_project(proj, out, patterns,
                                    respect_ignores=respect)
        self.post("ok", out, stats, _inside(out, proj))

    def handle(self, m):
        if m[0] == "err":
            self.running = False
            self.status("✖ " + m[1], RED)
            self.btn_go.configure(state="normal", text="Generar TXT")
        elif m[0] == "ok":
            _, out, st, inside = m
            self.running = False
            self.out_path = out
            extra = ""
            if st["binaries"] or st["big"]:
                extra = (f"  ·  omitidos: {st['binaries']} binarios, "
                         f"{st['big']} enormes")
            if st.get("truncated"):
                extra += "  ·  ⚠ salida truncada"
            self.status(f"✔ TXT generado: {st['files']} archivos, "
                        f"{fmt_size(st['bytes'])} en {st['seconds']:.1f} s"
                        f"{extra}\n{out}", GREEN_T)
            self.btn_go.configure(state="normal", text="Generar TXT")
            self.btn_open.configure(state="normal")
            self.app.log(f"✔ Proyecto consolidado en {out} ({st['files']} "
                         f"archivos, {fmt_size(st['bytes'])}).", "ok")
            if inside:
                self.app.add_ignore_patterns([Path(out).name])
                self.app.log("  El TXT quedó dentro del proyecto: lo añadí "
                             "al cuadro de ignorar para que no se suba a "
                             "GitHub.", "warn")
