# -*- coding: utf-8 -*-
"""HTTP con las precauciones que urllib no toma por su cuenta.

  · Redirecciones: urllib copia TODAS las cabeceras de la petición
    original a la nueva, también `Authorization`. Si GitHub (o un proxy)
    redirige a otro servidor, el token viajaría con ella. Aquí se quita
    al cambiar de servidor, y nunca se sigue una redirección que baje de
    https a http.
  · Tamaño: una respuesta que no acaba nunca (un proxy roto, un servidor
    hostil) no puede llenar la memoria: se lee con un tope.
  · Esperas: «Retry-After» se interpreta con límites razonables.
"""

import urllib.error
import urllib.parse
import urllib.request

# Cabeceras que nunca deben cruzar a otro servidor.
_SENSITIVE = ("authorization", "cookie", "proxy-authorization")
MAX_BODY = 32 * 1024 * 1024


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Sigue redirecciones sin filtrar credenciales a otro servidor."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        nueva = super().redirect_request(req, fp, code, msg, headers, newurl)
        if nueva is None:
            return None
        antes = urllib.parse.urlsplit(req.full_url)
        despues = urllib.parse.urlsplit(nueva.full_url)
        if antes.scheme == "https" and despues.scheme != "https":
            raise urllib.error.HTTPError(
                newurl, code, "redirección insegura (https → http)",
                headers, fp)
        if (antes.hostname or "").lower() != (despues.hostname or "").lower() \
                or (antes.port or 443) != (despues.port or 443):
            for nombre in list(nueva.headers):
                if nombre.lower() in _SENSITIVE:
                    del nueva.headers[nombre]
            for nombre in list(nueva.unredirected_hdrs):
                if nombre.lower() in _SENSITIVE:
                    del nueva.unredirected_hdrs[nombre]
        return nueva


_OPENER = urllib.request.build_opener(SafeRedirectHandler)


def open_url(url, headers=None, data=None, method=None, timeout=30):
    """Como `urllib.request.urlopen`, con las redirecciones seguras."""
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=dict(headers or {}))
    return _OPENER.open(req, timeout=timeout)


def read_limited(resp, limit=MAX_BODY):
    """Cuerpo completo de la respuesta, sin pasar de `limit` bytes."""
    datos = resp.read(limit + 1)
    if len(datos) > limit:
        raise ValueError("respuesta demasiado grande")
    return datos


def retry_after(headers, default=0.0, cap=120.0):
    """Segundos que pide esperar «Retry-After» (solo la forma numérica),
    acotados a [0, cap]; `default` si no hay o no se entiende."""
    try:
        valor = float((headers or {}).get("Retry-After", ""))
    except (TypeError, ValueError):
        return default
    if valor != valor or valor < 0:            # NaN o negativo
        return default
    return min(valor, cap)
