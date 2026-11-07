# -*- coding: utf-8 -*-
"""¿Hay una versión nueva de GitPush Studio?

Se pregunta a GitHub por la última «release» del repositorio de la app
(el que indiques en Opciones: «usuario/repositorio»), como mucho una vez
al día, y solo se avisa: nunca se descarga ni se instala nada solo. Sin
repositorio indicado no se pregunta nada a nadie.
"""

import json
import re
import time
from typing import Callable, Optional, Tuple

from .net import open_url, read_limited

CHECK_EVERY = 24 * 60 * 60          # segundos entre comprobaciones
API = "https://api.github.com/repos/{repo}/releases/latest"
_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")

Version = Tuple[int, int, int]


def parse_version(text: str) -> Optional[Version]:
    """(mayor, menor, parche) de «v3.4.0», «3.4» o «GitPush 3.4.1»."""
    match = _VERSION.search(text or "")
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)),
            int(match.group(3) or 0))


def valid_repo(repo: str) -> bool:
    return bool(_REPO.match((repo or "").strip()))


def due(last_check: float, now: Optional[float] = None) -> bool:
    """¿Toca comprobar? (una vez al día)."""
    now = time.time() if now is None else now
    try:
        last = float(last_check or 0)
    except (TypeError, ValueError):
        last = 0.0
    return now - last >= CHECK_EVERY or now < last


def latest_release(repo: str, opener: Callable = open_url,
                   timeout: float = 10) -> Optional[Tuple[str, str]]:
    """(etiqueta, página) de la última release de `repo`, o None."""
    if not valid_repo(repo):
        return None
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "GitPush-Studio"}
    try:
        with opener(API.format(repo=repo.strip()), headers=headers,
                    timeout=timeout) as response:
            data = json.loads(read_limited(response, 512 * 1024))
    except (OSError, ValueError):
        return None
    tag = str(data.get("tag_name") or "")
    page = str(data.get("html_url") or "")
    if not tag or not page.startswith("https://github.com/"):
        return None
    return tag, page


def newer_than(current: str, repo: str, opener: Callable = open_url
               ) -> Optional[Tuple[str, str]]:
    """(etiqueta, página) si en `repo` hay una versión más nueva que
    `current`, o None."""
    found = latest_release(repo, opener)
    if found is None:
        return None
    mine = parse_version(current)
    theirs = parse_version(found[0])
    if mine is None or theirs is None or theirs <= mine:
        return None
    return found
