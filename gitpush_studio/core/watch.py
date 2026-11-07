# -*- coding: utf-8 -*-
"""Enterarse al momento de que algo cambia en una carpeta, sin sondear.

La auto-publicación miraba la carpeta cada X minutos: tarde (un cambio
esperaba hasta la siguiente ronda) y caro (cada ronda recorría la
carpeta aunque nada hubiera cambiado). Aquí el sistema operativo avisa
de cada cambio, sin librerías externas (ctypes):

  · Windows: `ReadDirectoryChangesW` sobre la carpeta, con subcarpetas.
    Para pararlo, `CancelIoEx` despierta la llamada que espera.
  · Linux: inotify, una vigilancia por carpeta (inotify no es
    recursivo); las carpetas nuevas se añaden al vuelo.
  · Otros sistemas: no hay vigilante y la auto-publicación sigue
    comprobando cada X minutos, como siempre.

Lo que pasa dentro de .git (los propios comandos de git escriben ahí)
no cuenta, ni las carpetas pesadas que nunca se publican
(node_modules, __pycache__, entornos virtuales…). El aviso no dice qué
hacer: quien lo recibe decide (y comprueba con git si de verdad hay
algo que publicar).
"""

import ctypes
import os
import select
import struct
import threading
from typing import Callable, Dict, List, Optional

# Carpetas cuyo contenido nunca dispara nada (en Linux, además, no se
# vigilan: cada carpeta vigilada gasta una de las vigilancias del sistema).
SKIPPED_DIRS = frozenset((".git", "node_modules", "__pycache__", ".venv",
                          "venv", ".tox", ".mypy_cache", ".pytest_cache",
                          ".idea", ".gradle"))

OnChange = Callable[[Optional[str]], None]


def _relevant(rel: Optional[str]) -> bool:
    """¿Cuenta un cambio en `rel` (None: «algo cambió, no se sabe qué»)?"""
    if rel is None:
        return True
    parts = rel.replace("\\", "/").split("/")
    for part in parts:
        if part in SKIPPED_DIRS:
            return False
    return True


class FolderWatcher:
    """Interfaz común: `start()` empieza a vigilar en un hilo propio,
    `stop()` lo detiene. `on_change(ruta_relativa_o_None)` se llama desde
    ese hilo: quien lo reciba debe pasarlo a su propio hilo (una cola)."""

    def __init__(self, folder: str, on_change: OnChange) -> None:
        self.folder = os.path.abspath(folder)
        self.on_change = on_change
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="vigilante-carpeta")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _notify(self, rel: Optional[str]) -> None:
        if _relevant(rel):
            try:
                self.on_change(rel)
            except Exception:                               # noqa: BLE001
                pass

    # ---- para cada sistema -------------------------------------------------
    def _run(self) -> None:
        raise NotImplementedError

    def _wake(self) -> None:
        """Despierta el hilo si está esperando al sistema."""


# ------------------------------------------------------------------ #
#  Windows: ReadDirectoryChangesW                                     #
# ------------------------------------------------------------------ #
_FILE_LIST_DIRECTORY = 0x0001
_FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_NOTIFY_FILTER = (0x00000001      # nombre de archivo
                  | 0x00000002    # nombre de carpeta
                  | 0x00000008    # tamaño
                  | 0x00000010    # última escritura
                  | 0x00000040)   # creación
_WATCH_BUFFER = 64 * 1024         # el máximo que admiten las carpetas de red
_ERROR_OPERATION_ABORTED = 995


class WindowsWatcher(FolderWatcher):

    def __init__(self, folder: str, on_change: OnChange) -> None:
        super().__init__(folder, on_change)
        self._handle = None
        # Cancelar y cerrar nunca a la vez: con el identificador ya cerrado
        # (y quizá reutilizado por otro archivo), CancelIoEx cancelaría lo
        # que no es suyo.
        self._handle_lock = threading.Lock()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        self._create = kernel32.CreateFileW
        self._create.restype = ctypes.c_void_p
        self._create.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32,
                                 ctypes.c_uint32, ctypes.c_void_p,
                                 ctypes.c_uint32, ctypes.c_uint32,
                                 ctypes.c_void_p)
        self._read = kernel32.ReadDirectoryChangesW
        self._read.restype = ctypes.c_int
        self._read.argtypes = (ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_uint32, ctypes.c_int,
                               ctypes.c_uint32,
                               ctypes.POINTER(ctypes.c_uint32),
                               ctypes.c_void_p, ctypes.c_void_p)
        self._cancel = kernel32.CancelIoEx
        self._cancel.restype = ctypes.c_int
        self._cancel.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        self._close = kernel32.CloseHandle
        self._close.restype = ctypes.c_int
        self._close.argtypes = (ctypes.c_void_p,)

    def open(self) -> bool:
        handle = self._create(self.folder, _FILE_LIST_DIRECTORY,
                              _FILE_SHARE_ALL, None, _OPEN_EXISTING,
                              _FILE_FLAG_BACKUP_SEMANTICS, None)
        if not handle or handle == _INVALID_HANDLE:
            return False
        self._handle = handle
        return True

    def _wake(self) -> None:
        with self._handle_lock:
            if self._handle:
                self._cancel(self._handle, None)

    def _run(self) -> None:
        buffer = ctypes.create_string_buffer(_WATCH_BUFFER)
        returned = ctypes.c_uint32(0)
        try:
            while not self._stop.is_set():
                ok = self._read(self._handle, buffer, _WATCH_BUFFER, True,
                                _NOTIFY_FILTER, ctypes.byref(returned),
                                None, None)
                if self._stop.is_set():
                    break
                if not ok:
                    if ctypes.get_last_error() == _ERROR_OPERATION_ABORTED:
                        break
                    self._notify(None)      # la carpeta desapareció…
                    break
                if returned.value == 0:
                    self._notify(None)      # se desbordó: algo cambió
                    continue
                for rel in parse_notify_buffer(buffer.raw[:returned.value]):
                    self._notify(rel)
        finally:
            with self._handle_lock:
                if self._handle:
                    self._close(self._handle)
                    self._handle = None


def parse_notify_buffer(data: bytes) -> List[str]:
    """Rutas de una tanda de FILE_NOTIFY_INFORMATION (UTF-16LE)."""
    paths: List[str] = []
    offset = 0
    while offset + 12 <= len(data):
        next_offset, _action, length = struct.unpack_from("<III", data,
                                                          offset)
        start = offset + 12
        name = data[start:start + length].decode("utf-16-le", "replace")
        paths.append(name)
        if next_offset == 0:
            break
        offset += next_offset
    return paths


# ------------------------------------------------------------------ #
#  Linux: inotify                                                     #
# ------------------------------------------------------------------ #
_IN_MODIFY = 0x00000002
_IN_ATTRIB = 0x00000004
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_FROM = 0x00000040
_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_DELETE_SELF = 0x00000400
_IN_MOVE_SELF = 0x00000800
_IN_Q_OVERFLOW = 0x00004000
_IN_IGNORED = 0x00008000
_IN_ONLYDIR = 0x01000000
_IN_ISDIR = 0x40000000
_IN_NONBLOCK = 0o4000
_IN_CLOEXEC = 0o2000000
_WATCH_MASK = (_IN_MODIFY | _IN_ATTRIB | _IN_CLOSE_WRITE | _IN_MOVED_FROM
               | _IN_MOVED_TO | _IN_CREATE | _IN_DELETE | _IN_DELETE_SELF
               | _IN_MOVE_SELF | _IN_ONLYDIR)
_EVENT_HEADER = struct.Struct("iIII")
# Tope de carpetas vigiladas: pasado, se sigue vigilando lo ya añadido y
# la auto-publicación mantiene su comprobación periódica.
MAX_LINUX_WATCHES = 8192


class LinuxWatcher(FolderWatcher):

    def __init__(self, folder: str, on_change: OnChange) -> None:
        super().__init__(folder, on_change)
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libc.inotify_init1.argtypes = (ctypes.c_int,)
        self._libc.inotify_add_watch.argtypes = (ctypes.c_int,
                                                 ctypes.c_char_p,
                                                 ctypes.c_uint32)
        self._fd = -1
        self._dirs: Dict[int, str] = {}       # vigilancia → ruta relativa
        self.truncated = False

    def open(self) -> bool:
        fd = self._libc.inotify_init1(_IN_NONBLOCK | _IN_CLOEXEC)
        if fd < 0:
            return False
        self._fd = fd
        self._add_tree("")
        return bool(self._dirs)

    def _add(self, rel: str) -> None:
        if len(self._dirs) >= MAX_LINUX_WATCHES:
            self.truncated = True
            return
        path = os.path.join(self.folder, rel) if rel else self.folder
        wd = self._libc.inotify_add_watch(self._fd, os.fsencode(path),
                                          _WATCH_MASK)
        if wd >= 0:
            self._dirs[wd] = rel

    def _add_tree(self, rel: str) -> None:
        """Vigila `rel` y todas sus subcarpetas (sin seguir enlaces)."""
        pending = [rel]
        while pending:
            current = pending.pop()
            self._add(current)
            base = os.path.join(self.folder, current) if current else \
                self.folder
            try:
                entries = list(os.scandir(base))
            except OSError:
                continue
            for entry in entries:
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir and entry.name not in SKIPPED_DIRS:
                    child = entry.name if not current else \
                        current + "/" + entry.name
                    pending.append(child)

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    ready, _w, _x = select.select([self._fd], [], [], 0.5)
                except (OSError, ValueError):
                    break
                if not ready:
                    continue
                try:
                    data = os.read(self._fd, 64 * 1024)
                except BlockingIOError:
                    continue
                except OSError:
                    break
                self._handle_events(data)
        finally:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1

    def _handle_events(self, data: bytes) -> None:
        offset = 0
        while offset + _EVENT_HEADER.size <= len(data):
            wd, mask, _cookie, length = _EVENT_HEADER.unpack_from(data,
                                                                  offset)
            start = offset + _EVENT_HEADER.size
            raw_name = data[start:start + length].rstrip(b"\0")
            offset = start + length
            if mask & _IN_Q_OVERFLOW:
                self._notify(None)
                continue
            if mask & _IN_IGNORED:
                self._dirs.pop(wd, None)
                continue
            parent = self._dirs.get(wd)
            if parent is None:
                continue
            name = os.fsdecode(raw_name)
            rel = name if not parent else (parent + "/" + name if name
                                           else parent)
            if mask & _IN_ISDIR and mask & (_IN_CREATE | _IN_MOVED_TO) \
                    and name not in SKIPPED_DIRS and _relevant(rel):
                self._add_tree(rel)
            self._notify(rel)


# ------------------------------------------------------------------ #
#  Elegir                                                             #
# ------------------------------------------------------------------ #
def start_watcher(folder: str, on_change: OnChange
                  ) -> Optional[FolderWatcher]:
    """Empieza a vigilar `folder`; None si en este sistema no se puede
    (entonces se sigue comprobando cada cierto tiempo)."""
    if not os.path.isdir(folder):
        return None
    try:
        if os.name == "nt":
            watcher: FolderWatcher = WindowsWatcher(folder, on_change)
        elif hasattr(os, "uname") and os.uname().sysname == "Linux":
            watcher = LinuxWatcher(folder, on_change)
        else:
            return None
        if not watcher.open():          # type: ignore[attr-defined]
            return None
    except (OSError, AttributeError, ValueError):
        return None
    watcher.start()
    return watcher
