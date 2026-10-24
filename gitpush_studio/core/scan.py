# -*- coding: utf-8 -*-
"""Revisión de seguridad: archivos grandes y posibles secretos.

`scan_entries` trabaja sobre cualquier lista de (rel, ruta, tamaño), así
que el pipeline le pasa la vista de git y el botón «Analizar» la del
recorrido, y ambos revisan exactamente lo mismo que se subiría.

Qué se garantiza (y antes no):

  · se mira el contenido de TODOS los archivos de texto, no solo los de
    hasta 1 MB: los grandes se leen por trozos solapados, con memoria
    acotada (un notebook de 3 MB con una clave dentro se publicaba sin
    aviso);
  · los textos en UTF-16 (lo que escribe PowerShell) se leen como texto,
    no se descartan como binarios;
  · cada regla revisa TODAS sus coincidencias: un valor de ejemplo
    encima de la clave de verdad ya no la tapa;
  · que haya muchos hallazgos por nombre no deja la lectura de contenido
    sin hacer: el tope se aplica al final, quedándose con lo más grave;
  · lo que no se pudo revisar (un texto descomunal, el tiempo agotado)
    se dice, en vez de dar un «sin hallazgos» que no es verdad.
"""

import os
import stat
import time

from . import secret_rules as rules
from .fs import MB, fmt_size, is_binary_ext
from .reader import Link, Special, iter_payloads
from .secret_rules import (ALTA, BAJA, MEDIA, looks_like_placeholder,
                           looks_secret_name)
from .textenc import detect, to_utf8
from .walk import project_entries

__all__ = ("scan_entries", "scan_project", "stats_lines",
           "looks_like_placeholder", "looks_secret_name", "LIMIT_HARD",
           "LIMIT_WARN")

LIMIT_HARD = 100 * MB       # GitHub rechaza archivos mayores
LIMIT_WARN = 50 * MB        # GitHub avisa a partir de aquí
SMALL_FILE = 1 * MB         # se leen enteros, en paralelo
MAX_SCAN_FILE = 64 * MB     # los mayores (de texto) se señalan sin leer
CONTENT_BUDGET = 1024 * MB  # tope total de lectura por revisión
CHUNK = 1 * MB
OVERLAP = 8 * 1024          # un secreto partido entre dos trozos se ve igual
MAX_FINDINGS = 300

_ORDER = {"huge": 0, "secret": 1, "big": 2}


def _finding(rel, size, kind, reason, confidence=ALTA, line=0):
    """`confidence` dice cuánto fiarse: «alta» casi seguro que es un
    secreto, «baja» probablemente sea un ejemplo. La interfaz marca por
    defecto solo lo que no es «baja», y la publicación automática solo se
    detiene por «alta» o «media». `line` (si se sabe) es dónde está."""
    return {"path": rel, "size": size, "kind": kind, "reason": reason,
            "confidence": confidence, "line": line}


class _Best:
    """El hallazgo de secreto más grave de cada archivo."""

    def __init__(self):
        self.por_ruta = {}

    def offer(self, rel, size, reason, confidence, line=0):
        """Se queda con lo más grave; a igual gravedad, con lo que dice
        la línea (más concreto que un aviso por el nombre)."""
        actual = self.por_ruta.get(rel)
        if actual is not None:
            antes = (rules.RANK[actual["confidence"]], not actual["line"])
            ahora = (rules.RANK[confidence], not line)
            if ahora >= antes:
                return
        self.por_ruta[rel] = _finding(rel, size, "secret", reason,
                                      confidence, line)


def _line_of(data, pos, linea_base=0):
    return linea_base + data.count(b"\n", 0, pos) + 1


def _scan_text(rel, size, texto, best):
    hit = rules.find_secrets(texto, rel.lower())
    if hit:
        motivo, conf, pos = hit
        linea = _line_of(texto, pos)
        best.offer(rel, size, f"{motivo} (línea {linea})", conf, linea)
        return True
    return False


def _scan_large(rel, path, size, best):
    """Archivo de texto grande: por trozos solapados, memoria acotada.
    Devuelve los bytes leídos (para el presupuesto)."""
    leidos = 0
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                     | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return 0
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return 0
        with os.fdopen(fd, "rb") as fh:
            fd = None
            primero = fh.read(CHUNK)
            leidos += len(primero)
            codificacion, _ = detect(primero)
            if codificacion is None:
                return leidos                      # binario: nada que leer
            if codificacion != "utf-8":
                # UTF-16 se pasa a UTF-8 de una vez (acotado por el tope).
                resto = fh.read(MAX_SCAN_FILE)
                leidos += len(resto)
                texto = to_utf8(primero + resto) or b""
                _scan_text(rel, size, texto, best)
                return leidos
            cola = b""
            lineas_antes = 0
            trozo = primero
            while trozo:
                datos = cola + trozo
                hit = rules.find_secrets(datos, rel.lower())
                if hit:
                    motivo, conf, pos = hit
                    linea = _line_of(datos, pos, lineas_antes)
                    best.offer(rel, size, f"{motivo} (línea {linea})", conf,
                               linea)
                    if conf == ALTA:
                        return leidos
                consumido = datos[:-OVERLAP] if len(datos) > OVERLAP else b""
                lineas_antes += consumido.count(b"\n")
                cola = datos[len(consumido):]
                trozo = fh.read(CHUNK)
                leidos += len(trozo)
    except OSError:
        return leidos
    finally:
        if fd is not None:
            os.close(fd)
    return leidos


def scan_entries(entries, budget=CONTENT_BUDGET, max_findings=MAX_FINDINGS):
    """Devuelve (hallazgos, estadísticas).

    Cada hallazgo: {"path", "size", "kind": huge|big|secret, "reason",
    "confidence"}. Las comprobaciones de tamaño y nombre no abren nada;
    el contenido de los textos se lee en paralelo (los pequeños) o por
    trozos (los grandes) hasta agotar `budget`.
    """
    t0 = time.time()
    tamanos, best = [], _Best()
    pequenos, grandes = [], []
    files_seen = content_read = 0
    sin_revisar = 0

    for rel, fp, size in entries:
        files_seen += 1
        name = rel.rsplit("/", 1)[-1]
        if size > LIMIT_HARD:
            tamanos.append(_finding(rel, size, "huge",
                                    f"{fmt_size(size)} · GitHub lo "
                                    "rechaza (más de 100 MB)"))
        elif size > LIMIT_WARN:
            tamanos.append(_finding(rel, size, "big",
                                    f"{fmt_size(size)} · aviso "
                                    "(más de 50 MB)", MEDIA))
        por_nombre = rules.name_rule(rel)
        if por_nombre:
            best.offer(rel, size, *por_nombre)
        mirar = rules.needs_inspection(rel)
        if size <= 0 or (is_binary_ext(name) and not mirar):
            continue
        if size <= SMALL_FILE:
            pequenos.append((rel, fp, size))
        elif size <= MAX_SCAN_FILE:
            grandes.append((rel, fp, size))
        else:
            sin_revisar += 1
            best.offer(rel, size, f"contenido sin revisar: texto de "
                                  f"{fmt_size(size)}", BAJA)

    gastado = 0
    lote = []
    for item in pequenos:
        if gastado + item[2] > budget:
            sin_revisar += 1
            continue
        gastado += item[2]
        lote.append(item)
    for rel, size, raw in iter_payloads(lote, SMALL_FILE):
        if isinstance(raw, (Link, Special, Exception)) or raw is None:
            continue
        content_read += 1
        texto = to_utf8(bytes(raw))
        encontrado = False
        if texto:
            encontrado = _scan_text(rel, size, texto, best)
        if not encontrado and rules.needs_inspection(rel):
            veredicto = rules.inspect_verdict(rel, bytes(raw))
            if veredicto:
                best.offer(rel, size, *veredicto)

    for rel, fp, size in grandes:
        if gastado + size > budget:
            sin_revisar += 1
            continue
        gastado += _scan_large(rel, fp, size, best)
        content_read += 1

    findings = tamanos + list(best.por_ruta.values())
    # Lo más grave primero; a igual gravedad, lo que se vio DENTRO (con
    # su línea) antes que lo que solo delata el nombre: si hay que
    # recortar la lista, que no se pierda un token por cien «.pem».
    findings.sort(key=lambda f: (rules.RANK[f["confidence"]],
                                 _ORDER[f["kind"]], not f["line"],
                                 -f["size"]))
    truncated = len(findings) > max_findings
    if truncated:
        findings = findings[:max_findings]
    stats = {"files": files_seen, "content": content_read,
             "unscanned": sin_revisar, "bytes": gastado,
             "seconds": time.time() - t0, "truncated": truncated}
    return findings, stats


def stats_lines(stats):
    """Lo que hay que contar de una revisión, ya redactado: (texto,
    etiqueta) con «dim» para lo normal y «warn» para lo que el usuario
    debe saber (archivos que no se pudieron leer, lista recortada)."""
    lineas = [(f"Revisados {stats['files']} archivos ({stats['content']} "
               f"por dentro) en {stats['seconds']:.1f} s.", "dim")]
    if stats.get("unscanned"):
        lineas.append((f"⚠ {stats['unscanned']} archivo(s) de texto no se "
                       "pudieron revisar por dentro (demasiado grandes o "
                       "sin tiempo): revísalos tú antes de publicar.",
                       "warn"))
    if stats.get("truncated"):
        lineas.append((f"⚠ Hay más de {MAX_FINDINGS} hallazgos; se muestran "
                       "los más graves.", "warn"))
    return lineas


def scan_project(folder, extra_patterns=(), use_git=True, git=None):
    """Revisión completa de una carpeta (git si es repositorio)."""
    entries, _ = project_entries(folder, extra_patterns, use_git=use_git,
                                 git=git)
    return scan_entries(entries)
