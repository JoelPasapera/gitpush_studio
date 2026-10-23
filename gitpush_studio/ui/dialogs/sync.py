# -*- coding: utf-8 -*-
"""Traer cambios de GitHub a la carpeta, en dos pasos: primero se enseña
qué llegaría y qué hay sin publicar; después se aplica, dejando la
versión anterior en una rama de respaldo."""

from pathlib import Path

import customtkinter as ctk

from ...core import sync
from ...core.fs import normalize_path
from ...core.urls import is_remote_url
from ..base import WorkerDialog
from ..theme import (AMBER, BADGE_BLUE_BG, BADGE_GREEN_BG, BADGE_RED_BG,
                     BADGE_RED_FG, BLUE, BORDER, GREEN_T, MUTED, PANEL, RED,
                     TEXT)
from ..widgets import (button, entry, hint, panel, scrollable, segmented,
                       separator, shorten)


class SyncDialog(WorkerDialog):
    TITLE = "Traer cambios de GitHub"
    SIZE = (660, 680)
    MIN_SIZE = (580, 520)

    def __init__(self, app):
        super().__init__(app)
        f = self.fonts
        self.report = None
        pad = {"padx": 22}
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(head, text="⬇⬆  Traer cambios de GitHub", font=f.title,
                     text_color=TEXT).pack(anchor="w", pady=(18, 0), **pad)
        ctk.CTkLabel(head, text="Lo contrario de publicar: lo que hay en "
                                "GitHub baja a tu carpeta. Tu versión "
                                "actual queda guardada por si acaso.",
                     font=f.small, text_color=MUTED, justify="left",
                     wraplength=600).pack(anchor="w", pady=(2, 10), **pad)

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=1, column=0, sticky="ew")
        c1 = panel(top, f, "🔗  Origen")
        self.lbl_folder = ctk.CTkLabel(c1, text="", font=f.mono_s,
                                       text_color=TEXT, anchor="w")
        self.lbl_folder.pack(anchor="w", padx=16, pady=(4, 0))
        self.lbl_url = ctk.CTkLabel(c1, text="", font=f.mono_s,
                                    text_color=MUTED, anchor="w")
        self.lbl_url.pack(anchor="w", padx=16)
        rowb = ctk.CTkFrame(c1, fg_color="transparent")
        rowb.pack(fill="x", padx=16, pady=(8, 0))
        ctk.CTkLabel(rowb, text="Rama:", font=f.small, text_color=MUTED
                     ).pack(side="left")
        self.e_branch = entry(rowb, f, "main")
        self.e_branch.configure(width=140)
        self.e_branch.pack(side="left", padx=(6, 14))
        ctk.CTkLabel(rowb, text="Modo:", font=f.small, text_color=MUTED
                     ).pack(side="left")
        self.seg_mode = segmented(rowb, f, ["Combinar", "Espejo de GitHub"],
                                  height=30, font=f.small)
        self.seg_mode.set("Combinar")
        self.seg_mode.pack(side="left", padx=(6, 0))
        hint(c1, f, "Combinar: lo que solo tienes tú se conserva y, donde "
                    "ambos cambiaron lo mismo, gana GitHub. Espejo: tu "
                    "carpeta queda exactamente como GitHub. En los dos "
                    "casos tu versión actual se guarda antes en una rama "
                    "«respaldo-fecha», recuperable desde «⏪ Versiones».",
             wraplength=580, pady=(6, 12))

        self.body = scrollable(self)
        self.body.grid(row=2, column=0, sticky="nsew", padx=16, pady=(6, 0))
        self.lbl_empty = ctk.CTkLabel(self.body, text="Pulsa «Comprobar» "
                                      "para ver qué cambiaría.", font=f.small,
                                      text_color=MUTED)
        self.lbl_empty.pack(anchor="w", padx=6, pady=10)

        foot = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0)
        foot.grid(row=3, column=0, sticky="ew")
        separator(foot).pack(fill="x", side="top")
        self.lbl_status = ctk.CTkLabel(foot, text="", font=f.small,
                                       text_color=MUTED, justify="left",
                                       wraplength=600)
        self.lbl_status.pack(anchor="w", pady=(10, 0), **pad)
        rowa = ctk.CTkFrame(foot, fg_color="transparent")
        rowa.pack(fill="x", pady=(10, 16), **pad)
        rowa.grid_columnconfigure(1, weight=1)
        self.btn_check = button(rowa, f, "Comprobar", self._check,
                                width=130, height=42)
        self.btn_check.grid(row=0, column=0, padx=(0, 10))
        self.btn_apply = button(rowa, f, "⬇⬆  Traer cambios", self._apply,
                                kind="primary", height=42, state="disabled")
        self.btn_apply.grid(row=0, column=1, sticky="ew", padx=(0, 10))
        button(rowa, f, "Cerrar", self.close, kind="ghost", width=90,
               height=42).grid(row=0, column=2)

        self.folder = normalize_path(app.e_folder.get())
        self.url = app.e_url.get().strip()
        self.lbl_folder.configure(text=shorten(self.folder or "(sin carpeta)",
                                               70))
        self.lbl_url.configure(text=shorten(self.url or "(sin URL)", 70))
        self.e_branch.insert(0, app.e_branch.get().strip() or "main")

    # ------------------------------------------------------------------
    def _valid(self):
        if not self.folder or not Path(self.folder).is_dir():
            self.status("Indica primero una carpeta de proyecto que exista.",
                        RED)
            return False
        if not is_remote_url(self.url):
            self.status("Escribe la URL del repositorio en el formulario.",
                        RED)
            return False
        return True

    def _mode(self):
        return "mirror" if "Espejo" in self.seg_mode.get() else "merge"

    def _check(self):
        if self.running or not self._valid():
            return
        self.running = True
        self.btn_check.configure(state="disabled", text="Comprobando…")
        self.btn_apply.configure(state="disabled")
        self.status("Consultando GitHub…", MUTED)
        self.run_in_thread(self._inspect, self.folder, self.url,
                           self.e_branch.get().strip() or "main",
                           self.app.connection_token())

    def _inspect(self, folder, url, branch, token):
        self.post("report", sync.inspect(folder, url, branch, token=token))

    def _apply(self):
        if self.running or not self._valid():
            return
        self.running = True
        self.btn_apply.configure(state="disabled", text="Trayendo…")
        self.btn_check.configure(state="disabled")
        self.status("Trayendo los cambios…", MUTED)
        self.app.log("─" * 60, "dim")
        identity = (self.app.e_user.get().strip(), self.app.e_email.get().strip())
        self.run_in_thread(self._do_apply, self.folder, self.url,
                           self.e_branch.get().strip() or "main", self._mode(),
                           identity if all(identity) else None,
                           self.app.connection_token())

    def _do_apply(self, folder, url, branch, mode, identity, token):
        r = sync.apply(folder, url, branch, mode=mode, identity=identity,
                       token=token,
                       log=lambda t, tag="dim": self.post("log", t, tag))
        self.post("done", r)

    # ------------------------------------------------------------------
    def _render(self, rep):
        for w in self.body.winfo_children():
            w.destroy()
        f = self.fonts
        if not rep.remote_exists:
            ctk.CTkLabel(self.body, text="Esa rama aún no existe en GitHub. "
                         "Publica primero, o elige otra rama.", font=f.small,
                         text_color=AMBER).pack(anchor="w", padx=6, pady=10)
            return False
        if rep.up_to_date:
            ctk.CTkLabel(self.body, text="✔ Tu carpeta ya está al día con "
                         "GitHub.", font=f.small, text_color=GREEN_T
                         ).pack(anchor="w", padx=6, pady=10)
            return False
        if rep.unborn:
            ctk.CTkLabel(self.body, text="Esta carpeta aún no tiene "
                         "versiones: se traerá el contenido completo de "
                         "GitHub.", font=f.small, text_color=MUTED,
                         justify="left", wraplength=580
                         ).pack(anchor="w", padx=6, pady=(10, 4))
        groups = (("LLEGARÁN NUEVOS", rep.added, BADGE_GREEN_BG, GREEN_T),
                  ("CAMBIARÁN", rep.modified, BADGE_BLUE_BG, BLUE),
                  ("DESAPARECERÁN DE TU CARPETA", rep.deleted, BADGE_RED_BG,
                   BADGE_RED_FG))
        for title, items, bg, fg in groups:
            if items:
                self._group(title, items, bg, fg)
        if rep.local_changes:
            self._group("CAMBIOS TUYOS SIN PUBLICAR (se guardan en la rama "
                        "de respaldo)", rep.local_changes, "#3a2d10", AMBER)
        elif rep.local_ahead:
            ctk.CTkLabel(self.body, text="Tienes versiones locales que no "
                         "están en GitHub: se guardarán en una rama de "
                         "respaldo.", font=f.small, text_color=AMBER,
                         justify="left", wraplength=580
                         ).pack(anchor="w", padx=6, pady=(10, 4))
        return True

    def _group(self, title, items, bg, fg, limit=200):   # bg: reservado
        f = self.fonts
        ctk.CTkLabel(self.body, text=f"{title}  ({len(items)})", font=f.small,
                     text_color=MUTED).pack(anchor="w", padx=6, pady=(10, 4))
        box = ctk.CTkFrame(self.body, fg_color=PANEL, corner_radius=10,
                           border_width=1, border_color=BORDER)
        box.pack(fill="x", padx=4)
        shown = items[:limit]
        for i, path in enumerate(shown):
            ctk.CTkLabel(box, text="  " + shorten(path), font=f.mono_s,
                         text_color=fg, anchor="w").pack(fill="x", padx=10,
                                          pady=(8 if i == 0 else 2,
                                                8 if i == len(shown) - 1
                                                else 2))
        if len(items) > limit:
            ctk.CTkLabel(box, text=f"… y {len(items) - limit} más",
                         font=f.small, text_color=MUTED
                         ).pack(anchor="w", padx=14, pady=(0, 8))

    def handle(self, m):
        kind = m[0]
        if kind == "log":
            self.app.log(m[1], m[2])
        elif kind == "err":
            self.running = False
            self.status("✖ " + m[1], RED)
            self.btn_check.configure(state="normal", text="Comprobar")
            self.btn_apply.configure(text="⬇⬆  Traer cambios")
            self.app.log("✖ " + m[1], "err")
        elif kind == "report":
            self.running = False
            self.report = m[1]
            actionable = self._render(self.report)
            self.btn_check.configure(state="normal", text="Comprobar")
            self.btn_apply.configure(state="normal" if actionable
                                     else "disabled")
            if actionable:
                self.status(f"{self.report.incoming} archivo(s) cambiarían "
                            "en tu carpeta.", MUTED)
            else:
                self.status("", MUTED)
        elif kind == "done":
            r = m[1]
            self.running = False
            self.btn_check.configure(state="normal", text="Comprobar")
            self.btn_apply.configure(text="⬇⬆  Traer cambios",
                                     state="disabled")
            if r.status == "ok":
                self.status("✔ " + r.message, GREEN_T)
                for w in self.body.winfo_children():
                    w.destroy()
                ctk.CTkLabel(self.body, text="✔ Listo. Vuelve a pulsar "
                             "«Comprobar» cuando quieras.", font=self.fonts.small,
                             text_color=GREEN_T).pack(anchor="w", padx=6,
                                                      pady=10)
                self.app.set_status("●  Carpeta actualizada", "ok")
            else:
                self.status("✖ " + r.message, RED)
