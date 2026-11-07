# -*- coding: utf-8 -*-
"""Clonar un repositorio a la carpeta elegida, con progreso en vivo."""

from pathlib import Path

import customtkinter as ctk

from ...core.desktop import open_path
from ...core.fs import normalize_path
from ...core.git import Git, clone_repo
from ...core.urls import is_remote_url, repo_name_from_url
from ..base import WorkerDialog
from ..theme import BLUE, GREEN_T, MUTED, RED, TEXT
from ..widgets import (browse_folder_into, button, checkbox, default_downloads,
                       entry, hint, panel)


class CloneDialog(WorkerDialog):
    TITLE = "Clonar repositorio de GitHub"
    SIZE = (580, 500)

    def __init__(self, app):
        super().__init__(app)
        f = self.fonts
        self.cloned_path = ""
        pad = {"padx": 22}

        ctk.CTkLabel(self, text="⬇  Clonar repositorio", font=f.title,
                     text_color=TEXT).pack(anchor="w", pady=(18, 0), **pad)
        ctk.CTkLabel(self, text="Descarga cualquier repositorio de GitHub a "
                                "la carpeta que elijas.", font=f.small,
                     text_color=MUTED).pack(anchor="w", pady=(2, 12), **pad)

        c1 = panel(self, f, "🔗  Enlace del repositorio")
        self.e_url = entry(c1, f, "https://github.com/usuario/repositorio.git")
        self.e_url.pack(fill="x", padx=16, pady=(6, 0))
        prev = app.e_url.get().strip()
        if prev:
            self.e_url.insert(0, prev)
        self.e_token = entry(c1, f, "Token (solo si el repositorio es privado)",
                             show="•")
        self.e_token.pack(fill="x", padx=16, pady=(8, 12))
        saved = app.saved_token()
        if saved:
            self.e_token.insert(0, saved)

        c2 = panel(self, f, "📁  Guardar en", pady=(12, 0))
        rowd = ctk.CTkFrame(c2, fg_color="transparent")
        rowd.pack(fill="x", padx=16, pady=(6, 0))
        rowd.grid_columnconfigure(0, weight=1)
        self.e_dest = entry(rowd, f, str(Path.home() / "Downloads"))
        self.e_dest.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        button(rowd, f, "Examinar…", lambda: browse_folder_into(
            self.e_dest, "¿Dónde guardar el proyecto clonado?"), width=110
               ).grid(row=0, column=1)
        self.e_dest.insert(0, default_downloads(app.config_data))
        self.v_use = ctk.BooleanVar(value=True)
        checkbox(c2, f, "Usarlo como proyecto actual al terminar", self.v_use
                 ).pack(anchor="w", padx=16, pady=(8, 0))
        hint(c2, f, "El proyecto se creará como una subcarpeta con el nombre "
                    "del repositorio. Para repositorios privados, escribe el "
                    "token o inicia sesión una vez con Git Credential "
                    "Manager.", wraplength=500, pady=(6, 12))

        self.lbl_status = ctk.CTkLabel(self, text="", font=f.mono_s,
                                       text_color=MUTED, justify="left",
                                       wraplength=520)
        self.lbl_status.pack(anchor="w", pady=(12, 0), **pad)

        rowb = ctk.CTkFrame(self, fg_color="transparent")
        rowb.pack(fill="x", pady=(10, 18), **pad)
        rowb.grid_columnconfigure(0, weight=1)
        self.btn_clone = button(rowb, f, "Clonar", self._clone, kind="primary",
                                height=42)
        self.btn_clone.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.btn_folder = button(rowb, f, "Abrir carpeta",
                                 lambda: open_path(self.cloned_path),
                                 kind="ghost", width=130, height=42,
                                 text_color=BLUE, state="disabled")
        self.btn_folder.grid(row=0, column=1, padx=(0, 10))
        button(rowb, f, "Cerrar", self.close, kind="ghost", width=90,
               height=42).grid(row=0, column=2)

    # ------------------------------------------------------------------
    def _clone(self):
        if self.running:
            return
        url = self.e_url.get().strip()
        dest = normalize_path(self.e_dest.get())
        if not is_remote_url(url):
            self.status("La URL debe empezar por https:// o git@", RED)
            return
        name = repo_name_from_url(url)
        if not name:
            self.status("No pude deducir el nombre del repositorio desde la "
                        "URL.", RED)
            return
        try:
            Path(dest).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.status(f"No puedo usar esa carpeta destino: {e}", RED)
            return
        target = Path(dest) / name
        if target.exists() and any(target.iterdir()):
            self.status(f"Ya existe y no está vacía:\n{target}", RED)
            return
        self.running = True
        self.btn_clone.configure(state="disabled", text="Clonando…")
        self.status("Conectando con GitHub…", MUTED)
        self.app.log("─" * 60, "dim")
        self.app.log(f"$ git clone {url}", "cmd")
        self.run_in_thread(self._work, url, dest, target,
                           self.e_token.get().strip(), bool(self.v_use.get()))

    def _work(self, url, dest, target, token, use_as_current):
        st, secs, err = clone_repo(Git(), url, target, token=token,
                                   on_line=lambda ln: self.post("st", ln))
        if st == "ok":
            self.post("ok", str(target), url, use_as_current, dest)
        else:
            self.post("err", err)

    def handle(self, m):
        kind = m[0]
        if kind == "st":
            self.status(m[1], MUTED)
        elif kind == "err":
            self.running = False
            self.status("✖ " + m[1], RED)
            self.btn_clone.configure(state="normal", text="Clonar")
            self.app.log("✖ " + m[1], "err")
        elif kind == "ok":
            _, target, url, use_as_current, dest = m
            self.running = False
            self.cloned_path = target
            self.app.config_data["clone_dest"] = dest
            self.app.save_config()
            self.status(f"✔ Clonado en:\n{target}", GREEN_T)
            self.btn_clone.configure(state="normal", text="Clonar")
            self.btn_folder.configure(state="normal")
            self.app.log(f"✔ Repositorio clonado en {target}", "ok")
            if use_as_current:
                self.app.set_current_project(target, url)
