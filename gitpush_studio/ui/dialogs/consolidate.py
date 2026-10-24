# -*- coding: utf-8 -*-
"""Consolidar un proyecto (árbol + contenido) en un único TXT."""

from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from ...core.desktop import open_path, reveal_in_folder
from ...core.fs import fmt_size, normalize_path, relative_inside
from ...core.ignore import anchor_pattern
from ...core.pack import consolidate_project, free_txt_path
from ..base import WorkerDialog
from ..theme import AMBER, BLUE, GREEN_T, MUTED, RED, TEXT
from ..widgets import (browse_folder_into, button, checkbox, entry, hint,
                       panel, set_text)


def _inside(out, proj):
    """Ruta del TXT relativa al proyecto si cae dentro, o ""."""
    return relative_inside(out, proj)


class ConsolidateDialog(WorkerDialog):
    TITLE = "Consolidar proyecto a TXT"
    SIZE = (660, 520)
    MIN_SIZE = (600, 440)

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
                               lambda: open_path(self.out_path),
                               kind="ghost", width=110, height=42,
                               text_color=BLUE, state="disabled")
        self.btn_open.grid(row=0, column=1, padx=(0, 10))
        # La carpeta donde quedó, con el TXT ya seleccionado (como «Abrir
        # ubicación del archivo» del Explorador).
        self.btn_reveal = button(rowb, f, "📂  Abrir ubicación", self._reveal,
                                 kind="ghost", width=150, height=42,
                                 text_color=BLUE, state="disabled")
        self.btn_reveal.grid(row=0, column=2, padx=(0, 10))
        button(rowb, f, "Cerrar", self.close, kind="ghost", width=90,
               height=42).grid(row=0, column=3)

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

    def _reveal(self):
        """Abre la carpeta del TXT con el archivo seleccionado. Hablar con
        el explorador de archivos puede tardar un momento: en un hilo,
        para que la ventana no se quede congelada."""
        destino = self.out_path
        if not destino:
            return
        if not Path(destino).exists():
            self.status(f"El TXT ya no está en «{Path(destino).parent}» (¿lo "
                        "moviste o lo borraste?): abro esa carpeta.", AMBER)
        self.run_in_thread(self._reveal_worker, destino)

    def _reveal_worker(self, destino):
        if not reveal_in_folder(destino):
            self.post("reveal_fail", destino)

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
        # Nunca encima de un archivo tuyo (ni a través de un enlace): si el
        # destino ya existe y no es un TXT nuestro, se elige otro nombre.
        pedido = Path(out)
        real, renombrado = free_txt_path(pedido)
        stats = consolidate_project(proj, real, patterns,
                                    respect_ignores=respect)
        stats["renamed_from"] = str(pedido) if renombrado else ""
        self.post("ok", str(real), stats, _inside(str(real), proj))

    def handle(self, m):
        if m[0] == "reveal_fail":
            self.status(f"✖ No pude abrir la carpeta «{Path(m[1]).parent}». "
                        "Está en la ruta de arriba.", RED)
            return
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
            if st.get("renamed_from"):
                extra += (f"\n«{Path(st['renamed_from']).name}» ya existía y "
                          "no es un TXT de la app: no lo toqué.")
            self.status(f"✔ TXT generado: {st['files']} archivos, "
                        f"{fmt_size(st['bytes'])} en {st['seconds']:.1f} s"
                        f"{extra}\n{out}", GREEN_T)
            self.btn_go.configure(state="normal", text="Generar TXT")
            self.btn_open.configure(state="normal")
            self.btn_reveal.configure(state="normal")
            self.app.log(f"✔ Proyecto consolidado en {out} ({st['files']} "
                         f"archivos, {fmt_size(st['bytes'])}).", "ok")
            if inside:
                self.app.add_ignore_patterns([anchor_pattern(inside)])
                self.app.log("  El TXT quedó dentro del proyecto: lo añadí "
                             "al cuadro de ignorar para que no se suba a "
                             "GitHub.", "warn")
