# -*- coding: utf-8 -*-
"""Issues y pull requests a TXT: todo lo que se habló en un repositorio
de GitHub (abiertos y cerrados), con quién escribió qué y cuándo, en un
solo archivo de texto (ver `core.ghdump`).

Con barra de progreso y «Detener». Lo que ya se descargó se guarda:
volver a pulsar sigue desde donde se quedó. Si GitHub pide esperar (la
cuota de consultas de la hora se acabó), la ventana dice hasta cuándo y
sigue sola; la espera se sigue viendo aunque otras partes avancen (por
ejemplo, GraphQL tiene su propia cuota y no para). Si algo no va como
debe (GraphQL no se pudo usar y todo va más lento), se avisa con el
porqué, en la ventana y en la consola.
"""

import threading
import time
from pathlib import Path
from tkinter import filedialog
from typing import Optional, Tuple

import customtkinter as ctk

from ...core.desktop import open_path, reveal_in_folder
from ...core.errors import Cancelled, StepError
from ...core.fs import fmt_size, normalize_path, relative_inside
from ...core.ghdump import (DumpOptions, DumpResult, PartialDownload,
                            default_name, dump_repository,
                            parse_repository, partial_download)
from ...core.ghfetch import Progress
from ...core.ignore import anchor_pattern
from ..base import WorkerDialog
from ..theme import AMBER, BLUE, GREEN_T, MUTED, RED
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
    SIZE = (760, 680)
    MIN_SIZE = (640, 420)

    def __init__(self, app):
        super().__init__(app)
        f = self.fonts
        self.out_path = ""
        self.cancel_event = threading.Event()
        self._auto_name = ""            # el nombre que propusimos nosotros
        self._partial_spec: Optional[str] = None   # lo último comprobado
        self.partial: Optional[PartialDownload] = None
        # La espera que pidió GitHub (por qué, hasta cuándo), si la hay.
        self._wait: Optional[Tuple[str, float]] = None
        self._logged_notice = ""        # el aviso ya escrito en la consola
        body, footer = self.scrolling_layout(
            "🗂  Issues y pull requests a TXT",
            "Todo lo que se habló en un repositorio de GitHub —quién abrió, "
            "quién respondió, qué y cuándo— en un solo archivo de texto.")

        c1 = panel(body, f, "📦  Repositorio de GitHub", padx=12,
                   pady=(0, 10))
        self.e_repo = entry(c1, f, "usuario/repositorio o su dirección")
        self.e_repo.pack(fill="x", padx=16, pady=(4, 0))
        self.e_repo.bind("<KeyRelease>", lambda _e: self._repo_changed())
        self.e_repo.bind("<FocusOut>", lambda _e: self._repo_changed())
        self.lbl_token = ctk.CTkLabel(c1, text="", font=f.small,
                                      text_color=MUTED, justify="left",
                                      wraplength=640)
        self.lbl_token.pack(anchor="w", padx=16, pady=(2, 8))

        c2 = panel(body, f, "💾  Archivo TXT", padx=12, pady=(0, 10))
        rowo = ctk.CTkFrame(c2, fg_color="transparent")
        rowo.pack(fill="x", padx=16, pady=(4, 12))
        rowo.grid_columnconfigure(0, weight=1)
        self.e_out = entry(rowo, f, "ruta\\del\\historial.txt")
        self.e_out.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        button(rowo, f, "Elegir…", self._browse_out, width=110
               ).grid(row=0, column=1)

        c3 = panel(body, f, "🧩  Qué incluir", padx=12, pady=(0, 4))
        self.v_commits = ctk.BooleanVar(value=True)
        self.v_mentions = ctk.BooleanVar(value=False)
        self.v_redact = ctk.BooleanVar(value=False)
        self.v_utc = ctk.BooleanVar(value=False)
        casillas = (
            (self.v_commits, "Los commits de cada pull request"),
            (self.v_mentions, "Las menciones desde otros issues y pull "
                              "requests"),
            (self.v_redact, "Tachar los secretos (tokens, contraseñas, "
                            "llaves) como «••••••••»"),
            (self.v_utc, "Horas en UTC (si no, las de este equipo, con su "
                         "diferencia con UTC)"),
        )
        for indice, (variable, texto) in enumerate(casillas):
            checkbox(c3, f, texto, variable
                     ).pack(anchor="w", padx=16,
                            pady=(4 if indice == 0 else 2, 0))
        # Solo se ve si hay una descarga a medias de ese repositorio.
        self.lbl_partial = ctk.CTkLabel(c3, text="", font=f.small,
                                        text_color=BLUE, justify="left",
                                        wraplength=640)
        self._hint_include = hint(
            c3, f,
            "Siempre entran el texto de cada issue y pull request, los "
            "comentarios, las revisiones, los comentarios en el código (en "
            "hilo) y los eventos. GitHub no dice quién puso cada reacción "
            "(solo cuántas) ni guarda lo borrado. Commits y menciones "
            "cuestan consultas: con token van por lotes, junto con las "
            "revisiones; sin token, una consulta por cada pull request o "
            "issue.",
            wraplength=640, pady=(6, 10))

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
        self._repo_changed()
        if self.app.connection_token():
            self.lbl_token.configure(
                text="🔑 Se usará tu token: GitHub permite 5.000 consultas "
                     "por hora, y con él todo se descarga a la vez por "
                     "varias conexiones (tantas como aguante GitHub) y las "
                     "revisiones, los commits y las menciones van por "
                     "lotes (un repositorio con 2.000 pull requests: unas "
                     "decenas de consultas para eso, en vez de miles).",
                text_color=MUTED)
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

    def _repo_changed(self) -> None:
        self._suggest_output()
        self._check_partial()

    def _check_partial(self) -> None:
        """Si hay una descarga a medias de ese repositorio, lo dice y
        marca las opciones con las que se hizo: solo con ESAS sigue desde
        donde iba (con otras, empezaría de cero). Solo lee el almacén."""
        spec = self.e_repo.get().strip()
        if spec == self._partial_spec:
            return
        self._partial_spec = spec
        self.partial = partial_download(spec)
        if self.partial is None:
            self.lbl_partial.configure(text="")
            self.lbl_partial.pack_forget()
            return
        self.v_commits.set(self.partial.commits)
        self.v_mentions.set(self.partial.mentions)
        nota = self.partial.upgrade_note()
        self.lbl_partial.configure(
            text=f"↻ Hay una descarga a medias de {self.partial.repository} "
                 f"({_num(self.partial.items)} issues y PR ya guardados), "
                 f"{self.partial.describe()}: esas opciones quedan marcadas "
                 "para seguir desde donde iba. Con otras, empezaría de cero."
                 + (f"\n{nota}" if nota else ""))
        self.lbl_partial.pack(anchor="w", padx=16, pady=(8, 0),
                              before=self._hint_include)

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
        self._wait = None
        self._logged_notice = ""
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
            self._wait = (why, until)
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

    def _wait_line(self) -> str:
        """La espera que pidió GitHub, mientras dure (vacío si no hay)."""
        if self._wait is None:
            return ""
        why, until = self._wait
        if time.time() >= until:
            self._wait = None
            return ""
        hasta = time.strftime("%H:%M", time.localtime(until))
        if hasta in why:
            return f"⏳ {why}"          # ya dice hasta cuándo
        return f"⏳ {why} (hasta las {hasta})."

    def _log_notice(self, notice: str) -> None:
        """Escribe el aviso en la consola (una vez por aviso)."""
        if notice and notice != self._logged_notice:
            self._logged_notice = notice
            self.app.log("⚠ " + notice, "warn")

    def _show_progress(self, p: Progress) -> None:
        if not self.running:
            return
        self.pbar.set(p.fraction * 0.95)
        lineas = [f"{p.stage}… {_num(p.requests)} consultas de unas "
                  f"{_num(p.estimate)}"]
        detalle = _counts(p.counts)
        if detalle:
            lineas.append(detalle)
        espera = self._wait_line()
        if espera:
            lineas.append(espera)
        if p.notice:
            lineas.append("⚠ " + p.notice)
            self._log_notice(p.notice)
        llamativo = bool(espera or p.notice)
        self.status("\n".join(lineas), AMBER if llamativo else MUTED)

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
        aviso = f"\n⚠ {r.notice}" if r.notice else ""
        self.status(f"✔ {r.repository}: {_num(r.issues)} issues y "
                    f"{_num(r.pulls)} pull requests, {fmt_size(r.bytes)} en "
                    f"{r.seconds:.1f} s con {_num(r.requests)} consultas"
                    f"{notas}.\n{_counts(r.counts)}\n{r.path}{aviso}",
                    GREEN_T)
        self.app.log(f"🗂 Historial de {r.repository} en {r.path} "
                     f"({_num(r.items)} issues y pull requests, "
                     f"{fmt_size(r.bytes)}).", "ok")
        self._log_notice(r.notice)
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
