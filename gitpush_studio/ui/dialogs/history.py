# -*- coding: utf-8 -*-
"""Versiones: ver el historial de la carpeta, volver a una versión
anterior (se crea un commit nuevo; nada se reescribe) y recuperar un
respaldo automático (sus archivos vuelven a la carpeta, sin commit)."""

from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from ... import APP_NAME
from ...core import history
from ...core.fs import normalize_path
from ..base import WorkerDialog
from ..theme import (BADGE_AMBER_BG, BADGE_AMBER_FG, BORDER, GREEN_T, MUTED,
                     PANEL, RED, TEXT)
from ..widgets import button, scrollable, separator, shorten


class HistoryDialog(WorkerDialog):
    TITLE = "Versiones"
    SIZE = (700, 620)
    MIN_SIZE = (600, 460)

    def __init__(self, app):
        super().__init__(app)
        f = self.fonts
        self.versions = []
        pad = {"padx": 22}
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(head, text="⏪  Versiones", font=f.title, text_color=TEXT
                     ).pack(anchor="w", pady=(18, 0), **pad)
        ctk.CTkLabel(head, text="Cada publicación deja una versión. Puedes "
                                "volver a cualquiera: la carpeta se pone como "
                                "estaba entonces y, si publicas, GitHub "
                                "también. Nada se borra: lo actual queda en "
                                "una rama de respaldo.", font=f.small,
                     text_color=MUTED, justify="left", wraplength=640
                     ).pack(anchor="w", pady=(2, 10), **pad)
        separator(self).grid(row=1, column=0, sticky="ew", padx=22)

        self.body = scrollable(self)
        self.body.grid(row=2, column=0, sticky="nsew", padx=16, pady=(8, 0))
        self.lbl_empty = ctk.CTkLabel(self.body, text="Cargando…",
                                      font=f.small, text_color=MUTED)
        self.lbl_empty.pack(anchor="w", padx=6, pady=10)

        foot = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0)
        foot.grid(row=3, column=0, sticky="ew")
        separator(foot).pack(fill="x", side="top")
        self.lbl_status = ctk.CTkLabel(foot, text="", font=f.small,
                                       text_color=MUTED, justify="left",
                                       wraplength=640)
        self.lbl_status.pack(anchor="w", pady=(10, 0), **pad)
        rowb = ctk.CTkFrame(foot, fg_color="transparent")
        rowb.pack(fill="x", pady=(10, 16), **pad)
        rowb.grid_columnconfigure(0, weight=1)
        self.btn_refresh = button(rowb, f, "Actualizar lista", self.refresh,
                                  width=150, height=40)
        self.btn_refresh.grid(row=0, column=1, padx=(0, 10))
        button(rowb, f, "Cerrar", self.close, kind="ghost", width=90,
               height=40).grid(row=0, column=2)

        self.folder = normalize_path(app.e_folder.get())
        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self):
        if self.running:
            return
        if not self.folder or not Path(self.folder).is_dir():
            self.status("Indica primero una carpeta de proyecto que exista.",
                        RED)
            return
        self.running = True
        self.btn_refresh.configure(state="disabled")
        self.run_in_thread(lambda: self.post(
            "versions", history.list_versions(self.folder)))

    def _render(self):
        for w in self.body.winfo_children():
            w.destroy()
        f = self.fonts
        if not self.versions:
            ctk.CTkLabel(self.body, text="Esta carpeta aún no tiene "
                         "versiones: publica una vez para crear la primera.",
                         font=f.small, text_color=MUTED, justify="left",
                         wraplength=600).pack(anchor="w", padx=6, pady=10)
            return
        commits = [v for v in self.versions if not v.is_backup]
        backups = [v for v in self.versions if v.is_backup]
        self._section("VERSIONES PUBLICADAS (la más reciente arriba)",
                      commits, current=True)
        if backups:
            self._section("RESPALDOS AUTOMÁTICOS", backups)

    def _section(self, title, items, current=False):
        f = self.fonts
        ctk.CTkLabel(self.body, text=title, font=f.small, text_color=MUTED
                     ).pack(anchor="w", padx=6, pady=(10, 4))
        box = ctk.CTkFrame(self.body, fg_color=PANEL, corner_radius=10,
                           border_width=1, border_color=BORDER)
        box.pack(fill="x", padx=4)
        for i, v in enumerate(items):
            row = ctk.CTkFrame(box, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=(8 if i == 0 else 3,
                                              8 if i == len(items) - 1 else 3))
            row.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(row, text=v.date, font=f.mono_s, text_color=MUTED,
                         width=120, anchor="w").grid(row=0, column=0,
                                                     sticky="w")
            subject = shorten(v.subject, 48)
            detail = (f"{v.files} archivo(s)  +{v.insertions} −{v.deletions}"
                      if not v.is_backup else f"rama {v.branch}")
            ctk.CTkLabel(row, text=f"{subject}\n{detail}", font=f.small,
                         text_color=TEXT, justify="left", anchor="w"
                         ).grid(row=0, column=1, sticky="ew", padx=(8, 8))
            if current and i == 0:
                ctk.CTkLabel(row, text=" actual ", font=f.small,
                             corner_radius=8, fg_color=BADGE_AMBER_BG,
                             text_color=BADGE_AMBER_FG, height=22
                             ).grid(row=0, column=2, sticky="e")
            else:
                button(row, f, "Restaurar", lambda v=v: self._restore(v),
                       kind="ghost", width=100, height=28, font=f.small
                       ).grid(row=0, column=2, sticky="e")

    def _restore(self, v):
        if self.running:
            return
        if v.is_backup:
            pregunta = (f"¿Recuperar los archivos del respaldo «{v.branch}»?"
                        "\n\nLa carpeta quedará como estaba entonces, sin "
                        "crear ninguna versión: lo recuperado queda como "
                        "cambios tuyos sin publicar (y pasará por la vista "
                        "previa y la revisión si publicas). Antes se guarda "
                        "una copia exacta de lo que tienes ahora.")
        else:
            pregunta = (f"¿Poner la carpeta como estaba en la versión "
                        f"{v.short} ({v.date})?\n\nAntes se guarda una copia "
                        "exacta de lo que tienes ahora (también lo que no "
                        "has publicado) en una rama de respaldo, y GitHub no "
                        "cambiará hasta que publiques.")
        if not messagebox.askyesno(APP_NAME, pregunta):
            return
        self.running = True
        self.status(f"Restaurando {v.short}…", MUTED)
        self.app.log("─" * 60, "dim")
        identity = (self.app.e_user.get().strip(), self.app.e_email.get().strip())
        self.run_in_thread(self._do_restore, v,
                           identity if all(identity) else None)

    def _do_restore(self, v, identity):
        log = lambda t, tag="dim": self.post("log", t, tag)    # noqa: E731
        if v.is_backup:
            r = history.restore_backup(self.folder, v.branch,
                                       identity=identity, log=log)
        else:
            r = history.restore_version(self.folder, v.sha,
                                        identity=identity, log=log)
        r.from_backup = v.is_backup
        self.post("restored", r)

    def handle(self, m):
        kind = m[0]
        if kind == "log":
            self.app.log(m[1], m[2])
        elif kind == "err":
            self.running = False
            self.btn_refresh.configure(state="normal")
            self.status("✖ " + m[1], RED)
        elif kind == "versions":
            self.running = False
            self.versions = m[1]
            self.btn_refresh.configure(state="normal")
            self._render()
        elif kind == "restored":
            r = m[1]
            self.running = False
            if r.status == "ok":
                self.status("✔ " + r.message, GREEN_T)
                self.app.set_status(
                    "●  Respaldo recuperado — revisa y publica"
                    if getattr(r, "from_backup", False)
                    else "●  Versión restaurada — publica", "warn")
            else:
                self.status("✖ " + r.message, RED)
            self.refresh()
