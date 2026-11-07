# -*- coding: utf-8 -*-
"""Revisar todo el historial en busca de secretos que ya se publicaron.

Enseña cada hallazgo con el commit en el que entró y si sigue en la
versión actual. Lo que sigue se puede dejar fuera desde aquí (pasa al
cuadro de ignorar); lo que ya solo está en el historial no se arregla
borrándolo: hay que cambiar la clave en su servicio.
"""

import threading
from pathlib import Path
from typing import List

import customtkinter as ctk

from ...core import secret_rules as rules
from ...core.errors import Cancelled
from ...core.fs import normalize_path
from ...core.history_scan import (ROTATE_ADVICE, HistoryFinding,
                                  HistoryReport, scan_history)
from ...core.ignore import anchor_pattern
from ..base import WorkerDialog
from ..theme import (AMBER, BADGE_AMBER_BG, BADGE_AMBER_FG, BADGE_RED_BG,
                     BADGE_RED_FG, BORDER, GREEN_T, MUTED, PANEL, RED, TEXT)
from ..widgets import (browse_folder_into, button, entry, panel,
                       progress_bar, scrollable, shorten)

MAX_ROWS = 200


class HistoryScanDialog(WorkerDialog):
    TITLE = "Revisar el historial"
    SIZE = (820, 700)
    MIN_SIZE = (660, 520)

    def __init__(self, app):
        super().__init__(app)
        f = self.fonts
        pad = {"padx": 22}
        self.cancel_event = threading.Event()
        self.report: HistoryReport = HistoryReport(findings=[])

        ctk.CTkLabel(self, text="🕵  Revisar el historial", font=f.title,
                     text_color=TEXT).pack(anchor="w", pady=(18, 0), **pad)
        ctk.CTkLabel(self, text="Busca contraseñas, tokens y llaves en TODOS "
                                "los commits, también en archivos que ya "
                                "borraste: si se publicaron alguna vez, "
                                "siguen en GitHub.", font=f.small,
                     text_color=MUTED, justify="left", wraplength=760
                     ).pack(anchor="w", pady=(2, 10), **pad)

        top = panel(self, f, "📁  Repositorio")
        row = ctk.CTkFrame(top, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(6, 12))
        row.grid_columnconfigure(0, weight=1)
        self.e_folder = entry(row, f, "Carpeta del repositorio")
        self.e_folder.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        button(row, f, "Examinar…",
               lambda: browse_folder_into(self.e_folder, "Repositorio"),
               width=110).grid(row=0, column=1)

        self.pbar = progress_bar(self)
        self.pbar.pack(fill="x", pady=(12, 0), **pad)
        self.results = scrollable(self, height=280)
        self.results.pack(fill="both", expand=True, pady=(10, 0), **pad)
        self.lbl_status = ctk.CTkLabel(self, text="", font=f.small,
                                       text_color=MUTED, justify="left",
                                       wraplength=760)
        self.lbl_status.pack(anchor="w", pady=(8, 0), **pad)

        rowb = ctk.CTkFrame(self, fg_color="transparent")
        rowb.pack(fill="x", pady=(10, 16), **pad)
        rowb.grid_columnconfigure(0, weight=1)
        self.btn_go = button(rowb, f, "🔍  Revisar historial", self._start,
                             kind="primary", height=42)
        self.btn_go.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.btn_stop = button(rowb, f, "Detener", self._stop, kind="ghost",
                               width=96, height=42, text_color=AMBER,
                               state="disabled")
        self.btn_stop.grid(row=0, column=1, padx=(0, 10))
        button(rowb, f, "Cerrar", self.close, kind="ghost", width=90,
               height=42).grid(row=0, column=2)

        folder = normalize_path(app.e_folder.get())
        if folder and Path(folder).is_dir():
            self.e_folder.insert(0, folder)

    # ------------------------------------------------------------------
    def close(self):
        self.cancel_event.set()
        super().close()

    def _stop(self):
        if self.running:
            self.cancel_event.set()
            self.btn_stop.configure(state="disabled")
            self.status("Deteniendo…", AMBER)

    def _start(self):
        if self.running:
            return
        folder = normalize_path(self.e_folder.get())
        if not folder or not (Path(folder) / ".git").is_dir():
            self.status("Indica la carpeta de un repositorio de git (con su "
                        "carpeta .git).", RED)
            return
        for child in self.results.winfo_children():
            child.destroy()
        self.running = True
        self.cancel_event = threading.Event()
        self.pbar.set(0)
        self.btn_go.configure(state="disabled", text="Revisando…")
        self.btn_stop.configure(state="normal")
        self.status("Leyendo el historial…", MUTED)
        self.run_in_thread(self._work, folder, self.cancel_event)

    def _work(self, folder: str, cancel: threading.Event) -> None:
        def progress(done: int, total: int) -> None:
            self.post("prog", done, total)
        try:
            report = scan_history(folder, cancel=cancel,
                                  on_progress=progress)
        except Cancelled:
            self.post("cancelled")
            return
        self.post("done", folder, report)

    def _reset(self):
        self.running = False
        self.btn_go.configure(state="normal", text="🔍  Revisar historial")
        self.btn_stop.configure(state="disabled")

    def _show(self, folder: str, report: HistoryReport) -> None:
        self.report = report
        f = self.fonts
        serious: List[HistoryFinding] = [
            x for x in report.findings if x.confidence != rules.BAJA]
        resumen = (f"Revisados {report.commits} commits y {report.read} "
                   f"contenidos distintos en {report.seconds:.1f} s.")
        if report.skipped:
            resumen += (f" {report.skipped} no se revisaron por dentro "
                        "(demasiado grandes).")
        if not serious:
            self.status("✔ Ningún secreto a la vista en el historial. "
                        + resumen, GREEN_T)
            self.app.log("🕵 Historial revisado: sin secretos a la vista.",
                         "ok")
            return
        ctk.CTkLabel(self.results, text="⚠ " + ROTATE_ADVICE, font=f.small,
                     text_color=AMBER, justify="left", wraplength=720
                     ).pack(anchor="w", padx=6, pady=(4, 8))
        for finding in serious[:MAX_ROWS]:
            self._row(folder, finding)
        self.status(f"⚠ {len(serious)} hallazgo(s). " + resumen, AMBER)
        self.app.log(f"🕵 Historial revisado: {len(serious)} hallazgo(s) "
                     "(ver la ventana «Revisar el historial»).", "warn")

    def _row(self, folder: str, finding: HistoryFinding) -> None:
        f = self.fonts
        box = ctk.CTkFrame(self.results, fg_color=PANEL, corner_radius=8,
                           border_width=1, border_color=BORDER)
        box.pack(fill="x", padx=4, pady=3)
        box.grid_columnconfigure(1, weight=1)
        alta = finding.confidence == rules.ALTA
        ctk.CTkLabel(box, text=" sigue " if finding.still_present
                     else " en el historial ", font=f.small, corner_radius=8,
                     fg_color=BADGE_RED_BG if alta else BADGE_AMBER_BG,
                     text_color=BADGE_RED_FG if alta else BADGE_AMBER_FG,
                     width=110, height=20
                     ).grid(row=0, column=0, sticky="w", padx=(8, 0), pady=6)
        ctk.CTkLabel(box, text=shorten(finding.path, 60), font=f.mono_s,
                     text_color=TEXT, anchor="w"
                     ).grid(row=0, column=1, sticky="ew", padx=(8, 8))
        if finding.still_present:
            button(box, f, "Dejar fuera",
                   lambda x=finding: self._exclude(x), kind="ghost",
                   width=104, height=26, font=f.small
                   ).grid(row=0, column=2, padx=(0, 8))
        linea = f"línea {finding.line} · " if finding.line else ""
        ctk.CTkLabel(box, text=f"{finding.reason} · {linea}"
                               f"{finding.where()}", font=f.small,
                     text_color=MUTED, justify="left", wraplength=680,
                     anchor="w").grid(row=1, column=0, columnspan=3,
                                      sticky="w", padx=10, pady=(0, 6))

    def _exclude(self, finding: HistoryFinding) -> None:
        """Al cuadro de ignorar: en la próxima publicación deja de estar
        en la versión actual de GitHub (el historial no cambia)."""
        self.app.add_ignore_patterns([anchor_pattern(finding.path)])
        self.status(f"«{finding.path}» irá fuera en la próxima publicación. "
                    "Recuerda cambiar esa clave: en el historial sigue.",
                    AMBER)

    def handle(self, msg):
        kind = msg[0]
        if kind == "prog":
            _, done, total = msg
            if self.running and total:
                self.pbar.set(done / total)
                self.status(f"Revisando contenidos… {done} de {total}",
                            MUTED)
        elif kind == "done":
            self._reset()
            self.pbar.set(1)
            self._show(msg[1], msg[2])
        elif kind == "cancelled":
            self._reset()
            self.pbar.set(0)
            self.status("Detenido.", AMBER)
        elif kind == "err":
            self._reset()
            self.pbar.set(0)
            self.status("✖ " + msg[1], RED)
