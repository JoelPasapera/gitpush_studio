# -*- coding: utf-8 -*-
"""Interpretación de URLs y nombres de GitHub. Sin red."""

import os
import re
from pathlib import Path

_OWNER_REPO = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
_USER_IN_URL = re.compile(r"github\.com[:/]+([^/\s]+)")
_USERNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_SSH_URL = re.compile(r"^git@([^:]+):(.+?)(\.git)?$")


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


def repo_web_url(url):
    """Convierte la URL del remoto (https o git@) en una URL navegable."""
    u = (url or "").strip()
    m = _SSH_URL.match(u)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    return re.sub(r"\.git/?$", "", u)


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
