# -*- coding: utf-8 -*-
"""Ventana principal. Solo widgets, estado de la sesión y callbacks:
toda la lógica vive en core/ y llega aquí por una cola."""

import queue
import threading
import time
import webbrowser
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from .. import APP_NAME, APP_VERSION
from ..core import config as cfg
from ..core.errors import StepError
from ..core.fs import normalize_path
from ..core.git import Git
from ..core.ignore import anchor_pattern, clean_patterns
from ..core.publish import PublishJob
from ..core.scan import scan_project
from ..core.urls import parse_owner_repo, repo_web_url
from ..core.walk import folder_fingerprint
from .console import Console
from .dialogs import (BranchDialog, BulkCloneDialog, CloneDialog,
                      ConsolidateDialog, CreateRepoDialog, HistoryDialog,
                      PagesDialog, ReleaseDialog, ReviewDialog, SyncDialog)
from .help import guide_parts
from .jobs import PublishController, QueuePanel, first_line
from .theme import (AMBER, BADGE_GREEN_BG, BG, BLUE, BORDER, GREEN, GREEN_H,
                    GREEN_T, MUTED, PANEL, PANEL2, STATUS_COLORS, TEXT,
                    Fonts)
from .widgets import (browse_folder_into, button, card, checkbox, entry, hint,
                      option_menu, progress_bar, scrollable, segmented,
                      separator, set_text)

POLL_MS = 80
SETTLE_SECONDS = 5          # espera para que los cambios se asienten
DEFAULT_IGNORE = "node_modules/"
DEFAULT_MESSAGE = "Actualizar proyecto completo"


class GitPushStudio(ctk.CTk):

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
        if last and last in self.config_data.get("profiles", {}):
            self.select_profile(last, load=False)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(POLL_MS, self._poll_queue)

        self.log("Bienvenido a GitPush Studio.", "ok")
        self.show_help()
        if warning:
            self.log("⚠ " + warning, "warn")

    # ================================================================ #
    #  Construcción                                                    #
    # ================================================================ #
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
        for i, (text, cmd) in enumerate((
                ("⬇⬆ Traer cambios de GitHub…", self.open_sync),
                ("⏪  Versiones…", self.open_history),
                ("🏷  Publicar versión…", self.open_release),
                ("⬇  Clonar repositorio…", self.open_clone),
                ("⬇⬇ Clonar perfil completo…", self.open_bulk),
                ("📄  Consolidar a TXT…", self.open_consolidate))):
            button(tools, f, text, cmd, height=32, anchor="w", font=f.small
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

    def _build_form(self, form):
        f = self.fonts
        c = card(form, f, "📁  Carpeta del proyecto")
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(6, 0))
        row.grid_columnconfigure(0, weight=1)
        self.e_folder = entry(row, f, r"C:\Users\PC\Proyectos\mi-app")
        self.e_folder.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        button(row, f, "Examinar…", lambda: browse_folder_into(
            self.e_folder, "Selecciona la carpeta del proyecto"), width=110
               ).grid(row=0, column=1)
        hint(c, f, "La carpeta local que quieres subir. También puedes pegar "
                   "rutas estilo Git Bash (/c/Users/…). ¿Aún no la tienes? "
                   "Usa «⬇ Clonar repositorio…» en la barra lateral.")

        c = card(form, f, "👤  Identidad Git")
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(6, 0))
        row.grid_columnconfigure((0, 1), weight=1)
        self.e_user = entry(row, f, "Nombre de usuario")
        self.e_user.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.e_email = entry(row, f, "Correo")
        self.e_email.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.v_local = ctk.BooleanVar(value=True)
        checkbox(c, f, "Aplicar la identidad solo a este repositorio "
                       "(recomendado)", self.v_local
                 ).pack(anchor="w", padx=16, pady=(8, 0))
        hint(c, f, "Si dejas los campos vacíos se usará la identidad ya "
                   "configurada en tu equipo.")

        c = card(form, f, "🔗  Repositorio de GitHub")
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(6, 0))
        row.grid_columnconfigure(0, weight=3)
        row.grid_columnconfigure(1, weight=1)
        self.e_url = entry(row, f, "https://github.com/usuario/mi-repositorio.git")
        self.e_url.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.e_branch = entry(row, f, "main")
        self.e_branch.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.e_branch.insert(0, "main")
        row2 = ctk.CTkFrame(c, fg_color="transparent")
        row2.pack(fill="x", padx=16, pady=(8, 0))
        button(row2, f, "➕  Crear repositorio nuevo…", self.open_create_repo,
               height=32, width=220).pack(side="left")
        button(row2, f, "🌍  Publicar como web…", self.open_pages, height=32,
               width=200).pack(side="left", padx=(8, 0))
        button(row2, f, "🌿  Ramas…", self.open_branches, height=32,
               width=120).pack(side="left", padx=(8, 0))
        hint(c, f, "«➕» crea el repositorio con tu token y rellena la URL "
                   "sola. «🌍» activa GitHub Pages para que tu proyecto sea "
                   "una página web con enlace propio (requiere repositorio "
                   "público y haber publicado antes). «🌿» enseña las ramas "
                   "que hay en GitHub para elegir el destino; a la derecha "
                   "de la URL va la rama (normalmente «main»).")

        c = card(form, f, "🙈  Archivos y carpetas a ignorar (.gitignore)")
        self.t_ignore = ctk.CTkTextbox(
            c, height=84, font=f.mono, corner_radius=8, fg_color=BG,
            border_color=BORDER, border_width=1, text_color=TEXT, wrap="none",
            scrollbar_button_color=BORDER)
        self.t_ignore.pack(fill="x", padx=16, pady=(6, 0))
        self.t_ignore.insert("1.0", DEFAULT_IGNORE + "\n")
        hint(c, f, "Un patrón por línea. Se añadirán al .gitignore del "
                   "proyecto sin borrar lo que ya tenga. La revisión de "
                   "seguridad también puede añadir líneas aquí, y el "
                   "consolidador a TXT respeta esta misma lista.")

        c = card(form, f, "📝  Mensaje del commit")
        self.e_msg = entry(c, f, DEFAULT_MESSAGE)
        self.e_msg.pack(fill="x", padx=16, pady=(6, 0))
        self.e_msg.insert(0, DEFAULT_MESSAGE)
        self.v_stamp = ctk.BooleanVar(value=False)
        checkbox(c, f, "Añadir fecha y hora al mensaje", self.v_stamp
                 ).pack(anchor="w", padx=16, pady=(8, 0))
        self.v_auto_msg = ctk.BooleanVar(value=False)
        checkbox(c, f, "Describir los cambios automáticamente (p. ej. «Añade "
                       "2 imágenes y actualiza 3 archivos en src/»)",
                 self.v_auto_msg).pack(anchor="w", padx=16, pady=(8, 12))

        c = card(form, f, "🤖  Auto-publicación")
        rowa = ctk.CTkFrame(c, fg_color="transparent")
        rowa.pack(fill="x", padx=16, pady=(6, 0))
        self.v_auto = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(rowa, text="Vigilar la carpeta y publicar sola",
                      variable=self.v_auto, font=f.body, text_color=TEXT,
                      progress_color=GREEN, button_color=TEXT,
                      button_hover_color="#ffffff", fg_color=PANEL2,
                      command=self.toggle_auto).pack(side="left")
        ctk.CTkLabel(rowa, text="   Comprobar cada", font=f.small,
                     text_color=MUTED).pack(side="left")
        self.om_interval = option_menu(rowa, f, ["2 min", "5 min", "10 min",
                                                 "30 min"],
                                       command=self._interval_changed)
        self.om_interval.set("10 min")
        self.om_interval.pack(side="left", padx=(8, 0))
        hint(c, f, "Si hubo cambios y ya dejaste de guardar, publica sola "
                   "(con la revisión de seguridad incluida: si encuentra "
                   "algo, se pausa y te avisa en vez de subir). Por "
                   "seguridad empieza apagada cada vez que abres la app.")

        c = card(form, f, "🔀  Modo de publicación")
        self.seg_mode = segmented(c, f, ["Agregar / editar", "Espejo"],
                                  command=self._mode_changed)
        self.seg_mode.set("Agregar / editar")
        self.seg_mode.pack(anchor="w", padx=16, pady=(6, 0))
        self.lbl_mode = ctk.CTkLabel(c, text="", font=f.small, text_color=MUTED,
                                     justify="left", wraplength=640)
        self.lbl_mode.pack(anchor="w", padx=16, pady=(8, 12))

        c = card(form, f, "⚙️  Opciones")
        self.v_force = ctk.BooleanVar(value=False)
        self.chk_force = checkbox(c, f, "Push forzado (-f): sobrescribe el "
                                        "historial del repositorio remoto",
                                  self.v_force, danger=True)
        self.chk_force.pack(anchor="w", padx=16, pady=(6, 0))
        self.v_check = ctk.BooleanVar(value=True)
        checkbox(c, f, "Revisión de seguridad antes de publicar (archivos "
                       "grandes y secretos)", self.v_check
                 ).pack(anchor="w", padx=16, pady=(8, 0))
        self.v_preview = ctk.BooleanVar(value=True)
        checkbox(c, f, "Vista previa antes de publicar (enseña qué cambiará "
                       "en GitHub)", self.v_preview
                 ).pack(anchor="w", padx=16, pady=(8, 0))
        hint(c, f, "El push forzado reemplaza lo que haya en GitHub por tu "
                   "versión local: úsalo solo si sabes lo que haces.")
        self._mode_changed()

    # ================================================================ #
    #  Consola y estado                                                #
    # ================================================================ #
    def log(self, text, tag="out"):
        self.log_box.write(text, tag)

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

    def _is_addedit(self):
        return "Agregar" in self.seg_mode.get()

    def _mode_changed(self, _value=None):
        """El push forzado borraría lo que hay en GitHub: en modo
        «agregar/editar» no está disponible."""
        if self._is_addedit():
            self.lbl_mode.configure(
                text="Sube lo nuevo y actualiza lo que coincida por nombre y "
                     "ruta. Lo que esté en GitHub y no tengas en tu carpeta "
                     "se queda intacto: nunca borra nada.")
            if self.v_force.get():
                self.v_force.set(False)
                self.log("El push forzado se desactiva en modo "
                         "«agregar/editar»: borraría lo que hay en GitHub.",
                         "dim")
            self.chk_force.configure(state="disabled")
        else:
            self.lbl_mode.configure(
                text="GitHub queda idéntico a tu carpeta. Si borraste algo en "
                     "local, también se borra en GitHub. El historial de "
                     "commits anterior se conserva.")
            self.chk_force.configure(state="normal")

    # ================================================================ #
    #  Estado del formulario y perfiles                                #
    # ================================================================ #
    def collect_state(self):
        return {
            "folder": self.e_folder.get().strip().strip('"'),
            "user": self.e_user.get().strip(),
            "email": self.e_email.get().strip(),
            "url": self.e_url.get().strip(),
            "branch": self.e_branch.get().strip() or "main",
            "ignore": self.t_ignore.get("1.0", "end").rstrip("\n"),
            "message": self.e_msg.get().strip(),
            "local_identity": bool(self.v_local.get()),
            "stamp": bool(self.v_stamp.get()),
            "auto_message": bool(self.v_auto_msg.get()),
            "force": bool(self.v_force.get()),
            "check": bool(self.v_check.get()),
            "mode": "addedit" if self._is_addedit() else "mirror",
            "preview": bool(self.v_preview.get()),
            "auto_interval": self._interval_minutes(),
        }

    def apply_state(self, s):
        if not s:
            return
        set_text(self.e_folder, s.get("folder", ""))
        set_text(self.e_user, s.get("user", ""))
        set_text(self.e_email, s.get("email", ""))
        set_text(self.e_url, s.get("url", ""))
        set_text(self.e_branch, s.get("branch", "main") or "main")
        set_text(self.e_msg, s.get("message", DEFAULT_MESSAGE))
        self.t_ignore.delete("1.0", "end")
        self.t_ignore.insert("1.0", s.get("ignore", DEFAULT_IGNORE))
        self.v_local.set(s.get("local_identity", True))
        self.v_stamp.set(s.get("stamp", False))
        self.v_auto_msg.set(s.get("auto_message", False))
        self.v_force.set(s.get("force", False))
        self.v_check.set(s.get("check", True))
        self.v_preview.set(s.get("preview", True))
        self.seg_mode.set("Espejo" if s.get("mode") == "mirror"
                          else "Agregar / editar")
        self._mode_changed()
        self.om_interval.set(f"{int(s.get('auto_interval', 10))} min")

    def save_config(self):
        try:
            cfg.save_config(self.config_data)
        except OSError as exc:
            self.log(f"⚠ No se pudo guardar la configuración: {exc}", "warn")

    @property
    def profiles(self):
        return self.config_data.setdefault("profiles", {})

    def refresh_profile_list(self):
        for w in self.profile_list.winfo_children():
            w.destroy()
        checked = {n for n, v in self._profile_checks.items() if v.get()}
        self._profile_checks = {}
        names = sorted(self.profiles)
        if not names:
            ctk.CTkLabel(self.profile_list, text="Aún no hay perfiles.\n"
                         "Guarda uno para reutilizar\nesta configuración.",
                         font=self.fonts.small, text_color=MUTED,
                         justify="left").pack(anchor="w", padx=10, pady=8)
            self._update_batch_button()
            return
        for name in names:
            row = ctk.CTkFrame(self.profile_list, fg_color="transparent")
            row.pack(fill="x", pady=2, padx=2)
            row.grid_columnconfigure(1, weight=1)
            var = ctk.BooleanVar(value=name in checked)
            self._profile_checks[name] = var
            ctk.CTkCheckBox(row, text="", variable=var, width=22,
                            checkbox_width=18, checkbox_height=18,
                            fg_color=GREEN, hover_color=GREEN_H,
                            border_color=BORDER,
                            command=self._update_batch_button
                            ).grid(row=0, column=0, sticky="w")
            button(row, self.fonts, "  " + name,
                   lambda n=name: self.select_profile(n), height=32,
                   anchor="w", fg_color=PANEL2 if name == self.selected_profile
                   else "transparent", hover_color=PANEL2, border_width=0
                   ).grid(row=0, column=1, sticky="ew")
        self._update_batch_button()

    def _update_batch_button(self):
        n = sum(1 for v in self._profile_checks.values() if v.get())
        self.btn_batch.configure(
            text=f"🚀🚀  Publicar seleccionados ({n})" if n
            else "🚀🚀  Publicar seleccionados",
            state="normal" if n and not self.controller_running() else
            "disabled")

    def controller_running(self):
        c = getattr(self, "controller", None)
        return bool(c and c.running)

    def select_profile(self, name, load=True):
        self.selected_profile = name
        self.config_data["last_profile"] = name
        if load:
            if self.v_auto.get():
                self.v_auto.set(False)
                self.toggle_auto()
            self.apply_state(self.profiles.get(name, {}))
            self.log(f"Perfil «{name}» cargado.", "dim")
        self.refresh_profile_list()

    def save_profile(self):
        dialog = ctk.CTkInputDialog(title="Guardar perfil",
                                    text="Nombre del perfil (ej. mi-web):")
        name = (dialog.get_input() or "").strip()
        if not name:
            return
        self.profiles[name] = self.collect_state()
        self.selected_profile = name
        self.config_data["last_profile"] = name
        self.save_config()
        self.refresh_profile_list()
        self.log(f"Perfil «{name}» guardado.", "ok")

    def delete_profile(self):
        name = self.selected_profile
        if not name or name not in self.profiles:
            messagebox.showinfo(APP_NAME, "Selecciona primero un perfil de "
                                          "la lista.")
            return
        if not messagebox.askyesno(APP_NAME, f"¿Eliminar el perfil «{name}»?"):
            return
        self.profiles.pop(name, None)
        self._profile_checks.pop(name, None)
        if self.config_data.get("last_profile") == name:
            self.config_data["last_profile"] = None
        self.selected_profile = None
        self.save_config()
        self.refresh_profile_list()
        self.log(f"Perfil «{name}» eliminado.", "warn")

    # ---- token ----------------------------------------------------------
    def saved_token(self):
        return cfg.saved_token(self.config_data)

    def store_token(self, token, remember):
        plain = cfg.store_token(self.config_data, token, remember)
        self.save_config()
        if plain:
            self.log("⚠ En este equipo no hay llavero disponible: el token "
                     "queda solo codificado en ~/.gitpush_studio.", "warn")

    # ================================================================ #
    #  Diálogos                                                        #
    # ================================================================ #
    def _singleton(self, key, factory):
        dlg = self._dialogs.get(key)
        if dlg is not None and dlg.winfo_exists():
            dlg.lift()
            dlg.focus()
            return
        self._dialogs[key] = factory()

    def open_create_repo(self):
        self._singleton("repo", lambda: CreateRepoDialog(self))

    def open_pages(self):
        owner, repo = parse_owner_repo(self.e_url.get())
        if not owner:
            messagebox.showwarning(APP_NAME, "Escribe primero la URL del "
                                             "repositorio de GitHub.")
            return
        self._singleton("pages", lambda: PagesDialog(self, owner, repo))

    def open_clone(self):
        self._singleton("clone", lambda: CloneDialog(self))

    def open_bulk(self):
        self._singleton("bulk", lambda: BulkCloneDialog(self))

    def open_consolidate(self):
        self._singleton("pack", lambda: ConsolidateDialog(self))

    def open_sync(self):
        self._singleton("sync", lambda: SyncDialog(self))

    def open_history(self):
        self._singleton("history", lambda: HistoryDialog(self))

    def open_branches(self):
        self._singleton("branches", lambda: BranchDialog(self))

    def open_release(self):
        owner, repo = parse_owner_repo(self.e_url.get())
        if not owner:
            messagebox.showwarning(APP_NAME, "Escribe primero la URL del "
                                             "repositorio de GitHub.")
            return
        self._singleton("release", lambda: ReleaseDialog(self, owner, repo))

    def set_branch(self, name):
        set_text(self.e_branch, name)
        self.log(f"Rama de destino: «{name}».", "dim")

    def open_repo(self):
        url = self.last_repo_web or repo_web_url(self.e_url.get())
        if url.startswith("http"):
            webbrowser.open(url)
        else:
            messagebox.showinfo(APP_NAME, "Escribe primero la URL del "
                                          "repositorio.")

    # ---- servicios para los diálogos -------------------------------------
    def ignore_lines(self):
        return [ln.strip() for ln in
                self.t_ignore.get("1.0", "end").splitlines() if ln.strip()]

    def add_ignore_patterns(self, paths):
        current = self.ignore_lines()
        new = [p for p in paths if p not in current]
        if not new:
            return
        self.t_ignore.delete("1.0", "end")
        self.t_ignore.insert("1.0", "\n".join(current + new) + "\n")
        self.log("Añadido al cuadro de ignorar: " + ", ".join(new), "dim")

    def set_current_project(self, folder, url):
        if self.v_auto.get():
            self.v_auto.set(False)
            self.toggle_auto()
        set_text(self.e_folder, str(folder))
        if url:
            set_text(self.e_url, url)
        self.log("Proyecto cargado en el formulario.", "dim")

    def set_repo_url(self, clone_url, web_url=""):
        set_text(self.e_url, clone_url)
        self.last_repo_web = web_url or repo_web_url(clone_url)

    def on_review_ignored(self, job, patterns):
        """La revisión excluyó rutas: se reflejan donde vive ese proyecto
        (el perfil guardado y, si es el cargado, también el formulario)."""
        if job.name and job.name in self.profiles:
            prof = self.profiles[job.name]
            lines = clean_patterns(prof.get("ignore", ""))
            new = [p for p in patterns if p not in lines]
            if new:
                prof["ignore"] = "\n".join(lines + new)
                self.save_config()
                self.log(f"[{job.name}] Añadido al perfil: {', '.join(new)}",
                         "dim")
        if not job.name or job.name == self.selected_profile:
            self.add_ignore_patterns(patterns)

    # ================================================================ #
    #  Análisis manual                                                 #
    # ================================================================ #
    def start_scan(self):
        if self.controller.running or self.scanning:
            return
        folder = normalize_path(self.e_folder.get())
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning(APP_NAME, "Indica primero una carpeta de "
                                             "proyecto que exista.")
            return
        self.scanning = True
        self.btn_scan.configure(state="disabled", text="⏳  Analizando…")
        self.btn_publish.configure(state="disabled")
        self.set_status("●  Analizando…", "work")
        self.log("─" * 60, "dim")
        self.log("🛡 Analizando el proyecto en busca de archivos grandes y "
                 "secretos…", "out")
        patterns = self.ignore_lines()

        def work():
            try:
                findings, stats = scan_project(folder, patterns)
            except (StepError, OSError) as e:
                self.q.put(("log", f"✖ Falló el análisis: {e}", "err"))
                findings, stats = [], {"files": 0, "seconds": 0,
                                       "truncated": False}
            self.q.put(("scan_result", findings, stats))
        threading.Thread(target=work, daemon=True).start()

    def _finish_scan(self, findings, stats):
        self.scanning = False
        self.btn_scan.configure(state="normal", text="🛡  Analizar")
        self.btn_publish.configure(state="normal")
        self.log(f"  Revisados {stats['files']} archivos en "
                 f"{stats['seconds']:.1f} s.", "dim")
        if stats.get("truncated"):
            self.log("  ⚠ Hay más de 300 hallazgos; se muestran los "
                     "primeros.", "warn")
        if not findings:
            self.log("✔ Sin archivos grandes ni secretos a la vista.", "ok")
            self.set_status("●  Sin riesgos", "ok")
            return
        self.log(f"⚠ {len(findings)} hallazgo(s). Revisa la ventana.", "warn")
        self.set_status("●  Hallazgos — revisa", "warn")
        ReviewDialog(self, findings, mode="manual",
                     on_done=lambda res: self._manual_review_done(*res))

    def _manual_review_done(self, decision, selected):
        if decision == "ignore" and selected:
            self.add_ignore_patterns([anchor_pattern(p) for p in selected])
            self.log("✔ Se excluirán en la próxima publicación.", "ok")
        self.set_status("●  Listo", "idle")

    # ================================================================ #
    #  Publicación                                                     #
    # ================================================================ #
    def _begin_work(self, text):
        self.btn_publish.configure(state="disabled", text=text)
        self.btn_scan.configure(state="disabled")
        self.btn_batch.configure(state="disabled")
        self.progress.set(0)
        self.set_status("●  Trabajando…", "work")
        self.log("─" * 60, "dim")

    def _end_work(self):
        self.btn_publish.configure(state="normal", text="🚀  Publicar en GitHub")
        self.btn_scan.configure(state="normal")
        self._update_batch_button()

    def _confirm_force(self, count=1):
        what = ("Todo lo que haya en el repositorio remoto se reemplazará "
                "por tu versión local." if count == 1 else
                f"{count} proyectos tienen el push forzado activado: sus "
                "repositorios remotos se reemplazarán por la versión local.")
        return messagebox.askyesno(
            APP_NAME, "Has activado el push forzado (-f).\n\n" + what
            + "\n\n¿Continuar?")

    def start_publish(self, auto=False):
        if self.controller.running or self.scanning:
            return
        state = self.collect_state()
        job = PublishJob.from_state(state, auto=auto)
        error = job.validate()
        if error:
            if auto:
                self._auto_abort(error)
            else:
                messagebox.showwarning(APP_NAME, error)
            return
        if job.force and not job.addedit and not auto \
                and not self._confirm_force():
            return
        self.config_data["last_state"] = state
        self.save_config()
        self.last_repo_web = ""
        self._begin_work("⏳  Publicando…")
        self.controller.start([job], lambda results: self._publish_finished(
            results[0], auto))

    def _publish_finished(self, r, auto):
        self._end_work()
        if r.ok:
            self.progress.set(1)
            self.last_repo_web = r.web_url
            self.set_status("●  Publicado con éxito", "ok")
            if self._auto_fp:
                self._last_pub_fp = self._auto_fp
            if not self.last_pages_url:
                self.log("  ¿Es una página web? Actívala con «🌍 Publicar "
                         "como web…».", "dim")
            self.bell()
        elif r.status == "blocked":
            self.progress.set(0)
            self.set_status("●  Hallazgos — revisa con 🛡", "warn")
            self._auto_abort(r.message)
        elif r.status == "cancelled":
            self.progress.set(0)
            self.set_status("●  Listo", "idle")
        else:
            self.set_status("●  Error — revisa la consola", "err")
        self._auto_fp = None
        if auto and self.v_auto.get():
            self._schedule_auto()

    def publish_selected(self):
        if self.controller.running or self.scanning:
            return
        names = [n for n in sorted(self._profile_checks)
                 if self._profile_checks[n].get()]
        if not names:
            messagebox.showinfo(APP_NAME, "Marca en la lista los perfiles "
                                          "que quieras publicar.")
            return
        jobs, problems = [], []
        for name in names:
            job = PublishJob.from_state(self.profiles.get(name, {}), name=name)
            error = job.validate()
            if error:
                problems.append(f"• {name}: {error}")
            else:
                jobs.append(job)
        if problems:
            messagebox.showwarning(APP_NAME, "Revisa estos perfiles antes de "
                                             "publicar:\n\n" + "\n".join(problems))
            return
        forced = sum(1 for j in jobs if j.force and not j.addedit)
        if forced and not self._confirm_force(forced):
            return
        if self.v_auto.get():
            self.v_auto.set(False)
            self.toggle_auto()
        self._begin_work(f"⏳  Publicando {len(jobs)}…")
        self.log(f"🚀🚀 Publicando {len(jobs)} proyectos: "
                 + ", ".join(j.label for j in jobs), "out")
        self.controller.start(jobs, self._batch_finished)

    def _batch_finished(self, results):
        self._end_work()
        ok = sum(1 for r in results if r.ok)
        bad = [r for r in results if not r.ok]
        self.progress.set(1 if not bad else 0)
        self.log("─" * 60, "dim")
        for r in results:
            mark = {"ok": "✔", "cancelled": "✋", "blocked": "🛡"}.get(r.status,
                                                                       "✖")
            detail = r.web_url if r.ok else first_line(r.message)
            self.log(f"{mark} {r.job.label}: {detail} ({r.seconds:.1f} s)",
                     "ok" if r.ok else "warn")
        if not bad:
            self.set_status(f"●  {ok} publicados con éxito", "ok")
            self.bell()
        else:
            self.set_status(f"●  {ok} de {len(results)} publicados", "warn")

    def cancel_publish(self):
        self.controller.cancel()

    # ================================================================ #
    #  Auto-publicación                                                #
    # ================================================================ #
    def _interval_minutes(self):
        try:
            return int(self.om_interval.get().split()[0])
        except (ValueError, IndexError):
            return 10

    def _interval_changed(self, _value=None):
        if self.v_auto.get():
            self._schedule_auto()
            self.log(f"🤖 Intervalo cambiado: cada "
                     f"{self._interval_minutes()} min.", "dim")

    def toggle_auto(self):
        if self.v_auto.get():
            folder = normalize_path(self.e_folder.get())
            if not folder or not Path(folder).is_dir():
                self.v_auto.set(False)
                messagebox.showwarning(APP_NAME, "Indica primero una carpeta "
                                                 "que exista.")
                return
            self.auto_pill.configure(fg_color=BADGE_GREEN_BG,
                                     text_color=GREEN_T)
            self.log(f"🤖 Auto-publicación activada: comprobaré la carpeta "
                     f"cada {self._interval_minutes()} min.", "ok")
            self._last_pub_fp = None
            patterns = self.ignore_lines()
            if not Git().is_repo(folder):
                # Sin repositorio aún: huella de partida, para publicar
                # solo cuando algo cambie a partir de ahora.
                threading.Thread(target=lambda: self.q.put(
                    ("set_fp", folder_fingerprint(folder, patterns))),
                    daemon=True).start()
            self._schedule_auto()
        else:
            self._cancel_auto()
            self.auto_pill.configure(fg_color="transparent", text_color=MUTED)
            self.log("🤖 Auto-publicación desactivada.", "dim")

    def _schedule_auto(self):
        self._cancel_auto()
        self._auto_job = self.after(self._interval_minutes() * 60 * 1000,
                                    self._auto_tick)

    def _cancel_auto(self):
        if self._auto_job is not None:
            try:
                self.after_cancel(self._auto_job)
            except Exception:
                pass
            self._auto_job = None

    def _auto_tick(self):
        self._auto_job = None
        if not self.v_auto.get():
            return
        if self.controller.running or self.scanning:
            self._schedule_auto()
            return
        threading.Thread(
            target=self._auto_check,
            args=(normalize_path(self.e_folder.get()), self.ignore_lines(),
                  self._is_addedit(), self._last_pub_fp),
            daemon=True).start()

    def _auto_check(self, folder, patterns, addedit, last_fp):
        """Hilo: ¿hay cambios, y ya se asentaron? Con repositorio usa
        `git status`; antes de la primera publicación, la huella."""
        if not Path(folder).is_dir():
            self.q.put(("auto_result", "missing", None))
            return
        try:
            git = Git()
            if git.is_repo(folder):
                def probe():
                    return git.status_changes(folder, ignore_deletions=addedit)
                fp_mode = False
            else:
                def probe():
                    return folder_fingerprint(folder, patterns)
                fp_mode = True
            first = probe()
            idle = (last_fp is not None and first == last_fp) if fp_mode \
                else not first
            if idle:
                self.q.put(("auto_result", "idle", None))
                return
            time.sleep(SETTLE_SECONDS)
            if probe() != first:
                self.q.put(("auto_result", "unstable", None))
            else:
                self.q.put(("auto_result", "publish",
                            first if fp_mode else None))
        except StepError as e:
            self.q.put(("auto_result", "error", str(e)))

    def _handle_auto_result(self, action, payload):
        if not self.v_auto.get():
            return
        if action == "missing":
            self.log("🤖 La carpeta ya no existe; auto-publicación "
                     "desactivada.", "warn")
            self.v_auto.set(False)
            self.toggle_auto()
            return
        if action == "publish" and not self.controller.running \
                and not self.scanning:
            self.log("🤖 Cambios detectados — publicando automáticamente…",
                     "out")
            self._auto_fp = payload
            self.start_publish(auto=True)
            return                      # se reprograma al terminar
        if action == "unstable":
            self.log("🤖 Aún estás guardando cambios; lo intento en la "
                     "próxima ronda.", "dim")
        elif action == "error":
            self.log(f"🤖 No pude comprobar la carpeta: {payload}", "warn")
        self._schedule_auto()

    def _auto_abort(self, reason):
        self.log(f"🤖 {reason} Auto-publicación en pausa.", "warn")
        if self.v_auto.get():
            self.v_auto.set(False)
            self._cancel_auto()
            self.auto_pill.configure(fg_color="transparent", text_color=MUTED)

    # ================================================================ #
    #  Cola de mensajes                                                #
    # ================================================================ #
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
