# -*- coding: utf-8 -*-
"""Tema visual (inspirado en el modo oscuro de GitHub)."""

import os

import customtkinter as ctk

BG      = "#0d1117"   # fondo general
PANEL   = "#161b22"   # tarjetas / sidebar
PANEL2  = "#21262d"   # hover / seleccionado
INPUT   = "#0d1117"   # fondo de campos
BORDER  = "#30363d"   # bordes
TEXT    = "#e6edf3"   # texto principal
MUTED   = "#8b949e"   # texto secundario
GREEN   = "#238636"   # acción principal (como el botón de GitHub)
GREEN_H = "#2ea043"
GREEN_T = "#3fb950"   # texto «ok»
BLUE    = "#388bfd"
BLUE_D  = "#1f6feb"
RED     = "#f85149"
RED_D   = "#da3633"
AMBER   = "#d29922"
CONSOLE_BG = "#010409"
CONSOLE_FG = "#c9d1d9"

BADGE_RED_BG   = "#3d1d1f"
BADGE_RED_FG   = "#f09595"
BADGE_AMBER_BG = "#3a2d10"
BADGE_AMBER_FG = "#fac775"
BADGE_GREEN_BG = "#12261e"
BADGE_BLUE_BG  = "#132339"

# (fondo, texto) de la píldora de estado por tipo
STATUS_COLORS = {"idle": (PANEL, MUTED), "work": (BADGE_BLUE_BG, BLUE),
                 "ok": (BADGE_GREEN_BG, GREEN_T), "err": ("#2d1215", RED),
                 "warn": ("#2d2412", AMBER)}

# color de cada etiqueta de la consola
LOG_COLORS = {"cmd": "#79c0ff", "ok": GREEN_T, "err": RED, "warn": AMBER,
              "dim": MUTED, "out": CONSOLE_FG}

MONO = "Consolas" if os.name == "nt" else "Menlo"


class Fonts:
    """CTkFont necesita una ventana raíz: se construye tras crearla."""

    def __init__(self):
        self.logo = ctk.CTkFont(size=22, weight="bold")
        self.title = ctk.CTkFont(size=19, weight="bold")
        self.section = ctk.CTkFont(size=14, weight="bold")
        self.body = ctk.CTkFont(size=13)
        self.small = ctk.CTkFont(size=11)
        self.btn = ctk.CTkFont(size=15, weight="bold")
        self.mono = ctk.CTkFont(family=MONO, size=12)
        self.mono_s = ctk.CTkFont(family=MONO, size=11)
