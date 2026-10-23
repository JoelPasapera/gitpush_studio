# -*- coding: utf-8 -*-
"""Descargar un repositorio como instantánea (.tar.gz), sin git.

Clonar negocia un packfile y crea la carpeta .git con miles de objetos
sueltos; en Windows, escribir todo eso (con el antivirus revisando cada
archivo) suele costar más que la descarga en sí. Si lo único que se
quiere son los archivos —para leerlos o para consolidarlos en un TXT—,
pedir el comprimido es una sola petición HTTP y va mucho más rápido.

El precio: la carpeta resultante NO es un repositorio. No tiene
historial ni remoto, así que desde ella no se puede publicar, traer
cambios ni ver versiones.

Dos puertas para el mismo archivo:

  · codeload.github.com  — la que usa el botón «Download ZIP» de la web.
    No consume peticiones de la API, pero no está documentada y necesita
    saber la rama.
  · /repos/owner/repo/tarball/ref — documentada y válida también para
    repositorios privados, pero cuenta como llamada a la API.

Se intenta la primera y, si falla, la segunda.
"""

import json
import os
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from .github import GITHUB_API, USER_AGENT

CODELOAD = "https://codeload.github.com/{owner}/{repo}/tar.gz/refs/heads/{branch}"
API_TARBALL = GITHUB_API + "/repos/{owner}/{repo}/tarball/{ref}"
CHUNK = 1 << 16
UNSAFE_NAMES = ("", ".", "..")
# Marca con el sha de lo descargado: permite saber si hace falta volver a
# bajar nada sin gastar ni una petición de la API (basta un ls-remote).
MARKER = ".gitpush-instantanea.json"


def snapshot_urls(owner, repo, branch="", token=""):
    """Las URLs a intentar, en orden, con sus cabeceras."""
    urls = []
    base = {"User-Agent": USER_AGENT}
    if branch:
        urls.append((CODELOAD.format(owner=owner, repo=repo, branch=branch),
                     dict(base)))
    headers = dict(base, Accept="application/vnd.github+json")
    if token:
        headers["Authorization"] = "Bearer " + token
    urls.append((API_TARBALL.format(owner=owner, repo=repo,
                                    ref=branch or "").rstrip("/"), headers))
    return urls


def _open(url, headers, timeout):
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def download(url, headers, destination, opener=None, timeout=300,
             on_bytes=None, should_stop=None):
    """Descarga `url` a `destination`. Devuelve los bytes escritos.

    `opener` se resuelve aquí y no en la firma: un valor por defecto se
    ata al definir la función, así que quien sustituya `_open` después
    (una prueba, otro transporte) no tendría efecto.
    """
    opener = opener or _open
    total = 0
    with opener(url, headers, timeout) as respuesta, \
            open(destination, "wb") as salida:
        while True:
            if should_stop is not None and should_stop():
                raise InterruptedError("descarga detenida")
            trozo = respuesta.read(CHUNK)
            if not trozo:
                break
            salida.write(trozo)
            total += len(trozo)
            if on_bytes:
                on_bytes(total)
    return total


# ------------------------------------------------------------------ #
#  Extracción segura                                                  #
# ------------------------------------------------------------------ #
def _inside(base, candidate):
    """¿`candidate` queda dentro de `base`? Sin tocar el disco."""
    base = os.path.normcase(os.path.abspath(base))
    candidate = os.path.normcase(os.path.abspath(candidate))
    return candidate == base or candidate.startswith(base + os.sep)


def safe_name(name, strip_top=True):
    """Nombre de destino de un miembro, o '' si no es de fiar.

    Un .tar puede traer rutas absolutas, «..» o enlaces que apunten
    fuera; extraerlo a ciegas escribiría donde le diera la gana. Aquí se
    rechaza todo eso y se quita la carpeta «owner-repo-sha/» que GitHub
    pone arriba del todo.
    """
    partes = [p for p in name.replace("\\", "/").split("/") if p]
    if strip_top:
        partes = partes[1:]
    if not partes:
        return ""
    limpio = name.replace("\\", "/")
    if limpio.startswith("/") or os.path.isabs(limpio) or (
            len(limpio) > 1 and limpio[1] == ":"
            and limpio[0].isalpha()):                   # C:\… en Windows
        return ""
    if any(p in UNSAFE_NAMES for p in partes):
        return ""
    return "/".join(partes)


def extract(tar_path, target, strip_top=True, should_stop=None):
    """Extrae la instantánea en `target`. Devuelve (archivos, omitidos).

    Se omiten los miembros que no sean archivo o carpeta normales
    (enlaces, dispositivos) y cualquiera cuya ruta se salga del destino.
    Extraer un repositorio grande lleva su tiempo, así que «Detener»
    también corta aquí: `should_stop` se consulta en cada archivo.
    """
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    raiz = str(target.resolve())
    archivos = omitidos = 0
    with tarfile.open(tar_path, "r:gz") as tar:
        for miembro in tar:
            if should_stop is not None and should_stop():
                raise InterruptedError("extracción detenida")
            nombre = safe_name(miembro.name, strip_top)
            if not nombre:
                # La carpeta «owner-repo-sha/» que envuelve todo se queda
                # sin nombre al quitarla: no es un archivo descartado.
                if not _es_envoltorio(miembro, strip_top):
                    omitidos += 1
                continue
            if not (miembro.isfile() or miembro.isdir()):
                omitidos += 1          # enlaces, dispositivos, tuberías
                continue
            destino = target / nombre
            if not _inside(raiz, destino):
                omitidos += 1
                continue
            try:
                if miembro.isdir():
                    destino.mkdir(parents=True, exist_ok=True)
                    continue
                destino.parent.mkdir(parents=True, exist_ok=True)
                origen = tar.extractfile(miembro)
                if origen is None:
                    omitidos += 1
                    continue
                with origen, open(destino, "wb") as salida:
                    shutil.copyfileobj(origen, salida, CHUNK)
            except OSError:
                # Un nombre imposible en este sistema (Windows no admite
                # «:», «?», «aux»…) no puede tirar la descarga entera.
                omitidos += 1
                continue
            archivos += 1
    return archivos, omitidos


def _es_envoltorio(miembro, strip_top):
    """La carpeta única que GitHub pone alrededor de todo el contenido."""
    partes = [p for p in miembro.name.replace("\\", "/").split("/") if p]
    return strip_top and miembro.isdir() and len(partes) == 1


# ------------------------------------------------------------------ #
#  Operación completa                                                 #
# ------------------------------------------------------------------ #
def fetch_snapshot(owner, repo, target, branch="", token="", opener=None,
                   timeout=300, on_bytes=None, should_stop=None,
                   retries=2, on_retry=None, sleep=time.sleep, sha=""):
    """Descarga y extrae. Devuelve (estado, segundos, error) con estado
    ok | skip (ya existía y no estaba vacía) | fail.

    Igual que al clonar, un fallo pasajero (corte de red, servidor
    ocupado, «429») se reintenta con espera creciente; lo que no se
    arregla insistiendo (no existe, sin permiso) falla a la primera.
    """
    target = Path(target)
    t0 = time.time()
    if target.exists() and any(target.iterdir()):
        return "skip", 0.0, "ya existía"

    for intento in range(retries + 1):
        estado, error, limitado, transitorio = _intentar(
            owner, repo, target, branch, token, opener, timeout, on_bytes,
            should_stop)
        if estado == "ok":
            write_marker(target, owner, repo, branch, sha)
        if estado == "ok" or intento == retries or not transitorio:
            return estado, time.time() - t0, error
        espera = 3 * (intento + 1)
        if on_retry:
            on_retry(espera, limitado, error)
        sleep(espera)
    return estado, time.time() - t0, error


def _intentar(owner, repo, target, branch, token, opener, timeout, on_bytes,
              should_stop):
    """Un intento por cada puerta. Devuelve (estado, error, ¿429?,
    ¿merece reintento?)."""
    fd, temporal = tempfile.mkstemp(prefix="snapshot-", suffix=".tar.gz")
    os.close(fd)
    existia = target.exists()
    error = "No se pudo descargar el archivo comprimido."
    limitado = transitorio = False
    try:
        for url, headers in snapshot_urls(owner, repo, branch, token):
            try:
                download(url, headers, temporal, opener, timeout, on_bytes,
                         should_stop)
                extract(temporal, target, should_stop=should_stop)
                return "ok", "", False, False
            except InterruptedError:
                _limpiar(target, existia)
                return "fail", "descarga detenida", False, False
            except urllib.error.HTTPError as e:
                error = _http_error(e.code)
                limitado = e.code == 429
                transitorio = limitado or e.code >= 500
                if e.code in (401, 403, 404):
                    continue            # prueba la otra puerta
            except (urllib.error.URLError, OSError, tarfile.TarError) as e:
                error = f"{type(e).__name__}: {e}"[:180]
                transitorio = True      # red o descarga truncada
            _limpiar(target, existia)
        return "fail", error, limitado, transitorio
    finally:
        try:
            os.unlink(temporal)
        except OSError:
            pass


def read_marker(target):
    """Datos de la última instantánea guardada en la carpeta, o {}."""
    try:
        datos = json.loads((Path(target) / MARKER).read_text(encoding="utf-8"))
        return datos if isinstance(datos, dict) else {}
    except (OSError, ValueError):
        return {}


def write_marker(target, owner, repo, branch, sha):
    try:
        (Path(target) / MARKER).write_text(json.dumps(
            {"owner": owner, "repo": repo, "branch": branch, "sha": sha,
             "fecha": datetime.now().isoformat(timespec="seconds")},
            ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass          # sin marca solo se pierde el «ya estaba al día»


def update_snapshot(owner, repo, target, branch="", token="", sha_remoto="",
                    opener=None, timeout=300, on_bytes=None,
                    should_stop=None):
    """Pone al día una instantánea. Si el sha del remoto coincide con el
    de la marca, no descarga NADA. Si cambió, baja la nueva y guarda la
    anterior al lado, para no perder lo que hubiera dentro.

    Devuelve (estado, segundos, mensaje) con ok | uptodate | skip | fail.
    """
    target = Path(target)
    t0 = time.time()
    if not target.exists() or not any(target.iterdir()):
        return "skip", 0.0, "no estaba descargada"
    marca = read_marker(target)
    if not marca:
        return "skip", 0.0, ("no sé de qué versión es (falta la marca); "
                             "bórrala si quieres volver a bajarla")
    if sha_remoto and marca.get("sha") == sha_remoto:
        return "uptodate", time.time() - t0, "ya estaba al día"

    anterior = target.with_name(
        f"{target.name}.anterior-{datetime.now():%Y%m%d-%H%M%S}")
    try:
        target.rename(anterior)
    except OSError as e:
        return "fail", time.time() - t0, f"no pude apartar la anterior: {e}"
    estado, segundos, error = fetch_snapshot(
        owner, repo, target, branch=branch, token=token, opener=opener,
        timeout=timeout, on_bytes=on_bytes, should_stop=should_stop,
        sha=sha_remoto)
    if estado != "ok":
        shutil.rmtree(target, ignore_errors=True)
        try:
            anterior.rename(target)      # devuelve todo como estaba
        except OSError:
            pass
        return "fail", time.time() - t0, error
    return "ok", time.time() - t0, (f"actualizado; la anterior queda en "
                                    f"«{anterior.name}»")


def _http_error(code):
    if code in (401, 403):
        return ("GitHub no dio acceso al archivo (¿repositorio privado sin "
                "token, o token sin permisos?).")
    if code == 404:
        return "No encontré el repositorio o la rama indicada."
    if code == 429:
        return "GitHub pidió bajar el ritmo (demasiadas descargas seguidas)."
    return f"GitHub respondió {code}."


def _limpiar(target, existia):
    """Que un fallo no deje media instantánea desperdigada."""
    try:
        if target.exists() and not existia:
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            for hijo in target.iterdir():
                if hijo.is_dir():
                    shutil.rmtree(hijo, ignore_errors=True)
                else:
                    hijo.unlink()
    except OSError:
        pass
