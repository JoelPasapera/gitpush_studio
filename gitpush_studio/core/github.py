# -*- coding: utf-8 -*-
"""Cliente mínimo de la API de GitHub (sin dependencias)."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from .errors import StepError

GITHUB_API = "https://api.github.com"
USER_AGENT = "GitPush-Studio"


def api(method, path, token="", payload=None, timeout=30, retries=1):
    """Devuelve (código_http, cuerpo_json).

    Reintenta una vez ante errores 5xx o de red; convierte el límite
    de peticiones agotado en un StepError con la hora de reinicio.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None \
        else None
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(GITHUB_API + path, data=data,
                                 method=method, headers=headers)
    attempt = 0
    while True:
        attempt += 1
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
                return r.status, (json.loads(body) if body.strip() else {})
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code >= 500 and attempt <= retries:
                time.sleep(1.5)
                continue
            if e.code in (403, 429) and \
                    e.headers.get("X-RateLimit-Remaining") == "0":
                raise StepError(_rate_limit_message(e.headers)) from None
            try:
                return e.code, (json.loads(body) if body.strip() else {})
            except ValueError:
                return e.code, {"message": body[:200]}
        except urllib.error.URLError:
            if attempt <= retries:
                time.sleep(1.5)
                continue
            raise


def upload_asset(owner, repo, release_id, token, name, data,
                 content_type="application/zip", timeout=300):
    """Sube un archivo adjunto a una release. Devuelve (código, cuerpo)."""
    url = (f"https://uploads.github.com/repos/{owner}/{repo}/releases/"
           f"{release_id}/assets?name={urllib.parse.quote(name)}")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "Content-Type": content_type,
        "Content-Length": str(len(data)),
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, (json.loads(body) if body.strip() else {})
        except ValueError:
            return e.code, {"message": body[:200]}


def _rate_limit_message(headers):
    reset = headers.get("X-RateLimit-Reset")
    when = ""
    try:
        when = datetime.fromtimestamp(int(reset)).strftime(" a las %H:%M")
    except (TypeError, ValueError):
        pass
    return ("GitHub limitó las peticiones (403). Se reinicia" + when +
            ". Añade un token para subir el límite.")


def _message(body, default="error desconocido"):
    return body.get("message", default) if isinstance(body, dict) \
        else default


def whoami(token):
    """Login del dueño del token, o cadena vacía."""
    if not token:
        return ""
    code, me = api("GET", "/user", token)
    return (me.get("login") or "") if code == 200 else ""


def list_user_repos(username, token="", max_pages=30):
    """Repositorios de un perfil (paginado). Con token, y si el perfil
    es el del propio token, incluye también los privados. Devuelve
    dicts con name, clone_url, fork, private y size_kb."""
    login = whoami(token)
    own = bool(token) and login.lower() == username.lower()
    repos, page = [], 1
    while True:
        if own:
            path = (f"/user/repos?per_page=100&page={page}"
                    f"&affiliation=owner&sort=full_name")
        else:
            path = (f"/users/{username}/repos?per_page=100&page={page}"
                    f"&sort=full_name")
        code, body = api("GET", path, token)
        if code == 404:
            raise StepError(f"El usuario «{username}» no existe en GitHub.")
        if code == 403:
            raise StepError("GitHub limitó las peticiones (403). "
                            "Añade un token para subir el límite.")
        if code != 200 or not isinstance(body, list):
            raise StepError(f"GitHub respondió {code}: {_message(body, '')}")
        for r in body:
            repos.append({"name": r.get("name", ""),
                          "clone_url": r.get("clone_url", ""),
                          "fork": bool(r.get("fork")),
                          "private": bool(r.get("private")),
                          "size_kb": int(r.get("size") or 0)})
        if len(body) < 100 or page >= max_pages:
            break
        page += 1
    return repos


def create_repo(token, name, description="", private=False):
    """Crea un repositorio en la cuenta del token.

    Devuelve (clone_url, html_url, ya_existía)."""
    code, me = api("GET", "/user", token)
    if code == 401:
        raise StepError("Token inválido o caducado. Genera uno nuevo en "
                        "GitHub.")
    if code != 200:
        raise StepError(f"GitHub respondió {code}: {_message(me)}")
    login = me.get("login", "")

    payload = {"name": name, "private": private,
               "description": description, "auto_init": False}
    code, body = api("POST", "/user/repos", token, payload)
    if code == 201:
        clone = body.get("clone_url") \
            or f"https://github.com/{login}/{name}.git"
        html = body.get("html_url") or f"https://github.com/{login}/{name}"
        return clone, html, False
    if code == 422 and "already exists" in json.dumps(body).lower():
        clone = f"https://github.com/{login}/{name}.git"
        return clone, clone[:-4], True
    if code == 403:
        raise StepError("GitHub denegó la operación (403). ¿El token "
                        "tiene el permiso «repo»?")
    raise StepError(f"No se pudo crear ({code}): {_message(body)}")


def get_repo(owner, repo, token=""):
    """Metadatos del repositorio. Lanza StepError con explicación."""
    code, info = api("GET", f"/repos/{owner}/{repo}", token)
    if code == 401:
        raise StepError("Token inválido o caducado.")
    if code == 404:
        raise StepError("No encuentro el repositorio. ¿Ya lo publicaste "
                        "con «🚀»?")
    if code != 200:
        raise StepError(f"GitHub respondió {code}: {_message(info, '')}")
    return info


def enable_pages(owner, repo, token, branch="main", path="/",
                 on_status=None, wait_seconds=90):
    """Activa (o actualiza) GitHub Pages y espera a que se construya.
    Devuelve la URL de la web."""
    say = on_status or (lambda text: None)
    base = f"/repos/{owner}/{repo}"
    info = get_repo(owner, repo, token)
    if info.get("private"):
        raise StepError("El repositorio es privado y GitHub Pages gratuito "
                        "solo funciona con públicos. Hazlo público en "
                        "github.com → Settings y vuelve a intentarlo.")

    src = {"source": {"branch": branch, "path": path}}
    code, pages = api("GET", base + "/pages", token)
    if code == 404:
        say("Activando GitHub Pages…")
        code, pages = api("POST", base + "/pages", token, src)
        if code == 409:
            code, pages = api("GET", base + "/pages", token)
        elif code == 422:
            raise StepError("GitHub no aceptó la configuración. ¿Existe la "
                            f"rama «{branch}» en el repositorio? Publica "
                            "primero con «🚀».")
        elif code not in (200, 201):
            raise StepError(f"No se pudo activar ({code}): "
                            f"{_message(pages, '')}")
    elif code == 200:
        cur = pages.get("source") or {}
        if cur.get("branch") != branch or cur.get("path") != path:
            say("Actualizando el origen de la web…")
            api("PUT", base + "/pages", token, src)
    else:
        raise StepError(f"GitHub respondió {code}: {_message(pages, '')}")

    url = (pages or {}).get("html_url") \
        or f"https://{owner}.github.io/{repo}/"
    say("La web se está construyendo… (suele tardar menos de un minuto)")
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(3)
        code, pages = api("GET", base + "/pages", token)
        if code != 200:
            continue
        url = pages.get("html_url") or url
        st = (pages.get("status") or "").lower()
        if st == "built":
            break
        if st == "errored":
            raise StepError("GitHub no pudo construir la web. Revisa que "
                            "haya un index.html en el origen elegido.")
    return url
