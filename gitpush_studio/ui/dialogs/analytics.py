# -*- coding: utf-8 -*-
"""Analizar un proyecto entero: de qué está hecho y cómo ha crecido.

El análisis corre en un hilo (`core.analytics.analyze_project`); aquí
solo se elige la carpeta, se enseña el progreso y el resumen, y se
guardan el informe HTML y el JSON. Si el informe se guarda DENTRO del
proyecto, se añade al cuadro de ignorar para que no se publique.
"""

import threading
import webbrowser
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from ...core.analytics import analyze_project, summary_lines
from ...core.errors import Cancelled
from ...core.fs import normalize_path, relative_inside
from ...core.ignore import anchor_pattern
from ...core.report_html import write_html, write_json
from ..base import WorkerDialog
from ..theme import AMBER, BLUE, GREEN_T, INPUT, MUTED, PANEL, RED, TEXT
from ..widgets import (browse_folder_into, button, checkbox, entry, hint,
                       panel, progress_bar, scrollable, separator, set_text)


class AnalyticsDialog(WorkerDialog):
    TITLE = "Analizar proyecto"
    SIZE = (760, 720)
    MIN_SIZE = (620, 520)

    def __init__(self, app):
        super().__init__(app)
        f = self.fonts
        self.report = None
        self.cancel_event = threading.Event()
        pad = {"padx": 22}
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(header, text="📊  Analizar proyecto", font=f.title,
                     text_color=TEXT).pack(anchor="w", pady=(18, 0), **pad)
        ctk.CTkLabel(header, text="De qué está hecho, cuánto es tuyo y cuánto "
                                  "generado, qué se repite y cómo ha "
                                  "crecido. Nada sale de tu equipo.",
                     font=f.small, text_color=MUTED
                     ).pack(anchor="w", pady=(2, 12), **pad)

        body = scrollable(self)
        body.grid(row=1, column=0, sticky="nsew")

        c1 = panel(body, f, "📁  Proyecto")
        rowp = ctk.CTkFrame(c1, fg_color="transparent")
        rowp.pack(fill="x", padx=16, pady=(6, 0))
        rowp.grid_columnconfigure(0, weight=1)
        self.e_proj = entry(rowp, f, "Carpeta del proyecto")
        self.e_proj.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        button(rowp, f, "Examinar…", lambda: browse_folder_into(
            self.e_proj, "Proyecto a analizar"), width=110
               ).grid(row=0, column=1)
        self.v_history = ctk.BooleanVar(value=True)
        self.v_ignores = ctk.BooleanVar(value=True)
        checkbox(c1, f, "Incluir el historial de git (personas, ritmo, lo "
                        "que más se toca)", self.v_history
                 ).pack(anchor="w", padx=16, pady=(10, 0))
        checkbox(c1, f, "Respetar .gitignore y el cuadro de ignorar "
                        "(analiza lo que se publicaría)", self.v_ignores
                 ).pack(anchor="w", padx=16, pady=(6, 0))
        hint(c1, f, "Las líneas se clasifican en código, comentario y en "
                    "blanco con las reglas de cada lenguaje (los docstrings "
                    "de Python cuentan como documentación). Las dependencias "
                    "copiadas, los archivos de bloqueo y lo minificado se "
                    "cuentan aparte, para que no inflen tu código.",
             wraplength=660, pady=(8, 12))

        c2 = panel(body, f, "📋  Resumen", pady=(12, 0))
        self.t_out = ctk.CTkTextbox(c2, height=330, font=f.mono_s,
                                    fg_color=INPUT, text_color=TEXT,
                                    wrap="none", corner_radius=8)
        self.t_out.pack(fill="both", expand=True, padx=16, pady=(6, 14))
        self._set_output("Elige la carpeta y pulsa «Analizar».")

        footer = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0)
        footer.grid(row=2, column=0, sticky="ew")
        separator(footer).pack(fill="x", side="top")
        self.pbar = progress_bar(footer)
        self.pbar.pack(fill="x", pady=(12, 0), **pad)
        self.lbl_status = ctk.CTkLabel(footer, text="", font=f.small,
                                       text_color=MUTED, justify="left",
                                       wraplength=700)
        self.lbl_status.pack(anchor="w", pady=(6, 0), **pad)
        rowb = ctk.CTkFrame(footer, fg_color="transparent")
        rowb.pack(fill="x", pady=(10, 16), **pad)
        rowb.grid_columnconfigure(0, weight=1)
        self.btn_go = button(rowb, f, "Analizar", self._start,
                             kind="primary", height=42)
        self.btn_go.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.btn_stop = button(rowb, f, "Detener", self._stop, kind="ghost",
                               width=96, height=42, text_color=AMBER,
                               state="disabled")
        self.btn_stop.grid(row=0, column=1, padx=(0, 10))
        self.btn_html = button(rowb, f, "Informe HTML…", self._save_html,
                               kind="ghost", width=140, height=42,
                               text_color=BLUE, state="disabled")
        self.btn_html.grid(row=0, column=2, padx=(0, 10))
        self.btn_json = button(rowb, f, "JSON…", self._save_json,
                               kind="ghost", width=80, height=42,
                               state="disabled")
        self.btn_json.grid(row=0, column=3, padx=(0, 10))
        button(rowb, f, "Cerrar", self.close, kind="ghost", width=90,
               height=42).grid(row=0, column=4)

        proj = normalize_path(app.e_folder.get())
        if proj and Path(proj).is_dir():
            set_text(self.e_proj, proj)

    # ------------------------------------------------------------------
    def _set_output(self, text):
        self.t_out.configure(state="normal")
        self.t_out.delete("1.0", "end")
        self.t_out.insert("1.0", text)
        self.t_out.configure(state="disabled")

    def close(self):
        self.cancel_event.set()
        super().close()

    def _stop(self):
        self.cancel_event.set()
        self.btn_stop.configure(state="disabled")
        self.status("Deteniendo…", AMBER)

    def _start(self):
        if self.running:
            return
        proj = normalize_path(self.e_proj.get())
        if not proj or not Path(proj).is_dir():
            self.status("Indica una carpeta que exista.", RED)
            return
        self.running = True
        self.report = None
        self.cancel_event = threading.Event()
        self.pbar.set(0)
        self.btn_go.configure(state="disabled", text="Analizando…")
        self.btn_stop.configure(state="normal")
        self.btn_html.configure(state="disabled")
        self.btn_json.configure(state="disabled")
        self.status("Buscando los archivos del proyecto…", MUTED)
        patrones = self.app.ignore_lines() if self.v_ignores.get() else []
        self.run_in_thread(self._work, proj, patrones,
                           bool(self.v_ignores.get()),
                           bool(self.v_history.get()), self.cancel_event)

    def _work(self, proj, patrones, respetar, historial, cancel):
        def progreso(hechos, total, rel):
            self.post("prog", hechos, total, rel)
        try:
            rep = analyze_project(proj, patrones, respect_ignores=respetar,
                                  history=historial, on_progress=progreso,
                                  cancel=cancel)
        except Cancelled:
            self.post("cancelled")
            return
        self.post("ok", rep)

    def _reset(self):
        self.running = False
        self.btn_go.configure(state="normal", text="Analizar")
        self.btn_stop.configure(state="disabled")

    def handle(self, m):
        kind = m[0]
        if kind == "prog":
            _, hechos, total, rel = m
            self.pbar.set(hechos / max(1, total))
            self.status(f"Leyendo {hechos:,} de {total:,} archivos…"
                        .replace(",", "."), MUTED)
        elif kind == "ok":
            self._reset()
            self.report = m[1]
            self.pbar.set(1)
            self._set_output("\n".join(summary_lines(self.report)))
            self.btn_html.configure(state="normal")
            self.btn_json.configure(state="normal")
            self.status(f"✔ Análisis listo en {self.report.seconds:.1f} s. "
                        "Guarda el informe HTML para verlo con gráficos.",
                        GREEN_T)
            self.app.log(f"📊 Proyecto analizado: {self.report.name} "
                         f"({self.report.total_files} archivos, "
                         f"{self.report.seconds:.1f} s).", "ok")
        elif kind == "cancelled":
            self._reset()
            self.pbar.set(0)
            self.status("Análisis detenido. No se ha guardado nada.", AMBER)
        elif kind == "err":
            self._reset()
            self.status("✖ " + m[1], RED)

    # ---- guardar ---------------------------------------------------------
    def _ask_path(self, sufijo, extension, tipos):
        proj = Path(self.report.root)
        # Por defecto, JUNTO al proyecto y no dentro: así no hay nada que
        # excluir y el informe no puede acabar publicado por descuido.
        return filedialog.asksaveasfilename(
            title="¿Dónde guardar el informe?", defaultextension=extension,
            initialfile=(proj.name or "proyecto") + sufijo + extension,
            initialdir=str(proj.parent if proj.parent.is_dir()
                           else Path.home()),
            filetypes=tipos)

    def _protect(self, destino):
        """Si el informe cae dentro del proyecto, que no se publique."""
        rel = relative_inside(destino, self.report.root)
        if rel:
            # Escapado: «informe[2026].html» no puede leerse como patrón.
            self.app.add_ignore_patterns([anchor_pattern(rel)])
            self.app.log("  El informe quedó dentro del proyecto: lo añadí "
                         "al cuadro de ignorar para que no se suba.", "warn")

    def _save_html(self):
        if self.report is None:
            return
        destino = self._ask_path("-analisis", ".html",
                                 [("Página web", "*.html")])
        if not destino:
            return
        try:
            write_html(self.report, destino)
        except OSError as e:
            self.status(f"✖ No se pudo guardar: {e}", RED)
            return
        self._protect(destino)
        self.status(f"✔ Informe guardado: {destino}", GREEN_T)
        self.app.log(f"  Informe HTML: {destino}", "cmd")
        try:
            webbrowser.open(Path(destino).resolve().as_uri())
        except Exception:                                   # noqa: BLE001
            pass

    def _save_json(self):
        if self.report is None:
            return
        destino = self._ask_path("-analisis", ".json",
                                 [("JSON", "*.json")])
        if not destino:
            return
        try:
            write_json(self.report, destino)
        except OSError as e:
            self.status(f"✖ No se pudo guardar: {e}", RED)
            return
        self._protect(destino)
        self.status(f"✔ JSON guardado: {destino}", GREEN_T)
        self.app.log(f"  Informe JSON: {destino}", "cmd")
