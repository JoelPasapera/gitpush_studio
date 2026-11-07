# -*- coding: utf-8 -*-
"""Comodidades del escritorio: arrastrar una carpeta (o un TXT) a la
ventana, el icono de la bandeja mientras la auto-publicación vigila, los
avisos al publicar sola, abrir la app al iniciar Windows y los atajos de
teclado.

Mezcla de `GitPushStudio`: usa sus widgets y su estado (`self.…`); no
tiene constructor propio. Lo propio de Windows vive en `winshell` y en
`core.startup`; fuera de Windows todo esto se queda en nada sin dar
errores (los atajos y soltar rutas funcionan en todas partes).
"""

import os
import threading
import time
from pathlib import Path
from tkinter import messagebox

from .. import APP_NAME, APP_VERSION
from ..core import startup, updates
from ..core.fs import normalize_path
from .help import SHORTCUTS
from .widgets import set_text
from .winshell import ICON_FILE, WindowsShell

ICON_PNG = ICON_FILE.with_suffix(".png")

class DesktopMixin:

    def _setup_desktop(self):
        """Atajos, icono, arrastrar y soltar (lo que el sistema permita)."""
        for sequence, method, _key, _what in SHORTCUTS:
            self.bind(sequence, lambda _e, m=method: self._shortcut(m))
        self.shell = WindowsShell(self)
        if self.shell.install():
            self.shell.enable_drop(self.on_paths_dropped)
            try:
                self.bind("<Unmap>", self._on_unmap, add="+")
            except Exception:                               # noqa: BLE001
                pass
        self._set_window_icon()

    def _set_window_icon(self):
        """El icono de la ventana: el .ico en Windows, el PNG en el resto."""
        try:
            if os.name == "nt" and ICON_FILE.is_file():
                self.iconbitmap(str(ICON_FILE))
            elif ICON_PNG.is_file():
                import tkinter
                self._icon_image = tkinter.PhotoImage(file=str(ICON_PNG))
                self.iconphoto(True, self._icon_image)
        except Exception:                                   # noqa: BLE001
            pass

    def _shortcut(self, method):
        action = getattr(self, method, None)
        if action is not None:
            action()
        return "break"

    # ---- soltar rutas en la ventana ---------------------------------------
    def on_paths_dropped(self, paths):
        """Una carpeta soltada pasa a ser la del proyecto; un TXT abre
        «Aplicar TXT» con él."""
        folders = [p for p in paths if Path(p).is_dir()]
        texts = [p for p in paths if Path(p).is_file()
                 and p.lower().endswith(".txt")]
        if folders:
            carpeta = normalize_path(folders[0])
            set_text(self.e_folder, carpeta)
            self.log(f"📁 Carpeta del proyecto: {carpeta}", "ok")
            self.folder_changed()
            if len(folders) > 1:
                self.log("  (Soltaste varias carpetas: uso la primera. "
                         "Para publicar varias, «🚀🚀 Publicar varias "
                         "carpetas…».)", "dim")
            return
        if texts:
            self.open_apply_txt()
            dialog = self._dialogs.get("apply_txt")
            if dialog is not None:
                dialog.use_file(texts[0])
            return
        self.log("Suelta una carpeta (será la del proyecto) o un TXT con "
                 "cambios (para aplicarlo).", "warn")

    # ---- bandeja --------------------------------------------------------
    def _tray_on(self):
        folder = normalize_path(self.e_folder.get())
        name = Path(folder).name if folder else "tu carpeta"
        self.shell.tray_show(f"{APP_NAME} · vigilando «{name}»",
                             self._restore_from_tray)

    def _tray_off(self):
        self.shell.tray_hide()

    def _restore_from_tray(self):
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
        except Exception:                                   # noqa: BLE001
            pass

    def _on_unmap(self, event):
        """Minimizada con la auto-publicación en marcha: a la bandeja (sin
        ocupar sitio en la barra de tareas). Solo si el icono está puesto:
        si no, la ventana no se podría recuperar."""
        if getattr(event, "widget", None) is not self:
            return
        if not (self.v_auto.get() and self.shell.tray_visible):
            return
        try:
            if self.state() == "iconic":
                self.withdraw()
                self.shell.notify(APP_NAME, "Sigo vigilando la carpeta. "
                                            "Pulsa el icono para volver.")
        except Exception:                                   # noqa: BLE001
            pass

    def notify_desktop(self, title, text):
        self.shell.notify(title, text)

    # ---- abrir al iniciar Windows ------------------------------------------
    def toggle_startup(self):
        quiere = bool(self.v_startup.get())
        try:
            if quiere:
                startup.enable()
            else:
                startup.disable()
        except OSError as e:
            self.v_startup.set(not quiere)
            messagebox.showwarning(APP_NAME, f"No pude cambiarlo: {e}")
            return
        self.config_data["startup_auto"] = quiere
        self.save_config()
        if quiere:
            self.log("🪟 GitPush Studio se abrirá al iniciar Windows, "
                     "minimizado y vigilando la última carpeta que tengas "
                     "en el formulario.", "ok")
        else:
            self.log("🪟 Ya no se abrirá al iniciar Windows.", "dim")

    def _resume_after_login(self):
        """Abierta sola al iniciar sesión: minimizada y, si se eligió así,
        vigilando la carpeta del último estado."""
        if not startup.started_at_login():
            return
        try:
            self.iconify()
        except Exception:                                   # noqa: BLE001
            pass
        if self.config_data.get("startup_auto"):
            folder = normalize_path(self.e_folder.get())
            if folder and Path(folder).is_dir():
                self.v_auto.set(True)
                self.toggle_auto()

    # ---- versiones nuevas ---------------------------------------------------
    def check_for_updates(self):
        """Una vez al día, si en Opciones hay un repositorio de la app:
        ¿hay una versión más nueva? Solo avisa en la consola."""
        repo = (self.config_data.get("update_repo") or "").strip()
        if not updates.valid_repo(repo):
            return
        if not updates.due(self.config_data.get("update_checked_at", 0)):
            return
        self.config_data["update_checked_at"] = time.time()
        self.save_config()
        threading.Thread(target=self._update_worker, args=(repo,),
                         daemon=True).start()

    def _update_worker(self, repo):
        try:
            found = updates.newer_than(APP_VERSION, repo)
        except Exception:                                   # noqa: BLE001
            found = None
        if found is not None:
            self.q.put(("update", found[0], found[1]))

    def show_update(self, tag, page):
        self.log(f"🆕 Hay una versión nueva de {APP_NAME}: {tag} (tienes la "
                 f"{APP_VERSION}). Descárgala de {page}", "ok")

    def update_repo_changed(self, _event=None):
        repo = self.e_update_repo.get().strip()
        if repo == (self.config_data.get("update_repo") or ""):
            return
        if repo and not updates.valid_repo(repo):
            self.log("Para los avisos de versiones escribe «usuario/"
                     "repositorio» (por ejemplo «ana/gitpush_studio»).",
                     "warn")
            return
        self.config_data["update_repo"] = repo
        self.config_data["update_checked_at"] = 0
        self.save_config()
        if repo:
            self.log(f"🆕 Avisaré de las versiones nuevas que publiques en "
                     f"«{repo}» (lo miro una vez al día).", "dim")
            self.check_for_updates()
