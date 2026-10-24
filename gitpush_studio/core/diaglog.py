# -*- coding: utf-8 -*-
"""Registro de diagnóstico: lo que pasa por la consola, guardado en disco
(~/.gitpush_studio/registro/), para poder entender un fallo DESPUÉS de
cerrar la app.

  · Rota solo: como mucho `backups` archivos de `max_bytes` cada uno
    (5 × 1 MB por defecto), así que nunca crece sin límite.
  · Nunca guarda secretos: antes de escribir se tapan los tokens con
    formato conocido y las contraseñas metidas en URLs
    («https://usuario:token@github.com/…» → «https://***@github.com/…»).
  · Nunca rompe la app: si el disco falla, se deja de registrar y punto.
"""

import os
import re
import threading
import time
from pathlib import Path

from .config import CONFIG_DIR

LOG_DIR = CONFIG_DIR / "registro"
LOG_NAME = "diagnostico.log"

# Credenciales dentro de una URL: todo lo que hay entre «//» y «@».
_URL_CREDS = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/@'\"`]+@")
# Formatos de token de los proveedores más comunes.
_TOKENS = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"glpat-[A-Za-z0-9_\-]{16,}|xox[abposer]-[A-Za-z0-9\-]{10,}|"
    r"sk-[A-Za-z0-9_\-]{20,}|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}|"
    r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{35}|npm_[A-Za-z0-9]{36}|"
    r"hf_[A-Za-z0-9]{30,}|pypi-AgE[A-Za-z0-9_\-]{40,})")
# Cabeceras de autorización (por si alguna salida de git las mostrara).
_AUTH_HEADER = re.compile(r"(?i)(authorization:\s*(?:basic|bearer|token)\s+)"
                          r"\S+")


def redact(text):
    """`text` sin secretos reconocibles."""
    text = _URL_CREDS.sub(r"\1***@", text)
    text = _AUTH_HEADER.sub(r"\1***", text)
    return _TOKENS.sub("***", text)


class DiagnosticLog:
    """Archivo de registro con rotación propia (sin `logging`: no se
    quiere su configuración global, que también afectaría a quien
    importe este paquete)."""

    def __init__(self, folder=LOG_DIR, name=LOG_NAME, max_bytes=1 << 20,
                 backups=4):
        self.folder = Path(folder)
        self.path = self.folder / name
        self.max_bytes = max(1024, int(max_bytes))
        self.backups = max(1, int(backups))
        self._lock = threading.Lock()
        self._broken = False

    def _rotate(self):
        """diagnostico.log → .1 → .2 … (el más antiguo se descarta)."""
        for i in range(self.backups - 1, 0, -1):
            viejo = self.path.with_name(f"{self.path.name}.{i}")
            if viejo.exists():
                os.replace(viejo, self.path.with_name(
                    f"{self.path.name}.{i + 1}"))
        os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
        sobrante = self.path.with_name(f"{self.path.name}.{self.backups + 1}")
        if sobrante.exists():
            sobrante.unlink()

    def write(self, text, tag="out"):
        """Añade una línea (o varias) con la hora. Nunca lanza."""
        if self._broken:
            return
        linea = "%s [%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), tag,
                                  redact(str(text)))
        datos = linea.encode("utf-8", "replace")
        with self._lock:
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                try:
                    tam = self.path.stat().st_size
                except FileNotFoundError:
                    tam = 0
                if tam and tam + len(datos) > self.max_bytes:
                    self._rotate()
                with open(self.path, "ab") as fh:
                    fh.write(datos)
            except OSError:
                self._broken = True          # sin registro, pero sin fallos

    def files(self):
        """Los archivos de registro existentes, del más nuevo al más viejo."""
        nombres = [self.path] + [self.path.with_name(f"{self.path.name}.{i}")
                                 for i in range(1, self.backups + 1)]
        return [p for p in nombres if p.exists()]
