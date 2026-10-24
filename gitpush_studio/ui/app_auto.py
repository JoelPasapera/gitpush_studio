# -*- coding: utf-8 -*-
"""Auto-publicación: comprobar la carpeta cada cierto tiempo y publicar
solo cuando hay cambios que ya dejaron de moverse.

Mezcla de `GitPushStudio`: usa sus widgets y su estado (`self.…`);
no tiene constructor propio."""

import threading
import time
from pathlib import Path
from tkinter import messagebox

from .. import APP_NAME
from ..core.errors import StepError
from ..core.fs import normalize_path
from ..core.git import Git
from ..core.walk import folder_fingerprint
from .theme import BADGE_GREEN_BG, GREEN_T, MUTED

SETTLE_SECONDS = 5          # espera para que los cambios se asienten


class AutoPublishMixin:

    def _interval_minutes(self):
        try:
            return int(self.om_interval.get().split()[0])
        except (ValueError, IndexError):
            return 10

    def _interval_changed(self, _value=None):
        if self.v_auto.get():
            self._schedule_auto()
            self.log(f"🤖 Intervalo cambiado: cada "
                     f"{self._interval_minutes()} min.", "dim")

    def toggle_auto(self):
        if self.v_auto.get():
            folder = normalize_path(self.e_folder.get())
            if not folder or not Path(folder).is_dir():
                self.v_auto.set(False)
                messagebox.showwarning(APP_NAME, "Indica primero una carpeta "
                                                 "que exista.")
                return
            self.auto_pill.configure(fg_color=BADGE_GREEN_BG,
                                     text_color=GREEN_T)
            self.log(f"🤖 Auto-publicación activada: comprobaré la carpeta "
                     f"cada {self._interval_minutes()} min.", "ok")
            self._last_pub_fp = None
            patterns = self.ignore_lines()
            if not Git().is_repo(folder):
                # Sin repositorio aún: huella de partida, para publicar
                # solo cuando algo cambie a partir de ahora.
                threading.Thread(target=self._initial_fingerprint,
                                 args=(folder, patterns), daemon=True).start()
            self._schedule_auto()
        else:
            self._cancel_auto()
            self.auto_pill.configure(fg_color="transparent", text_color=MUTED)
            self.log("🤖 Auto-publicación desactivada.", "dim")

    def _initial_fingerprint(self, folder, patterns):
        """Hilo: huella de partida. Un fallo aquí no puede morir en
        silencio (la vigilancia seguiría «activa» sin vigilar nada)."""
        try:
            huella = folder_fingerprint(folder, patterns)
        except Exception as e:                              # noqa: BLE001
            huella = None
            self.q.put(("log", f"🤖 No pude leer la carpeta para la huella "
                               f"inicial ({e}).", "warn"))
        self.q.put(("set_fp", huella))

    def _schedule_auto(self):
        self._cancel_auto()
        self._auto_job = self.after(self._interval_minutes() * 60 * 1000,
                                    self._auto_tick)

    def _cancel_auto(self):
        if self._auto_job is not None:
            try:
                self.after_cancel(self._auto_job)
            except Exception:
                pass
            self._auto_job = None

    def _auto_tick(self):
        self._auto_job = None
        if not self.v_auto.get():
            return
        if self.controller.running or self.scanning:
            self._schedule_auto()
            return
        threading.Thread(
            target=self._auto_check,
            args=(normalize_path(self.e_folder.get()), self.ignore_lines(),
                  self._is_addedit(), self._last_pub_fp),
            daemon=True).start()

    def _auto_check(self, folder, patterns, addedit, last_fp):
        """Hilo: ¿hay cambios, y ya se asentaron? Con repositorio usa
        `git status`; antes de la primera publicación, la huella."""
        if not Path(folder).is_dir():
            self.q.put(("auto_result", "missing", None))
            return
        try:
            git = Git()
            if git.is_repo(folder):
                def probe():
                    return git.pending_changes(folder,
                                               ignore_deletions=addedit)
                fp_mode = False
            else:
                def probe():
                    return folder_fingerprint(folder, patterns)
                fp_mode = True
            first = probe()
            idle = (last_fp is not None and first == last_fp) if fp_mode \
                else not first
            if idle:
                self.q.put(("auto_result", "idle", None))
                return
            time.sleep(SETTLE_SECONDS)
            if probe() != first:
                self.q.put(("auto_result", "unstable", None))
            else:
                self.q.put(("auto_result", "publish",
                            first if fp_mode else None))
        except StepError as e:
            self.q.put(("auto_result", "error", str(e)))
        except Exception as e:                              # noqa: BLE001
            # Cualquier otro fallo (un nombre de archivo imposible, un
            # disco que desaparece) también se informa y se reprograma:
            # si el hilo muriera sin avisar, la vigilancia se pararía
            # sin que nadie lo supiera.
            self.q.put(("auto_result", "error",
                        f"{type(e).__name__}: {e}"))

    def _handle_auto_result(self, action, payload):
        if not self.v_auto.get():
            return
        if action == "missing":
            self.log("🤖 La carpeta ya no existe; auto-publicación "
                     "desactivada.", "warn")
            self.v_auto.set(False)
            self.toggle_auto()
            return
        if action == "publish" and not self.controller.running \
                and not self.scanning:
            self.log("🤖 Cambios detectados — publicando automáticamente…",
                     "out")
            self._auto_fp = payload
            self.start_publish(auto=True)
            return                      # se reprograma al terminar
        if action == "unstable":
            self.log("🤖 Aún estás guardando cambios; lo intento en la "
                     "próxima ronda.", "dim")
        elif action == "error":
            self.log(f"🤖 No pude comprobar la carpeta: {payload}", "warn")
        self._schedule_auto()

    def _auto_abort(self, reason):
        self.log(f"🤖 {reason} Auto-publicación en pausa.", "warn")
        if self.v_auto.get():
            self.v_auto.set(False)
            self._cancel_auto()
            self.auto_pill.configure(fg_color="transparent", text_color=MUTED)
