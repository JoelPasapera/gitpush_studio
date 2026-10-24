# -*- coding: utf-8 -*-
"""Crear un repositorio en la cuenta del token y dejar su URL lista."""

import os
import re
import sys
from pathlib import Path

import customtkinter as ctk

from ...core.github import create_repo
from ..base import WorkerDialog
from ..theme import MUTED, PANEL, RED, TEXT
from ..widgets import (button, checkbox, entry, hint, panel, scrollable,
                       segmented, separator)

_NAME_RX = re.compile(r"[A-Za-z0-9._-]+")


def _storage_note():
    if os.name == "nt":
        return "Se guarda cifrado con tu cuenta de Windows (DPAPI)."
    if sys.platform == "darwin":
        return "Se guarda en el Llavero de macOS."
    return ("Se guarda en el llavero del sistema (secret-tool); si no hay "
            "llavero, queda solo codificado y la app te lo avisará.")


class CreateRepoDialog(WorkerDialog):
    TITLE = "Crear repositorio en GitHub"
    SIZE = (560, 640)
    MIN_SIZE = (520, 460)

    def __init__(self, app):
        super().__init__(app)
        f = self.fonts
        pad = {"padx": 22}
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(header, text="➕  Crear repositorio en GitHub",
                     font=f.title, text_color=TEXT
                     ).pack(anchor="w", pady=(18, 0), **pad)
        ctk.CTkLabel(header, text="Se crea en tu cuenta personal usando la "
                                  "API oficial de GitHub.", font=f.small,
                     text_color=MUTED).pack(anchor="w", pady=(2, 12), **pad)

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
        saved = app.saved_token()
        self.v_remember = ctk.BooleanVar(value=bool(saved))
        checkbox(c1, f, "Recordar el token en este equipo", self.v_remember
                 ).pack(anchor="w", padx=16, pady=(8, 0))
        hint(c1, f, "Créalo en github.com → Settings → Developer settings → "
                    "Personal access tokens (classic) → Generate new token, "
                    "marcando el permiso «repo».  " + _storage_note(),
             wraplength=500, pady=(6, 12))

        c2 = panel(body, f, "📦  Datos del repositorio", pady=(12, 0))
        self.e_name = entry(c2, f, "nombre-del-repositorio")
        self.e_name.pack(fill="x", padx=16, pady=(6, 0))
        self.e_desc = entry(c2, f, "Descripción (opcional)")
        self.e_desc.pack(fill="x", padx=16, pady=(8, 0))
        self.seg_vis = segmented(c2, f, ["Público", "Privado"], height=32)
        self.seg_vis.set("Público")
        self.seg_vis.pack(anchor="w", padx=16, pady=(10, 0))
        hint(c2, f, "Consejo: GitHub Pages gratuito necesita un repositorio "
                    "público.", pady=(6, 12))

        base = app.e_folder.get().strip().strip('"')
        if base:
            sug = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(base).name).strip("-.")
            if sug:
                self.e_name.insert(0, sug)
        if saved:
            self.e_token.insert(0, saved)

        footer = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0)
        footer.grid(row=2, column=0, sticky="ew")
        separator(footer).pack(fill="x", side="top")
        self.lbl_status = ctk.CTkLabel(footer, text="", font=f.small,
                                       text_color=MUTED, justify="left",
                                       wraplength=500)
        self.lbl_status.pack(anchor="w", pady=(12, 0), **pad)
        rowb = ctk.CTkFrame(footer, fg_color="transparent")
        rowb.pack(fill="x", pady=(10, 16), **pad)
        rowb.grid_columnconfigure(0, weight=1)
        self.btn_create = button(rowb, f, "Crear repositorio", self._create,
                                 kind="primary", height=42)
        self.btn_create.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        button(rowb, f, "Cancelar", self.close, kind="ghost", width=120,
               height=42).grid(row=0, column=1)

    # ------------------------------------------------------------------
    def _create(self):
        if self.running:
            return
        token = self.e_token.get().strip()
        name = self.e_name.get().strip()
        if not token:
            self.status("Escribe tu token de GitHub.", RED)
            return
        if not _NAME_RX.fullmatch(name or ""):
            self.status("Nombre inválido: usa solo letras, números, guiones, "
                        "puntos o guion bajo.", RED)
            return
        self.running = True
        self.btn_create.configure(state="disabled", text="Creando…")
        self.status("Conectando con GitHub…", MUTED)
        self.run_in_thread(self._work, token, name,
                           self.e_desc.get().strip(),
                           self.seg_vis.get() == "Privado",
                           bool(self.v_remember.get()))

    def _work(self, token, name, desc, private, remember):
        clone, html, existed = create_repo(token, name, desc, private)
        self.post("ok", clone, html, existed, token, remember)

    def handle(self, m):
        if m[0] == "err":
            self.running = False
            self.status("✖ " + m[1], RED)
            self.btn_create.configure(state="normal", text="Crear repositorio")
            return
        if m[0] != "ok":
            return
        _, clone, html, existed, token, remember = m
        self.running = False
        self.app.store_token(token, remember)
        self.app.set_repo_url(clone, html)
        if existed:
            self.app.log("⚠ El repositorio ya existía en tu cuenta; he "
                         "colocado su URL en el formulario.", "warn")
        else:
            self.app.log(f"✔ Repositorio creado: {html}", "ok")
        self.app.log("  URL lista. Ahora pulsa «Publicar en GitHub».", "dim")
        self.app.set_status("●  Repositorio listo", "ok")
        self.destroy()
