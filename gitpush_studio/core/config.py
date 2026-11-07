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
import shutil
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


# Clave interna (no se guarda): la configuración original no se pudo
# apartar, así que guardar encima la destruiría.
_NO_SAVE = "_no_sobrescribir"
INTERVALS = (2, 5, 10, 30)
_TEXT_KEYS = ("folder", "user", "email", "url", "branch", "ignore",
              "license", "license_owner", "message", "consolidate_path")
_BOOL_KEYS = ("local_identity", "stamp", "auto_message", "force", "check",
              "preview", "use_token", "consolidate")


def clean_state(s):
    """Un estado del formulario (o un perfil) con los tipos correctos.

    El archivo lo puede haber editado una persona, otra versión de la
    app o un programa de sincronización: un `"check": "no"` o un
    `"auto_interval": "abc"` no pueden tumbar la app al arrancar. Lo que
    no se entiende se descarta y queda el valor por defecto.
    """
    if not isinstance(s, dict):
        return {}
    out = {}
    for k in _TEXT_KEYS:
        v = s.get(k)
        if isinstance(v, str):
            out[k] = v
    for k in _BOOL_KEYS:
        v = s.get(k)
        if isinstance(v, bool):
            out[k] = v
    if s.get("mode") in ("mirror", "addedit"):
        out["mode"] = s["mode"]
    try:
        intervalo = int(s.get("auto_interval", 10))
    except (TypeError, ValueError, OverflowError):
        intervalo = 10
    out["auto_interval"] = intervalo if intervalo in INTERVALS else 10
    for k, v in s.items():                 # lo desconocido se conserva
        if k not in out and k not in _TEXT_KEYS + _BOOL_KEYS + (
                "mode", "auto_interval"):
            out[k] = v
    return out


def _migrate(data):
    """Completa y valida los datos. Devuelve (datos, problemas): lo que
    no tenía la forma esperada se arregla, pero se ANOTA, para que quien
    carga haga antes una copia del archivo tal cual estaba."""
    problemas = []
    if not isinstance(data.get("version"), int):
        data["version"] = SCHEMA_VERSION
    perfiles = data.get("profiles", {})
    if not isinstance(perfiles, dict):
        problemas.append("la lista de perfiles no tenía el formato esperado")
        perfiles = {}
    limpios = {}
    for nombre, perfil in perfiles.items():
        if isinstance(nombre, str) and isinstance(perfil, dict):
            limpios[nombre] = perfil
        else:
            problemas.append(f"el perfil «{nombre}» no era válido")
    data["profiles"] = limpios
    if not isinstance(data.get("last_state", {}), dict):
        problemas.append("el último estado no era válido")
        data["last_state"] = {}
    data.setdefault("last_state", {})
    ultimo = data.get("last_profile")
    if ultimo is not None and not isinstance(ultimo, str):
        problemas.append("el último perfil no era válido")
        data["last_profile"] = None
    data.setdefault("last_profile", None)
    return data, problemas


def _backup_name(path):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup, n = path.with_name(f"{path.name}.bak-{stamp}"), 2
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak-{stamp}-{n}")
        n += 1
    return backup


def load_config(path=CONFIG_FILE):
    """Devuelve (datos, aviso). `aviso` es None salvo que el archivo
    existiera y no se pudiera leer entero: entonces describe la copia
    hecha. Si ni siquiera se pudo copiar, los datos quedan marcados para
    NO guardarse encima (se perdería lo que no se pudo leer)."""
    path = Path(path)
    if not path.exists():
        return defaults(), None
    try:
        # «utf-8-sig»: el Bloc de notas añade una marca BOM al guardar.
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("el contenido no es un objeto JSON")
    except (OSError, ValueError, RecursionError) as exc:
        backup = _backup_name(path)
        try:
            os.replace(path, backup)
            where = f"se guardó una copia en {backup.name}"
            datos = defaults()
        except OSError:
            datos = defaults()
            datos[_NO_SAVE] = True
            where = ("no se pudo apartar el original, así que no se "
                     "guardará nada encima hasta que se pueda leer")
        return datos, (f"No se pudo leer la configuración ({exc}); "
                       f"{where}. Se empieza con una vacía.")
    data, problemas = _migrate(data)
    if not problemas:
        return data, None
    backup = _backup_name(path)
    try:
        shutil.copy2(path, backup)
        where = f"el archivo original está copiado en {backup.name}"
    except OSError:
        data[_NO_SAVE] = True
        where = ("no se pudo copiar el original, así que no se guardará "
                 "nada encima")
    return data, ("La configuración tenía partes dañadas ("
                  + "; ".join(problemas[:3]) + f"); {where}.")


def save_config(data, path=CONFIG_FILE):
    """Guardado atómico. Lanza OSError si no se puede escribir (o si no
    se debe: ver `load_config`)."""
    if data.get(_NO_SAVE):
        raise OSError("la configuración original no se pudo leer ni "
                      "apartar; no la sobrescribo")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    limpio = {k: v for k, v in data.items() if not k.startswith("_")}
    fd, tmp = tempfile.mkstemp(prefix=".profiles-", suffix=".tmp",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(limpio, f, ensure_ascii=False, indent=2)
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
