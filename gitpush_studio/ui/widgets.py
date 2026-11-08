# -*- coding: utf-8 -*-
"""Fábricas de widgets con el estilo de la app, para no repetir
veinte argumentos de color en cada botón."""

from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from .theme import (AMBER, BLUE, BLUE_D, BORDER, GREEN, GREEN_H, INPUT,
                    MUTED, PANEL, PANEL2, RED, RED_D, TEXT)

_BUTTON_STYLES = {
    "primary": dict(fg_color=GREEN, hover_color=GREEN_H, text_color="#ffffff",
                    border_width=0),
    "secondary": dict(fg_color=PANEL2, hover_color=BORDER, text_color=TEXT,
                      border_width=1, border_color=BORDER),
    "ghost": dict(fg_color="transparent", hover_color=PANEL2, text_color=MUTED,
                  border_width=1, border_color=BORDER),
}


def button(parent, fonts, text, command, kind="secondary", width=None,
           height=36, text_color=None, state="normal", anchor="center",
           font=None, **overrides):
    kw = dict(_BUTTON_STYLES[kind])
    if text_color:
        kw["text_color"] = text_color
    if width is not None:
        kw["width"] = width
    kw.update(overrides)
    if font is None:
        font = fonts.btn if (kind == "primary" and height >= 40) \
            else fonts.body
    return ctk.CTkButton(parent, text=text, command=command, height=height,
                         corner_radius=10 if height >= 40 else 8,
                         font=font, state=state, anchor=anchor, **kw)


def card(parent, fonts, title, pady=(0, 12)):
    c = ctk.CTkFrame(parent, fg_color=PANEL, corner_radius=12,
                     border_width=1, border_color=BORDER)
    c.pack(fill="x", padx=4, pady=pady)
    ctk.CTkLabel(c, text=title, font=fonts.section, text_color=TEXT
                 ).pack(anchor="w", padx=16, pady=(12, 2))
    return c


def panel(parent, fonts, title, **pack_kw):
    """Tarjeta con título para diálogos (padx fijo de 22)."""
    c = ctk.CTkFrame(parent, fg_color=PANEL, corner_radius=12,
                     border_width=1, border_color=BORDER)
    pack_kw.setdefault("padx", 22)
    c.pack(fill="x", **pack_kw)
    ctk.CTkLabel(c, text=title, font=fonts.section, text_color=TEXT
                 ).pack(anchor="w", padx=16, pady=(12, 2))
    return c


def entry(parent, fonts, placeholder="", show=None):
    e = ctk.CTkEntry(parent, placeholder_text=placeholder, height=36,
                     font=fonts.body, corner_radius=8, fg_color=INPUT,
                     border_color=BORDER, border_width=1, text_color=TEXT,
                     placeholder_text_color=MUTED)
    if show:
        e.configure(show=show)
    return e


def hint(parent, fonts, text, wraplength=640, pady=(2, 12)):
    """Una nota en gris bajo un grupo de opciones. Devuelve la etiqueta
    (para poder colocar otra delante con `pack(before=…)`)."""
    etiqueta = ctk.CTkLabel(parent, text=text, font=fonts.small,
                            text_color=MUTED, justify="left",
                            wraplength=wraplength)
    etiqueta.pack(anchor="w", padx=16, pady=pady)
    return etiqueta


def checkbox(parent, fonts, text, variable, danger=False, command=None):
    return ctk.CTkCheckBox(
        parent, text=text, variable=variable, font=fonts.small,
        text_color=AMBER if danger else MUTED,
        fg_color=RED_D if danger else GREEN,
        hover_color=RED if danger else GREEN_H,
        border_color=BORDER, checkbox_width=18, checkbox_height=18,
        command=command)


def option_menu(parent, fonts, values, command=None, width=95, height=28):
    return ctk.CTkOptionMenu(
        parent, values=values, width=width, height=height, font=fonts.small,
        fg_color=PANEL2, button_color=BORDER, button_hover_color=MUTED,
        text_color=TEXT, dropdown_fg_color=PANEL, dropdown_text_color=TEXT,
        dropdown_hover_color=PANEL2, command=command)


def segmented(parent, fonts, values, command=None, height=34, font=None):
    return ctk.CTkSegmentedButton(
        parent, values=values, font=font or fonts.body, fg_color=INPUT,
        selected_color=BLUE_D, selected_hover_color=BLUE,
        unselected_color=PANEL2, unselected_hover_color=BORDER,
        text_color=TEXT, corner_radius=8, height=height, command=command)


def separator(parent, **grid_or_pack):
    return ctk.CTkFrame(parent, height=1, fg_color=BORDER)


def progress_bar(parent):
    pb = ctk.CTkProgressBar(parent, height=6, corner_radius=3,
                            fg_color=PANEL2, progress_color=GREEN_H)
    pb.set(0)
    return pb


def scrollable(parent, **kw):
    kw.setdefault("fg_color", "transparent")
    return ctk.CTkScrollableFrame(parent, scrollbar_button_color=BORDER,
                                  scrollbar_button_hover_color=MUTED, **kw)


def set_text(entry_widget, value):
    entry_widget.delete(0, "end")
    if value:
        entry_widget.insert(0, value)


def browse_folder_into(entry_widget, title):
    folder = filedialog.askdirectory(title=title)
    if folder:
        set_text(entry_widget, str(Path(folder)))
    return folder


def default_downloads(config_data):
    dl = Path.home() / "Downloads"
    return config_data.get("clone_dest") or str(dl if dl.is_dir()
                                                else Path.home())


# Lo que se deja libre en vertical: la barra de tareas, la barra de
# título de la propia ventana y un poco de aire (en píxeles reales).
SCREEN_MARGIN_V = 120
SCREEN_MARGIN_H = 40


def window_scaling(win):
    """El factor con el que customtkinter agranda las ventanas: el
    «escalado» de Windows (1.25 al 125 %, 1.5 al 150 %…). 1.0 si no se
    puede saber."""
    try:
        valor = float(ctk.ScalingTracker.get_window_scaling(win))
    except Exception:
        return 1.0
    return valor if valor > 0 else 1.0


def fit_to_screen(width, height, min_width, min_height, screen_w, screen_h,
                  scaling=1.0):
    """(ancho, alto, ancho mínimo, alto mínimo) para que una ventana quepa
    en la pantalla.

    El tamaño va en las unidades de customtkinter, que luego lo multiplica
    por el escalado de Windows; la pantalla se mide en píxeles reales.
    Por eso se divide: una ventana de 680 de alto en un portátil de 768
    píxeles al 125 % mediría 850 y sus botones quedarían fuera. El mínimo
    tampoco puede ser más alto que la pantalla (si no, el sistema no la
    deja encoger)."""
    if screen_w < 200 or screen_h < 200:          # no se sabe: lo habitual
        screen_w, screen_h = 1280, 720
    escala = scaling if scaling > 0 else 1.0
    cabe_ancho = max(240, int((screen_w - SCREEN_MARGIN_H) / escala))
    cabe_alto = max(240, int((screen_h - SCREEN_MARGIN_V) / escala))
    ancho = min(width, cabe_ancho)
    alto = min(height, cabe_alto)
    return ancho, alto, min(min_width, ancho), min(min_height, alto)


def place_dialog(win, parent, w, base_h, min_w=520, min_h=420):
    """Centra una ventana sobre su padre y la ACOTA a la pantalla (también
    con el escalado de Windows), para que sus botones inferiores nunca
    queden fuera de vista. Lo que no quepa, que se desplace dentro: ver
    `BaseDialog.scrolling_layout`."""
    try:
        win.resizable(True, True)
    except Exception:
        pass
    escala = window_scaling(win)
    try:
        screen_w = int(win.winfo_screenwidth())
        screen_h = int(win.winfo_screenheight())
    except Exception:
        screen_w, screen_h = 0, 0
    w, h, min_w, min_h = fit_to_screen(w, base_h, min_w, min_h, screen_w,
                                       screen_h, escala)
    try:
        # La posición va en píxeles reales; el tamaño, sin escalar.
        ancho_real = int(w * escala)
        alto_real = int(h * escala)
        x = parent.winfo_x() + (parent.winfo_width() - ancho_real) // 2
        y = parent.winfo_y() + (parent.winfo_height() - alto_real) // 2
        if screen_w >= 200 and screen_h >= 200:
            x = min(x, screen_w - ancho_real - 10)
            y = min(y, screen_h - alto_real - SCREEN_MARGIN_V + 20)
        win.geometry(f"{w}x{h}+{max(x, 10)}+{max(y, 10)}")
    except Exception:
        win.geometry(f"{w}x{h}")
    try:
        win.minsize(min_w, min_h)
    except Exception:
        pass


def shorten(path, limit=66):
    return path if len(path) <= limit else "…" + path[-(limit - 1):]
