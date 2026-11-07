# -*- coding: utf-8 -*-
"""Abrir GitPush Studio al iniciar Windows (para que la auto-publicación
vigile la carpeta sin acordarse de abrir la app).

Se usa la clave de inicio del USUARIO (HKCU\\…\\Run): no hace falta ser
administrador y no afecta a nadie más del equipo. El valor es la orden
que abre la app con «--al-iniciar»: minimizada y, si se eligió así, con
la auto-publicación de la última carpeta en marcha.

En otros sistemas no hace nada (y lo dice): cada uno tiene su manera.
"""

import os
import sys
from pathlib import Path
from typing import Optional

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "GitPush Studio"
STARTUP_FLAG = "--al-iniciar"


def supported() -> bool:
    return os.name == "nt"


def _winreg():
    import winreg                                     # solo en Windows
    return winreg


def launch_command(executable: Optional[str] = None,
                   launcher: Optional[str] = None,
                   frozen: Optional[bool] = None) -> str:
    """La orden que abre la app: el .exe (si es la versión instalable) o
    `pythonw` con el lanzador GitPushStudio.pyw (sin ventana de consola)."""
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    executable = executable or sys.executable
    if frozen:
        return f'"{executable}" {STARTUP_FLAG}'
    python = Path(executable)
    windowless = python.with_name("pythonw.exe")
    if python.name.lower() == "python.exe" and windowless.exists():
        python = windowless
    if launcher is None:
        launcher = str(Path(__file__).resolve().parent.parent.parent
                       / "GitPushStudio.pyw")
    return f'"{python}" "{launcher}" {STARTUP_FLAG}'


def is_enabled(winreg_module=None) -> bool:
    if winreg_module is None and not supported():
        return False
    winreg = winreg_module or _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return False
    return bool(value)


def enable(command: Optional[str] = None, winreg_module=None) -> None:
    """Abre la app al iniciar sesión. Lanza OSError si no se pudo."""
    if winreg_module is None and not supported():
        raise OSError("solo está disponible en Windows")
    winreg = winreg_module or _winreg()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ,
                          command or launch_command())


def disable(winreg_module=None) -> None:
    """Deja de abrirla al iniciar sesión (si no estaba, no pasa nada)."""
    if winreg_module is None and not supported():
        return
    winreg = winreg_module or _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        pass


def started_at_login(argv=None) -> bool:
    """¿Se abrió la app sola, al iniciar sesión?"""
    return STARTUP_FLAG in (sys.argv if argv is None else argv)
