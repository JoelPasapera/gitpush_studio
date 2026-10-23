# -*- coding: utf-8 -*-
"""Revisión de seguridad: archivos grandes y posibles secretos.

`scan_entries` trabaja sobre cualquier lista de (rel, ruta, tamaño), así
que el pipeline le pasa la vista de git y el botón «Analizar» la del
recorrido, y ambos revisan exactamente lo mismo que se subiría.
"""

import fnmatch
import re
import time

from .fs import MB, fmt_size, is_binary_ext, looks_binary
from .reader import iter_payloads
from .walk import project_entries

LIMIT_HARD = 100 * MB       # GitHub rechaza archivos mayores
LIMIT_WARN = 50 * MB        # GitHub avisa a partir de aquí
MAX_CONTENT = 1 * MB        # solo se lee el contenido de textos pequeños
CONTENT_BUDGET = 80 * MB    # tope total de lectura para no tardar
MAX_FINDINGS = 300

SECRET_NAME_EXCLUDE = (".env.example", ".env.sample", ".env.template",
                       "*.pub")
SECRET_NAME_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "*.ppk",
    "*.keystore", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "credentials.json", "service-account*.json", "secrets.json",
    "secrets.yml", "secrets.yaml", ".npmrc", ".netrc", ".htpasswd",
)

# Cada patrón, con el formato concreto de un proveedor, casi nunca se
# equivoca: confianza ALTA. El último es una heurística general y sí se
# equivoca (un ejemplo en la documentación, una prueba…): confianza MEDIA,
# o BAJA si el valor parece de mentira.
SECRET_CONTENT_PATTERNS = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
     "llave privada dentro", "alta"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "token de GitHub dentro",
     "alta"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
     "token de GitHub dentro", "alta"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "clave de AWS dentro", "alta"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "clave de Google dentro",
     "alta"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
     "token de Slack dentro", "alta"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"), "clave API (sk-…) dentro",
     "alta"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\b\s*[:=]\s*"
                r"['\"]([^'\"\n]{8,})['\"]"),
     "posible clave o contraseña escrita", "media"),
)

# Palabras que delatan un valor de relleno, no un secreto de verdad.
PLACEHOLDER_WORDS = ("example", "ejemplo", "sample", "muestra", "dummy",
                     "fake", "falso", "placeholder", "changeme", "cambiame",
                     "your", "tu-", "tu_", "yourtoken", "yourkey", "xxxx",
                     "todo", "test", "prueba", "demo", "none", "null",
                     "aqui", "here", "abcdef", "123456")


def looks_like_placeholder(value):
    """¿El valor parece de mentira? («aaaa…», «<tu-token>», «abcdefgh»)."""
    v = (value or "").strip()
    if not v:
        return True
    low = v.lower()
    if any(c in v for c in "<>{}$"):          # <tu-token>, ${VAR}, {{key}}
        return True
    if any(w in low for w in PLACEHOLDER_WORDS):
        return True
    if len(set(v)) <= 2:                       # "aaaaaaaa", "xxxxxxxx"
        return True
    run = longest = 1                          # "abcdefghijk", "12345678"
    for a, b in zip(v, v[1:]):
        run = run + 1 if ord(b) - ord(a) == 1 else 1
        longest = max(longest, run)
    return longest >= 8


_ORDER = {"huge": 0, "secret": 1, "big": 2}
_CONFIDENCE_ORDER = {"alta": 0, "media": 1, "baja": 2}


def looks_secret_name(name):
    low = name.lower()
    for pat in SECRET_NAME_EXCLUDE:
        if fnmatch.fnmatch(low, pat):
            return False
    return any(fnmatch.fnmatch(low, pat) for pat in SECRET_NAME_PATTERNS)


def _finding(rel, size, kind, reason, confidence="alta"):
    """`confidence` dice cuánto fiarse: «alta» casi seguro que es un
    secreto, «baja» probablemente sea un ejemplo. La interfaz marca por
    defecto solo lo que no es «baja», y la publicación automática solo se
    detiene por «alta» o «media»."""
    return {"path": rel, "size": size, "kind": kind, "reason": reason,
            "confidence": confidence}


def scan_entries(entries, budget=CONTENT_BUDGET, max_findings=MAX_FINDINGS):
    """Devuelve (hallazgos, estadísticas).

    Cada hallazgo: {"path", "size", "kind": huge|big|secret, "reason"}.
    Las comprobaciones de tamaño y nombre no abren nada; el contenido
    de los textos pequeños se lee en paralelo hasta agotar `budget`.
    """
    t0 = time.time()
    findings, candidates = [], []
    files_seen = content_read = used = 0
    truncated = False

    for rel, fp, size in entries:
        files_seen += 1
        name = rel.rsplit("/", 1)[-1]
        if size > LIMIT_HARD:
            findings.append(_finding(rel, size, "huge",
                                     f"{fmt_size(size)} · GitHub lo "
                                     "rechaza (más de 100 MB)"))
        elif size > LIMIT_WARN:
            findings.append(_finding(rel, size, "big",
                                     f"{fmt_size(size)} · aviso "
                                     "(más de 50 MB)", "media"))
        elif looks_secret_name(name):
            findings.append(_finding(rel, size, "secret",
                                     "nombre típico de archivo con claves"))
        elif (0 < size <= MAX_CONTENT and used < budget
              and not is_binary_ext(name)):
            candidates.append((rel, fp, size))
            used += size
        if len(findings) >= max_findings:
            truncated = True
            break

    if not truncated:
        for rel, size, raw in iter_payloads(candidates, MAX_CONTENT):
            if not isinstance(raw, bytes) or not raw or looks_binary(raw):
                continue
            content_read += 1
            txt = raw.decode("utf-8", "replace")
            for rx, reason, confidence in SECRET_CONTENT_PATTERNS:
                m = rx.search(txt)
                if not m:
                    continue
                if confidence == "media" and m.lastindex \
                        and looks_like_placeholder(m.group(m.lastindex)):
                    confidence = "baja"
                    reason += " (parece un valor de ejemplo)"
                findings.append(_finding(rel, size, "secret", reason,
                                         confidence))
                break
            if len(findings) >= max_findings:
                truncated = True
                break

    findings.sort(key=lambda f: (_CONFIDENCE_ORDER[f["confidence"]],
                                 _ORDER[f["kind"]], -f["size"]))
    stats = {"files": files_seen, "content": content_read,
             "seconds": time.time() - t0, "truncated": truncated}
    return findings, stats


def scan_project(folder, extra_patterns=(), use_git=True, git=None):
    """Revisión completa de una carpeta (git si es repositorio)."""
    entries, _ = project_entries(folder, extra_patterns, use_git=use_git,
                                 git=git)
    return scan_entries(entries)
