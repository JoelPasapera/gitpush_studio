# -*- coding: utf-8 -*-
"""Interpretación de URLs y nombres de GitHub. Sin red."""

import os
import re
from pathlib import Path

_OWNER_REPO = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
_USER_IN_URL = re.compile(r"github\.com[:/]+([^/\s]+)")
_USERNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_SSH_URL = re.compile(r"^git@([^:]+):(.+?)(\.git)?$")
# «esquema://[usuario[:clave]@]servidor[:puerto]/ruta»
_SCHEME_URL = re.compile(r"^(?:https?|ssh|git)://(?:[^@/]*@)?([^/:]+)"
                         r"(?::\d*)?/(.*)$", re.IGNORECASE)
# El usuario (y el token, si lo hay) dentro de una URL http(s) o ssh://.
_CREDENTIALS = re.compile(r"^((?:https?|ssh|git)://)[^@/]*@", re.IGNORECASE)
# «file:///C:/…» en Windows: la barra de delante sobra.
_FILE_DRIVE = re.compile(r"^/[A-Za-z]:")


def parse_owner_repo(url):
    """Extrae (usuario, repositorio) de una URL de GitHub, o (None, None)."""
    m = _OWNER_REPO.search((url or "").strip())
    return (m.group(1), m.group(2)) if m else (None, None)


def repo_name_from_url(url):
    owner, repo = parse_owner_repo(url)
    if repo:
        return repo
    tail = Path((url or "").rstrip("/")).name
    return tail[:-4] if tail.endswith(".git") else tail


def parse_username(text):
    """Acepta «usuario» o «https://github.com/usuario» y devuelve el
    nombre de usuario, o cadena vacía si no es válido."""
    t = (text or "").strip().rstrip("/")
    m = _USER_IN_URL.search(t)
    if m:
        t = m.group(1)
    return t if _USERNAME.fullmatch(t) else ""


def without_credentials(url):
    """La URL sin el usuario ni el token que pueda llevar dentro
    («https://ana:ghp_…@github.com/ana/app» → «https://github.com/ana/app»).

    Lo que se enseña en la consola, en la barra de estado o en un aviso
    pasa siempre por aquí: un token pegado en la URL del «origin» no
    puede acabar a la vista (ni en un registro)."""
    return _CREDENTIALS.sub(r"\1", (url or "").strip())


def repo_web_url(url):
    """Convierte la URL del remoto (https o git@) en una URL navegable,
    sin credenciales."""
    u = without_credentials(url)
    m = _SSH_URL.match(u)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    return re.sub(r"\.git/?$", "", u)


def readable_url(url):
    """La URL tal como conviene enseñarla: navegable y sin credenciales.
    Un repositorio local se enseña con su ruta tal cual."""
    text = (url or "").strip()
    if repository_key(text).startswith("local:"):
        return text
    return repo_web_url(text)


def _local_key(path, base=""):
    """Ruta absoluta y normalizada de un repositorio local («/x/repo»,
    «file:///x/repo», «..\\repo» desde `base`)."""
    if path.lower().startswith("file://"):
        path = path[len("file://"):]
        if _FILE_DRIVE.match(path):
            path = path[1:]
    path = os.path.expanduser(path)
    if not os.path.isabs(path) and base:
        path = os.path.join(base, path)
    resolved = os.path.realpath(path)
    return os.path.normcase(resolved.rstrip("/\\") or resolved)


def repository_key(url, base=""):
    """Lo que identifica un repositorio, escrito de cualquier forma.

    «https://github.com/Ana/App.git», «git@github.com:ana/app» y
    «ssh://git@github.com/ana/app/» son el MISMO repositorio: las tres
    dan «github.com/ana/app» (GitHub no distingue mayúsculas en el
    usuario ni en el nombre). El usuario y el token que lleve dentro no
    cuentan. Un repositorio local (una ruta o «file://») da «local:» y
    su ruta absoluta; una ruta relativa se resuelve desde `base` (git
    resuelve la del «origin» desde la carpeta del proyecto).

    Devuelve "" si no hay URL.
    """
    text = (url or "").strip()
    if not text:
        return ""
    ssh = _SSH_URL.match(text)
    scheme = None if ssh else _SCHEME_URL.match(text)
    if ssh:
        host, path = ssh.group(1), ssh.group(2)
    elif scheme:
        host, path = scheme.group(1), scheme.group(2)
    else:
        return "local:" + _local_key(text, base)
    path = path.strip("/")
    if path.lower().endswith(".git"):
        path = path[:-len(".git")].rstrip("/")
    return f"{host.lower()}/{path.lower()}"


def same_repository(a, b, base_a="", base_b=""):
    """¿Son `a` y `b` el mismo repositorio (ver `repository_key`)?"""
    key = repository_key(a, base_a)
    return bool(key) and key == repository_key(b, base_b)


def is_remote_url(url):
    """URL aceptable como remoto: https, ssh, file:// o un repositorio
    local existente (útil para espejos locales y pruebas)."""
    u = (url or "").strip()
    if not u:
        return False
    if u.startswith(("https://", "git@", "ssh://", "file://")):
        return True
    return os.path.isdir(u)


def http_origin(url):
    """«https://github.com/» para una URL http(s); cadena vacía si no."""
    m = re.match(r"^(https?://[^/]+/)", (url or "").strip())
    return m.group(1) if m else ""
