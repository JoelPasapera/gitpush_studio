# -*- coding: utf-8 -*-
"""Abrir las herramientas: cada ventana una sola vez (si ya está abierta,
se trae al frente) y los accesos directos de la barra lateral.

Mezcla de `GitPushStudio`: usa sus widgets y su estado (`self.…`);
no tiene constructor propio."""

import webbrowser
from tkinter import messagebox

from .. import APP_NAME
from ..core.desktop import open_path
from ..core.urls import parse_owner_repo, repo_web_url
from .dialogs import (AnalyticsDialog, BatchPublishDialog, BranchDialog,
                      BulkCloneDialog, CloneDialog, ConsolidateDialog,
                      CreateRepoDialog, HistoryDialog, PagesDialog,
                      ReleaseDialog, SyncDialog)


class DialogsMixin:

    def _singleton(self, key, factory):
        dlg = self._dialogs.get(key)
        if dlg is not None and dlg.winfo_exists():
            dlg.lift()
            dlg.focus()
            return
        self._dialogs[key] = factory()

    def open_create_repo(self):
        self._singleton("repo", lambda: CreateRepoDialog(self))

    def open_pages(self):
        owner, repo = parse_owner_repo(self.e_url.get())
        if not owner:
            messagebox.showwarning(APP_NAME, "Escribe primero la URL del "
                                             "repositorio de GitHub.")
            return
        self._singleton("pages", lambda: PagesDialog(self, owner, repo))

    def open_clone(self):
        self._singleton("clone", lambda: CloneDialog(self))

    def open_bulk(self):
        self._singleton("bulk", lambda: BulkCloneDialog(self))

    def open_consolidate(self):
        self._singleton("pack", lambda: ConsolidateDialog(self))

    def open_batch(self):
        self._singleton("batch", lambda: BatchPublishDialog(self))

    def open_analytics(self):
        self._singleton("analytics", lambda: AnalyticsDialog(self))

    def open_sync(self):
        self._singleton("sync", lambda: SyncDialog(self))

    def open_history(self):
        self._singleton("history", lambda: HistoryDialog(self))

    def open_branches(self):
        self._singleton("branches", lambda: BranchDialog(self))

    def open_release(self):
        owner, repo = parse_owner_repo(self.e_url.get())
        if not owner:
            messagebox.showwarning(APP_NAME, "Escribe primero la URL del "
                                             "repositorio de GitHub.")
            return
        self._singleton("release", lambda: ReleaseDialog(self, owner, repo))

    def open_repo(self):
        url = self.last_repo_web or repo_web_url(self.e_url.get())
        if url.startswith("http"):
            webbrowser.open(url)
        else:
            messagebox.showinfo(APP_NAME, "Escribe primero la URL del "
                                          "repositorio.")

    def open_diagnostics(self):
        """Abre la carpeta del registro de diagnóstico."""
        carpeta = self.diag.folder
        try:
            carpeta.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self.log(f"🩺 Registro de diagnóstico: {carpeta}", "dim")
        self.log("  Guarda lo que pasa por esta consola (sin tokens ni "
                 "contraseñas) para poder revisar un fallo después.", "dim")
        open_path(str(carpeta))
