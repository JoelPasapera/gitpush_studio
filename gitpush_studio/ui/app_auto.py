# -*- coding: utf-8 -*-
"""Auto-publicación: publicar solo cuando hay cambios que ya dejaron de
moverse.

Donde el sistema lo permite (Windows y Linux) la carpeta se vigila en
tiempo real (`core.watch`): un cambio provoca una comprobación pocos
segundos después, sin recorrer la carpeta cada X minutos. La
comprobación periódica sigue funcionando como red de seguridad (y es la
única en los sistemas sin vigilante).

Mezcla de `GitPushStudio`: usa sus widgets y su estado (`self.…`);
no tiene constructor propio."""

import threading
import time
from pathlib import Path
from tkinter import messagebox

from .. import APP_NAME
from ..core import gitspeed
from ..core.errors import StepError
from ..core.fs import folder_key, normalize_path
from ..core.git import Git
from ..core.walk import folder_fingerprint
from ..core.watch import start_watcher
from .theme import BADGE_GREEN_BG, GREEN_T, MUTED

SETTLE_SECONDS = 5          # espera para que los cambios se asienten
# Tras el último aviso del vigilante se espera esto antes de comprobar:
# guardar un proyecto toca muchos archivos seguidos.
DEBOUNCE_MS = 4000


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
            self._auto_folder = folder_key(folder)
            self._start_watching(folder)
            self._tray_on()
            if self._watcher is not None:
                self.log(f"🤖 Auto-publicación activada: vigilo la carpeta en "
                         f"tiempo real (y la compruebo además cada "
                         f"{self._interval_minutes()} min).", "ok")
            else:
                self.log(f"🤖 Auto-publicación activada: comprobaré la "
                         f"carpeta cada {self._interval_minutes()} min.",
                         "ok")
            self._last_pub_fp = None
            patterns = self.ignore_lines()
            if not Git().is_repo(folder):
                # Sin repositorio aún: huella de partida, para publicar
                # solo cuando algo cambie a partir de ahora.
                threading.Thread(target=self._initial_fingerprint,
                                 args=(folder, patterns), daemon=True).start()
            self._schedule_auto()
        else:
            self._auto_folder = ""
            self._cancel_auto()
            self._stop_watching()
            self._tray_off()
            self.auto_pill.configure(fg_color="transparent", text_color=MUTED)
            self.log("🤖 Auto-publicación desactivada.", "dim")

    # ---- la carpeta vigilada ----------------------------------------------
    def _watching_other_folder(self):
        """¿Dice el formulario otra carpeta que la que se activó?"""
        return bool(self._auto_folder) and \
            folder_key(self.e_folder.get()) != self._auto_folder

    def pause_auto_if_folder_changed(self):
        """Cambiar la carpeta del formulario pone en pausa la
        auto-publicación: nunca pasa sola a vigilar y publicar la carpeta
        nueva en el repositorio de la anterior."""
        if not self.v_auto.get() or not self._watching_other_folder():
            return False
        self.log("🤖 Cambiaste la carpeta del proyecto: la auto-publicación "
                 "vigilaba otra. Actívala de nuevo para vigilar esta.",
                 "warn")
        self.v_auto.set(False)
        self.toggle_auto()
        return True

    # ---- vigilancia en tiempo real ----------------------------------------
    def _start_watching(self, folder):
        self._stop_watching()
        self._watch_flag.clear()
        self._watcher = start_watcher(folder, self._on_folder_event)

    def _stop_watching(self):
        watcher, self._watcher = self._watcher, None
        if watcher is not None:
            watcher.stop()
        if self._watch_job is not None:
            try:
                self.after_cancel(self._watch_job)
            except Exception:
                pass
            self._watch_job = None

    def _on_folder_event(self, _rel):
        """Hilo del vigilante: solo avisa (una vez hasta que se atienda),
        para no llenar la cola con mil avisos durante un «npm install»."""
        if not self._watch_flag.is_set():
            self._watch_flag.set()
            self.q.put(("watch_event",))

    def _handle_watch_event(self):
        """Hilo de la interfaz: comprobar dentro de unos segundos (cada
        aviso nuevo aplaza la comprobación: se espera a que pare)."""
        self._watch_flag.clear()
        if not self.v_auto.get():
            return
        if self._watch_job is not None:
            try:
                self.after_cancel(self._watch_job)
            except Exception:
                pass
        self._watch_job = self.after(DEBOUNCE_MS, self._watch_tick)

    def _watch_tick(self):
        self._watch_job = None
        if not self.v_auto.get():
            return
        if self.controller.running or self.scanning or self._auto_checking:
            # Ahora no: se vuelve a mirar en cuanto haya otro aviso o en la
            # comprobación periódica.
            self._watch_job = self.after(DEBOUNCE_MS, self._watch_tick)
            return
        self._launch_check()

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
        if self.controller.running or self.scanning or self._auto_checking:
            self._schedule_auto()
            return
        self._launch_check()

    def _launch_check(self):
        # El campo pudo cambiar sin salir de él (sin aviso de la interfaz):
        # se comprueba aquí también, antes de mirar nada.
        if self.pause_auto_if_folder_changed():
            return
        folder = normalize_path(self.e_folder.get())
        self._auto_checking = True
        threading.Thread(
            target=self._auto_check,
            args=(folder, self.ignore_lines(), self._is_addedit(),
                  self._last_pub_fp),
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
                # Una vez por sesión: que cada comprobación sea rápida.
                gitspeed.tune(folder, git)

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
        self._auto_checking = False
        if not self.v_auto.get():
            return
        if action == "missing":
            self.log("🤖 La carpeta ya no existe; auto-publicación "
                     "desactivada.", "warn")
            self.v_auto.set(False)
            self.toggle_auto()
            return
        if action == "publish" and self.pause_auto_if_folder_changed():
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
            self._stop_watching()
            self._tray_off()
            self.notify_desktop(APP_NAME, "Auto-publicación en pausa: " + reason)
            self.auto_pill.configure(fg_color="transparent", text_color=MUTED)
