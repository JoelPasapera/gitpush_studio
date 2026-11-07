# -*- coding: utf-8 -*-
"""Aplicar al proyecto los cambios de un TXT (lo que devuelve una IA).

Se pega la respuesta de la IA (o se abre un TXT), se pulsa «Revisar» y
la ventana enseña, archivo a archivo, qué cambiaría: nuevos,
modificados (con su «Ver cambios»), iguales y rechazados con su motivo.
Nada se escribe hasta pulsar «Aplicar», que pide confirmación y hace
antes una copia exacta (ver `core.apply_txt`).
"""

import threading
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Dict, List, Optional

import customtkinter as ctk

from ... import APP_NAME
from ...core.apply_txt import (STATUS_MODIFIED, STATUS_NEW, STATUS_REJECTED,
                               STATUS_SAME, ApplyPlan, FileChange,
                               apply_plan, plan_apply)
from ...core.fs import fmt_size, normalize_path
from ...core.textenc import to_utf8
from ..base import WorkerDialog
from ..diffview import DiffContent, DiffDialog
from ..theme import (AMBER, BADGE_AMBER_BG, BADGE_AMBER_FG, BADGE_BLUE_BG,
                     BADGE_GREEN_BG, BADGE_RED_BG, BADGE_RED_FG, BLUE,
                     BORDER, GREEN_T, INPUT, MUTED, PANEL, RED, TEXT)
from ..widgets import (browse_folder_into, button, entry, panel, scrollable,
                       shorten)

# Filas que se pintan como mucho (un TXT del proyecto entero puede traer
# miles de archivos iguales: esos solo se cuentan).
MAX_ROWS = 300
# Lo que cabe en el cuadro de pegar sin que Tk se arrastre; un TXT mayor
# se abre como archivo y se lee directamente del disco.
MAX_PASTE_PREVIEW = 2 * 1024 * 1024

BADGES = {
    STATUS_NEW: (" nuevo ", BADGE_GREEN_BG, GREEN_T),
    STATUS_MODIFIED: (" modifica ", BADGE_BLUE_BG, BLUE),
    STATUS_SAME: (" igual ", PANEL, MUTED),
    STATUS_REJECTED: (" no se aplica ", BADGE_RED_BG, BADGE_RED_FG),
    "sospechoso": (" ⚠ revisar ", BADGE_AMBER_BG, BADGE_AMBER_FG),
}


def read_txt_file(path: str) -> str:
    """El texto de un TXT en disco, en UTF-8 (también si se guardó en
    UTF-16 o en ANSI)."""
    raw = Path(path).read_bytes()
    converted = to_utf8(raw)
    if converted is None:
        raise ValueError("no es un archivo de texto")
    return converted.decode("utf-8", "replace")


class ApplyTxtDialog(WorkerDialog):
    TITLE = "Aplicar cambios de un TXT"
    SIZE = (860, 760)
    MIN_SIZE = (700, 560)

    def __init__(self, app):
        super().__init__(app)
        f = self.fonts
        pad = {"padx": 22}
        self.plan: Optional[ApplyPlan] = None
        self.source_file = ""
        # Un texto pegado demasiado grande para el cuadro se guarda aquí
        # (entero) y el cuadro solo dice cuánto ocupa.
        self.source_text = ""
        self.checks: Dict[str, ctk.BooleanVar] = {}
        self._busy = threading.Lock()

        ctk.CTkLabel(self, text="📥  Aplicar cambios de un TXT",
                     font=f.title, text_color=TEXT
                     ).pack(anchor="w", pady=(18, 0), **pad)
        ctk.CTkLabel(self, text="Pega la respuesta de la IA (o abre un TXT). "
                                "Antes de tocar nada verás qué cambiaría en "
                                "cada archivo.", font=f.small,
                     text_color=MUTED, justify="left", wraplength=780
                     ).pack(anchor="w", pady=(2, 10), **pad)

        top = panel(self, f, "📁  Proyecto")
        rowp = ctk.CTkFrame(top, fg_color="transparent")
        rowp.pack(fill="x", padx=16, pady=(6, 12))
        rowp.grid_columnconfigure(0, weight=1)
        self.e_proj = entry(rowp, f, "Carpeta del proyecto")
        self.e_proj.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        button(rowp, f, "Examinar…",
               lambda: browse_folder_into(self.e_proj, "Proyecto"),
               width=110).grid(row=0, column=1)

        src = panel(self, f, "📝  Texto con los cambios", pady=(10, 0))
        self.txt = ctk.CTkTextbox(src, height=150, font=f.mono_s,
                                  fg_color=INPUT, border_color=BORDER,
                                  border_width=1, text_color=TEXT,
                                  wrap="none")
        self.txt.pack(fill="x", padx=16, pady=(6, 6))
        rows = ctk.CTkFrame(src, fg_color="transparent")
        rows.pack(fill="x", padx=16, pady=(0, 12))
        button(rows, f, "📋 Pegar", self._paste, kind="ghost", width=100,
               height=30, font=f.small).pack(side="left", padx=(0, 6))
        button(rows, f, "📂 Abrir TXT…", self._open_file, kind="ghost",
               width=120, height=30, font=f.small).pack(side="left",
                                                       padx=(0, 6))
        button(rows, f, "Limpiar", self._clear_source, kind="ghost",
               width=80, height=30, font=f.small).pack(side="left")
        self.lbl_source = ctk.CTkLabel(rows, text="", font=f.small,
                                       text_color=MUTED)
        self.lbl_source.pack(side="left", padx=(10, 0))

        self.btn_review = button(self, f, "🔍  Revisar cambios", self._review,
                                 kind="primary", height=40)
        self.btn_review.pack(fill="x", pady=(10, 0), **pad)

        self.results = scrollable(self, height=200)
        self.results.pack(fill="both", expand=True, pady=(10, 0), **pad)

        self.lbl_status = ctk.CTkLabel(self, text="", font=f.small,
                                       text_color=MUTED, justify="left",
                                       wraplength=780)
        self.lbl_status.pack(anchor="w", pady=(8, 0), **pad)
        rowb = ctk.CTkFrame(self, fg_color="transparent")
        rowb.pack(fill="x", pady=(8, 16), **pad)
        rowb.grid_columnconfigure(0, weight=1)
        self.btn_apply = button(rowb, f, "✅  Aplicar los marcados",
                                self._apply, kind="primary", height=42,
                                state="disabled")
        self.btn_apply.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        button(rowb, f, "Cerrar", self.close, kind="ghost", width=100,
               height=42).grid(row=0, column=1)

        proj = normalize_path(app.e_folder.get())
        if proj and Path(proj).is_dir():
            self.e_proj.insert(0, proj)

    # ---- origen del texto ---------------------------------------------
    def _paste(self) -> None:
        try:
            text = self.clipboard_get()
        except Exception:                                   # noqa: BLE001
            text = ""
        if not text.strip():
            self.status("El portapapeles está vacío.", AMBER)
            return
        size = len(text.encode("utf-8"))
        self.source_file = ""
        self.source_text = ""
        self.lbl_source.configure(text="")
        self.txt.delete("1.0", "end")
        if size > MAX_PASTE_PREVIEW:
            # Pintar decenas de MB en el cuadro dejaría la ventana colgada:
            # se usa entero, pero no se enseña.
            self.source_text = text
            self.lbl_source.configure(text=f"Pegado: {fmt_size(size)} "
                                           "(demasiado para enseñarlo aquí; "
                                           "se usa entero)")
        else:
            self.txt.insert("end", text)
        self.status(f"Pegado ({fmt_size(size)}). Pulsa «Revisar cambios».",
                    MUTED)

    def _open_file(self) -> None:
        chosen = filedialog.askopenfilename(
            title="TXT con los cambios",
            filetypes=[("Archivo de texto", "*.txt"), ("Todos", "*.*")])
        if chosen:
            self.use_file(chosen)

    def use_file(self, chosen: str) -> None:
        """Usa un TXT del disco como origen (también al soltarlo en la
        ventana principal)."""
        self.source_file = str(Path(chosen))
        self.source_text = ""
        self.txt.delete("1.0", "end")
        try:
            size = Path(chosen).stat().st_size
        except OSError:
            size = 0
        self.lbl_source.configure(text=f"Usando «{Path(chosen).name}» "
                                       f"({fmt_size(size)})")
        self.status("Pulsa «Revisar cambios».", MUTED)

    def _clear_source(self) -> None:
        self.source_file = ""
        self.source_text = ""
        self.lbl_source.configure(text="")
        self.txt.delete("1.0", "end")
        self._clear_results()
        self.status("", MUTED)

    def _pasted_text(self) -> str:
        if self.source_text:
            return self.source_text
        return self.txt.get("1.0", "end")

    # ---- revisar -------------------------------------------------------
    def _review(self) -> None:
        if self.running:
            return
        proj = normalize_path(self.e_proj.get())
        if not proj or not Path(proj).is_dir():
            self.status("Indica una carpeta de proyecto que exista.", RED)
            return
        text = "" if self.source_file else self._pasted_text()
        if not self.source_file and not text.strip():
            self.status("Pega primero el texto con los cambios, o abre un "
                        "TXT.", AMBER)
            return
        self.running = True
        self.btn_review.configure(state="disabled", text="Revisando…")
        self.btn_apply.configure(state="disabled")
        self._clear_results()
        self.status("Comparando con los archivos de la carpeta…", MUTED)
        self.run_in_thread(self._plan_worker, proj, text, self.source_file)

    def _plan_worker(self, proj: str, text: str, source_file: str) -> None:
        if source_file:
            try:
                text = read_txt_file(source_file)
            except (OSError, ValueError) as e:
                self.post("err", f"No pude leer «{Path(source_file).name}» "
                                 f"({e}).")
                return
        self.post("plan", plan_apply(proj, text))

    def _clear_results(self) -> None:
        for child in self.results.winfo_children():
            child.destroy()
        self.checks = {}
        self.plan = None

    def _show_plan(self, plan: ApplyPlan) -> None:
        self.plan = plan
        f = self.fonts
        counts = plan.counts()
        visible = [c for c in plan.changes if c.status != STATUS_SAME]
        for note in plan.notes:
            ctk.CTkLabel(self.results, text="ℹ " + note, font=f.small,
                         text_color=AMBER, justify="left", wraplength=740
                         ).pack(anchor="w", padx=6, pady=(4, 0))
        selected = set(plan.default_selection())
        for change in visible[:MAX_ROWS]:
            self._row(change, change.path in selected)
        if len(visible) > MAX_ROWS:
            ctk.CTkLabel(self.results, text=f"… y {len(visible) - MAX_ROWS} "
                                            "más (no se aplican desde aquí)",
                         font=f.small, text_color=MUTED
                         ).pack(anchor="w", padx=6, pady=(4, 0))
        partes = []
        if counts[STATUS_MODIFIED]:
            partes.append(f"{counts[STATUS_MODIFIED]} a modificar")
        if counts[STATUS_NEW]:
            partes.append(f"{counts[STATUS_NEW]} nuevos")
        if counts[STATUS_SAME]:
            partes.append(f"{counts[STATUS_SAME]} sin cambios")
        if counts[STATUS_REJECTED]:
            partes.append(f"{counts[STATUS_REJECTED]} que no se aplican")
        resumen = " · ".join(partes) or "nada que hacer"
        self.status("Revisado: " + resumen + ". Nada se ha escrito todavía.",
                    MUTED if plan.applicable else AMBER)
        self._sync_apply_button()

    def _row(self, change: FileChange, checked: bool) -> None:
        f = self.fonts
        row = ctk.CTkFrame(self.results, fg_color=PANEL, corner_radius=8)
        row.pack(fill="x", padx=4, pady=3)
        row.grid_columnconfigure(1, weight=1)
        key = "sospechoso" if change.suspicious else change.status
        text, bg, fg = BADGES[key]
        ctk.CTkLabel(row, text=text, font=f.small, corner_radius=8,
                     fg_color=bg, text_color=fg, width=104, height=20
                     ).grid(row=0, column=0, sticky="w", padx=(8, 0),
                            pady=6)
        detail = shorten(change.path, 56)
        if change.applicable:
            detail += f"   +{change.added} −{change.removed}"
        ctk.CTkLabel(row, text=detail, font=f.mono_s, text_color=TEXT,
                     anchor="w").grid(row=0, column=1, sticky="ew",
                                      padx=(8, 8))
        if change.applicable:
            var = ctk.BooleanVar(value=checked)
            self.checks[change.path] = var
            ctk.CTkCheckBox(row, text="aplicar", variable=var,
                            font=f.small, text_color=MUTED,
                            checkbox_width=18, checkbox_height=18,
                            command=self._sync_apply_button
                            ).grid(row=0, column=2, padx=(0, 6))
            button(row, f, "Ver cambios", lambda c=change: self._show(c),
                   kind="ghost", width=104, height=26, font=f.small
                   ).grid(row=0, column=3, padx=(0, 8))
        why = change.reason if change.status == STATUS_REJECTED else \
            " · ".join(change.warnings)
        if why:
            ctk.CTkLabel(row, text=why, font=f.small,
                         text_color=RED if change.status == STATUS_REJECTED
                         else AMBER, justify="left", wraplength=700,
                         anchor="w").grid(row=1, column=0, columnspan=4,
                                          sticky="w", padx=10, pady=(0, 6))

    def _show(self, change: FileChange) -> None:
        def loader() -> DiffContent:
            return DiffContent(old_lines=change.old_lines,
                               new_lines=change.new_lines,
                               codes=change.codes)
        etiqueta = "archivo nuevo" if change.status == STATUS_NEW else \
            f"codificación {change.encoding}"
        DiffDialog(self.app, change.path, loader, subtitle=etiqueta)

    def selected_paths(self) -> List[str]:
        return [path for path, var in self.checks.items() if var.get()]

    def _sync_apply_button(self) -> None:
        count = len(self.selected_paths())
        if count and not self.running:
            self.btn_apply.configure(state="normal",
                                     text=f"✅  Aplicar {count} "
                                          f"{'archivo' if count == 1 else 'archivos'}")
        else:
            self.btn_apply.configure(state="disabled",
                                     text="✅  Aplicar los marcados")

    # ---- aplicar -------------------------------------------------------
    def _apply(self) -> None:
        if self.running or self.plan is None:
            return
        paths = self.selected_paths()
        if not paths:
            return
        chosen = [c for c in self.plan.changes if c.path in set(paths)]
        nuevos = sum(1 for c in chosen if c.status == STATUS_NEW)
        cambiados = len(chosen) - nuevos
        sospechosos = sum(1 for c in chosen if c.suspicious)
        pregunta = (f"Se modificarán {cambiados} archivo(s) y se crearán "
                    f"{nuevos} en «{self.plan.folder.name}».\n\n"
                    "Antes se guarda una copia exacta de lo que se "
                    "modifica. ¿Aplicar?")
        if sospechosos:
            pregunta = (f"⚠ {sospechosos} de ellos parecen un resumen o una "
                        "respuesta cortada de la IA: revisa sus cambios.\n\n"
                        + pregunta)
        if not messagebox.askyesno(APP_NAME, pregunta, default="no"):
            return
        self.running = True
        self.btn_apply.configure(state="disabled", text="Aplicando…")
        self.btn_review.configure(state="disabled")
        self.status("Guardando la copia y aplicando…", MUTED)
        identity = (self.app.e_user.get().strip(),
                    self.app.e_email.get().strip())
        self.run_in_thread(self._apply_worker, self.plan, paths, identity)

    def _apply_worker(self, plan: ApplyPlan, paths: List[str],
                      identity) -> None:
        def log(text: str, tag: str = "dim") -> None:
            self.post("log", text, tag)
        result = apply_plan(plan, paths,
                            identity=identity if all(identity) else None,
                            log=log)
        self.post("applied", result)

    def _finish_apply(self, result) -> None:
        hechos = len(result.written) + len(result.created)
        texto = f"✔ Aplicado: {len(result.written)} modificados, " \
                f"{len(result.created)} nuevos."
        if result.backup_kind == "rama":
            texto += (f"\nLa versión anterior está en la rama «{result.backup}» "
                      "(«⏪ Versiones» la recupera).")
        elif result.backup_kind == "carpeta":
            texto += f"\nLa versión anterior está en «{result.backup}»."
        color = GREEN_T
        if result.failed:
            color = AMBER
            texto += "\n⚠ No se pudieron escribir: " + ", ".join(
                f"«{p}» ({why})" for p, why in result.failed[:5])
        self.status(texto, color)
        self.app.log(f"📥 TXT aplicado en {self.plan.folder}: {hechos} "
                     "archivo(s).", "ok")
        for path in result.written:
            self.app.log(f"  ~ {path}", "dim")
        for path in result.created:
            self.app.log(f"  + {path}", "dim")
        for path, why in result.failed:
            self.app.log(f"  ✖ {path}: {why}", "err")
        # Lo revisado ya no vale: hay que volver a revisar para aplicar más.
        for child in self.results.winfo_children():
            child.destroy()
        self.checks = {}
        self.plan = None

    def handle(self, msg) -> None:
        kind = msg[0]
        if kind == "plan":
            self.running = False
            self.btn_review.configure(state="normal",
                                      text="🔍  Revisar cambios")
            self._show_plan(msg[1])
        elif kind == "log":
            self.app.log(msg[1], msg[2])
        elif kind == "applied":
            self.running = False
            self.btn_review.configure(state="normal",
                                      text="🔍  Revisar cambios")
            self._finish_apply(msg[1])
            self._sync_apply_button()
        elif kind == "err":
            self.running = False
            self.btn_review.configure(state="normal",
                                      text="🔍  Revisar cambios")
            self.status("✖ " + msg[1], RED)
            self._sync_apply_button()
