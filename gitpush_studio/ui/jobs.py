# -*- coding: utf-8 -*-
"""Publicación desde la interfaz: uno o varios proyectos a la vez.

`PublishController` es el único puente entre `core.publish` y los
widgets. Los hilos de trabajo solo encolan mensajes; el hilo de la
interfaz los consume en `handle`. Cuando un trabajo necesita una
decisión humana (revisión de seguridad, vista previa) su hilo se queda
esperando en un `Decision`; las decisiones se muestran DE UNA EN UNA,
en orden de llegada, mientras los demás trabajos siguen avanzando
hasta su propio punto de decisión.
"""

import threading
from collections import deque

import customtkinter as ctk

from ..core.ignore import anchor_pattern
from ..core.publish import Callbacks, PublishResult, publish_many
from .dialogs.preview import PreviewDialog
from .dialogs.review import ReviewDialog
from .theme import AMBER, BORDER, GREEN_T, MUTED, PANEL, RED, TEXT
from .widgets import button, progress_bar, shorten

STATUS_TEXT = {"ok": ("✔ publicado", GREEN_T), "error": ("✖ error", RED),
               "cancelled": ("✋ cancelado", AMBER),
               "blocked": ("🛡 bloqueado", AMBER)}


def first_line(text):
    return (text or "").splitlines()[0] if (text or "").strip() else ""


class Decision:
    """Una pregunta pendiente de respuesta humana."""
    __slots__ = ("kind", "job", "payload", "result", "_event")

    def __init__(self, kind, job, payload):
        self.kind = kind            # "review" | "preview"
        self.job = job
        self.payload = payload
        self.result = None
        self._event = threading.Event()

    def wait(self):
        self._event.wait()
        return self.result

    def resolve(self, result):
        self.result = result
        self._event.set()

    def cancel_result(self):
        return ("cancel", []) if self.kind == "review" else False


class JobRow:
    """Fila del panel: nombre · barra · estado."""

    def __init__(self, parent, fonts, job):
        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        self.frame.pack(fill="x", padx=12, pady=(0, 6))
        self.frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self.frame, text=shorten(job.label, 28), font=fonts.body,
                     text_color=TEXT, anchor="w", width=190
                     ).grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.bar = progress_bar(self.frame)
        self.bar.grid(row=0, column=1, sticky="ew")
        self._done = 0.0
        self.lbl = ctk.CTkLabel(self.frame, text="en cola", font=fonts.small,
                                text_color=MUTED, anchor="w", width=250)
        self.lbl.grid(row=0, column=2, sticky="w", padx=(10, 0))

    def update(self, n, total, label):
        self._done = n / max(1, total)
        self.bar.set(self._done)
        self.lbl.configure(text=f"[{n}/{total}] {shorten(label, 34)}",
                           text_color=MUTED)

    def finish(self, result):
        text, color = STATUS_TEXT.get(result.status, ("?", MUTED))
        self.bar.set(1 if result.ok else self._done)
        detail = result.web_url if result.ok else first_line(result.message)
        self.lbl.configure(text=f"{text}  {shorten(detail, 40)}",
                           text_color=color)


class QueuePanel(ctk.CTkFrame):
    """Cola de publicación: una fila por proyecto. Oculto si no hay
    trabajos; el propio usuario lo cierra al terminar."""

    def __init__(self, parent, fonts, on_cancel):
        super().__init__(parent, fg_color=PANEL, corner_radius=12,
                         border_width=1, border_color=BORDER)
        self.fonts = fonts
        self.rows = {}
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=14, pady=(10, 6))
        bar.grid_columnconfigure(0, weight=1)
        self.lbl_title = ctk.CTkLabel(bar, text="COLA DE PUBLICACIÓN",
                                      font=fonts.small, text_color=MUTED)
        self.lbl_title.grid(row=0, column=0, sticky="w")
        self.btn_cancel = button(bar, fonts, "Detener", on_cancel,
                                 kind="ghost", width=90, height=24,
                                 text_color=AMBER, font=fonts.small)
        self.btn_cancel.grid(row=0, column=1, padx=(0, 6))
        self.btn_close = button(bar, fonts, "Cerrar", self.hide, kind="ghost",
                                width=70, height=24, font=fonts.small,
                                state="disabled")
        self.btn_close.grid(row=0, column=2)
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="x", pady=(0, 8))
        self._grid_kw = {}

    def show(self, jobs, **grid_kw):
        for w in self.body.winfo_children():
            w.destroy()
        self.rows = {id(j): JobRow(self.body, self.fonts, j) for j in jobs}
        self.lbl_title.configure(text=f"COLA DE PUBLICACIÓN · {len(jobs)} "
                                      "proyectos")
        self.btn_cancel.configure(state="normal")
        self.btn_close.configure(state="disabled")
        if grid_kw:
            self._grid_kw = grid_kw
        self.grid(**self._grid_kw)

    def hide(self):
        self.grid_remove()

    def update_job(self, job, n, total, label):
        # No llamarlo `update`: tkinter.Misc.update() ya existe y se
        # invoca sin argumentos desde dentro de tkinter.
        row = self.rows.get(id(job))
        if row:
            row.update(n, total, label)

    def finish(self, results):
        for r in results:
            row = self.rows.get(id(r.job))
            if row:
                row.finish(r)
        self.btn_cancel.configure(state="disabled")
        self.btn_close.configure(state="normal")


class PublishController:

    def __init__(self, app, panel):
        self.app = app
        self.q = app.q
        self.panel = panel
        self.running = False
        self.many = False
        self.jobs = []
        self.on_finished = None
        self.cancel_event = threading.Event()
        self._pending = deque()
        self._dialog = None

    # ---- arranque / parada (hilo de la interfaz) -----------------------
    def start(self, jobs, on_finished, workers=3):
        if self.running:
            return False
        self.jobs = list(jobs)
        if not self.jobs:
            return False
        self.on_finished = on_finished
        self.cancel_event = threading.Event()
        self._pending.clear()
        self._dialog = None
        self.many = len(self.jobs) > 1
        self.running = True
        if self.many:
            self.panel.show(self.jobs)
        threading.Thread(target=self._run, args=(workers,),
                         daemon=True).start()
        return True

    def _run(self, workers):
        try:
            results = publish_many(self.jobs, self._callbacks_for,
                                   workers=workers, cancel=self.cancel_event)
        except Exception as e:                                  # noqa: BLE001
            results = [PublishResult(j, "error", f"Error inesperado: {e}")
                       for j in self.jobs]
        self.q.put(("jobs_done", results))

    def cancel(self):
        """Cooperativo: el paso en curso termina, no se empieza otro y
        las preguntas pendientes se responden «cancelar»."""
        if not self.running:
            return
        self.cancel_event.set()
        while self._pending:
            d = self._pending.popleft()
            d.resolve(d.cancel_result())
        if self._dialog is not None and self._dialog.winfo_exists():
            self._dialog.close()
        self.app.log("✋ Deteniendo… lo que ya está en marcha se completa; "
                     "no se empieza nada nuevo.", "warn")

    # ---- callbacks (hilos de trabajo) ----------------------------------
    def _callbacks_for(self, job):
        prefix = f"[{job.label}] " if self.many else ""

        def log(text, tag="out"):
            self.q.put(("job_log", prefix + text, tag))

        def progress(n, total, label):
            self.q.put(("job_progress", job, n, total, label))

        def ask_review(findings):
            return self._ask(Decision("review", job, findings))

        def ask_preview(added, modified, deleted, mode):
            return self._ask(Decision("preview", job,
                                      (added, modified, deleted, mode)))

        return Callbacks(log=log, progress=progress, ask_review=ask_review,
                         ask_preview=ask_preview)

    def _ask(self, decision):
        if self.cancel_event.is_set():
            return decision.cancel_result()
        self.q.put(("decision", decision))
        return decision.wait()

    # ---- mensajes (hilo de la interfaz) --------------------------------
    def handle(self, msg):
        """Devuelve True si el mensaje era suyo."""
        kind = msg[0]
        if kind == "job_log":
            self.app.log(msg[1], msg[2])
        elif kind == "job_progress":
            _, job, n, total, label = msg
            if self.many:
                self.panel.update_job(job, n, total, label)
            else:
                self.app.progress.set(n / max(1, total))
        elif kind == "decision":
            self._pending.append(msg[1])
            self._show_next()
        elif kind == "jobs_done":
            self._finish(msg[1])
        else:
            return False
        return True

    def _show_next(self):
        if self._dialog is not None and self._dialog.winfo_exists():
            return
        self._dialog = None
        while self._pending:
            d = self._pending.popleft()
            if self.cancel_event.is_set():
                d.resolve(d.cancel_result())
                continue
            label = d.job.label if self.many else ""
            done = lambda res, d=d: self._answered(d, res)     # noqa: E731
            if d.kind == "review":
                self._dialog = ReviewDialog(self.app, d.payload,
                                            mode="publish", on_done=done,
                                            label=label)
            else:
                added, modified, deleted, mode = d.payload
                self._dialog = PreviewDialog(self.app, added, modified,
                                             deleted, mode, on_done=done,
                                             label=label)
            return

    def _answered(self, decision, result):
        decision.resolve(result)
        self._dialog = None
        if decision.kind == "review" and result[0] == "ignore" and result[1]:
            # Las mismas líneas que el pipeline escribe en el .gitignore.
            self.app.on_review_ignored(
                decision.job, [anchor_pattern(p) for p in result[1]])
        self.app.after(50, self._show_next)

    def _finish(self, results):
        self.running = False
        while self._pending:                      # por si acaso
            d = self._pending.popleft()
            d.resolve(d.cancel_result())
        if self.many:
            self.panel.finish(results)
        if self.on_finished:
            self.on_finished(results)
