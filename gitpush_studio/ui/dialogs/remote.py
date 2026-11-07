# -*- coding: utf-8 -*-
"""La carpeta ya publica en OTRO repositorio: ¿en cuál publico?

Pausa la publicación hasta decidir (ver `core.remotes`). Responde:

    remotes.USE_FOLDER  publicar en el repositorio de la carpeta (y el
                        formulario pasa a decir ese);
    remotes.USE_FORM    publicar en el del formulario: la carpeta deja de
                        estar conectada al suyo;
    remotes.CANCEL      no publicar nada (también al cerrar la ventana).

La opción que viene destacada es la de la carpeta: es la que no cambia
nada que ya funcionara. La del formulario va en ámbar y explica qué
pasará con el contenido de ese repositorio.
"""

from pathlib import Path

import customtkinter as ctk

from ...core import remotes
from ..base import DecisionDialog
from ..theme import AMBER, BORDER, MUTED, PANEL, TEXT
from ..widgets import button, shorten

_URL_WIDTH = 64


class RemoteDialog(DecisionDialog):
    TITLE = "¿En qué repositorio publico?"
    SIZE = (620, 400)
    MIN_SIZE = (540, 360)
    CLOSE_RESULT = remotes.CANCEL

    def __init__(self, app, conflict, on_done, label=""):
        self.conflict = conflict
        super().__init__(app, on_done)
        f = self.fonts
        self.grid_columnconfigure(0, weight=1)

        title = "🔗  Esta carpeta ya publica en otro repositorio"
        if label:
            title += f"  ·  {label}"
        ctk.CTkLabel(self, text=title, font=f.title, text_color=TEXT,
                     anchor="w").grid(row=0, column=0, sticky="ew",
                                      padx=22, pady=(18, 4))
        carpeta = Path(conflict.folder).name or conflict.folder
        ctk.CTkLabel(self, text=f"La carpeta «{carpeta}» está conectada a un "
                                "repositorio y el formulario dice otro. "
                                "Publicar no lo cambia sin preguntarte.",
                     font=f.small, text_color=MUTED, justify="left",
                     wraplength=570, anchor="w"
                     ).grid(row=1, column=0, sticky="ew", padx=22)

        box = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=12,
                           border_width=1, border_color=BORDER)
        box.grid(row=2, column=0, sticky="ew", padx=22, pady=(12, 0))
        box.grid_columnconfigure(1, weight=1)
        self._url_row(box, 0, "La carpeta:", conflict.current_web)
        self._url_row(box, 1, "El formulario:", conflict.wanted_web)

        ctk.CTkLabel(self, text="Si publicas en el del formulario, la carpeta "
                                "pasará a estar conectada a él y su contenido "
                                "se subirá allí: en modo espejo, ese "
                                "repositorio quedará igual que esta carpeta.",
                     font=f.small, text_color=AMBER, justify="left",
                     wraplength=570, anchor="w"
                     ).grid(row=3, column=0, sticky="ew", padx=22,
                            pady=(12, 0))

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=4, column=0, sticky="ew", padx=22, pady=(14, 16))
        foot.grid_columnconfigure(0, weight=1)
        self.btn_folder = button(
            foot, f, "Publicar en el de la carpeta",
            lambda: self.finish(remotes.USE_FOLDER), kind="primary",
            height=40)
        self.btn_folder.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.btn_form = button(
            foot, f, "Publicar en el del formulario",
            lambda: self.finish(remotes.USE_FORM), kind="ghost", height=36,
            text_color=AMBER)
        self.btn_form.grid(row=1, column=0, sticky="ew", padx=(0, 8),
                           pady=(8, 0))
        button(foot, f, "No publicar", self.close, kind="ghost", width=130,
               height=36).grid(row=1, column=1, pady=(8, 0))

    def _url_row(self, parent, row, title, url):
        f = self.fonts
        ctk.CTkLabel(parent, text=title, font=f.small, text_color=MUTED,
                     anchor="w", width=110
                     ).grid(row=row, column=0, sticky="w", padx=(14, 6),
                            pady=(10 if row == 0 else 4,
                                  10 if row == 1 else 4))
        ctk.CTkLabel(parent, text=shorten(url, _URL_WIDTH), font=f.mono_s,
                     text_color=TEXT, anchor="w"
                     ).grid(row=row, column=1, sticky="ew", padx=(0, 14),
                            pady=(10 if row == 0 else 4,
                                  10 if row == 1 else 4))
