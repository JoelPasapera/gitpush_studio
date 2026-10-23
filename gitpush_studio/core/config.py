# -*- coding: utf-8 -*-
"""Configuración persistente (~/.gitpush_studio/profiles.json).

Dos garantías que antes no existían:
  · un archivo ilegible se aparta como copia de seguridad y se avisa,
    en vez de sobrescribirlo en silencio en el siguiente guardado;
  · cada guardado es atómico (temporal + os.replace): un corte a mitad
    de escritura no deja el archivo a medias.
"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from . import secrets

CONFIG_DIR = Path.home() / ".gitpush_studio"
CONFIG_FILE = CONFIG_DIR / "profiles.json"
SCHEMA_VERSION = 1


def defaults():
    return {"version": SCHEMA_VERSION, "profiles": {},
            "last_profile": None, "last_state": {}}


def _migrate(data):
    data.setdefault("version", SCHEMA_VERSION)
    data.setdefault("profiles", {})
    data.setdefault("last_state", {})
    data.setdefault("last_profile", None)
    if not isinstance(data["profiles"], dict):
        data["profiles"] = {}
    return data


def load_config(path=CONFIG_FILE):
    """Devuelve (datos, aviso). `aviso` es None salvo que el archivo
    existiera y no se pudiera leer: entonces describe la copia hecha."""
    path = Path(path)
    if not path.exists():
        return defaults(), None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("el contenido no es un objeto JSON")
        return _migrate(data), None
    except (OSError, ValueError) as exc:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        try:
            os.replace(path, backup)
            where = f"se guardó una copia en {backup.name}"
        except OSError:
            where = "no se pudo hacer copia"
        return defaults(), (f"No se pudo leer la configuración ({exc}); "
                            f"{where}. Se empieza con una vacía.")


def save_config(data, path=CONFIG_FILE):
    """Guardado atómico. Lanza OSError si no se puede escribir."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".profiles-", suffix=".tmp",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------------ #
#  Token                                                              #
# ------------------------------------------------------------------ #
def saved_token(data):
    return secrets.unprotect_token(data.get("github_token"))


def store_token(data, token, remember):
    """Actualiza `data` (no guarda en disco). Devuelve True si el token
    quedó en texto plano, para que la interfaz lo avise."""
    old = data.get("github_token")
    if remember and token:
        data["github_token"] = secrets.protect_token(token)
        return secrets.is_plaintext(data["github_token"])
    secrets.forget_token(old)
    data.pop("github_token", None)
    return False
