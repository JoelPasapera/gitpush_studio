# -*- coding: utf-8 -*-
"""Cliente mínimo de la API de GitHub (sin dependencias)."""

import http.client
import json
import time
import urllib.error
import urllib.parse
from datetime import datetime

from .errors import StepError
from .net import open_url, read_limited

GITHUB_API = "https://api.github.com"
USER_AGENT = "GitPush-Studio"
# Mensaje único: `list_user_repos` lo reconoce para no reintentar con
# REST, que fallaría igual y con una explicación peor.
TOKEN_INVALID = "Token inválido o caducado."


def _decode_json(raw):
    """JSON de la respuesta, o {"message": …} si no lo es (un proxy, una
    página de error): nunca una excepción."""
    texto = raw.decode("utf-8", "replace")
    if not texto.strip():
        return {}
    try:
        return json.loads(texto)
    except (ValueError, RecursionError):
        return {"message": texto[:200]}


def _transport_error(e):
    return StepError(f"No pude conectar con GitHub ({type(e).__name__}). "
                     "Revisa tu conexión y vuelve a intentarlo.")


def api(method, path, token="", payload=None, timeout=30, retries=1):
    """Devuelve (código_http, cuerpo_json).

    Reintenta una vez ante errores 5xx o de red; convierte el límite
    de peticiones agotado en un StepError con la hora de reinicio. Los
    fallos de red, las respuestas que no son JSON y las desmesuradas se
    convierten en StepError o en un cuerpo {"message": …}: nunca llegan
    como excepciones sueltas a quien llama.
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
    attempt = 0
    while True:
        attempt += 1
        try:
            with open_url(GITHUB_API + path, headers, data, method,
                          timeout) as r:
                return r.status, _decode_json(read_limited(r))
        except urllib.error.HTTPError as e:
            try:
                body = _decode_json(read_limited(e))
            except (OSError, ValueError, http.client.HTTPException):
                body = {}
            finally:
                _close_quietly(e)      # la conexión, también en los errores
            if e.code >= 500 and attempt <= retries:
                time.sleep(1.5)
                continue
            if e.code in (403, 429) and (
                    e.headers.get("X-RateLimit-Remaining") == "0"
                    or "rate limit" in str(_message(body, "")).lower()):
                raise StepError(_rate_limit_message(e.headers)) from None
            return e.code, body
        except (urllib.error.URLError, OSError,
                http.client.HTTPException, ValueError) as e:
            if attempt <= retries:
                time.sleep(1.5)
                continue
            raise _transport_error(e) from None


def upload_asset(owner, repo, release_id, token, name, data,
                 content_type="application/zip", timeout=300):
    """Sube un archivo adjunto a una release. Devuelve (código, cuerpo)."""
    url = (f"https://uploads.github.com/repos/{_seg(owner)}/{_seg(repo)}/"
           f"releases/{int(release_id)}/assets?name={urllib.parse.quote(name)}")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "Content-Type": content_type,
        "Content-Length": str(len(data)),
        "User-Agent": USER_AGENT,
    }
    try:
        with open_url(url, headers, data, "POST", timeout) as r:
            return r.status, _decode_json(read_limited(r))
    except urllib.error.HTTPError as e:
        try:
            return e.code, _decode_json(read_limited(e))
        except (OSError, ValueError, http.client.HTTPException):
            return e.code, {}
        finally:
            _close_quietly(e)
    except (urllib.error.URLError, OSError, http.client.HTTPException,
            ValueError) as e:
        raise _transport_error(e) from None


def _close_quietly(respuesta):
    """Cierra la respuesta de un error HTTP: guarda la conexión abierta y,
    si no se cierra, el socket se queda colgando hasta que lo recoja la
    basura (con un aviso de recurso sin cerrar)."""
    try:
        respuesta.close()
    except Exception:                                       # noqa: BLE001
        pass


def _seg(texto):
    """Un tramo de ruta de la API, escapado (nunca «..» ni «/»)."""
    return urllib.parse.quote(str(texto or ""), safe="")


def _int(valor):
    try:
        return max(0, int(valor or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _rate_limit_message(headers):
    reset = headers.get("X-RateLimit-Reset")
    when = ""
    try:
        when = datetime.fromtimestamp(int(reset)).strftime(" a las %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return ("GitHub limitó las peticiones (403). Se reinicia" + when +
            ". Añade un token para subir el límite.")


def _message(body, default="error desconocido"):
    return body.get("message", default) if isinstance(body, dict) \
        else default


# Una sola consulta trae 100 repositorios con exactamente los campos
# que hacen falta, e incluye de paso el login del dueño (con REST harían
# falta dos llamadas: /user y /users/X/repos). GraphQL EXIGE token, así
# que sin él se sigue usando REST.
REPOS_QUERY = """
query($login: String!, $after: String) {
  repositoryOwner(login: $login) {
    login
    repositories(first: 100, after: $after, ownerAffiliations: OWNER,
                 orderBy: {field: NAME, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        url
        isFork
        isPrivate
        diskUsage
        defaultBranchRef { name }
        owner { login }
      }
    }
  }
}
"""


def graphql(query, variables, token, timeout=30):
    """Lanza una consulta GraphQL. Devuelve el bloque «data».

    GraphQL responde 200 aunque la consulta falle: el error viene dentro,
    en «errors», así que hay que mirarlo a mano.
    """
    code, body = api("POST", "/graphql", token,
                     {"query": query, "variables": variables},
                     timeout=timeout)
    if code == 401:
        raise StepError(TOKEN_INVALID)
    if code != 200 or not isinstance(body, dict):
        raise StepError(f"GraphQL respondió {code}: {_message(body, '')}")
    errores = body.get("errors")
    if errores:
        primero = errores[0] if isinstance(errores, list) else {}
        raise StepError("GraphQL: " + (primero.get("message")
                                       if isinstance(primero, dict)
                                       else "consulta rechazada"))
    return body.get("data") or {}


def _repo_from_graphql(nodo, username):
    url = nodo.get("url") or ""
    rama = nodo.get("defaultBranchRef") or {}
    dueno = nodo.get("owner") or {}
    rama = rama if isinstance(rama, dict) else {}
    dueno = dueno if isinstance(dueno, dict) else {}
    return {"name": nodo.get("name", ""),
            # GraphQL no expone clone_url; en github.com es la web + .git
            "clone_url": (url + ".git") if url else "",
            "fork": bool(nodo.get("isFork")),
            "private": bool(nodo.get("isPrivate")),
            "size_kb": _int(nodo.get("diskUsage")),
            "owner": dueno.get("login") or username,
            "default_branch": rama.get("name") or ""}


def list_user_repos_graphql(username, token, max_pages=30):
    """Repositorios de un perfil por GraphQL. Devuelve (repos, peticiones)."""
    repos, cursor, peticiones = [], None, 0
    while True:
        data = graphql(REPOS_QUERY, {"login": username, "after": cursor},
                       token)
        peticiones += 1
        dueno = data.get("repositoryOwner") if isinstance(data, dict) else None
        if not isinstance(dueno, dict):
            raise StepError(f"El usuario «{username}» no existe en GitHub.")
        conexion = dueno.get("repositories") or {}
        if not isinstance(conexion, dict):
            conexion = {}
        for nodo in conexion.get("nodes") or []:
            if isinstance(nodo, dict) and nodo.get("name"):
                repos.append(_repo_from_graphql(nodo, username))
        pagina = conexion.get("pageInfo") or {}
        if not isinstance(pagina, dict):
            pagina = {}
        siguiente = pagina.get("endCursor")
        # Sin cursor nuevo no hay forma de avanzar: seguir pidiendo
        # devolvería la misma página una y otra vez.
        if (not pagina.get("hasNextPage") or not siguiente
                or siguiente == cursor or peticiones >= max_pages):
            return repos, peticiones
        cursor = siguiente


def whoami(token):
    """Login del dueño del token, o cadena vacía."""
    if not token:
        return ""
    code, me = api("GET", "/user", token)
    # Un 200 con algo que no sea un objeto (un proxy raro, una página de
    # error) no puede tumbar la aplicación.
    return (me.get("login") or "") if code == 200 and isinstance(me, dict) \
        else ""


def list_user_repos(username, token="", max_pages=30, on_note=None):
    """Repositorios de un perfil. Con token, y si el perfil es el del
    propio token, incluye también los privados. Devuelve dicts con name,
    clone_url, fork, private, size_kb, owner y default_branch.

    Con token se intenta GraphQL, que trae los 100 de cada página con
    una sola petición y sin la llamada extra para saber quién eres. Si
    algo falla —GraphQL caído, token sin permisos, GitHub Enterprise
    antiguo— se vuelve a REST sin molestar al usuario.
    """
    if token:
        try:
            repos, peticiones = list_user_repos_graphql(username, token,
                                                        max_pages)
            _nota(on_note, len(repos), peticiones, "GraphQL")
            return repos
        except StepError as e:
            if str(e) == TOKEN_INVALID:
                raise          # REST fallaría igual, y peor explicado
            if on_note:
                on_note(f"GraphQL no funcionó ({e}); uso el método de "
                        "siempre.")
        except Exception:                                   # noqa: BLE001
            if on_note:
                on_note("GraphQL no funcionó; uso el método de siempre.")
    return _list_user_repos_rest(username, token, max_pages, on_note)


def _nota(on_note, repos, peticiones, via):
    if on_note:
        veces = "1 petición" if peticiones == 1 else f"{peticiones} peticiones"
        on_note(f"{repos} repositorio(s) en {veces} ({via}).")


def _list_user_repos_rest(username, token="", max_pages=30, on_note=None):
    """El camino de siempre: una página REST de 100 en 100."""
    login = whoami(token)
    peticiones = 1 if token else 0
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
        peticiones += 1
        if code == 404:
            raise StepError(f"El usuario «{username}» no existe en GitHub.")
        if code == 401:
            raise StepError(TOKEN_INVALID)
        if code == 403:
            raise StepError("GitHub limitó las peticiones (403). "
                            "Añade un token para subir el límite.")
        if code != 200 or not isinstance(body, list):
            raise StepError(f"GitHub respondió {code}: {_message(body, '')}")
        for r in body:
            if not isinstance(r, dict):
                continue
            dueno = r.get("owner") if isinstance(r.get("owner"), dict) else {}
            repos.append({"name": r.get("name", ""),
                          "clone_url": r.get("clone_url", ""),
                          "fork": bool(r.get("fork")),
                          "private": bool(r.get("private")),
                          "size_kb": _int(r.get("size")),
                          "owner": dueno.get("login", username),
                          "default_branch": r.get("default_branch") or ""})
        if len(body) < 100 or page >= max_pages:
            break
        page += 1
    _nota(on_note, len(repos), peticiones, "REST")
    return repos


def create_repo(token, name, description="", private=False):
    """Crea un repositorio en la cuenta del token.

    Devuelve (clone_url, html_url, ya_existía)."""
    code, me = api("GET", "/user", token)
    if code == 401:
        raise StepError("Token inválido o caducado. Genera uno nuevo en "
                        "GitHub.")
    if code != 200 or not isinstance(me, dict) or not me.get("login"):
        raise StepError(f"GitHub respondió {code}: {_message(me)}")
    login = me.get("login", "")

    payload = {"name": name, "private": private,
               "description": description, "auto_init": False}
    code, body = api("POST", "/user/repos", token, payload)
    if not isinstance(body, dict):
        body = {}
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
    code, info = api("GET", f"/repos/{_seg(owner)}/{_seg(repo)}", token)
    if code == 401:
        raise StepError("Token inválido o caducado.")
    if code == 404:
        raise StepError("No encuentro el repositorio. ¿Ya lo publicaste "
                        "con «🚀»?")
    if code != 200 or not isinstance(info, dict):
        raise StepError(f"GitHub respondió {code}: {_message(info, '')}")
    return info


def enable_pages(owner, repo, token, branch="main", path="/",
                 on_status=None, wait_seconds=90):
    """Activa (o actualiza) GitHub Pages y espera a que se construya.
    Devuelve la URL de la web."""
    say = on_status or (lambda text: None)
    base = f"/repos/{_seg(owner)}/{_seg(repo)}"
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
    elif code == 200 and isinstance(pages, dict):
        cur = pages.get("source") or {}
        cur = cur if isinstance(cur, dict) else {}
        if cur.get("branch") != branch or cur.get("path") != path:
            say("Actualizando el origen de la web…")
            put, cuerpo = api("PUT", base + "/pages", token, src)
            if put not in (200, 204):
                raise StepError(
                    f"GitHub no aceptó el nuevo origen de la web ({put}): "
                    f"{_message(cuerpo, '')}. ¿Existe la rama «{branch}»?")
    else:
        raise StepError(f"GitHub respondió {code}: {_message(pages, '')}")

    pages = pages if isinstance(pages, dict) else {}
    url = pages.get("html_url") or f"https://{owner}.github.io/{repo}/"
    say("La web se está construyendo… (suele tardar menos de un minuto)")
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(3)
        code, pages = api("GET", base + "/pages", token)
        if code != 200 or not isinstance(pages, dict):
            continue
        url = pages.get("html_url") or url
        st = (pages.get("status") or "").lower()
        if st == "built":
            break
        if st == "errored":
            raise StepError("GitHub no pudo construir la web. Revisa que "
                            "haya un index.html en el origen elegido.")
    return url
