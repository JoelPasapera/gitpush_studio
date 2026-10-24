# -*- coding: utf-8 -*-
"""Perfiles guardados y token: la lista de la barra lateral, guardar,
seleccionar y borrar perfiles, y dónde vive el token de GitHub.

Mezcla de `GitPushStudio`: usa sus widgets y su estado (`self.…`);
no tiene constructor propio."""

from tkinter import messagebox

import customtkinter as ctk

from .. import APP_NAME
from ..core import config as cfg
from .theme import BORDER, GREEN, GREEN_H, MUTED, PANEL2
from .widgets import button


class ProfilesMixin:

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

    def saved_token(self):
        return cfg.saved_token(self.config_data)

    def connection_token(self, state=None):
        """Token con el que conectarse a GitHub, o cadena vacía si el
        usuario prefiere su sesión de Git."""
        use = self.v_use_token.get() if state is None \
            else state.get("use_token", True)
        return self.saved_token() if use else ""

    def store_token(self, token, remember):
        plain = cfg.store_token(self.config_data, token, remember)
        self.save_config()
        if plain:
            self.log("⚠ En este equipo no hay llavero disponible: el token "
                     "queda solo codificado en ~/.gitpush_studio.", "warn")
