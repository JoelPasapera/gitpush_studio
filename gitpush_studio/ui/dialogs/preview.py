# -*- coding: utf-8 -*-
"""Vista previa de cambios: enseña, antes de subir nada, qué cambiará
exactamente en GitHub y deja sacar archivos concretos de la publicación.

En cada grupo, un botón por archivo (y uno para todos):
    se borrarán  → «Mantener»       el archivo sigue en GitHub
    nuevos       → «No subir»       no se crea en GitHub
    modificados  → «No actualizar»  GitHub se queda con su versión

Responde False (cancelar) o un PreviewChoice.
"""

import customtkinter as ctk

from ...core.publish import PreviewChoice
from ..base import DecisionDialog
from ..theme import (AMBER, BADGE_AMBER_BG, BADGE_AMBER_FG, BADGE_BLUE_BG,
                     BADGE_GREEN_BG, BADGE_RED_BG, BADGE_RED_FG, BLUE, BORDER,
                     GREEN_T, MUTED, PANEL, TEXT)
from ..widgets import button, checkbox, scrollable, separator, shorten

# Por grupo: título, etiqueta y botón normales, y los de un archivo
# excluido de esta publicación.
GROUPS = {
    "deleted": {
        "title": "SE BORRARÁN DE GITHUB",
        "badge": (" borra ", BADGE_RED_BG, BADGE_RED_FG),
        "badge_off": (" se mantiene ", BADGE_GREEN_BG, GREEN_T),
        "action": "Mantener", "undo": "Borrar",
        "all": "Mantener todos", "all_undo": "Borrar todos",
        "counter": "se borrarán", "counter_off": "se mantienen",
    },
    "added": {
        "title": "NUEVOS",
        "badge": (" nuevo ", BADGE_GREEN_BG, GREEN_T),
        "badge_off": (" no se sube ", BADGE_AMBER_BG, BADGE_AMBER_FG),
        "action": "No subir", "undo": "Subir",
        "all": "No subir ninguno", "all_undo": "Subir todos",
        "counter": "nuevos", "counter_off": "no se suben",
    },
    "modified": {
        "title": "MODIFICADOS",
        "badge": (" edita ", BADGE_BLUE_BG, BLUE),
        "badge_off": (" sin cambios ", BADGE_AMBER_BG, BADGE_AMBER_FG),
        "action": "No actualizar", "undo": "Actualizar",
        "all": "No actualizar ninguno", "all_undo": "Actualizar todos",
        "counter": "modificados", "counter_off": "no se actualizan",
    },
}


class _Group:
    """Un grupo de la lista, con su selección.

    Guarda los widgets de cada fila en un diccionario: alternar un
    archivo es una búsqueda en un conjunto y dos `configure`, sin
    reconstruir la lista ni volver a consultar git.
    """

    def __init__(self, dialog, parent, kind, items, max_shown):
        self.dialog = dialog
        self.kind = kind
        self.spec = GROUPS[kind]
        self.items = list(items)
        self.off = set()
        self.rows = {}
        f = dialog.fonts

        head = ctk.CTkFrame(parent, fg_color="transparent")
        head.pack(fill="x", padx=6, pady=(10, 4))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text=f"{self.spec['title']}  ({len(self.items)})",
                     font=f.small, text_color=MUTED
                     ).grid(row=0, column=0, sticky="w")
        self.btn_all = button(head, f, self.spec["all"], self.toggle_all,
                              kind="ghost", width=140, height=24,
                              font=f.small)
        self.btn_all.grid(row=0, column=1, sticky="e")

        box = ctk.CTkFrame(parent, fg_color=PANEL, corner_radius=10,
                           border_width=1, border_color=BORDER)
        box.pack(fill="x", padx=4)
        shown = self.items[:max_shown]
        last = len(shown) - 1
        for i, path in enumerate(shown):
            row = ctk.CTkFrame(box, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=(8 if i == 0 else 3,
                                              8 if i == last else 3))
            row.grid_columnconfigure(1, weight=1)
            text, bg, fg = self.spec["badge"]
            badge = ctk.CTkLabel(row, text=text, font=f.small,
                                 corner_radius=8, fg_color=bg, text_color=fg,
                                 width=96, height=20)
            badge.grid(row=0, column=0, sticky="w")
            ctk.CTkLabel(row, text=shorten(path, 50), font=f.mono_s,
                         text_color=TEXT, anchor="w"
                         ).grid(row=0, column=1, sticky="ew", padx=(8, 8))
            btn = button(row, f, self.spec["action"],
                         lambda p=path: self.toggle(p), kind="ghost",
                         width=104, height=26, font=f.small)
            btn.grid(row=0, column=2, sticky="e")
            self.rows[path] = (badge, btn)
        if len(self.items) > max_shown:
            ctk.CTkLabel(box, text=f"… y {len(self.items) - max_shown} más "
                                   f"(el botón «{self.spec['all']}» los "
                                   "incluye)", font=f.small, text_color=MUTED
                         ).pack(anchor="w", padx=14, pady=(0, 8))

    # ------------------------------------------------------------------
    def _paint(self, path):
        widgets = self.rows.get(path)
        if widgets is None:
            return
        badge, btn = widgets
        excluded = path in self.off
        text, bg, fg = self.spec["badge_off" if excluded else "badge"]
        badge.configure(text=text, fg_color=bg, text_color=fg)
        btn.configure(text=self.spec["undo" if excluded else "action"])

    def toggle(self, path):
        if path in self.off:
            self.off.discard(path)
        else:
            self.off.add(path)
        self._paint(path)
        self._sync_all_button()
        self.dialog.refresh_summary()

    def toggle_all(self):
        if len(self.off) == len(self.items):
            self.off.clear()
        else:
            self.off.update(self.items)
        for path in self.rows:
            self._paint(path)
        self._sync_all_button()
        self.dialog.refresh_summary()

    def _sync_all_button(self):
        every = self.items and len(self.off) == len(self.items)
        self.btn_all.configure(text=self.spec["all_undo" if every else "all"])

    @property
    def excluded(self):
        return sorted(self.off)

    def counters(self):
        """(«12 modificados», «3 no se actualizan» o None)."""
        active = len(self.items) - len(self.off)
        main = f"{active} {self.spec['counter']}"
        extra = (f"{len(self.off)} {self.spec['counter_off']}"
                 if self.off else None)
        return main, extra


class PreviewDialog(DecisionDialog):
    TITLE = "Vista previa de cambios"
    SIZE = (720, 660)
    MIN_SIZE = (620, 470)
    MAX_SHOWN = 250
    CLOSE_RESULT = False

    def __init__(self, app, added, modified, deleted, mode, on_done,
                 label=""):
        super().__init__(app, on_done)
        f = self.fonts
        self.groups = {}
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)
        pad = {"padx": 22}

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew")
        title = "👁  Vista previa de cambios" + (f"  ·  {label}" if label
                                                 else "")
        ctk.CTkLabel(head, text=title, font=f.title, text_color=TEXT
                     ).pack(anchor="w", pady=(18, 0), **pad)
        self.lbl_summary = ctk.CTkLabel(head, text="", font=f.small,
                                        text_color=MUTED, justify="left",
                                        wraplength=660)
        self.lbl_summary.pack(anchor="w", pady=(2, 0), **pad)
        aviso = ("Cada archivo tiene su botón: puedes dejar fuera de esta "
                 "publicación lo que no quieras tocar.")
        if deleted:
            aviso = ("⚠ Estás en modo Espejo: los archivos de «se borrarán» "
                     "desaparecerán de GitHub. " + aviso)
        ctk.CTkLabel(head, text=aviso, font=f.small,
                     text_color=AMBER if deleted else MUTED, justify="left",
                     wraplength=660).pack(anchor="w", pady=(6, 0), **pad)
        separator(self).grid(row=1, column=0, sticky="ew", padx=22,
                             pady=(12, 0))

        body = scrollable(self)
        body.grid(row=2, column=0, sticky="nsew", padx=16, pady=(8, 0))
        for kind, items in (("deleted", deleted), ("added", added),
                            ("modified", modified)):
            if items:
                self.groups[kind] = _Group(self, body, kind, items,
                                           self.MAX_SHOWN)

        foot = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0)
        foot.grid(row=3, column=0, sticky="ew")
        separator(foot).pack(fill="x", side="top")
        self.v_restore = ctk.BooleanVar(value=True)
        self.v_ignore = ctk.BooleanVar(value=True)
        if deleted:
            checkbox(foot, f, "Los que mantengo, cópialos también de vuelta a "
                              "mi carpeta (si no, la próxima publicación "
                              "volverá a preguntar)", self.v_restore
                     ).pack(anchor="w", pady=(10, 0), **pad)
        if added:
            checkbox(foot, f, "Los que no suba, añádelos al .gitignore para no "
                              "volver a preguntar (siguen en tu carpeta)",
                     self.v_ignore).pack(anchor="w", pady=(8, 0), **pad)
        nota = ("Nada se ha subido todavía. Si cancelas, tu carpeta se "
                "queda como está.")
        if modified:
            nota += ("  Un modificado que dejes fuera conserva tu versión "
                     "local intacta, así que volverá a aparecer la próxima "
                     "vez; para quedarte con la de GitHub usa «⬇⬆ Traer "
                     "cambios».")
        if mode == "addedit":
            nota += "  En modo agregar/editar nunca se borra nada de GitHub."
        ctk.CTkLabel(foot, text=nota, font=f.small, text_color=MUTED,
                     justify="left", wraplength=660
                     ).pack(anchor="w", pady=(8, 0), **pad)
        row = ctk.CTkFrame(foot, fg_color="transparent")
        row.pack(fill="x", pady=(10, 16), **pad)
        row.grid_columnconfigure(0, weight=1)
        self.btn_publish = button(row, f, "", self._publish, kind="primary",
                                  height=42)
        self.btn_publish.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        button(row, f, "Cancelar", lambda: self.finish(False), kind="ghost",
               width=120, height=42).grid(row=0, column=1)
        self.refresh_summary()

    # ------------------------------------------------------------------
    def excluded(self, kind):
        group = self.groups.get(kind)
        return group.excluded if group else []

    def refresh_summary(self):
        main, extra = [], []
        for kind in ("added", "modified", "deleted"):
            group = self.groups.get(kind)
            if group is None:
                continue
            a, b = group.counters()
            main.append(a)
            if b:
                extra.append(b)
        text = "Esto es lo que pasará en GitHub:  " + " · ".join(main)
        if extra:
            text += "\nSe quedan fuera:  " + " · ".join(extra)
        self.lbl_summary.configure(text=text)
        left_out = sum(len(g.off) for g in self.groups.values())
        self.btn_publish.configure(
            text="🚀  Publicar estos cambios" if not left_out else
            f"🚀  Publicar (dejando fuera {left_out})")

    def _publish(self):
        self.finish(PreviewChoice(
            go=True,
            keep_deleted=self.excluded("deleted"),
            restore_to_folder=bool(self.v_restore.get()),
            skip_added=self.excluded("added"),
            ignore_skipped=bool(self.v_ignore.get()),
            skip_modified=self.excluded("modified")))
