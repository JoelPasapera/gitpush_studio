# -*- coding: utf-8 -*-
"""Integración con Windows: arrastrar carpetas a la ventana y el icono de
la bandeja del sistema. Todo con ctypes, sin librerías.

    shell = WindowsShell(ventana)
    shell.install()                       # False fuera de Windows
    shell.enable_drop(al_soltar)          # al_soltar(lista_de_rutas)
    shell.tray_show("texto", al_pulsar)   # icono junto al reloj
    shell.notify("Título", "texto")       # globo de aviso
    shell.tray_hide(); shell.uninstall()

Cómo funciona: se «subclasifica» la ventana principal (se pone un
procedimiento de ventana propio delante del de Tk) para recibir dos
mensajes que Tk no atiende: WM_DROPFILES (algo soltado encima, tras
`DragAcceptFiles`) y el mensaje del icono de la bandeja
(`Shell_NotifyIconW`). Todo lo demás pasa intacto al procedimiento de
Tk. Lo que se recibe se entrega a la interfaz con `after(0, …)`, nunca
desde dentro del procedimiento.

Si algo no está disponible (otro sistema, Tk sin el identificador de la
ventana…) no pasa nada: la app funciona igual, sin estas comodidades.
Ningún fallo de aquí llega a la interfaz como excepción.
"""

import ctypes
import os
from pathlib import Path
from typing import Callable, Dict, List, Optional

WM_DROPFILES = 0x0233
WM_APP = 0x8000
TRAY_MESSAGE = WM_APP + 0x47
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
GWLP_WNDPROC = -4

NIM_ADD = 0x0
NIM_MODIFY = 0x1
NIM_DELETE = 0x2
NIF_MESSAGE = 0x1
NIF_ICON = 0x2
NIF_TIP = 0x4
NIF_INFO = 0x10
NIIF_INFO = 0x1
IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10
LR_DEFAULTSIZE = 0x40
TRAY_ID = 1

ICON_FILE = Path(__file__).resolve().parent.parent / "assets" / "gitpush.ico"

Handler = Callable[[int, int], Optional[int]]


def _structures():
    """Las estructuras de Windows (solo se definen al usarlas)."""
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                    ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                    ("uCallbackMessage", wintypes.UINT),
                    ("hIcon", wintypes.HICON),
                    ("szTip", ctypes.c_wchar * 128),
                    ("dwState", wintypes.DWORD),
                    ("dwStateMask", wintypes.DWORD),
                    ("szInfo", ctypes.c_wchar * 256),
                    ("uVersion", wintypes.UINT),
                    ("szInfoTitle", ctypes.c_wchar * 64),
                    ("dwInfoFlags", wintypes.DWORD),
                    ("guidItem", GUID),
                    ("hBalloonIcon", wintypes.HICON)]

    return NOTIFYICONDATAW


def drop_paths(query: Callable[..., int], hdrop: int) -> List[str]:
    """Las rutas de un WM_DROPFILES (`query` es DragQueryFileW)."""
    count = query(hdrop, 0xFFFFFFFF, None, 0)
    paths: List[str] = []
    for index in range(count):
        length = query(hdrop, index, None, 0)
        buffer = ctypes.create_unicode_buffer(length + 1)
        query(hdrop, index, buffer, length + 1)
        paths.append(buffer.value)
    return paths


class WindowsShell:
    """Mensajes de Windows para la ventana principal de Tk."""

    def __init__(self, window) -> None:
        self.window = window
        self.handlers: Dict[int, Handler] = {}
        self.hwnd = 0
        self.installed = False
        self.tray_visible = False
        self._old_proc = None
        self._proc = None
        self._user32 = None
        self._shell32 = None
        self._set_long = None
        self._icon = None
        self._tray_tip = ""
        self._taskbar_created = 0

    # ---- subclase de la ventana -------------------------------------------
    def install(self) -> bool:
        if os.name != "nt" or self.installed:
            return self.installed
        try:
            from ctypes import wintypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)  # type: ignore[attr-defined]
            lresult = ctypes.c_ssize_t
            wndproc = ctypes.WINFUNCTYPE(lresult, wintypes.HWND,  # type: ignore[attr-defined]
                                         wintypes.UINT, wintypes.WPARAM,
                                         wintypes.LPARAM)
            set_long = getattr(user32, "SetWindowLongPtrW", None) or \
                user32.SetWindowLongW
            set_long.restype = ctypes.c_void_p
            set_long.argtypes = (wintypes.HWND, ctypes.c_int,
                                 ctypes.c_void_p)
            call = user32.CallWindowProcW
            call.restype = lresult
            call.argtypes = (ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)
            register = user32.RegisterWindowMessageW
            register.restype = wintypes.UINT
            register.argtypes = (wintypes.LPCWSTR,)
            self.hwnd = int(self.window.wm_frame(), 16)

            def procedure(hwnd, message, wparam, lparam):
                handler = self.handlers.get(message)
                if handler is not None:
                    try:
                        result = handler(wparam or 0, lparam or 0)
                    except Exception:                       # noqa: BLE001
                        result = None
                    if result is not None:
                        return result
                return call(self._old_proc, hwnd, message, wparam, lparam)

            self._proc = wndproc(procedure)
            self._old_proc = set_long(self.hwnd, GWLP_WNDPROC,
                                      ctypes.cast(self._proc,
                                                  ctypes.c_void_p))
            if not self._old_proc:
                return False
            self._user32 = user32
            self._shell32 = shell32
            self._set_long = set_long
            # Si el Explorador se reinicia, los iconos de la bandeja
            # desaparecen: avisa con «TaskbarCreated» y se vuelve a poner.
            self._taskbar_created = register("TaskbarCreated")
            if self._taskbar_created:
                self.handlers[self._taskbar_created] = self._readd
            self.installed = True
        except Exception:                                   # noqa: BLE001
            self.installed = False
        return self.installed

    def uninstall(self) -> None:
        """Deja la ventana como estaba (antes de cerrarla)."""
        self.tray_hide()
        if self.installed and self._set_long is not None:
            try:
                self._set_long(self.hwnd, GWLP_WNDPROC, self._old_proc)
            except Exception:                               # noqa: BLE001
                pass
        self.installed = False

    def _later(self, action: Callable[[], None]) -> None:
        try:
            self.window.after(0, action)
        except Exception:                                   # noqa: BLE001
            pass

    # ---- arrastrar y soltar ------------------------------------------------
    def enable_drop(self, on_paths: Callable[[List[str]], None]) -> bool:
        if not self.install():
            return False
        try:
            from ctypes import wintypes
            shell32 = self._shell32
            shell32.DragAcceptFiles.argtypes = (wintypes.HWND,
                                                wintypes.BOOL)
            shell32.DragAcceptFiles.restype = None
            query = shell32.DragQueryFileW
            query.argtypes = (ctypes.c_void_p, wintypes.UINT,
                              ctypes.c_wchar_p, wintypes.UINT)
            query.restype = wintypes.UINT
            shell32.DragFinish.argtypes = (ctypes.c_void_p,)
            shell32.DragFinish.restype = None

            def on_drop(wparam: int, _lparam: int) -> int:
                try:
                    paths = drop_paths(query, wparam)
                finally:
                    shell32.DragFinish(wparam)
                self._later(lambda: on_paths(paths))
                return 0

            self.handlers[WM_DROPFILES] = on_drop
            shell32.DragAcceptFiles(self.hwnd, True)
            return True
        except Exception:                                   # noqa: BLE001
            return False

    # ---- bandeja del sistema -----------------------------------------------
    def _load_icon(self):
        from ctypes import wintypes
        user32 = self._user32
        load_image = user32.LoadImageW
        load_image.restype = wintypes.HANDLE
        load_image.argtypes = (wintypes.HINSTANCE, wintypes.LPCWSTR,
                               wintypes.UINT, ctypes.c_int, ctypes.c_int,
                               wintypes.UINT)
        if ICON_FILE.is_file():
            icon = load_image(None, str(ICON_FILE), IMAGE_ICON, 0, 0,
                              LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if icon:
                return icon
        load_icon = user32.LoadIconW
        load_icon.restype = wintypes.HICON
        load_icon.argtypes = (wintypes.HINSTANCE, ctypes.c_void_p)
        return load_icon(None, ctypes.c_void_p(IDI_APPLICATION))

    def _notify_icon(self, message: int, flags: int, tip: str = "",
                     title: str = "", text: str = "") -> bool:
        from ctypes import wintypes
        structure = _structures()
        data = structure()
        data.cbSize = ctypes.sizeof(structure)
        data.hWnd = self.hwnd
        data.uID = TRAY_ID
        data.uFlags = flags
        data.uCallbackMessage = TRAY_MESSAGE
        data.hIcon = self._icon
        data.szTip = tip[:127]
        data.szInfo = text[:255]
        data.szInfoTitle = title[:63]
        data.dwInfoFlags = NIIF_INFO
        notify = self._shell32.Shell_NotifyIconW
        notify.argtypes = (wintypes.DWORD, ctypes.c_void_p)
        notify.restype = wintypes.BOOL
        return bool(notify(message, ctypes.byref(data)))

    def tray_show(self, tip: str, on_click: Callable[[], None]) -> bool:
        """Pone el icono (o cambia su texto si ya estaba)."""
        if not self.install():
            return False
        try:
            if self._icon is None:
                self._icon = self._load_icon()
            self._tray_tip = tip

            def on_tray(_wparam: int, lparam: int) -> int:
                if (lparam & 0xFFFF) in (WM_LBUTTONUP, WM_LBUTTONDBLCLK,
                                         WM_RBUTTONUP):
                    self._later(on_click)
                return 0

            self.handlers[TRAY_MESSAGE] = on_tray
            message = NIM_MODIFY if self.tray_visible else NIM_ADD
            self.tray_visible = self._notify_icon(
                message, NIF_MESSAGE | NIF_ICON | NIF_TIP, tip) or \
                self.tray_visible
        except Exception:                                   # noqa: BLE001
            return False
        return self.tray_visible

    def _readd(self, _wparam: int, _lparam: int) -> Optional[int]:
        if self.tray_visible:
            self.tray_visible = False
            try:
                self.tray_visible = self._notify_icon(
                    NIM_ADD, NIF_MESSAGE | NIF_ICON | NIF_TIP,
                    self._tray_tip)
            except Exception:                               # noqa: BLE001
                pass
        return None

    def notify(self, title: str, text: str) -> bool:
        """Un globo de aviso junto al icono (si está puesto)."""
        if not self.tray_visible:
            return False
        try:
            return self._notify_icon(NIM_MODIFY, NIF_INFO, self._tray_tip,
                                     title, text)
        except Exception:                                   # noqa: BLE001
            return False

    def tray_hide(self) -> None:
        if not self.tray_visible:
            return
        try:
            self._notify_icon(NIM_DELETE, 0)
        except Exception:                                   # noqa: BLE001
            pass
        self.tray_visible = False
