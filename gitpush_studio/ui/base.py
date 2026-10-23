# -*- coding: utf-8 -*-
"""Clases base de los diálogos.

    BaseDialog      título, colocación, grab, cierre.
    DecisionDialog  una pregunta con respuesta única (vista previa,
                    revisión): `finish(resultado)` llama a `on_done`
                    exactamente una vez y cierra.
    WorkerDialog    lanza trabajo en un hilo y recibe sus mensajes por
                    una cola que se vacía cada 80 ms en el hilo de la
                    interfaz (`handle(msg)`).

Ninguna función de core se llama desde aquí con widgets a mano: los
hilos solo encolan; los widgets se tocan en `handle`.
"""

import queue
import threading
import tkinter as tk

import customtkinter as ctk

from ..core.errors import StepError
from .theme import BG, MUTED
from .widgets import place_dialog

POLL_MS = 80


class BaseDialog(ctk.CTkToplevel):
    TITLE = ""
    SIZE = (580, 480)
    MIN_SIZE = (520, 420)

    def __init__(self, app, title=None, size=None, min_size=None):
        super().__init__(app)
        self.app = app
        self.fonts = app.fonts
        self.lbl_status = None
        self._token_visible = False
        self.title(title or self.TITLE)
        self.configure(fg_color=BG)
        self.transient(app)
        self.protocol("WM_DELETE_WINDOW", self.close)
        w, h = size or self.SIZE
        mw, mh = min_size or self.MIN_SIZE
        place_dialog(self, app, w, h, min_w=mw, min_h=mh)
        self.after(150, self._safe_grab)

    def _safe_grab(self):
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def close(self):
        self.destroy()

    def status(self, text, color=MUTED):
        if self.lbl_status is not None:
            self.lbl_status.configure(text=text, text_color=color)

    def toggle_token(self, entry_widget):
        self._token_visible = not self._token_visible
        entry_widget.configure(show="" if self._token_visible else "•")


class DecisionDialog(BaseDialog):
    """`on_done` recibe la respuesta una sola vez, también si el usuario
    cierra la ventana (entonces `CLOSE_RESULT`)."""
    CLOSE_RESULT = None

    def __init__(self, app, on_done, **kw):
        self.on_done = on_done
        self._answered = False
        super().__init__(app, **kw)

    def close(self):
        self.finish(self.close_result())

    def close_result(self):
        return self.CLOSE_RESULT

    def finish(self, result):
        if self._answered:
            return
        self._answered = True
        try:
            self.on_done(result)
        finally:
            self.destroy()


class WorkerDialog(BaseDialog):

    def __init__(self, app, **kw):
        self.q = queue.Queue()
        self.running = False
        super().__init__(app, **kw)
        self.after(POLL_MS, self._poll)

    # ---- desde hilos --------------------------------------------------
    def post(self, *msg):
        self.q.put(msg)

    def run_in_thread(self, fn, *args):
        """Ejecuta `fn(*args)` en un hilo; los errores llegan como
        ("err", texto)."""
        def guarded():
            try:
                fn(*args)
            except StepError as e:
                self.post("err", str(e))
            except Exception as e:                          # noqa: BLE001
                self.post("err", f"Error: {e}")
        threading.Thread(target=guarded, daemon=True).start()

    # ---- hilo de la interfaz ------------------------------------------
    def handle(self, msg):
        raise NotImplementedError

    def _poll(self):
        # Los `after` no se cancelan al destruir la ventana: si el usuario
        # la cerró con trabajo en marcha, los mensajes tardíos se ignoran.
        if not self.winfo_exists():
            return
        try:
            while True:
                self.handle(self.q.get_nowait())
        except queue.Empty:
            pass
        except tk.TclError:
            return                         # handle() cerró la ventana
        if self.winfo_exists():
            self.after(POLL_MS, self._poll)
