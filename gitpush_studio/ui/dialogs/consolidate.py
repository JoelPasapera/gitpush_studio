# -*- coding: utf-8 -*-
"""Consolidar un proyecto (árbol + contenido) en un único TXT.

Sin límite de tamaño: con una barra de progreso y «Detener», que deja el
TXT anterior tal como estaba. Con las opciones pensadas para pasárselo a
una IA: solo lo que cambió, sin lo que no aporta, con los secretos
tachados, repartido en partes que quepan en su contexto y copiado al
portapapeles (entero o parte a parte).
"""

import threading
from pathlib import Path
from tkinter import filedialog
from typing import List

import customtkinter as ctk

from ...core.desktop import open_path, reveal_in_folder
from ...core.errors import Cancelled
from ...core.fs import fmt_size, normalize_path, relative_inside
from ...core.ignore import anchor_pattern
from ...core.pack import PackOptions, consolidate_project, free_txt_path
from ...core.txtchanges import CHANGES_DIFF, CHANGES_FILES, CHANGES_NONE
from ..base import WorkerDialog
from ..theme import AMBER, BLUE, GREEN_T, MUTED, RED
from ..widgets import (browse_folder_into, button, checkbox, entry, hint,
                       option_menu, panel, progress_bar, segmented,
                       set_text)

# Lo que se puede llevar al portapapeles de una vez sin que el sistema
# (o la IA en la que se pega) se atragante. Más grande: por partes.
CLIPBOARD_MAX = 16 * 1024 * 1024

CONTENT_CHOICES = (("Todo el proyecto", CHANGES_NONE),
                   ("Solo lo que cambió", CHANGES_FILES),
                   ("Solo las diferencias", CHANGES_DIFF))
SPLIT_CHOICES = (("No dividir", 0), ("32.000 tokens", 32000),
                 ("128.000 tokens", 128000), ("200.000 tokens", 200000),
                 ("1.000.000 tokens", 1000000))


def _num(n):
    return f"{n:,}".replace(",", ".")


def _inside(out, proj):
    """Ruta del TXT relativa al proyecto si cae dentro, o ""."""
    return relative_inside(out, proj)


class ConsolidateDialog(WorkerDialog):
    TITLE = "Consolidar proyecto a TXT"
    SIZE = (760, 720)
    MIN_SIZE = (640, 420)

    def __init__(self, app):
        super().__init__(app)
        f = self.fonts
        self.out_path = ""
        self.parts: List[str] = []
        self.next_part = 0
        self.cancel_event = threading.Event()
        body, footer = self.scrolling_layout(
            "📄  Consolidar a TXT",
            "Todo el proyecto (árbol + contenido) en un solo archivo de "
            "texto.")

        c1 = panel(body, f, "📁  Proyecto a empaquetar", padx=12,
                   pady=(0, 10))
        rowp = ctk.CTkFrame(c1, fg_color="transparent")
        rowp.pack(fill="x", padx=16, pady=(4, 12))
        rowp.grid_columnconfigure(0, weight=1)
        self.e_proj = entry(rowp, f, "Carpeta del proyecto")
        self.e_proj.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        button(rowp, f, "Examinar…", self._browse_proj, width=110
               ).grid(row=0, column=1)

        c2 = panel(body, f, "💾  Archivo TXT de salida", padx=12,
                   pady=(0, 10))
        rowo = ctk.CTkFrame(c2, fg_color="transparent")
        rowo.pack(fill="x", padx=16, pady=(4, 0))
        rowo.grid_columnconfigure(0, weight=1)
        self.e_out = entry(rowo, f, "ruta\\del\\proyecto.txt")
        self.e_out.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        button(rowo, f, "Elegir…", self._browse_out, width=110
               ).grid(row=0, column=1)
        self.v_ign = ctk.BooleanVar(value=True)
        checkbox(c2, f, "Respetar .gitignore y el cuadro de ignorar "
                        "(recomendado)", self.v_ign
                 ).pack(anchor="w", padx=16, pady=(8, 0))
        hint(c2, f, "Por defecto se llama como el proyecto y se guarda junto "
                    "a él. Los archivos de texto entran enteros, midan lo "
                    "que midan (también los que están en UTF-16 o en ANSI "
                    "de Windows, ya convertidos); los binarios solo se "
                    "mencionan con su tamaño. El TXT nunca se incluye a sí "
                    "mismo.", wraplength=640, pady=(6, 10))

        c3 = panel(body, f, "🤖  Para pasárselo a una IA", padx=12,
                   pady=(0, 4))
        self.seg_content = segmented(c3, f, [c[0] for c in CONTENT_CHOICES],
                                     height=30, font=f.small)
        self.seg_content.set(CONTENT_CHOICES[0][0])
        self.seg_content.pack(anchor="w", padx=16, pady=(4, 0))
        self.v_ai = ctk.BooleanVar(value=False)
        checkbox(c3, f, "Dejar fuera lo que no aporta (archivos de bloqueo "
                        "de dependencias, minificados, generados, código de "
                        "terceros)", self.v_ai
                 ).pack(anchor="w", padx=16, pady=(8, 0))
        self.v_redact = ctk.BooleanVar(value=True)
        checkbox(c3, f, "Tachar los secretos (contraseñas, tokens, llaves) "
                        "como «••••••••»", self.v_redact
                 ).pack(anchor="w", padx=16, pady=(4, 0))
        rows = ctk.CTkFrame(c3, fg_color="transparent")
        rows.pack(fill="x", padx=16, pady=(8, 0))
        ctk.CTkLabel(rows, text="Dividir en partes de:", font=f.small,
                     text_color=MUTED).pack(side="left", padx=(0, 8))
        self.opt_split = option_menu(rows, f, [c[0] for c in SPLIT_CHOICES],
                                     width=150)
        self.opt_split.set(SPLIT_CHOICES[0][0])
        self.opt_split.pack(side="left")
        hint(c3, f, "«Solo lo que cambió» lleva los archivos modificados "
                    "desde la última publicación (el último commit), enteros "
                    "y listos para aplicarlos de vuelta; «Solo las "
                    "diferencias», solo sus líneas cambiadas. Los secretos "
                    "tachados se recuperan solos al aplicar los cambios "
                    "con «📥 Aplicar TXT».", wraplength=640, pady=(6, 10))

        # El pie: progreso, estado y botones, siempre a la vista.
        self.pbar = progress_bar(footer)
        self.pbar.pack(fill="x", pady=(2, 0))
        self.lbl_status = ctk.CTkLabel(footer, text="", font=f.small,
                                       text_color=MUTED, justify="left",
                                       wraplength=700)
        self.lbl_status.pack(anchor="w", pady=(4, 0))

        rowb = ctk.CTkFrame(footer, fg_color="transparent")
        rowb.pack(fill="x", pady=(6, 0))
        rowb.grid_columnconfigure(0, weight=1)
        self.btn_go = button(rowb, f, "Generar TXT", self._generate,
                             kind="primary", height=42)
        self.btn_go.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        # Un botón aparte (no el mismo «Generar» convertido): un doble
        # clic sobre «Generar» no puede detener lo que acaba de empezar.
        self.btn_stop = button(rowb, f, "Detener", self._stop, kind="ghost",
                               width=96, height=42, text_color=AMBER,
                               state="disabled")
        self.btn_stop.grid(row=0, column=1, padx=(0, 10))
        self.btn_open = button(rowb, f, "Abrir TXT",
                               lambda: open_path(self.out_path),
                               kind="ghost", width=110, height=42,
                               text_color=BLUE, state="disabled")
        self.btn_open.grid(row=0, column=2, padx=(0, 10))
        # La carpeta donde quedó, con el TXT ya seleccionado (como «Abrir
        # ubicación del archivo» del Explorador).
        self.btn_reveal = button(rowb, f, "📂  Abrir ubicación", self._reveal,
                                 kind="ghost", width=150, height=42,
                                 text_color=BLUE, state="disabled")
        self.btn_reveal.grid(row=0, column=3, padx=(0, 10))
        self.btn_copy = button(rowb, f, "📋 Copiar", self._copy,
                               kind="ghost", width=120, height=42,
                               text_color=BLUE, state="disabled")
        self.btn_copy.grid(row=0, column=4, padx=(0, 10))
        button(rowb, f, "Cerrar", self.close, kind="ghost", width=90,
               height=42).grid(row=0, column=5)

        proj = normalize_path(app.e_folder.get())
        if proj and Path(proj).is_dir():
            self.e_proj.insert(0, proj)
            self._suggest_output()

    # ------------------------------------------------------------------
    def _suggest_output(self):
        proj = normalize_path(self.e_proj.get())
        p = Path(proj)
        if proj and p.is_dir():
            set_text(self.e_out, str(p.parent / (p.name + ".txt")))

    def _browse_proj(self):
        if browse_folder_into(self.e_proj, "Proyecto a empaquetar"):
            self._suggest_output()

    def _browse_out(self):
        proj = Path(normalize_path(self.e_proj.get() or "."))
        chosen = filedialog.asksaveasfilename(
            title="¿Dónde guardar el TXT?", defaultextension=".txt",
            initialfile=(proj.name or "proyecto") + ".txt",
            initialdir=str(proj.parent if proj.parent.is_dir()
                           else Path.home()),
            filetypes=[("Archivo de texto", "*.txt")])
        if chosen:
            set_text(self.e_out, str(Path(chosen)))

    def _reveal(self):
        """Abre la carpeta del TXT con el archivo seleccionado. Hablar con
        el explorador de archivos puede tardar un momento: en un hilo,
        para que la ventana no se quede congelada."""
        destino = self.out_path
        if not destino:
            return
        if not Path(destino).exists():
            self.status(f"El TXT ya no está en «{Path(destino).parent}» (¿lo "
                        "moviste o lo borraste?): abro esa carpeta.", AMBER)
        self.run_in_thread(self._reveal_worker, destino)

    def _reveal_worker(self, destino):
        if not reveal_in_folder(destino):
            self.post("reveal_fail", destino)

    # ---- portapapeles -------------------------------------------------
    def _sync_copy_button(self) -> None:
        if self.parts:
            numero = self.next_part % len(self.parts) + 1
            self.btn_copy.configure(state="normal",
                                    text=f"📋 Parte {numero}/{len(self.parts)}")
        elif self.out_path:
            self.btn_copy.configure(state="normal", text="📋 Copiar")
        else:
            self.btn_copy.configure(state="disabled", text="📋 Copiar")

    def _copy(self) -> None:
        """Copia el TXT al portapapeles; si está en partes, la siguiente
        parte cada vez (para pegarlas de una en una en la IA)."""
        if self.parts:
            indice = self.next_part % len(self.parts)
            ruta = self.parts[indice]
            etiqueta = f"la parte {indice + 1} de {len(self.parts)}"
        else:
            ruta = self.out_path
            etiqueta = "el TXT"
        if not ruta:
            return
        try:
            tam = Path(ruta).stat().st_size
            if tam > CLIPBOARD_MAX:
                self.status(f"«{Path(ruta).name}» ocupa {fmt_size(tam)}: "
                            "demasiado para el portapapeles. Divídelo en "
                            "partes y cópialas de una en una.", AMBER)
                return
            texto = Path(ruta).read_bytes().decode("utf-8", "replace")
        except OSError as e:
            self.status(f"✖ No pude leer «{Path(ruta).name}» "
                        f"({e.strerror or e}).", RED)
            return
        self.clipboard_clear()
        self.clipboard_append(texto)
        siguiente = ""
        if self.parts:
            self.next_part = indice + 1
            if self.next_part < len(self.parts):
                siguiente = " Pégala y vuelve a pulsar para la siguiente."
            else:
                siguiente = " Era la última."
        self.status(f"📋 Copiado {etiqueta} ({fmt_size(tam)}).{siguiente}",
                    GREEN_T)
        self._sync_copy_button()

    def close(self):
        # Cerrar con el TXT a medias lo detiene: no se guarda nada.
        self.cancel_event.set()
        super().close()

    def _stop(self):
        if not self.running:
            return
        self.cancel_event.set()
        self.btn_stop.configure(state="disabled")
        self.status("Deteniendo…", AMBER)

    def _generate(self):
        if self.running:
            return
        proj = normalize_path(self.e_proj.get())
        if not proj or not Path(proj).is_dir():
            self.status("Indica una carpeta de proyecto que exista.", RED)
            return
        out = normalize_path(self.e_out.get())
        if not out:
            p = Path(proj)
            out = str(p.parent / (p.name + ".txt"))
            set_text(self.e_out, out)
        self.running = True
        self.cancel_event = threading.Event()
        self.pbar.set(0)
        self.btn_go.configure(state="disabled", text="Generando…")
        self.btn_stop.configure(state="normal")
        self.btn_copy.configure(state="disabled", text="📋 Copiar")
        self.status("Buscando los archivos del proyecto…", MUTED)
        self.run_in_thread(self._work, proj, out, self.app.ignore_lines(),
                           bool(self.v_ign.get()), self.cancel_event,
                           self.options())

    def options(self) -> PackOptions:
        """Las opciones elegidas en la ventana."""
        content = dict(CONTENT_CHOICES).get(self.seg_content.get(),
                                            CHANGES_NONE)
        split = dict(SPLIT_CHOICES).get(self.opt_split.get(), 0)
        return PackOptions(for_ai=bool(self.v_ai.get()),
                           redact=bool(self.v_redact.get()),
                           changes=content, split_tokens=split)

    def _work(self, proj, out, patterns, respect, cancel, options=None):
        # Nunca encima de un archivo tuyo (ni a través de un enlace): si el
        # destino ya existe y no es un TXT nuestro, se elige otro nombre.
        pedido = Path(out)
        real, renombrado = free_txt_path(pedido)

        def progreso(hechos, total, escritos):
            self.post("prog", hechos, total, escritos)
        try:
            stats = consolidate_project(proj, real, patterns,
                                        respect_ignores=respect,
                                        on_progress=progreso, cancel=cancel,
                                        options=options)
        except Cancelled:
            self.post("cancelled")
            return
        stats["renamed_from"] = str(pedido) if renombrado else ""
        self.post("ok", str(real), stats, _inside(str(real), proj), proj)

    def _reset(self):
        self.running = False
        self.btn_go.configure(state="normal", text="Generar TXT")
        self.btn_stop.configure(state="disabled")

    def handle(self, m):
        if m[0] == "reveal_fail":
            self.status(f"✖ No pude abrir la carpeta «{Path(m[1]).parent}». "
                        "Está en la ruta de arriba.", RED)
            return
        if m[0] == "prog":
            _, hechos, total, escritos = m
            if not self.running:
                return
            self.pbar.set(hechos / total if total else 0)
            self.status(f"Escribiendo el TXT… {_num(hechos)} de "
                        f"{_num(total)} archivos · {fmt_size(escritos)}",
                        MUTED)
        elif m[0] == "cancelled":
            self._reset()
            self.pbar.set(0)
            self.status("Detenido. No se ha guardado nada: si ya había un "
                        "TXT, sigue como estaba.", AMBER)
        elif m[0] == "err":
            self._reset()
            self.pbar.set(0)
            self.status("✖ " + m[1], RED)
        elif m[0] == "ok":
            _, out, st, inside, proj = m
            self._reset()
            self.pbar.set(1)
            self.out_path = out
            self.parts = list(st.get("parts") or [])
            self.next_part = 0
            partes = ""
            if self.parts:
                partes = (f"\nRepartido en {len(self.parts)} partes (en "
                          f"«{Path(self.parts[0]).parent.name}»), para "
                          "pegarlas de una en una.")
            self.status(f"✔ TXT generado: {_detalle(st)}, "
                        f"{fmt_size(st['bytes'])} (≈ "
                        f"{_num(st.get('tokens', 0))} tokens) en "
                        f"{st['seconds']:.1f} s{_avisos(st)}{partes}\n{out}",
                        GREEN_T)
            self.btn_open.configure(state="normal")
            self.btn_reveal.configure(state="normal")
            self._sync_copy_button()
            self.app.log(f"✔ Proyecto consolidado en {out} ({st['files']} "
                         f"archivos, {fmt_size(st['bytes'])}, ≈ "
                         f"{_num(st.get('tokens', 0))} tokens).", "ok")
            if inside:
                patrones = [anchor_pattern(inside)]
                if self.parts:
                    carpeta = relative_inside(str(Path(self.parts[0]).parent),
                                              proj)
                    if carpeta:
                        patrones.append(anchor_pattern(carpeta) + "/")
                self.app.add_ignore_patterns(patrones)
                self.app.log("  El TXT quedó dentro del proyecto: lo añadí "
                             "al cuadro de ignorar para que no se suba a "
                             "GitHub.", "warn")


def _detalle(st):
    """«120 archivos (110 de texto, enteros; 10 binarios mencionados)»."""
    partes = []
    if st.get("diffs"):
        partes.append(f"{_num(st['diffs'])} con sus diferencias")
    if st.get("text") or not partes:
        partes.append(f"{_num(st.get('text', 0))} de texto, enteros")
    if st.get("binaries"):
        partes.append(f"{_num(st['binaries'])} binarios mencionados")
    if st.get("deleted"):
        partes.append(f"{_num(st['deleted'])} borrados")
    return f"{_num(st['files'])} archivos ({'; '.join(partes)})"


def _avisos(st):
    extra = ""
    otros = []
    if st.get("redacted"):
        extra += f"  ·  🔒 {_num(st['redacted'])} secreto(s) tachado(s)"
    if st.get("for_ai"):
        extra += f"  ·  {_num(st['for_ai'])} omitido(s) para la IA"
    if st.get("errors"):
        otros.append(f"{_num(st['errors'])} no se pudieron leer")
    if st.get("cut"):
        otros.append(f"{_num(st['cut'])} crecieron mientras se leían")
    if otros:
        extra += "  ·  ⚠ " + ", ".join(otros) + " (está anotado en el TXT)"
    if st.get("renamed_from"):
        extra += (f"\n«{Path(st['renamed_from']).name}» ya existía y no es "
                  "un TXT de la app: no lo toqué.")
    return extra
