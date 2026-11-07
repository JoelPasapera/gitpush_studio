# -*- coding: utf-8 -*-
"""Publicar como web: activa GitHub Pages y entrega el enlace."""

import webbrowser

import customtkinter as ctk

from ...core.github import enable_pages
from ..base import WorkerDialog
from ..theme import BLUE, GREEN_T, MUTED, RED, TEXT
from ..widgets import button, entry, hint, panel, segmented


class PagesDialog(WorkerDialog):
    TITLE = "Publicar como web (GitHub Pages)"
    SIZE = (560, 560)
    MIN_SIZE = (520, 460)

    def __init__(self, app, owner, repo):
        super().__init__(app)
        f = self.fonts
        self.owner, self.repo = owner, repo
        self.site_url = ""
        pad = {"padx": 22}

        ctk.CTkLabel(self, text="🌍  Publicar como web", font=f.title,
                     text_color=TEXT).pack(anchor="w", pady=(18, 0), **pad)
        ctk.CTkLabel(self, text=f"Repositorio: {owner}/{repo}   ·   Activa "
                                "GitHub Pages y te doy el enlace.",
                     font=f.small, text_color=MUTED
                     ).pack(anchor="w", pady=(2, 12), **pad)

        c1 = panel(self, f, "🔑  Token de acceso personal")
        rowt = ctk.CTkFrame(c1, fg_color="transparent")
        rowt.pack(fill="x", padx=16, pady=(6, 0))
        rowt.grid_columnconfigure(0, weight=1)
        self.e_token = entry(rowt, f, "ghp_…", show="•")
        self.e_token.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        button(rowt, f, "👁", lambda: self.toggle_token(self.e_token),
               width=42).grid(row=0, column=1)
        hint(c1, f, "El mismo token del botón «➕» (permiso «repo»). Se usa "
                    "solo para activar Pages.", pady=(6, 12))

        c2 = panel(self, f, "📄  Origen de la página", pady=(12, 0))
        rowo = ctk.CTkFrame(c2, fg_color="transparent")
        rowo.pack(fill="x", padx=16, pady=(6, 0))
        ctk.CTkLabel(rowo, text="Rama:", font=f.small, text_color=MUTED
                     ).pack(side="left")
        self.e_branch = entry(rowo, f, "main")
        self.e_branch.configure(width=120)
        self.e_branch.pack(side="left", padx=(6, 14))
        self.e_branch.insert(0, app.e_branch.get().strip() or "main")
        ctk.CTkLabel(rowo, text="Carpeta:", font=f.small, text_color=MUTED
                     ).pack(side="left")
        self.seg_path = segmented(rowo, f, ["/ (raíz)", "/docs"], height=30,
                                  font=f.small)
        self.seg_path.set("/ (raíz)")
        self.seg_path.pack(side="left", padx=(6, 0))
        hint(c2, f, f"Tu web quedará en:  https://{owner}.github.io/{repo}/\n"
                    "Requisitos: repositorio público y rama ya publicada.",
             pady=(8, 12))

        self.lbl_status = ctk.CTkLabel(self, text="", font=f.small,
                                       text_color=MUTED, justify="left",
                                       wraplength=500)
        self.lbl_status.pack(anchor="w", pady=(12, 0), **pad)

        rowb = ctk.CTkFrame(self, fg_color="transparent")
        rowb.pack(fill="x", pady=(10, 18), **pad)
        rowb.grid_columnconfigure(0, weight=1)
        self.btn_go = button(rowb, f, "Activar la web", self._activate,
                             kind="primary", height=42)
        self.btn_go.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.btn_visit = button(rowb, f, "Abrir la web", self._visit,
                                kind="ghost", width=130, height=42,
                                text_color=BLUE, state="disabled")
        self.btn_visit.grid(row=0, column=1, padx=(0, 10))
        button(rowb, f, "Cerrar", self.close, kind="ghost", width=90,
               height=42).grid(row=0, column=2)

        saved = app.saved_token()
        if saved:
            self.e_token.insert(0, saved)

    # ------------------------------------------------------------------
    def _visit(self):
        if self.site_url:
            webbrowser.open(self.site_url)

    def _activate(self):
        if self.running:
            return
        token = self.e_token.get().strip()
        if not token:
            self.status("Escribe tu token de GitHub.", RED)
            return
        branch = self.e_branch.get().strip() or "main"
        path = "/docs" if "docs" in self.seg_path.get() else "/"
        self.running = True
        self.btn_go.configure(state="disabled", text="Activando…")
        self.status("Conectando con GitHub…", MUTED)
        self.run_in_thread(self._work, token, branch, path)

    def _work(self, token, branch, path):
        url = enable_pages(self.owner, self.repo, token, branch, path,
                           on_status=lambda t: self.post("st", t))
        self.post("ok", url)

    def handle(self, m):
        if m[0] == "st":
            self.status(m[1], MUTED)
        elif m[0] == "err":
            self.running = False
            self.status("✖ " + m[1], RED)
            self.btn_go.configure(state="normal", text="Activar la web")
        elif m[0] == "ok":
            self.running = False
            self.site_url = m[1]
            self.app.last_pages_url = m[1]
            self.status("✔ ¡Tu web está en línea!\n" + m[1], GREEN_T)
            self.btn_go.configure(state="normal", text="Activar la web")
            self.btn_visit.configure(state="normal")
            self.app.log(f"✔ Web activada: {m[1]}", "ok")
            self.app.log("  Si acabas de publicar, puede tardar 1-2 min en "
                         "verse la última versión.", "dim")
