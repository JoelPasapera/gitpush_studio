# -*- coding: utf-8 -*-
"""¿En qué repositorio publica ya esta carpeta?

Una carpeta que ya es un repositorio de git suele estar conectada a uno
de GitHub: su remoto «origin». Si lo que se pide es publicar en OTRO
(se cargó otro perfil, se soltó otra carpeta en la ventana, la URL se
quedó la del proyecto anterior…), publicar cambiaría ese «origin» sin
decir nada y subiría el proyecto al repositorio equivocado. En modo
espejo, ese repositorio pasaría a ser igual que esta carpeta; con el
push forzado, perdería su historial entero.

Este módulo solo DETECTA el conflicto y lo explica; quién decide es
quien publica:

  · la interfaz pregunta: publicar en el del formulario, en el de la
    carpeta o no publicar;
  · la auto-publicación nunca publica en otro repositorio: se pone en
    pausa y lo explica;
  · la terminal pregunta, y con --yes exige además --switch-remote.

Dos URLs del MISMO repositorio escritas de forma distinta (https y ssh,
mayúsculas, «.git» al final, un usuario o un token dentro) no son un
conflicto: ver `urls.repository_key`.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .git import Git
from .urls import readable_url, same_repository

# Respuestas posibles a «la carpeta publica en otro repositorio».
USE_FORM = "formulario"     # publicar en el pedido (la carpeta cambia de origin)
USE_FOLDER = "carpeta"      # publicar en el que ya tiene la carpeta
CANCEL = "cancelar"         # no publicar nada
CHOICES = (USE_FORM, USE_FOLDER, CANCEL)


@dataclass(frozen=True)
class RemoteConflict:
    """La carpeta `folder` publica en `current`; se pidió `wanted`.

    Las URLs se guardan tal cual (pueden llevar un token dentro, si así
    lo configuró el usuario): para enseñarlas, `current_web` y
    `wanted_web`, que nunca llevan credenciales.
    """
    folder: str
    current: str
    wanted: str

    @property
    def current_web(self) -> str:
        return readable_url(self.current)

    @property
    def wanted_web(self) -> str:
        return readable_url(self.wanted)

    def explanation(self) -> str:
        """Una frase para la consola, la terminal o un aviso."""
        return (f"Esta carpeta ya publica en {self.current_web}, pero se "
                f"pidió publicar en {self.wanted_web}.")


def folder_remote(folder: str, git: Optional[Git] = None) -> str:
    """La URL de «origin» de la carpeta, o "" si no tiene.

    Solo cuenta un repositorio de la propia carpeta (con su `.git`): una
    subcarpeta de otro repositorio heredaría el «origin» del de arriba, y
    publicarla crea un repositorio nuevo dentro, sin remoto todavía.
    """
    if not folder or not (Path(folder) / ".git").exists():
        return ""
    return (git or Git()).remote_url(folder)


# «[remote "origin"]» (y la forma antigua «[remote.origin]»).
_SECTION = re.compile(r'^\s*\[\s*([A-Za-z0-9.-]+)'
                      r'(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]')
_ESCAPES = {"n": "\n", "t": "\t", "b": "\b"}
_MAX_CONFIG = 1024 * 1024


def _config_value(text: str) -> str:
    """Un valor de la configuración de git: sin comillas, sin el
    comentario del final («# …», «; …») y con sus escapes."""
    out: List[str] = []
    quoted = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            following = text[index + 1]
            out.append(_ESCAPES.get(following, following))
            index += 2
            continue
        if char == '"':
            quoted = not quoted
        elif char in "#;" and not quoted:
            break
        else:
            out.append(char)
        index += 1
    return "".join(out).strip()


def configured_origin(folder: str) -> str:
    """La URL de «origin» leída directamente de `.git/config`, sin lanzar
    git: para PROPONER nombres en una lista de cien carpetas (en Windows,
    lanzar un proceso por carpeta se nota). No resuelve «insteadOf» ni
    los «include»: para decidir si se publica se usa `folder_remote`,
    que pregunta a git. "" si no se puede saber."""
    config = Path(folder) / ".git" / "config"
    try:
        # Una configuración de git de más de 1 MB no es una de verdad.
        if not config.is_file() or config.stat().st_size > _MAX_CONFIG:
            return ""
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    in_origin = False
    for line in text.splitlines():
        header = _SECTION.match(line)
        if header:
            section = header.group(1).lower()
            in_origin = (section == "remote" and header.group(2) == "origin"
                         ) or section == "remote.origin"
            continue
        if not in_origin:
            continue
        key, separator, value = line.partition("=")
        if separator and key.strip().lower() == "url":
            return _config_value(value)
    return ""


def find_conflict(folder: str, url: str,
                  git: Optional[Git] = None) -> Optional[RemoteConflict]:
    """El conflicto entre el «origin» de `folder` y `url`, o None si la
    carpeta no tiene remoto todavía o ya es ese mismo repositorio."""
    wanted = (url or "").strip()
    if not wanted:
        return None
    current = folder_remote(folder, git)
    if not current:
        return None
    # Una ruta relativa en «origin» la resuelve git desde la carpeta.
    if same_repository(current, wanted, base_a=folder):
        return None
    return RemoteConflict(folder=str(folder), current=current, wanted=wanted)
