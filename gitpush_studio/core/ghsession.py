# -*- coding: utf-8 -*-
"""Una sesión con la API de GitHub para descargas largas.

`github.api` abre una conexión por consulta: perfecto para una o para
diez. Volcar el historial entero de un repositorio son cientos o miles
de páginas, y ahí cada detalle cuenta:

  · UNA conexión HTTPS que se reutiliza (keep-alive): sin un saludo TLS
    por consulta, cada página cuesta un viaje de ida y vuelta en vez de
    tres;
  · respuestas comprimidas (gzip): una página de 100 issues pasa de
    cientos de KB a unas decenas; se descomprime con el mismo tope de
    tamaño que lo demás (una «bomba» comprimida no llena la memoria);
  · paginación siguiendo la cabecera «Link» (rel="next"), la única forma
    correcta en GitHub (unos listados van por número de página y otros
    por cursor), y NUNCA hacia otro servidor; cuando GitHub dice cuál es
    la última página (rel="last"), también se apunta, para pedir las
    demás a la vez (`ghfetch`);
  · los límites de GitHub: si se acaba la cuota (5.000 consultas por hora
    con token, 60 sin él) se espera a que se reinicie, y ante el límite
    «secundario» (demasiadas consultas seguidas) se espera lo que pida
    «Retry-After»; todas las esperas se pueden detener al momento. REST y
    GraphQL tienen cuotas separadas, y cada una se lleva aparte;
  · consultas GraphQL (POST que solo lee): GitHub avisa de su cuota
    agotada con un 200 y el error «RATE_LIMITED», y eso también se
    espera;
  · reintentos ante fallos de red o del servidor (5xx), reconectando;
  · el proxy del sistema (el mismo que usaría el navegador), por túnel
    CONNECT;
  · el token va solo al servidor de la API: una redirección o un enlace
    de paginación hacia otro servidor se rechazan, no se siguen.
"""

import base64
import http.client
import json
import ssl
import threading
import time
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from .errors import Cancelled, StepError
from .ghlimits import (CANCELLED, OK, SERVER_ERROR, SLOW_DOWN, Quota,
                       SharedLimits)
from .net import MAX_BODY, retry_after

API_ROOT = "https://api.github.com"
USER_AGENT = "GitPush-Studio"
API_VERSION = "2022-11-28"
PER_PAGE = 100
# Lo más que se espera a que GitHub reinicie la cuota (la reinicia cada
# hora): si pide más, se para y se explica.
MAX_RATE_WAIT = 65 * 60
MAX_NETWORK_ATTEMPTS = 6
MAX_SECONDARY_WAITS = 8
# Veces que una MISMA consulta puede toparse con la cuota agotada (tras
# cada espera, GitHub tendría que haberla reiniciado).
MAX_RATE_WAITS = 3
MAX_REDIRECTS = 5
SECONDARY_DEFAULT = 60.0
SECONDARY_CAP = 15 * 60.0
READ_CHUNK = 64 * 1024

# (servidor, puerto, cabeceras del CONNECT) de un proxy.
Proxy = Tuple[str, int, Dict[str, str]]
# on_wait(motivo, hasta_cuándo como marca de tiempo)
WaitCallback = Callable[[str, float], None]


class ApiError(StepError):
    """GitHub respondió, pero con un error (404, 401, 410…)."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class _TooBig(Exception):
    """La respuesta pasa del tope de tamaño."""


@dataclass
class Response:
    """Una respuesta ya leída entera: código, cabeceras (en minúsculas) y
    el JSON decodificado (o {"message": …} si no lo era)."""
    status: int
    headers: Dict[str, str]
    data: object

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def message(self) -> str:
        if isinstance(self.data, dict):
            return str(self.data.get("message") or "")
        return ""


@dataclass
class Page:
    """Una página de un listado."""
    url: str                  # la que se pidió (para retomar desde ella)
    items: List[object]
    next_url: str             # "" si era la última
    last_page: int            # el número de la última página, si se sabe
    last_url: str = ""        # su URL (para pedir las demás en paralelo)


# ------------------------------------------------------------------ #
#  Piezas puras                                                       #
# ------------------------------------------------------------------ #
def _split_links(header: str) -> List[str]:
    """Parte una cabecera «Link» por las comas que NO van dentro de <…>."""
    parts: List[str] = []
    current: List[str] = []
    inside = False
    for char in header or "":
        if char == "<":
            inside = True
        elif char == ">":
            inside = False
        if char == "," and not inside:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def link_targets(header: str) -> Dict[str, str]:
    """{rel: url} de una cabecera «Link» (RFC 8288), p. ej.
    «<https://…?page=2>; rel="next", <https://…?page=9>; rel="last"»."""
    out: Dict[str, str] = {}
    for part in _split_links(header):
        part = part.strip()
        end = part.find(">")
        if not part.startswith("<") or end < 0:
            continue
        url = part[1:end].strip()
        for param in part[end + 1:].split(";"):
            name, _sep, value = param.strip().partition("=")
            if name.strip().lower() != "rel":
                continue
            for rel in value.strip().strip('"').split():
                out.setdefault(rel.lower(), url)
    return out


def page_number(url: str) -> int:
    """El parámetro «page» de una URL de paginación, o 0."""
    query = urllib.parse.urlsplit(url or "").query
    values = urllib.parse.parse_qs(query).get("page") or ["0"]
    try:
        return max(0, int(values[0]))
    except ValueError:
        return 0


def gunzip_limited(raw: bytes, limit: int) -> bytes:
    """Descomprime gzip sin pasar de `limit` bytes (una respuesta que se
    infla mil veces no llega a ocupar la memoria)."""
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = decompressor.decompress(raw, limit + 1)
        if len(out) > limit or decompressor.unconsumed_tail:
            raise _TooBig()
        out += decompressor.flush()
    except zlib.error:
        raise StepError("GitHub mandó una respuesta comprimida rota.") \
            from None
    if len(out) > limit:
        raise _TooBig()
    return out


def _decode_json(raw: bytes) -> Tuple[object, bool]:
    """(datos, era_json). Una respuesta vacía es {}."""
    text = raw.decode("utf-8", "replace")
    if not text.strip():
        return {}, True
    try:
        return json.loads(text), True
    except (ValueError, RecursionError):
        return {"message": text[:200]}, False


def _graphql_rate_limited(data: object) -> bool:
    """GraphQL avisa del límite con un 200 y {"errors": [{"type":
    "RATE_LIMITED"}]}."""
    if not isinstance(data, dict):
        return False
    errores = data.get("errors")
    if not isinstance(errores, list):
        return False
    return any(isinstance(e, dict) and e.get("type") == "RATE_LIMITED"
               for e in errores)


def _is_slow_down(status: int, headers: Dict[str, str], data: object) -> bool:
    """¿Es el «ve más despacio» de GitHub (su límite secundario)?"""
    if status not in (403, 429) or headers.get("x-ratelimit-remaining") == "0":
        return False
    mensaje = data.get("message", "") if isinstance(data, dict) else ""
    return "retry-after" in headers \
        or "secondary rate limit" in str(mensaje).lower()


def _outcome(status: int, headers: Dict[str, str], data: object
             ) -> Optional[str]:
    """Qué dice una respuesta de cuánto aguanta GitHub ahora: OK, que
    vaya más despacio, que falló, o nada (cuota agotada: eso es esperar,
    no ir más despacio)."""
    if _is_slow_down(status, headers, data):
        return SLOW_DOWN
    if status in (403, 429) and headers.get("x-ratelimit-remaining") == "0":
        return None
    if status == 200 and _graphql_rate_limited(data):
        return None
    if status >= 500:
        return SERVER_ERROR
    return OK


def system_proxy(host: str) -> Optional[Proxy]:
    """El proxy https del sistema para `host` (variables HTTPS_PROXY y
    NO_PROXY, o lo configurado en Windows y macOS), o None."""
    try:
        if urllib.request.proxy_bypass(host):
            return None
        configured = urllib.request.getproxies().get("https", "")
    except Exception:                                       # noqa: BLE001
        return None
    if not configured:
        return None
    if "://" not in configured:
        configured = "http://" + configured
    parts = urllib.parse.urlsplit(configured)
    if not parts.hostname:
        return None
    headers: Dict[str, str] = {}
    if parts.username:
        credentials = (urllib.parse.unquote(parts.username) + ":"
                       + urllib.parse.unquote(parts.password or ""))
        headers["Proxy-Authorization"] = "Basic " + base64.b64encode(
            credentials.encode("utf-8")).decode("ascii")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None
    return parts.hostname, port, headers


def describe_error(response: Response, what: str = "") -> str:
    """Una frase para el usuario a partir de un error de la API."""
    sujeto = what or "eso"
    if response.status == 401:
        return "GitHub no acepta el token (caducó o se revocó)."
    if response.status == 404:
        return (f"GitHub no encuentra {sujeto}: no existe, o es privado y "
                "hace falta un token con permiso para leerlo.")
    if response.status == 410:
        return f"{sujeto[:1].upper()}{sujeto[1:]} está desactivado en GitHub."
    if response.status == 451:
        return f"GitHub bloquea {sujeto} por motivos legales."
    detalle = response.message()
    return (f"GitHub respondió {response.status} al pedir {sujeto}"
            + (f": {detalle}" if detalle else "") + ".")


def _clock_text(timestamp: float) -> str:
    try:
        return datetime.fromtimestamp(timestamp).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return "dentro de un rato"


# ------------------------------------------------------------------ #
#  La sesión                                                          #
# ------------------------------------------------------------------ #
class ApiSession:
    """Consultas a la API de GitHub (GET de REST y POST de GraphQL, que
    solo lee) por una conexión que se reutiliza. Una sesión es de UN
    hilo: para varias conexiones a la vez, `ghpool.SessionPool`.

    `root` es la raíz de la API (en las pruebas, un servidor local por
    http). `proxy`: None = el del sistema (solo para https), False =
    ninguno, o un (servidor, puerto, cabeceras) concreto. `limits`: lo
    que se sabe de los límites de GitHub, compartido con las demás
    conexiones de la descarga (si no, uno propio). `sleep` y `clock` se
    pueden sustituir en las pruebas para no esperar de verdad.
    """

    def __init__(self, token: str = "", root: str = API_ROOT,
                 cancel: Optional[threading.Event] = None,
                 on_wait: Optional[WaitCallback] = None,
                 proxy=None, timeout: float = 60.0,
                 sleep: Optional[Callable[[float], None]] = None,
                 clock: Callable[[], float] = time.time,
                 limits: Optional[SharedLimits] = None) -> None:
        parts = urllib.parse.urlsplit(root)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError(f"raíz de la API no válida: {root!r}")
        self.scheme = parts.scheme
        self.host = parts.hostname.lower()
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.base_path = parts.path.rstrip("/")
        self.token = token or ""
        self.cancel = cancel or threading.Event()
        self.on_wait = on_wait
        self.timeout = timeout
        self._proxy = proxy
        self._sleep = sleep or self._sleep_cancellable
        self._clock = clock
        self._connection: Optional[http.client.HTTPConnection] = None
        self.requests = 0                 # consultas respondidas
        self.connections = 0              # conexiones abiertas
        self.limits = limits or SharedLimits()
        # Lo que ESTA sesión ya esperó: el reinicio de cada recurso y el
        # «ve más despacio» (no se espera dos veces por lo mismo).
        self._waited_reset: Dict[str, int] = {}
        self._held_until = 0.0

    # ---- vida de la conexión -------------------------------------------
    def __enter__(self) -> "ApiSession":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:                               # noqa: BLE001
                pass

    def _open(self) -> http.client.HTTPConnection:
        proxy = self._proxy
        if proxy is None:
            proxy = system_proxy(self.host) if self.scheme == "https" \
                else False
        if self.scheme == "https":
            context = ssl.create_default_context()
            if proxy:
                connection = http.client.HTTPSConnection(
                    proxy[0], proxy[1], timeout=self.timeout,
                    context=context)
            else:
                connection = http.client.HTTPSConnection(
                    self.host, self.port, timeout=self.timeout,
                    context=context)
        else:
            connection = http.client.HTTPConnection(
                proxy[0] if proxy else self.host,
                proxy[1] if proxy else self.port, timeout=self.timeout)
        if proxy:
            connection.set_tunnel(self.host, self.port,
                                  headers=dict(proxy[2]))
        self.connections += 1
        return connection

    # ---- esperas ------------------------------------------------------
    def _sleep_cancellable(self, seconds: float) -> None:
        if self.cancel.wait(max(0.0, seconds)):
            raise Cancelled(CANCELLED)

    def check_cancel(self) -> None:
        """Lanza Cancelled si se pidió parar."""
        if self.cancel.is_set():
            raise Cancelled(CANCELLED)

    def _pause(self, seconds: float, why: str) -> None:
        if self.on_wait is not None:
            self.on_wait(why, self._clock() + seconds)
        self._sleep(seconds)
        self.check_cancel()

    def quota(self, resource: str = "core") -> Quota:
        """(quedan, límite, reinicio) de un recurso, o Nones si no se
        sabe aún."""
        return self.limits.quota(resource)

    def _wait_for_reset(self, resource: str) -> None:
        """La cuota de `resource` está agotada: esperar a que GitHub la
        reinicie."""
        _quedan, limite, reinicio = self.quota(resource)
        reset = reinicio or int(self._clock() + 60)
        seconds = reset - self._clock() + 1
        hora = _clock_text(reset)
        cuota = ""
        if limite:
            cuota = f" ({limite:,} por hora)".replace(",", ".")
        if seconds > MAX_RATE_WAIT:
            raise StepError(f"GitHub agotó tus consultas{cuota} hasta las "
                            f"{hora}. Vuelve a intentarlo entonces: lo ya "
                            "descargado se guarda.")
        self._waited_reset[resource] = reset
        self._pause(max(1.0, seconds),
                    f"GitHub agotó las consultas de esta hora{cuota}: sigo "
                    f"a las {hora}.")

    def _respect_quota(self, resource: str) -> None:
        """Sin cuota no se pregunta: se espera antes (una consulta que
        GitHub rechazaría también cuenta como abuso). Vale también si la
        agotó otra conexión de la misma descarga."""
        quedan, _limite, reinicio = self.quota(resource)
        if quedan == 0 and reinicio and reinicio > self._clock() \
                and reinicio > self._waited_reset.get(resource, 0):
            self._wait_for_reset(resource)

    def _respect_hold(self) -> None:
        """Si GitHub pidió ir más despacio (a esta conexión o a otra de
        la misma descarga), esperar hasta cuando dijo."""
        hasta = self.limits.resume_at
        if hasta <= self._held_until:
            return
        self._held_until = hasta
        resto = hasta - self._clock()
        if resto > 0:
            self._pause(resto, "GitHub pide ir más despacio: espero un "
                               "poco y sigo.")

    # ---- rutas --------------------------------------------------------
    def same_origin_target(self, url: str) -> str:
        """El «/ruta?consulta» de una URL absoluta de la API, si va al
        MISMO servidor; si no, StepError (el token no sale de aquí)."""
        parts = urllib.parse.urlsplit(url)
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            port = -1
        same = (parts.scheme == self.scheme
                and (parts.hostname or "").lower() == self.host
                and port == self.port
                and (parts.path == self.base_path
                     or parts.path.startswith(self.base_path + "/")))
        if not same or parts.username or parts.password:
            raise StepError("GitHub mandó a otra dirección "
                            f"({parts.scheme}://{parts.hostname or '?'}): no "
                            "la sigo, tu token no sale de la API de GitHub.")
        return parts.path + (f"?{parts.query}" if parts.query else "")

    def target(self, path: str, params: Optional[Dict[str, object]] = None
               ) -> str:
        """«/ruta?consulta» de `path` (relativa a la raíz, o una URL
        absoluta de la propia API)."""
        if "://" in path:
            target = self.same_origin_target(path)
        else:
            target = self.base_path + "/" + path.lstrip("/")
        if params:
            joiner = "&" if "?" in target else "?"
            target += joiner + urllib.parse.urlencode(
                [(k, str(v)) for k, v in params.items()])
        return target

    def url(self, target: str) -> str:
        """La URL absoluta de un «/ruta?consulta»."""
        default = 443 if self.scheme == "https" else 80
        port = "" if self.port == default else f":{self.port}"
        return f"{self.scheme}://{self.host}{port}{target}"

    # ---- una consulta -------------------------------------------------
    def _headers(self, has_body: bool) -> Dict[str, str]:
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": API_VERSION,
                   "User-Agent": USER_AGENT,
                   "Accept-Encoding": "gzip",
                   "Connection": "keep-alive"}
        if has_body:
            headers["Content-Type"] = "application/json; charset=utf-8"
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        return headers

    def _read_body(self, response: http.client.HTTPResponse) -> bytes:
        chunks: List[bytes] = []
        total = 0
        while True:
            piece = response.read(min(READ_CHUNK, MAX_BODY + 1 - total))
            if not piece:
                break
            chunks.append(piece)
            total += len(piece)
            if total > MAX_BODY:
                raise _TooBig()
        return b"".join(chunks)

    def _exchange(self, method: str, target: str, body: Optional[bytes]
                  ) -> Tuple[int, Dict[str, str], bytes]:
        """Una petición y su respuesta entera, por la conexión abierta."""
        if self._connection is None:
            self._connection = self._open()
        connection = self._connection
        connection.request(method, target, body=body,
                           headers=self._headers(body is not None))
        response = connection.getresponse()
        try:
            raw = self._read_body(response)
        except _TooBig:
            self.close()           # el resto del cuerpo sigue en el cable
            raise
        headers: Dict[str, str] = {}
        for name, value in response.getheaders():
            key = name.lower()
            headers[key] = f"{headers[key]}, {value}" if key in headers \
                else value
        if response.will_close:
            self.close()
        if headers.get("content-encoding", "").strip().lower() == "gzip":
            raw = gunzip_limited(raw, MAX_BODY)
        return response.status, headers, raw

    def get(self, path: str, params: Optional[Dict[str, object]] = None
            ) -> Response:
        """GET con esperas, reintentos y redirecciones (solo dentro de la
        API). Devuelve también las respuestas de error (404, 410…): qué
        hacer con ellas lo decide quien pregunta."""
        return self._request("GET", self.target(path, params), None, "core")

    def post_json(self, path: str, payload: Dict[str, object],
                  server_attempts: int = MAX_NETWORK_ATTEMPTS) -> Response:
        """POST de un JSON (la API GraphQL), con las mismas esperas y
        reintentos. Una consulta GraphQL se puede repetir sin efectos:
        solo lee. `server_attempts`: cuántas veces se pide ante un error
        5xx de GitHub antes de devolverlo (para GraphQL suele ser una
        consulta demasiado pesada: mejor partirla que repetirla)."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        resource = "graphql" if path.rstrip("/").endswith("/graphql") \
            else "core"
        return self._request("POST", self.target(path), body, resource,
                             server_attempts=server_attempts)

    def _request(self, method: str, target: str, body: Optional[bytes],
                 resource: str,
                 server_attempts: int = MAX_NETWORK_ATTEMPTS) -> Response:
        failures = 0
        secondary = 0
        redirects = 0
        rate_waits = 0
        while True:
            self.check_cancel()
            self._respect_hold()
            self._respect_quota(resource)
            # Un puesto entre las consultas que van a la vez (las de todas
            # las conexiones de la descarga): si GitHub pidió ir más
            # despacio, esto espera aunque la tarea ya estuviera en marcha.
            epoch = self.limits.acquire(self.cancel)
            try:
                status, headers, raw = self._exchange(method, target, body)
            except _TooBig:
                self.limits.release(epoch, None)
                raise StepError("GitHub mandó una respuesta demasiado "
                                "grande: no la leo.") from None
            except (OSError, http.client.HTTPException) as e:
                # También una conexión que GitHub ya cerró por estar
                # quieta: se reconecta (la primera vez, sin esperar).
                self.close()
                failures += 1
                self.limits.release(epoch,
                                    SERVER_ERROR if failures > 1 else None)
                if failures >= MAX_NETWORK_ATTEMPTS:
                    raise StepError(f"No pude conectar con GitHub "
                                    f"({type(e).__name__}). Revisa tu "
                                    "conexión: lo ya descargado se "
                                    "guarda.") from None
                if failures > 1:
                    self._pause(min(30.0, 2.0 ** (failures - 1)),
                                "Sin conexión con GitHub: lo vuelvo a "
                                "intentar.")
                continue
            except BaseException:
                self.limits.release(epoch, None)
                raise
            self.requests += 1
            self.limits.update(headers)
            resource = headers.get("x-ratelimit-resource", resource) \
                or resource
            data, is_json = _decode_json(raw)
            self.limits.release(epoch, _outcome(status, headers, data))
            if status in (301, 302, 303, 307, 308) and method == "GET":
                redirects += 1
                if redirects > MAX_REDIRECTS:
                    raise StepError("GitHub redirige una y otra vez: lo "
                                    "dejo.")
                target = self.same_origin_target(
                    urllib.parse.urljoin(self.url(target),
                                         headers.get("location", "")))
                continue
            agotada = headers.get("x-ratelimit-remaining") == "0" \
                and status in (403, 429)
            if agotada or (status == 200 and _graphql_rate_limited(data)):
                rate_waits += 1
                if rate_waits > MAX_RATE_WAITS:
                    raise StepError("GitHub sigue sin dar consultas "
                                    "aunque ya pasó la hora de reinicio: "
                                    "lo dejo por ahora (lo descargado se "
                                    "guarda).")
                self.limits.exhaust(resource)
                self._wait_for_reset(resource)
                continue
            if status in (403, 429):
                if _is_slow_down(status, headers, data):
                    secondary += 1
                    if secondary > MAX_SECONDARY_WAITS:
                        raise StepError("GitHub sigue pidiendo ir más "
                                        "despacio: lo dejo por ahora (lo "
                                        "descargado se guarda).")
                    default = min(SECONDARY_CAP,
                                  SECONDARY_DEFAULT * 2.0 ** (secondary - 1))
                    # `retry_after` lee la cabecera con su nombre de
                    # siempre; aquí las cabeceras van en minúsculas.
                    pedido = {"Retry-After": headers.get("retry-after", "")}
                    espera = retry_after(pedido, default=default,
                                         cap=SECONDARY_CAP)
                    # También para las demás conexiones de la descarga
                    # (`_respect_hold`); esta espera ya aquí lo pedido.
                    hasta = self._clock() + espera
                    self.limits.hold(hasta)
                    self._held_until = max(self._held_until, hasta)
                    self._pause(espera, "GitHub pide ir más despacio: "
                                        "espero un poco y sigo.")
                    continue
            if status >= 500:
                failures += 1
                if failures < max(1, min(server_attempts,
                                         MAX_NETWORK_ATTEMPTS)):
                    self.close()
                    self._pause(min(30.0, 2.0 ** (failures - 1)),
                                f"GitHub falló ({status}): lo vuelvo a "
                                "intentar.")
                    continue
            if 200 <= status < 300 and not is_json:
                raise StepError("GitHub respondió algo que no se entiende "
                                "(no es JSON).")
            return Response(status, headers, data)

    # ---- listados -----------------------------------------------------
    def pages(self, path: str, params: Optional[Dict[str, object]] = None,
              start: str = "", what: str = "") -> Iterator[Page]:
        """Las páginas de un listado, de 100 en 100, siguiendo «Link».

        `start` retoma desde una página ya conocida (la URL que llevaba
        `Page.url`). Un error de GitHub se lanza como `ApiError`.
        """
        if start:
            target = self.target(start)
        else:
            query = dict(params or {})
            query["per_page"] = PER_PAGE
            target = self.target(path, query)
        while target:
            page = self.page(target, what)
            yield page
            target = self.target(page.next_url) if page.next_url else ""

    def page(self, url: str, what: str = "") -> Page:
        """Una página suelta de un listado (por su URL o su «/ruta»)."""
        target = self.target(url)
        response = self.get(target)
        if not response.ok:
            raise ApiError(describe_error(response, what), response.status)
        if not isinstance(response.data, list):
            raise StepError("GitHub respondió algo que no es una lista.")
        links = link_targets(response.headers.get("link", ""))
        following = links.get("next", "")
        last = links.get("last", "")
        next_target = self.same_origin_target(following) if following else ""
        last_target = self.same_origin_target(last) if last else ""
        return Page(url=self.url(target), items=response.data,
                    next_url=self.url(next_target) if next_target else "",
                    last_page=page_number(last),
                    last_url=self.url(last_target) if last_target else "")
