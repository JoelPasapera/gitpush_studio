# -*- coding: utf-8 -*-
"""Guardado del token de GitHub.

    Windows → DPAPI (cifrado con la cuenta del usuario, vía ctypes)
    macOS   → Llavero del sistema (`security`)
    Linux   → Secret Service (`secret-tool`, si está instalado)
    resto   → base64, que NO es cifrado: `is_plaintext` lo delata para
              que la interfaz lo avise en vez de decir «cifrado».

La entrada guardada en la configuración nunca contiene el token cuando
se usa el llavero: solo una marca de dónde está.
"""

import base64
import os
import subprocess
import sys

from .fs import NO_WINDOW

SERVICE = "GitPush Studio"
ACCOUNT = "github-token"


# ------------------------------------------------------------------ #
#  Windows: DPAPI                                                     #
# ------------------------------------------------------------------ #
def _dpapi(data, encrypt):
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_byte))]

    buf_in = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data),
                        ctypes.cast(buf_in, ctypes.POINTER(ctypes.c_byte)))
    blob_out = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32                # type: ignore[attr-defined]
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI falló")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)  # type: ignore


# ------------------------------------------------------------------ #
#  macOS / Linux: llavero del sistema                                 #
# ------------------------------------------------------------------ #
def _run(args, stdin_text=None):
    p = subprocess.run(args, input=stdin_text, capture_output=True,
                       text=True, timeout=15, creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise OSError((p.stderr or p.stdout or "").strip()[:200])
    return (p.stdout or "").strip()


_SAFE_TOKEN = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                        "0123456789_-.")


def _keychain_set(token):
    if sys.platform == "darwin":
        if token and set(token) <= _SAFE_TOKEN:
            # Por la ENTRADA estándar de `security -i`, no como argumento:
            # los argumentos de un proceso los ve cualquier programa del
            # equipo mientras dura la llamada (`ps`). El juego de
            # caracteres está acotado, así que no hay comillas que escapar.
            _run(["security", "-i"],
                 stdin_text=(f'add-generic-password -U -a {ACCOUNT} '
                             f'-s "{SERVICE}" -w {token}\n'))
            if _keychain_get() != token:
                raise OSError("el llavero no guardó el token")
        else:
            _run(["security", "add-generic-password", "-U", "-a", ACCOUNT,
                  "-s", SERVICE, "-w", token])
    else:
        _run(["secret-tool", "store", f"--label={SERVICE}",
              "service", SERVICE, "account", ACCOUNT], stdin_text=token)


def _keychain_get():
    if sys.platform == "darwin":
        return _run(["security", "find-generic-password", "-a", ACCOUNT,
                     "-s", SERVICE, "-w"])
    return _run(["secret-tool", "lookup", "service", SERVICE,
                 "account", ACCOUNT])


def _keychain_delete():
    try:
        if sys.platform == "darwin":
            _run(["security", "delete-generic-password", "-a", ACCOUNT,
                  "-s", SERVICE])
        else:
            _run(["secret-tool", "clear", "service", SERVICE,
                  "account", ACCOUNT])
    except (OSError, subprocess.SubprocessError):
        pass


# ------------------------------------------------------------------ #
#  API pública                                                        #
# ------------------------------------------------------------------ #
def protect_token(token):
    """Devuelve la entrada a guardar en la configuración."""
    raw = token.encode("utf-8")
    if os.name == "nt":
        try:
            return {"v": base64.b64encode(_dpapi(raw, True)).decode(),
                    "mode": "dpapi"}
        except Exception:                                   # noqa: BLE001
            pass
    else:
        try:
            _keychain_set(token)
            return {"mode": "keychain"}
        except (OSError, subprocess.SubprocessError):
            pass
    return {"v": base64.b64encode(raw).decode(), "mode": "b64"}


def unprotect_token(entry):
    if not isinstance(entry, dict):
        return ""
    mode = entry.get("mode")
    try:
        if mode == "keychain":
            return _keychain_get() if os.name != "nt" else ""
        raw = base64.b64decode(entry.get("v", ""))
        if mode == "dpapi":
            if os.name != "nt":
                return ""
            raw = _dpapi(raw, False)
        return raw.decode("utf-8")
    except Exception:                                       # noqa: BLE001
        return ""


def forget_token(entry):
    """Borra el secreto del llavero si estaba allí."""
    if isinstance(entry, dict) and entry.get("mode") == "keychain":
        _keychain_delete()


def is_plaintext(entry):
    """True cuando el token está guardado sin cifrar (modo b64)."""
    return isinstance(entry, dict) and entry.get("mode") == "b64"
