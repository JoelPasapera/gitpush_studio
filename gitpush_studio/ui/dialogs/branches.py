# -*- coding: utf-8 -*-
"""Ramas sin terminal: elegir a qué rama de GitHub publicar (o desde
cuál traer cambios). La carpeta manda: la rama es solo el destino."""

from pathlib import Path

import customtkinter as ctk

from ...core.fs import normalize_path
from ...core.git import Git
from ...core.urls import is_remote_url
from ..base import WorkerDialog
from ..theme import (BADGE_GREEN_BG, BORDER, GREEN_T, MUTED, PANEL, RED,
                     TEXT)
from ..widgets import button, entry, hint, panel, scrollable, separator

_NAME_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
               "0123456789._-/")


def valid_branch_name(name):
    name = (name or "").strip()
    return bool(name) and set(name) <= _NAME_OK and not name.startswith(
        ("-", "/", ".")) and ".." not in name and not name.endswith(("/",
                                                                     "."))


class BranchDialog(WorkerDialog):
    TITLE = "Ramas"
    SIZE = (560, 560)
    MIN_SIZE = (500, 440)

    def __init__(self, app):
        super().__init__(app)
        f = self.fonts
        pad = {"padx": 22}
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(head, text="🌿  Ramas", font=f.title, text_color=TEXT
                     ).pack(anchor="w", pady=(18, 0), **pad)
        ctk.CTkLabel(head, text="Elige a qué rama publicar. Tu carpeta no "
                                "cambia al elegir: la rama es solo el "
                                "destino en GitHub.", font=f.small,
                     text_color=MUTED, justify="left", wraplength=500
                     ).pack(anchor="w", pady=(2, 10), **pad)

        c1 = panel(self, f, "✏️  Rama nueva o escrita a mano")
        rown = ctk.CTkFrame(c1, fg_color="transparent")
        rown.pack(fill="x", padx=16, pady=(6, 0))
        rown.grid_columnconfigure(0, weight=1)
        self.e_name = entry(rown, f, "por ejemplo: gh-pages, develop, v2")
        self.e_name.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        button(rown, f, "Usar", self._use_typed, kind="primary", width=90,
               height=36, font=f.body).grid(row=0, column=1)
        hint(c1, f, "Si la rama no existe en GitHub, se creará al publicar.",
             pady=(6, 12))

        self.body = scrollable(self)
        self.body.grid(row=2, column=0, sticky="nsew", padx=16, pady=(6, 0))
        self.lbl_empty = ctk.CTkLabel(self.body, text="Consultando las ramas "
                                      "de GitHub…", font=f.small,
                                      text_color=MUTED)
        self.lbl_empty.pack(anchor="w", padx=6, pady=10)

        foot = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0)
        foot.grid(row=3, column=0, sticky="ew")
        separator(foot).pack(fill="x", side="top")
        self.lbl_status = ctk.CTkLabel(foot, text="", font=f.small,
                                       text_color=MUTED, justify="left",
                                       wraplength=500)
        self.lbl_status.pack(anchor="w", pady=(10, 0), **pad)
        rowb = ctk.CTkFrame(foot, fg_color="transparent")
        rowb.pack(fill="x", pady=(10, 16), **pad)
        rowb.grid_columnconfigure(0, weight=1)
        self.btn_refresh = button(rowb, f, "Actualizar", self.refresh,
                                  width=120, height=40)
        self.btn_refresh.grid(row=0, column=1, padx=(0, 10))
        button(rowb, f, "Cerrar", self.close, kind="ghost", width=90,
               height=40).grid(row=0, column=2)

        self.folder = normalize_path(app.e_folder.get())
        self.url = app.e_url.get().strip()
        self.current_branch = app.e_branch.get().strip() or "main"
        self._remote, self._local = [], ""
        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self):
        if self.running:
            return
        if not is_remote_url(self.url):
            for w in self.body.winfo_children():
                w.destroy()
            ctk.CTkLabel(self.body, text="Escribe la URL del repositorio en "
                         "el formulario para ver sus ramas.",
                         font=self.fonts.small, text_color=MUTED,
                         justify="left", wraplength=480
                         ).pack(anchor="w", padx=6, pady=10)
            return
        self.running = True
        self.btn_refresh.configure(state="disabled")
        self.run_in_thread(self._load, self.url, self.folder,
                           self.app.connection_token())

    def _load(self, url, folder, token):
        git = Git()
        local = ""
        if folder and Path(folder).is_dir() and git.is_repo(folder):
            local = git.current_branch(folder)
        self.post("branches", git.remote_branches(url, token=token), local)

    def _use(self, name):
        self.app.set_branch(name)
        self.current_branch = name
        self.status(f"✔ Se publicará en la rama «{name}».", GREEN_T)
        self._render(self._remote, self._local)

    def _use_typed(self):
        name = self.e_name.get().strip()
        if not valid_branch_name(name):
            self.status("Nombre no válido: usa letras, números, puntos, "
                        "guiones o «/», sin espacios.", RED)
            return
        self._use(name)

    def _render(self, remote, local):
        self._remote, self._local = remote, local
        for w in self.body.winfo_children():
            w.destroy()
        f = self.fonts
        ctk.CTkLabel(self.body, text=f"EN GITHUB  ({len(remote)})",
                     font=f.small, text_color=MUTED
                     ).pack(anchor="w", padx=6, pady=(10, 4))
        if not remote:
            ctk.CTkLabel(self.body, text="El repositorio aún no tiene ramas: "
                         "publica una vez.", font=f.small, text_color=MUTED
                         ).pack(anchor="w", padx=10, pady=(0, 6))
        box = ctk.CTkFrame(self.body, fg_color=PANEL, corner_radius=10,
                           border_width=1, border_color=BORDER)
        box.pack(fill="x", padx=4)
        for i, name in enumerate(remote):
            row = ctk.CTkFrame(box, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=(8 if i == 0 else 3,
                                              8 if i == len(remote) - 1 else 3))
            row.grid_columnconfigure(0, weight=1)
            marks = []
            if name == self.current_branch:
                marks.append("destino actual")
            if name == local:
                marks.append("rama local")
            ctk.CTkLabel(row, text=name + (f"   ·   {', '.join(marks)}"
                                            if marks else ""),
                         font=f.mono_s, text_color=TEXT, anchor="w"
                         ).grid(row=0, column=0, sticky="ew")
            if name == self.current_branch:
                ctk.CTkLabel(row, text=" elegida ", font=f.small,
                             corner_radius=8, fg_color=BADGE_GREEN_BG,
                             text_color=GREEN_T, height=22
                             ).grid(row=0, column=1, sticky="e")
            else:
                button(row, f, "Usar", lambda n=name: self._use(n),
                       kind="ghost", width=80, height=28, font=f.small
                       ).grid(row=0, column=1, sticky="e")
        if local and local not in remote:
            ctk.CTkLabel(self.body, text=f"Tu carpeta está en la rama local "
                         f"«{local}», que aún no existe en GitHub.",
                         font=f.small, text_color=MUTED, justify="left",
                         wraplength=480).pack(anchor="w", padx=6, pady=(10, 4))

    def handle(self, m):
        kind = m[0]
        if kind == "err":
            self.running = False
            self.btn_refresh.configure(state="normal")
            self.status("✖ " + m[1], RED)
        elif kind == "branches":
            self.running = False
            self.btn_refresh.configure(state="normal")
            self._render(m[1], m[2])
