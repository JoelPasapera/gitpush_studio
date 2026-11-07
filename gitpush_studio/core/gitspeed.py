# -*- coding: utf-8 -*-
"""`git status` más rápido en las carpetas grandes, sin tocar tus archivos.

Publicar, la revisión de seguridad y la auto-publicación preguntan a git
qué ha cambiado. En un proyecto grande eso significa recorrer toda la
carpeta cada vez. Git tiene dos ayudas que vienen apagadas:

  · core.untrackedCache: recuerda qué carpetas no cambiaron desde la
    última vez y no vuelve a listarlas. Solo es fiable si el sistema de
    archivos actualiza la fecha de una carpeta al crear o borrar algo
    dentro: los discos locales normales (NTFS, ext4, APFS…) sí; algunos
    de red o FAT no. Por eso solo se activa en los que se sabe que sí.
  · core.fsmonitor: un proceso de git en segundo plano que escucha al
    sistema operativo y sabe de antemano qué cambió (solo existe en
    Windows y macOS, y solo merece la pena en proyectos grandes).

Ambas viven en .git/config (que nunca se publica) y solo se ponen si no
tienes ya un valor tuyo para ellas, en cualquier nivel de configuración:
lo que hayas decidido tú siempre manda.
"""

import ctypes
import os
import struct
from pathlib import Path
from typing import Callable, List, Optional, Set, Tuple

from .git import Git

# Sistemas de archivos en los que la caché de no rastreados es fiable.
UNTRACKED_CACHE_SAFE = frozenset(("ntfs", "refs", "ext2", "ext3", "ext4",
                                  "xfs", "btrfs", "f2fs", "zfs", "tmpfs",
                                  "apfs", "hfs"))
# A partir de cuántos archivos rastreados compensa el proceso de fondo.
FSMONITOR_MIN_FILES = 2000

_TUNED: Set[str] = set()                  # carpetas ya revisadas en esta sesión


# ------------------------------------------------------------------ #
#  ¿Qué disco es?                                                     #
# ------------------------------------------------------------------ #
def _windows_volume(path: str) -> Tuple[str, bool]:
    """(sistema de archivos en minúsculas, ¿es de red?) en Windows."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    root = ctypes.create_unicode_buffer(1024)
    if not kernel32.GetVolumePathNameW(ctypes.c_wchar_p(path), root, 1024):
        return "", False
    drive_type = kernel32.GetDriveTypeW(ctypes.c_wchar_p(root.value))
    name = ctypes.create_unicode_buffer(64)
    ok = kernel32.GetVolumeInformationW(ctypes.c_wchar_p(root.value), None,
                                        0, None, None, None, name, 64)
    remote = drive_type == 4                  # DRIVE_REMOTE
    return (name.value.lower() if ok else ""), remote


def _unescape_mount(field: str) -> str:
    """/proc/mounts escribe los espacios como \\040 (en octal)."""
    out = []
    index = 0
    while index < len(field):
        code = field[index + 1:index + 4]
        if field[index] == "\\" and len(code) == 3 and code.isdigit():
            out.append(chr(int(code, 8)))
            index += 4
        else:
            out.append(field[index])
            index += 1
    return "".join(out)


def _linux_filesystem(path: str, mounts_file: str = "/proc/mounts") -> str:
    """El tipo de sistema de archivos del punto de montaje más largo que
    contiene `path`."""
    real = os.path.realpath(path)
    best = ""
    best_type = ""
    try:
        with open(mounts_file, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return ""
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        mount_point = _unescape_mount(fields[1])
        inside = (real == mount_point or real.startswith(
            mount_point.rstrip("/") + "/"))
        if inside and len(mount_point) >= len(best):
            best = mount_point
            best_type = fields[2].lower()
    return best_type


def filesystem_type(path: str) -> Tuple[str, bool]:
    """(sistema de archivos, ¿es de red?); ("", False) si no se sabe."""
    try:
        if os.name == "nt":
            return _windows_volume(path)
        if hasattr(os, "uname") and os.uname().sysname == "Linux":
            return _linux_filesystem(path), False
    except (OSError, AttributeError, ValueError):
        pass
    return "", False


# ------------------------------------------------------------------ #
#  Git                                                                #
# ------------------------------------------------------------------ #
def tracked_count(folder: str) -> int:
    """Archivos del índice, leídos de la cabecera de .git/index (12 bytes:
    «DIRC», versión y número de entradas), sin lanzar git."""
    try:
        with open(os.path.join(folder, ".git", "index"), "rb") as handle:
            head = handle.read(12)
    except OSError:
        return 0
    if len(head) < 12 or head[:4] != b"DIRC":
        return 0
    return struct.unpack(">I", head[8:12])[0]


def has_fsmonitor_daemon(git: Git) -> bool:
    """¿Trae este git el proceso de vigilancia integrado?"""
    process = git.run(["version", "--build-options"], check=False,
                      echo=False)
    return "fsmonitor--daemon" in (process.stdout or "")


def _is_unset(git: Git, folder: str, key: str) -> bool:
    """¿Nadie ha dado valor a `key` (ni en el repositorio, ni en tu
    configuración global, ni en la del sistema)?"""
    process = git.run(["config", "--get", key], cwd=folder, check=False,
                      echo=False)
    return process.returncode == 1


def tune(folder: str, git: Optional[Git] = None,
         log: Optional[Callable[[str, str], None]] = None) -> List[str]:
    """Activa lo que haga falta (y sea seguro) en el repositorio de
    `folder`. Devuelve los ajustes que puso. Cada carpeta se revisa una
    sola vez por sesión."""
    key = os.path.normcase(os.path.abspath(folder))
    if key in _TUNED or not (Path(folder) / ".git").is_dir():
        return []
    _TUNED.add(key)
    git = git or Git()
    applied: List[str] = []
    filesystem, remote = filesystem_type(folder)
    if filesystem in UNTRACKED_CACHE_SAFE and not remote and \
            _is_unset(git, folder, "core.untrackedCache"):
        done = git.run(["config", "--local", "core.untrackedCache", "true"],
                       cwd=folder, check=False, echo=False)
        if done.returncode == 0:
            applied.append("core.untrackedCache")
    if fsmonitor_platform() and not remote and \
            tracked_count(folder) >= FSMONITOR_MIN_FILES and \
            _is_unset(git, folder, "core.fsmonitor") and \
            has_fsmonitor_daemon(git):
        done = git.run(["config", "--local", "core.fsmonitor", "true"],
                       cwd=folder, check=False, echo=False)
        if done.returncode == 0:
            applied.append("core.fsmonitor")
    if applied and log is not None:
        log("  ⚡ Para que git responda más rápido en esta carpeta activé "
            + " y ".join(applied) + " (solo en su .git/config, que no se "
            "publica).", "dim")
    return applied


def fsmonitor_platform() -> bool:
    """¿Es un sistema en el que git tiene su vigilante integrado?"""
    if os.name == "nt":
        return True
    return hasattr(os, "uname") and os.uname().sysname == "Darwin"


def forget(folder: Optional[str] = None) -> None:
    """Olvida qué carpetas se revisaron (para las pruebas)."""
    if folder is None:
        _TUNED.clear()
    else:
        _TUNED.discard(os.path.normcase(os.path.abspath(folder)))
