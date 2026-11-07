# -*- coding: utf-8 -*-
"""Issues y pull requests a TXT: todo lo que se habló en un repositorio
de GitHub (abiertos y cerrados), con quién escribió qué y cuándo, en un
solo archivo de texto (ver `core.ghdump`).

Con barra de progreso y «Detener». Lo que ya se descargó se guarda:
volver a pulsar sigue desde donde se quedó. Si GitHub pide esperar (la
cuota de consultas de la hora se acabó), la ventana dice hasta cuándo y
sigue sola.
"""

import threading
import time
from pathlib import Path
from tkinter import filedialog
from typing import Optional

import customtkinter as ctk

from ...core.desktop import open_path, reveal_in_folder
from ...core.errors import Cancelled, StepError
from ...core.fs import fmt_size, normalize_path, relative_inside
from ...core.ghdump import (DumpOptions, DumpResult, default_name,
                            dump_repository, parse_repository)
from ...core.ghfetch import Progress
from ...core.ignore import anchor_pattern
from ..base import WorkerDialog
from ..theme import AMBER, BLUE, GREEN_T, MUTED, RED, TEXT
from ..widgets import (button, checkbox, entry, hint, panel, progress_bar,
                       set_text)

COUNT_NAMES = (("items", "issues y PR"), ("comment", "comentarios"),
               ("review", "revisiones"), ("code", "en el código"),
               ("commit", "commits"), ("event", "eventos"),
               ("mention", "menciones"))


def _num(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _counts(counts: dict) -> str:
    partes = [f"{_num(counts[k])} {nombre}" for k, nombre in COUNT_NAMES
              if counts.get(k)]
    return " · ".join(partes)


class IssuesTxtDialog(WorkerDialog):
    TITLE = "Issues y pull requests a TXT"
    SIZE = (760, 720)
    MIN_SIZE = (660, 560)

    def __init__(self, app):
        super().__init__(app)
        f = self.fonts
        self.out_path = ""
        self.cancel_event = threading.Event()
        self._auto_name = ""            # el nombre que propusimos nosotros
        pad = {"padx": 22}

        ctk.CTkLabel(self, text="🗂  Issues y pull requests a TXT",
                     font=f.title, text_color=TEXT
                     ).pack(anchor="w", pady=(18, 0), **pad)
        ctk.CTkLabel(self, text="Todo lo que se habló en un repositorio de "
                                "GitHub —quién abrió, quién respondió, qué y "
                                "cuándo— en un solo archivo de texto.",
                     font=f.small, text_color=MUTED, justify="left",
                     wraplength=680).pack(anchor="w", pady=(2, 12), **pad)

        c1 = panel(self, f, "📦  Repositorio de GitHub")
        self.e_repo = entry(c1, f, "usuario/repositorio o su dirección")
        self.e_repo.pack(fill="x", padx=16, pady=(6, 0))
        self.e_repo.bind("<KeyRelease>", lambda _e: self._suggest_output())
        self.e_repo.bind("<FocusOut>", lambda _e: self._suggest_output())
        self.lbl_token = ctk.CTkLabel(c1, text="", font=f.small,
                                      text_color=MUTED, justify="left",
                                      wraplength=680)
        self.lbl_token.pack(anchor="w", padx=16, pady=(6, 12))

        c2 = panel(self, f, "💾  Archivo TXT", pady=(12, 0))
        rowo = ctk.CTkFrame(c2, fg_color="transparent")
        rowo.pack(fill="x", padx=16, pady=(6, 12))
        rowo.grid_columnconfigure(0, weight=1)
        self.e_out = entry(rowo, f, "ruta\\del\\historial.txt")
        self.e_out.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        button(rowo, f, "Elegir…", self._browse_out, width=110
               ).grid(row=0, column=1)

        c3 = panel(self, f, "🧩  Qué incluir", pady=(12, 0))
        self.v_commits = ctk.BooleanVar(value=True)
        checkbox(c3, f, "Los commits de cada pull request (una consulta más "
                        "por cada uno)", self.v_commits
                 ).pack(anchor="w", padx=16, pady=(6, 0))
        self.v_mentions = ctk.BooleanVar(value=False)
        checkbox(c3, f, "Las menciones desde otros issues y pull requests "
                        "(una consulta más por cada uno)", self.v_mentions
                 ).pack(anchor="w", padx=16, pady=(6, 0))
        self.v_redact = ctk.BooleanVar(value=False)
        checkbox(c3, f, "Tachar los secretos (tokens, contraseñas, llaves) "
                        "como «••••••••»", self.v_redact
                 ).pack(anchor="w", padx=16, pady=(6, 0))
        self.v_utc = ctk.BooleanVar(value=False)
        checkbox(c3, f, "Horas en UTC (si no, las de este equipo, con su "
                        "diferencia con UTC)", self.v_utc
                 ).pack(anchor="w", padx=16, pady=(6, 0))
        hint(c3, f, "Siempre entran el texto de cada issue y pull request, "
                    "los comentarios, las revisiones, los comentarios en el "
                    "código (en hilo, con su trozo de código) y los eventos "
                    "(cerrado, fusionado, etiquetas, asignaciones…). GitHub "
                    "no dice quién puso cada reacción (solo cuántas) ni "
                    "guarda lo borrado.", wraplength=680, pady=(6, 12))

        self.pbar = progress_bar(self)
        self.pbar.pack(fill="x", pady=(14, 0), **pad)
        self.lbl_status = ctk.CTkLabel(self, text="", font=f.small,
                                       text_color=MUTED, justify="left",
                                       wraplength=680)
        self.lbl_status.pack(anchor="w", pady=(8, 0), **pad)

        rowb = ctk.CTkFrame(self, fg_color="transparent")
        rowb.pack(fill="x", pady=(10, 18), **pad)
        rowb.grid_columnconfigure(0, weight=1)
        self.btn_go = button(rowb, f, "Descargar y generar TXT", self._start,
                             kind="primary", height=42)
        self.btn_go.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.btn_stop = button(rowb, f, "Detener", self._stop, kind="ghost",
                               width=96, height=42, text_color=AMBER,
                               state="disabled")
        self.btn_stop.grid(row=0, column=1, padx=(0, 10))
        self.btn_open = button(rowb, f, "Abrir TXT",
                               lambda: open_path(self.out_path),
                               kind="ghost", width=110, height=42,
                               text_color=BLUE, state="disabled")
        self.btn_open.grid(row=0, column=2, padx=(0, 10))
        self.btn_reveal = button(rowb, f, "📂  Abrir ubicación", self._reveal,
                                 kind="ghost", width=150, height=42,
                                 text_color=BLUE, state="disabled")
        self.btn_reveal.grid(row=0, column=3, padx=(0, 10))
        button(rowb, f, "Cerrar", self.close, kind="ghost", width=90,
               height=42).grid(row=0, column=4)

        self._prefill()

    # ---- valores iniciales --------------------------------------------
    def _prefill(self) -> None:
        url = self.app.e_url.get().strip()
        try:
            owner, repo = parse_repository(url)
            set_text(self.e_repo, f"{owner}/{repo}")
        except StepError:
            pass
        self._suggest_output()
        if self.app.connection_token():
            self.lbl_token.configure(
                text="🔑 Se usará tu token: GitHub permite 5.000 consultas "
                     "por hora (un repositorio con 1.000 pull requests "
                     "necesita unas 2.000).", text_color=MUTED)
        else:
            self.lbl_token.configure(
                text="⚠ Sin token, GitHub permite 60 consultas por hora: "
                     "solo da para repositorios pequeños, y los privados no "
                     "se ven. Guarda tu token en Opciones.", text_color=AMBER)

    def _output_folder(self) -> Path:
        """Junto a la carpeta del proyecto (nunca dentro: el TXT puede
        llevar lo que alguien pegó en un issue); si no hay proyecto, la
        carpeta personal."""
        proj = normalize_path(self.app.e_folder.get())
        if proj and Path(proj).is_dir():
            return Path(proj).resolve().parent
        return Path.home()

    def _suggest_output(self) -> None:
        """Propone el nombre a partir del repositorio, salvo que el
        usuario ya haya elegido otro."""
        try:
            _owner, repo = parse_repository(self.e_repo.get())
        except StepError:
            return
        actual = self.e_out.get().strip()
        if actual and actual != self._auto_name:
            return
        propuesto = str(self._output_folder() / default_name(repo))
        self._auto_name = propuesto
        set_text(self.e_out, propuesto)

    def _browse_out(self) -> None:
        actual = Path(normalize_path(self.e_out.get()) or
                      str(self._output_folder() / "historial.txt"))
        chosen = filedialog.asksaveasfilename(
            title="¿Dónde guardar el historial?", defaultextension=".txt",
            initialfile=actual.name,
            initialdir=str(actual.parent if actual.parent.is_dir()
                           else Path.home()),
            filetypes=[("Archivo de texto", "*.txt")])
        if chosen:
            set_text(self.e_out, str(Path(chosen)))

    def options(self) -> DumpOptions:
        return DumpOptions(commits=bool(self.v_commits.get()),
                           mentions=bool(self.v_mentions.get()),
                           redact=bool(self.v_redact.get()),
                           utc=bool(self.v_utc.get()))

    # ---- en marcha ----------------------------------------------------
    def _start(self) -> None:
        if self.running:
            return
        spec = self.e_repo.get().strip()
        try:
            _owner, repo = parse_repository(spec)
        except StepError as e:
            self.status(str(e), RED)
            return
        out = normalize_path(self.e_out.get())
        if not out:
            out = str(self._output_folder() / default_name(repo))
            set_text(self.e_out, out)
        self.running = True
        self.cancel_event = threading.Event()
        self.pbar.set(0)
        self.btn_go.configure(state="disabled", text="Descargando…")
        self.btn_stop.configure(state="normal")
        self.status("Consultando GitHub…", MUTED)
        self.run_in_thread(self._work, spec, out,
                           self.app.connection_token(), self.options(),
                           self.cancel_event)

    def _work(self, spec: str, out: str, token: str, options: DumpOptions,
              cancel: threading.Event) -> None:
        try:
            result = dump_repository(
                spec, out, token=token, options=options, cancel=cancel,
                on_progress=lambda p: self.post("progress", p),
                on_wait=lambda why, until: self.post("wait", why, until),
                on_write=lambda done, total: self.post("write", done, total))
        except Cancelled:
            self.post("cancelled")
            return
        self.post("ok", result)

    def _stop(self) -> None:
        if not self.running:
            return
        self.cancel_event.set()
        self.btn_stop.configure(state="disabled")
        self.status("Deteniendo…", AMBER)

    def close(self) -> None:
        # Cerrar a mitad lo detiene: lo descargado se guarda igual.
        self.cancel_event.set()
        super().close()

    def _reveal(self) -> None:
        if self.out_path:
            self.run_in_thread(self._reveal_worker, self.out_path)

    def _reveal_worker(self, destino: str) -> None:
        if not reveal_in_folder(destino):
            self.post("reveal_fail", destino)

    def _reset(self) -> None:
        self.running = False
        self.btn_go.configure(state="normal", text="Descargar y generar TXT")
        self.btn_stop.configure(state="disabled")

    # ---- mensajes del hilo ----------------------------------------------
    def handle(self, m) -> None:
        kind = m[0]
        if kind == "progress":
            self._show_progress(m[1])
        elif kind == "wait":
            _, why, until = m
            hasta = time.strftime("%H:%M", time.localtime(until))
            self.status(f"⏳ {why} (hasta las {hasta}; puedes dejar la "
                        "ventana abierta o pulsar «Detener»: lo descargado "
                        "se guarda).", AMBER)
        elif kind == "write":
            _, done, total = m
            if self.running:
                self.pbar.set(done / total if total else 1)
                self.status(f"Escribiendo el TXT… {_num(done)} de "
                            f"{_num(total)} issues y pull requests", MUTED)
        elif kind == "cancelled":
            self._reset()
            self.status("Detenido. Lo descargado se guardó: vuelve a pulsar "
                        "y sigue desde ahí. Si ya había un historial, sigue "
                        "como estaba.", AMBER)
        elif kind == "err":
            self._reset()
            self.pbar.set(0)
            self.status("✖ " + m[1], RED)
        elif kind == "ok":
            self._done(m[1])
        elif kind == "reveal_fail":
            self.status(f"✖ No pude abrir la carpeta «{Path(m[1]).parent}». "
                        "Está en la ruta de arriba.", RED)

    def _show_progress(self, p: Progress) -> None:
        if not self.running:
            return
        self.pbar.set(p.fraction * 0.95)
        detalle = _counts(p.counts)
        self.status(f"{p.stage}… {_num(p.requests)} consultas de unas "
                    f"{_num(p.estimate)}" + (f"\n{detalle}" if detalle
                                              else ""), MUTED)

    def _done(self, r: DumpResult) -> None:
        self._reset()
        self.pbar.set(1)
        self.out_path = r.path
        self.btn_open.configure(state="normal")
        self.btn_reveal.configure(state="normal")
        extra = []
        if r.resumed:
            extra.append("siguió la descarga anterior")
        if r.renamed:
            extra.append("ya había un archivo tuyo con ese nombre: no lo "
                         "toqué")
        if self.v_redact.get():
            extra.append(f"🔒 {_num(r.redacted)} secreto(s) tachado(s)")
        notas = f" ({'; '.join(extra)})" if extra else ""
        self.status(f"✔ {r.repository}: {_num(r.issues)} issues y "
                    f"{_num(r.pulls)} pull requests, {fmt_size(r.bytes)} en "
                    f"{r.seconds:.1f} s con {_num(r.requests)} consultas"
                    f"{notas}.\n{_counts(r.counts)}\n{r.path}", GREEN_T)
        self.app.log(f"🗂 Historial de {r.repository} en {r.path} "
                     f"({_num(r.items)} issues y pull requests, "
                     f"{fmt_size(r.bytes)}).", "ok")
        self._keep_out_of_project(r.path)

    def _keep_out_of_project(self, path: str) -> Optional[str]:
        """Si quedó dentro de la carpeta del proyecto, que nunca se
        publique: va al cuadro de ignorar (lo que alguien pegó en un
        issue no tiene por qué acabar en el repositorio)."""
        proj = normalize_path(self.app.e_folder.get())
        if not proj or not Path(proj).is_dir():
            return None
        dentro = relative_inside(path, proj)
        if not dentro:
            return None
        self.app.add_ignore_patterns([anchor_pattern(dentro)])
        self.app.log("  El historial quedó dentro del proyecto: lo añadí "
                     "al cuadro de ignorar para que no se suba a GitHub.",
                     "warn")
        return dentro
