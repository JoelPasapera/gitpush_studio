# -*- coding: utf-8 -*-
"""Publicar una versión: etiqueta git + GitHub Release con zip opcional."""

import webbrowser
from pathlib import Path

import customtkinter as ctk

from ...core import release
from ...core.fs import normalize_path
from ..base import WorkerDialog
from ..theme import BG, BLUE, BORDER, GREEN_T, MUTED, RED, TEXT
from ..widgets import button, checkbox, entry, hint, panel, scrollable


class ReleaseDialog(WorkerDialog):
    TITLE = "Publicar versión (GitHub Release)"
    SIZE = (580, 660)
    MIN_SIZE = (520, 500)

    def __init__(self, app, owner, repo):
        super().__init__(app)
        f = self.fonts
        self.owner, self.repo = owner, repo
        self.html_url = ""
        pad = {"padx": 22}
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(head, text="🏷  Publicar versión", font=f.title,
                     text_color=TEXT).pack(anchor="w", pady=(18, 0), **pad)
        ctk.CTkLabel(head, text=f"Repositorio: {owner}/{repo}   ·   Crea "
                                "una etiqueta y una Release descargable.",
                     font=f.small, text_color=MUTED
                     ).pack(anchor="w", pady=(2, 10), **pad)

        body = scrollable(self)
        body.grid(row=1, column=0, sticky="nsew")

        c1 = panel(body, f, "🔑  Token de acceso personal")
        rowt = ctk.CTkFrame(c1, fg_color="transparent")
        rowt.pack(fill="x", padx=16, pady=(6, 0))
        rowt.grid_columnconfigure(0, weight=1)
        self.e_token = entry(rowt, f, "ghp_…", show="•")
        self.e_token.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        button(rowt, f, "👁", lambda: self.toggle_token(self.e_token),
               width=42).grid(row=0, column=1)
        hint(c1, f, "El mismo token del botón «➕» (permiso «repo»).",
             pady=(6, 12))

        c2 = panel(body, f, "🏷  Datos de la versión", pady=(12, 0))
        rowv = ctk.CTkFrame(c2, fg_color="transparent")
        rowv.pack(fill="x", padx=16, pady=(6, 0))
        rowv.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(rowv, text="Número:", font=f.small, text_color=MUTED
                     ).grid(row=0, column=0, sticky="w")
        self.e_tag = entry(rowv, f, "v1.0.0")
        self.e_tag.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self.e_title = entry(c2, f, "Título (opcional; por defecto, el número)")
        self.e_title.pack(fill="x", padx=16, pady=(8, 0))
        ctk.CTkLabel(c2, text="Notas (qué trae esta versión):", font=f.small,
                     text_color=MUTED).pack(anchor="w", padx=16, pady=(8, 0))
        self.t_notes = ctk.CTkTextbox(c2, height=110, font=f.body,
                                      corner_radius=8, fg_color=BG,
                                      border_color=BORDER, border_width=1,
                                      text_color=TEXT, wrap="word")
        self.t_notes.pack(fill="x", padx=16, pady=(4, 0))
        self.v_zip = ctk.BooleanVar(value=True)
        checkbox(c2, f, "Adjuntar un zip del proyecto (lo publicado, sin "
                        ".git)", self.v_zip).pack(anchor="w", padx=16,
                                                  pady=(8, 0))
        hint(c2, f, "Requisitos: haber publicado antes y no tener cambios "
                    "pendientes, para que la versión coincida con GitHub. "
                    "Las versiones aparecen en github.com → Releases.",
             pady=(6, 12))

        self.lbl_status = ctk.CTkLabel(self, text="", font=f.small,
                                       text_color=MUTED, justify="left",
                                       wraplength=520)
        self.lbl_status.grid(row=2, column=0, sticky="w", padx=22,
                             pady=(10, 0))
        rowb = ctk.CTkFrame(self, fg_color="transparent")
        rowb.grid(row=3, column=0, sticky="ew", padx=22, pady=(10, 18))
        rowb.grid_columnconfigure(0, weight=1)
        self.btn_go = button(rowb, f, "Crear versión", self._create,
                             kind="primary", height=42)
        self.btn_go.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.btn_open = button(rowb, f, "Abrir en GitHub", self._open,
                               kind="ghost", width=140, height=42,
                               text_color=BLUE, state="disabled")
        self.btn_open.grid(row=0, column=1, padx=(0, 10))
        button(rowb, f, "Cerrar", self.close, kind="ghost", width=90,
               height=42).grid(row=0, column=2)

        self.folder = normalize_path(app.e_folder.get())
        self.url = app.e_url.get().strip()
        self.branch = app.e_branch.get().strip() or "main"
        saved = app.saved_token()
        if saved:
            self.e_token.insert(0, saved)
        if self.folder and Path(self.folder).is_dir():
            self.run_in_thread(lambda: self.post(
                "suggest", release.suggest_tag(self.folder)))

    # ------------------------------------------------------------------
    def _open(self):
        if self.html_url:
            webbrowser.open(self.html_url)

    def _create(self):
        if self.running:
            return
        token = self.e_token.get().strip()
        tag = self.e_tag.get().strip()
        if not token:
            self.status("Escribe tu token de GitHub.", RED)
            return
        if not release.valid_tag(tag):
            self.status("Número de versión no válido (ej. v1.2.0).", RED)
            return
        self.running = True
        self.btn_go.configure(state="disabled", text="Creando…")
        self.status("Preparando…", MUTED)
        self.app.log("─" * 60, "dim")
        self.run_in_thread(self._work, token, tag, self.e_title.get().strip(),
                           self.t_notes.get("1.0", "end").strip(),
                           bool(self.v_zip.get()))

    def _work(self, token, tag, title, notes, attach):
        r = release.create_release(
            self.folder, self.url, self.owner, self.repo, token, tag, title,
            notes, attach_zip=attach, branch=self.branch,
            log=lambda t, tg="dim": self.post("log", t, tg),
            on_status=lambda t: self.post("st", t))
        self.post("done", r)

    def handle(self, m):
        kind = m[0]
        if kind == "suggest":
            if not self.e_tag.get().strip():
                self.e_tag.insert(0, m[1])
        elif kind == "st":
            self.status(m[1], MUTED)
        elif kind == "log":
            self.app.log(m[1], m[2])
        elif kind == "err":
            self.running = False
            self.status("✖ " + m[1], RED)
            self.btn_go.configure(state="normal", text="Crear versión")
        elif kind == "done":
            r = m[1]
            self.running = False
            self.btn_go.configure(state="normal", text="Crear versión")
            if r.status == "ok":
                self.html_url = r.html_url
                self.status("✔ " + r.message
                            + (f"\nAdjunto: {r.asset}" if r.asset else ""),
                            GREEN_T)
                self.btn_open.configure(state="normal")
                self.app.set_status(f"●  Versión {r.tag} publicada", "ok")
            else:
                self.status("✖ " + r.message, RED)
