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
    return ctk.CTkLabel(parent, text=text, font=fonts.small, text_color=MUTED,
                        justify="left", wraplength=wraplength
                        ).pack(anchor="w", padx=16, pady=pady)


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


def place_dialog(win, parent, w, base_h, min_w=520, min_h=420):
    """Centra una ventana sobre su padre y ACOTA su alto a la pantalla,
    para que sus botones inferiores nunca queden fuera de vista."""
    try:
        win.resizable(True, True)
    except Exception:
        pass
    try:
        screen_h = win.winfo_screenheight()
    except Exception:
        screen_h = 720
    h = min(base_h, max(min_h, screen_h - 120))
    try:
        x = parent.winfo_x() + (parent.winfo_width() - w) // 2
        y = parent.winfo_y() + (parent.winfo_height() - h) // 2
        win.geometry(f"{w}x{h}+{max(x, 20)}+{max(y, 20)}")
    except Exception:
        win.geometry(f"{w}x{h}")
    try:
        win.minsize(min_w, min_h)
    except Exception:
        pass


def shorten(path, limit=66):
    return path if len(path) <= limit else "…" + path[-(limit - 1):]
