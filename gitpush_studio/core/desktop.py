# -*- coding: utf-8 -*-
"""Abrir archivos y carpetas en el escritorio del sistema.

    open_path(ruta)         con el programa predeterminado: un TXT en el
                            editor, una carpeta en el explorador.
    reveal_in_folder(ruta)  la carpeta que lo contiene, con el archivo
                            ya seleccionado («Abrir ubicación del
                            archivo» del Explorador de Windows).

Sin dependencias externas:

  · Windows: la propia API del shell por ctypes
    (`SHOpenFolderAndSelectItems`), que además reutiliza una ventana del
    Explorador que ya esté en esa carpeta. Si falla, `explorer /select,`
    con la ruta entre comillas.
  · macOS: `open -R`, que la muestra en el Finder.
  · Linux: el servicio estándar `org.freedesktop.FileManager1` por D-Bus
    (Nautilus, Dolphin, Nemo, Caja, Thunar…), con `dbus-send` o `gdbus`.

Si no se puede señalar el archivo, se abre al menos su carpeta. Nada de
esto lanza: cada función devuelve True si pudo pedírselo al sistema.
`reveal_in_folder` habla con otro proceso y puede tardar un poco: desde
la interfaz, mejor en un hilo.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

_TIMEOUT = 5                      # segundos para el gestor de archivos
_COINIT_APARTMENTTHREADED = 0x2


def platform_name():
    """«windows», «mac» o «linux» (cualquier otro Unix cuenta como linux)."""
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


def open_path(path):
    """Abre `path` (archivo o carpeta) con el programa predeterminado."""
    ruta = os.fspath(path)
    try:
        sistema = platform_name()
        if sistema == "windows":
            os.startfile(ruta)                     # type: ignore[attr-defined]
        elif sistema == "mac":
            subprocess.run(["open", ruta], check=False)
        else:
            subprocess.run(["xdg-open", ruta], check=False)
        return True
    except OSError:
        return False


def reveal_in_folder(path):
    """Abre la carpeta que contiene `path`, con `path` seleccionado.

    Si `path` ya no existe (lo moviste o lo borraste), abre la carpeta
    donde estaba, si sigue existiendo. Devuelve True si pudo pedírselo
    al sistema."""
    ruta = os.path.abspath(os.fspath(path))
    carpeta = os.path.dirname(ruta)
    if not os.path.lexists(ruta):
        return os.path.isdir(carpeta) and open_path(carpeta)
    try:
        sistema = platform_name()
        if sistema == "windows":
            ok = _reveal_windows(ruta)
        elif sistema == "mac":
            ok = _run(["open", "-R", ruta])
        else:
            ok = _reveal_freedesktop(ruta)
    except Exception:                                       # noqa: BLE001
        ok = False
    # Si no se pudo señalar el archivo, al menos su carpeta.
    return ok or open_path(carpeta)


# ------------------------------------------------------------------ #
#  Windows                                                            #
# ------------------------------------------------------------------ #
def _reveal_windows(ruta):
    ruta = os.path.normpath(ruta)            # el shell no entiende «/»
    try:
        if _shell_select(ruta):
            return True
    except Exception:                                       # noqa: BLE001
        pass                                 # sin COM o sin shell32: la orden
    subprocess.Popen(explorer_select_command(ruta))
    return True


def explorer_select_command(ruta):
    """La orden de respaldo, con la ruta SIEMPRE entre comillas: el
    Explorador separa sus argumentos por comas, y sin ellas una carpeta
    llamada «Notas, 2026» partía la ruta en dos y abría otra cosa. En
    Windows una ruta no puede contener comillas, así que no hay nada más
    que escapar."""
    return 'explorer /select,"%s"' % ruta


def _shell_select(ruta):
    """`SHOpenFolderAndSelectItems` del shell de Windows, por ctypes."""
    import ctypes
    from ctypes import wintypes

    # Cargadores propios: los tipos que se fijan aquí no cambian los de
    # `ctypes.windll`, que puede usar otro código del proceso.
    shell32 = ctypes.WinDLL("shell32")
    ole32 = ctypes.WinDLL("ole32")
    # Tipos explícitos: sin `restype`, ctypes devolvería el puntero como
    # un entero de 32 bits y en Windows de 64 bits se cortaría.
    shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
    shell32.ILCreateFromPathW.restype = ctypes.c_void_p
    shell32.ILFree.argtypes = [ctypes.c_void_p]
    shell32.ILFree.restype = None
    shell32.SHOpenFolderAndSelectItems.argtypes = [
        ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, wintypes.DWORD]
    shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None

    hr = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    # S_OK o S_FALSE: este hilo queda inicializado y hay que cerrarlo.
    # Otro valor (ya estaba en otro modo): se usa tal cual, sin cerrar.
    propio = hr in (0, 1)
    try:
        pidl = shell32.ILCreateFromPathW(ruta)
        if not pidl:
            return False
        try:
            # Sin lista de elementos: abre la carpeta padre de `pidl` y
            # selecciona ese elemento.
            return shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) == 0
        finally:
            shell32.ILFree(pidl)
    finally:
        if propio:
            ole32.CoUninitialize()


# ------------------------------------------------------------------ #
#  Linux (y otros escritorios libres)                                 #
# ------------------------------------------------------------------ #
def freedesktop_commands(ruta):
    """Órdenes, en orden de preferencia, para pedir al gestor de archivos
    que muestre `ruta` (método `ShowItems` de FileManager1). La ruta va
    como URI: comas, espacios y comillas salen escapados, así que no
    pueden partir la lista de `dbus-send` ni el texto de `gdbus`."""
    uri = Path(ruta).as_uri()
    return (
        ["dbus-send", "--session", "--print-reply",
         "--dest=org.freedesktop.FileManager1", "--type=method_call",
         "/org/freedesktop/FileManager1",
         "org.freedesktop.FileManager1.ShowItems",
         "array:string:" + uri, "string:"],
        ["gdbus", "call", "--session",
         "--dest", "org.freedesktop.FileManager1",
         "--object-path", "/org/freedesktop/FileManager1",
         "--method", "org.freedesktop.FileManager1.ShowItems",
         "['%s']" % uri, "''"],
    )


def _reveal_freedesktop(ruta):
    for orden in freedesktop_commands(ruta):
        if shutil.which(orden[0]) and _run(orden):
            return True
    return False


def _run(orden, timeout=_TIMEOUT):
    """True si la orden terminó bien (sin esperar más de `timeout`)."""
    try:
        p = subprocess.run(orden, check=False, capture_output=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0
