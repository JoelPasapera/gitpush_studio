# -*- coding: utf-8 -*-
"""Ventana principal: compone la ventana (barra lateral, formulario,
consola y cola) y reparte el trabajo. Solo widgets, estado de la sesión
y callbacks: toda la lógica vive en core/ y llega aquí por una cola.

Cada responsabilidad vive en su módulo y entra aquí como mezcla:

    app_form       el formulario (estado, licencia, modo, ignorar)
    app_profiles   perfiles guardados y token
    app_dialogs    abrir cada herramienta una sola vez
    app_publish    analizar y publicar (una, marcadas, varias carpetas)
    app_auto       auto-publicación
"""

import queue
from tkinter import messagebox

import customtkinter as ctk

from .. import APP_NAME, APP_VERSION
from ..core import config as cfg
from ..core import diaglog
from .app_auto import AutoPublishMixin
from .app_dialogs import DialogsMixin
from .app_form import FormMixin
from .app_profiles import ProfilesMixin
from .app_publish import PublishingMixin
from .console import Console
from .help import guide_parts
from .jobs import PublishController, QueuePanel
from .theme import (AMBER, BG, BLUE, BORDER, MUTED, PANEL, STATUS_COLORS,
                    TEXT, Fonts)
from .widgets import button, progress_bar, scrollable, separator

POLL_MS = 80

# Herramientas de la barra lateral: (texto del botón, método que lo abre).
# Está aquí fuera porque una prueba recorre la lista y exige que cada
# herramienta esté explicada en la ayuda de la consola: añadir una obliga
# a documentarla.
SIDEBAR_TOOLS = (
    ("🚀🚀 Publicar varias carpetas…", "open_batch"),
    ("⬇⬆ Traer cambios de GitHub…", "open_sync"),
    ("⏪  Versiones…", "open_history"),
    ("🏷  Publicar versión…", "open_release"),
    ("⬇  Clonar repositorio…", "open_clone"),
    ("⬇⬇ Clonar perfil completo…", "open_bulk"),
    ("📄  Consolidar a TXT…", "open_consolidate"),
    ("📊  Analizar proyecto…", "open_analytics"),
    ("🩺  Registro de diagnóstico", "open_diagnostics"),
)


class GitPushStudio(FormMixin, ProfilesMixin, DialogsMixin, PublishingMixin,
                    AutoPublishMixin, ctk.CTk):

    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title(f"{APP_NAME} — Publica en GitHub sin terminal")
        self.geometry("1080x820")
        self.minsize(940, 700)
        self.configure(fg_color=BG)
        self.fonts = Fonts()

        # Estado interno
        self.q = queue.Queue()
        self.scanning = False
        self.last_repo_web = ""
        self.last_pages_url = ""
        # Registro en disco de lo que pasa por la consola (rota solo y
        # nunca guarda tokens): para entender un fallo después de cerrar.
        self.diag = diaglog.DiagnosticLog(diaglog.LOG_DIR)
        self.config_data, warning = cfg.load_config()
        self.selected_profile = None
        self._profile_checks = {}
        self._dialogs = {}
        self._auto_job = None
        self._last_pub_fp = None
        self._auto_fp = None

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self._build_main()
        self.controller = PublishController(self, self.queue_panel)

        self.apply_state(self.config_data.get("last_state", {}))
        last = self.config_data.get("last_profile")
        if isinstance(last, str) and last in self.profiles:
            self.select_profile(last, load=False)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(POLL_MS, self._poll_queue)

        self.log("Bienvenido a GitPush Studio.", "ok")
        self.show_help()
        if warning:
            self.log("⚠ " + warning, "warn")

    def _build_sidebar(self):
        f = self.fonts
        sb = ctk.CTkFrame(self, width=260, corner_radius=0, fg_color=PANEL,
                          border_width=0)
        sb.grid(row=0, column=0, sticky="nsew")
        sb.grid_propagate(False)
        sb.grid_columnconfigure(0, weight=1)
        sb.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(sb, text="⬆ GitPush Studio", font=f.logo, text_color=TEXT
                     ).grid(row=0, column=0, sticky="w", padx=18, pady=(20, 0))
        ctk.CTkLabel(sb, text="Tu proyecto, en GitHub, sin comandos.",
                     font=f.small, text_color=MUTED, justify="left"
                     ).grid(row=1, column=0, sticky="w", padx=18, pady=(2, 14))
        separator(sb).grid(row=2, column=0, sticky="ew", padx=14)
        ctk.CTkLabel(sb, text="PERFILES GUARDADOS  ·  marca varios para "
                              "publicarlos juntos", font=f.small,
                     text_color=MUTED, justify="left", wraplength=220
                     ).grid(row=3, column=0, sticky="w", padx=18, pady=(14, 4))

        self.profile_list = scrollable(sb, corner_radius=0)
        self.profile_list.grid(row=4, column=0, sticky="nsew", padx=8)

        btns = ctk.CTkFrame(sb, fg_color="transparent")
        btns.grid(row=5, column=0, sticky="ew", padx=14, pady=(8, 4))
        btns.grid_columnconfigure(0, weight=1)
        self.btn_batch = button(btns, f, "🚀🚀  Publicar seleccionados",
                                self.publish_selected, kind="primary",
                                height=36, state="disabled", font=f.body)
        self.btn_batch.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        button(btns, f, "💾  Guardar perfil actual", self.save_profile,
               height=34).grid(row=1, column=0, sticky="ew", pady=(0, 6))
        button(btns, f, "🗑  Eliminar perfil", self.delete_profile,
               kind="ghost", height=30, font=f.small, hover_color="#3d1d1f"
               ).grid(row=2, column=0, sticky="ew")

        separator(sb).grid(row=6, column=0, sticky="ew", padx=14, pady=(10, 0))
        ctk.CTkLabel(sb, text="HERRAMIENTAS", font=f.small, text_color=MUTED
                     ).grid(row=7, column=0, sticky="w", padx=18, pady=(10, 4))
        tools = ctk.CTkFrame(sb, fg_color="transparent")
        tools.grid(row=8, column=0, sticky="ew", padx=14)
        tools.grid_columnconfigure(0, weight=1)
        for i, (text, method) in enumerate(SIDEBAR_TOOLS):
            button(tools, f, text, getattr(self, method), height=32,
                   anchor="w", font=f.small
                   ).grid(row=i, column=0, sticky="ew", pady=(0, 4))

        ctk.CTkLabel(sb, text="Los perfiles se guardan en\n~/.gitpush_studio",
                     font=f.small, text_color=MUTED, justify="left"
                     ).grid(row=9, column=0, sticky="w", padx=18, pady=(12, 2))
        ctk.CTkLabel(sb, text=f"v{APP_VERSION}", font=f.small,
                     text_color=BORDER).grid(row=10, column=0, sticky="w",
                                             padx=18, pady=(0, 12))
        self.refresh_profile_list()

    def _build_main(self):
        f = self.fonts
        main = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(main, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=24, pady=(18, 8))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text="Publicar proyecto", font=f.title,
                     text_color=TEXT).grid(row=0, column=0, sticky="w")
        self.auto_pill = ctk.CTkLabel(head, text="🤖 auto", font=f.small,
                                      text_color=MUTED, fg_color="transparent",
                                      corner_radius=12, width=70, height=26)
        self.auto_pill.grid(row=0, column=1, sticky="e", padx=(0, 8))
        self.status_pill = ctk.CTkLabel(head, text="●  Listo", font=f.small,
                                        text_color=MUTED, fg_color=PANEL,
                                        corner_radius=12, width=150, height=26)
        self.status_pill.grid(row=0, column=2, sticky="e")

        form = scrollable(main)
        form.grid(row=1, column=0, sticky="nsew", padx=20)
        self._build_form(form)

        act = ctk.CTkFrame(main, fg_color="transparent")
        act.grid(row=2, column=0, sticky="ew", padx=24, pady=(6, 6))
        act.grid_columnconfigure(0, weight=1)
        row = ctk.CTkFrame(act, fg_color="transparent")
        row.grid(row=0, column=0, sticky="ew")
        row.grid_columnconfigure(0, weight=1)
        self.btn_publish = button(row, f, "🚀  Publicar en GitHub",
                                  self.start_publish, kind="primary",
                                  height=46)
        self.btn_publish.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.btn_scan = button(row, f, "🛡  Analizar", self.start_scan,
                               kind="ghost", width=140, height=46,
                               text_color=AMBER)
        self.btn_scan.grid(row=0, column=1, padx=(0, 10))
        self.btn_open = button(row, f, "🌐  Abrir repositorio", self.open_repo,
                               kind="ghost", width=190, height=46,
                               text_color=BLUE)
        self.btn_open.grid(row=0, column=2)
        self.progress = progress_bar(act)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(10, 0))

        self.queue_panel = QueuePanel(main, f, on_cancel=self.cancel_publish)
        self.queue_panel.show([], row=3, column=0, sticky="ew", padx=24,
                              pady=(6, 0))
        self.queue_panel.hide()

        logf = ctk.CTkFrame(main, fg_color=PANEL, corner_radius=12,
                            border_width=1, border_color=BORDER)
        logf.grid(row=4, column=0, sticky="ew", padx=24, pady=(6, 18))
        logf.grid_columnconfigure(0, weight=1)
        bar = ctk.CTkFrame(logf, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 0))
        bar.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(bar, text="CONSOLA DE ACTIVIDAD", font=f.small,
                     text_color=MUTED).grid(row=0, column=0, sticky="w")
        button(bar, f, "❔ Ayuda", self.show_help, kind="ghost", width=80,
               height=22, font=f.small).grid(row=0, column=1, padx=(0, 6))
        self.btn_copy = button(bar, f, "📋 Copiar", self.copy_log,
                               kind="ghost", width=90, height=22,
                               font=f.small)
        self.btn_copy.grid(row=0, column=2, padx=(0, 6))
        button(bar, f, "Limpiar", self.clear_log, kind="ghost", width=70,
               height=22, font=f.small).grid(row=0, column=3)
        self.log_box = Console(logf, f)
        self.log_box.grid(row=1, column=0, sticky="ew", padx=12, pady=(6, 12))

    def log(self, text, tag="out"):
        self.log_box.write(text, tag)
        self.diag.write(text, tag)

    def clear_log(self):
        self.log_box.clear()

    def show_help(self):
        """Explica en la consola para qué sirve cada botón."""
        self.log("─" * 60, "dim")
        for parts in guide_parts():
            self.log_box.write_parts(parts)

    def copy_log(self):
        """Copia lo seleccionado o, si no hay selección, toda la consola."""
        kind = self.log_box.copy()
        self.btn_copy.configure(text="✔ Copiado" if kind else "Nada que copiar")
        self.after(1600, lambda: self.btn_copy.configure(text="📋 Copiar"))

    def set_status(self, text, kind="idle"):
        bg, fg = STATUS_COLORS.get(kind, STATUS_COLORS["idle"])
        self.status_pill.configure(text=text, fg_color=bg, text_color=fg)

    def _poll_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if self.controller.handle(msg):
                    continue
                kind = msg[0]
                if kind == "log":
                    self.log(msg[1], msg[2])
                elif kind == "scan_result":
                    self._finish_scan(msg[1], msg[2])
                elif kind == "set_fp":
                    self._last_pub_fp = msg[1]
                elif kind == "auto_result":
                    self._handle_auto_result(msg[1], msg[2])
        except queue.Empty:
            pass
        self.after(POLL_MS, self._poll_queue)

    def _on_close(self):
        if self.controller.running and not messagebox.askyesno(
                APP_NAME, "Hay una publicación en marcha. ¿Cerrar de todas "
                          "formas?"):
            return
        self.config_data["last_state"] = self.collect_state()
        self.save_config()
        self.destroy()


def main():
    app = GitPushStudio()
    app.mainloop()
