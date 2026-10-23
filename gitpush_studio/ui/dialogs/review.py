# -*- coding: utf-8 -*-
"""Revisión de seguridad: muestra los hallazgos y deja excluirlos.

    mode="publish": pausa la publicación hasta decidir.
                    Responde ("ignore" | "continue" | "cancel", rutas).
    mode="manual":  solo añade patrones al cuadro de ignorar.
                    Responde ("ignore" | "close", rutas).
"""

import customtkinter as ctk

from ..base import DecisionDialog
from ..theme import (AMBER, BADGE_AMBER_BG, BADGE_AMBER_FG, BADGE_RED_BG,
                     BADGE_RED_FG, BORDER, GREEN, GREEN_H, MUTED, PANEL,
                     TEXT)
from ..widgets import button, scrollable, separator, shorten


class ReviewDialog(DecisionDialog):
    TITLE = "Revisión antes de publicar"
    SIZE = (640, 560)
    MIN_SIZE = (560, 460)

    def __init__(self, app, findings, mode, on_done, label=""):
        self.mode = mode
        self.findings = findings
        self._vars = []
        super().__init__(app, on_done)
        f = self.fonts
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=22, pady=(18, 0))
        title = "🛡  Revisión antes de publicar" + (f"  ·  {label}" if label
                                                   else "")
        ctk.CTkLabel(head, text=title, font=f.title, text_color=TEXT
                     ).pack(anchor="w")
        dudosos = sum(1 for x in findings
                      if x.get("confidence", "alta") == "baja")
        aviso = (f"Encontré {len(findings)} cosa(s) que conviene revisar. "
                 "Marca las que quieras excluir.")
        if dudosos:
            aviso += (f"  {dudosos} de ellas parecen ejemplos y vienen sin "
                      "marcar: si es código tuyo, déjalas y pulsa "
                      "«Continuar igualmente».")
        ctk.CTkLabel(head, text=aviso, font=f.small, text_color=MUTED,
                     justify="left", wraplength=580
                     ).pack(anchor="w", pady=(2, 0))
        separator(self).grid(row=1, column=0, sticky="ew", padx=22,
                             pady=(12, 0))

        body = scrollable(self)
        body.grid(row=2, column=0, sticky="nsew", padx=16, pady=(8, 0))
        bigs = [x for x in findings if x["kind"] in ("huge", "big")]
        secrets = [x for x in findings if x["kind"] == "secret"]
        if bigs:
            self._group(body, "ARCHIVOS GRANDES", bigs)
        if secrets:
            self._group(body, "POSIBLES SECRETOS", secrets)

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=3, column=0, sticky="ew", padx=22, pady=(10, 16))
        foot.grid_columnconfigure(0, weight=1)
        note = ("Ignorar no borra nada: los archivos siguen en tu PC, "
                "solo no se suben a GitHub.")
        if mode == "manual":
            note += " Se aplicará en la próxima publicación."
        ctk.CTkLabel(foot, text=note, font=f.small, text_color=MUTED,
                     justify="left", wraplength=580
                     ).grid(row=0, column=0, columnspan=3, sticky="w",
                            pady=(0, 10))
        publish = mode == "publish"
        button(foot, f, "Ignorar marcados y continuar" if publish
               else "Añadir marcados al .gitignore",
               lambda: self._finish("ignore"), kind="primary", height=40
               ).grid(row=1, column=0, sticky="ew", padx=(0, 8))
        if publish:
            button(foot, f, "Continuar igualmente",
                   lambda: self._finish("continue"), kind="ghost",
                   width=170, height=40, text_color=AMBER
                   ).grid(row=1, column=1, padx=(0, 8))
        button(foot, f, "Cancelar" if publish else "Cerrar", self.close,
               kind="ghost", width=110, height=40).grid(row=1, column=2)

    def _group(self, parent, title, items):
        f = self.fonts
        ctk.CTkLabel(parent, text=title, font=f.small, text_color=MUTED
                     ).pack(anchor="w", padx=6, pady=(10, 4))
        box = ctk.CTkFrame(parent, fg_color=PANEL, corner_radius=10,
                           border_width=1, border_color=BORDER)
        box.pack(fill="x", padx=4)
        for i, x in enumerate(items):
            row = ctk.CTkFrame(box, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=(8 if i == 0 else 4,
                                              8 if i == len(items) - 1 else 4))
            row.grid_columnconfigure(1, weight=1)
            # Lo que solo «parece» un secreto no se marca solo: excluirlo
            # sacaría de GitHub un archivo que quizá sea código tuyo.
            dudoso = x.get("confidence", "alta") == "baja"
            var = ctk.BooleanVar(value=not dudoso)
            self._vars.append((x, var))
            ctk.CTkCheckBox(row, text="", variable=var, width=22,
                            checkbox_width=18, checkbox_height=18,
                            fg_color=GREEN, hover_color=GREEN_H,
                            border_color=BORDER).grid(row=0, column=0,
                                                      sticky="w")
            ctk.CTkLabel(row, text=shorten(x["path"], 58), font=f.mono_s,
                         text_color=TEXT, anchor="w"
                         ).grid(row=0, column=1, sticky="ew", padx=(4, 8))
            danger = x["kind"] in ("huge", "secret") and not dudoso
            ctk.CTkLabel(row, text=" " + x["reason"] + " ", font=f.small,
                         corner_radius=10,
                         fg_color=BADGE_RED_BG if danger else BADGE_AMBER_BG,
                         text_color=BADGE_RED_FG if danger else BADGE_AMBER_FG,
                         height=22).grid(row=0, column=2, sticky="e")

    def selected(self):
        return [x["path"] for x, var in self._vars if var.get()]

    def close_result(self):
        return ("cancel" if self.mode == "publish" else "close",
                self.selected())

    def _finish(self, decision):
        self.finish((decision, self.selected()))
