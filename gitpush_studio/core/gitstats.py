# -*- coding: utf-8 -*-
"""Analíticas del historial de git: quién, cuándo y qué se toca más.

Una sola llamada a `git log` con `--numstat -z`, leída EN FLUJO: la
salida de un repositorio con cien mil commits no se carga entera en
memoria, se procesa registro a registro según llega. Con `-z` las rutas
llegan tal cual (espacios, tildes, saltos de línea incluidos) y los
renombrados como «campo vacío + ruta vieja + ruta nueva».

Los renombrados se siguen: `git log` va del commit más nuevo al más
viejo, así que cuando aparece «viejo → nuevo» ya se sabe cómo se llama
hoy el archivo, y todo lo anterior que tocó «viejo» se suma a su nombre
actual. Sin esto, mover una carpeta partiría el historial de cada
archivo en dos y ninguno parecería tocarse mucho.

La hora y el día de la semana son los del AUTOR (los que muestra `%ai`),
no los del equipo que analiza: «a qué hora trabaja este equipo» solo
tiene sentido en su propia zona horaria.
"""

import datetime as _dt
import os
import subprocess
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

from .git import BASE_ENV, NO_WINDOW

DEFAULT_MAX_COMMITS = 100000
_SEP_REC = b"\x1e"
_SEP_CAMPO = b"\x1f"
_FORMATO = "%x1e%H%x1f%aN%x1f%aE%x1f%at%x1f%ai"
DIAS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")


@dataclass
class AuthorStats:
    name: str
    email: str
    commits: int = 0
    added: int = 0
    deleted: int = 0
    first: int = 0            # marca de tiempo (UTC) del primer commit
    last: int = 0


@dataclass
class FileChurn:
    path: str
    commits: int = 0
    added: int = 0
    deleted: int = 0
    exists: bool = True       # sigue en el proyecto hoy

    @property
    def lines_changed(self):
        return self.added + self.deleted


@dataclass
class GitReport:
    commits: int = 0
    first: int = 0
    last: int = 0
    authors: list = field(default_factory=list)      # [AuthorStats]
    months: list = field(default_factory=list)       # [("2026-10", n)]
    heatmap: list = field(default_factory=list)      # 7 × 24 (lun=0)
    churn: list = field(default_factory=list)        # [FileChurn]
    bus_factor: int = 0
    added: int = 0
    deleted: int = 0
    truncated: bool = False
    shallow: bool = False
    seconds: float = 0.0

    def to_dict(self):
        return {
            "commits": self.commits, "first": self.first, "last": self.last,
            "added": self.added, "deleted": self.deleted,
            "bus_factor": self.bus_factor, "truncated": self.truncated,
            "shallow": self.shallow, "seconds": round(self.seconds, 3),
            "authors": [a.__dict__ for a in self.authors],
            "months": [list(m) for m in self.months],
            "heatmap": self.heatmap,
            "churn": [dict(c.__dict__, lines_changed=c.lines_changed)
                      for c in self.churn],
        }


# ------------------------------------------------------------------ #
#  Lectura                                                            #
# ------------------------------------------------------------------ #
def _git_log_chunks(folder, max_commits, cancel=None, exe="git"):
    """Trozos de bytes de `git log`, según llegan. Para si `cancel`."""
    args = [exe, "-c", "core.quotepath=false", "log", "--no-merges", "-M",
            "--numstat", "-z", "--format=" + _FORMATO,
            "--max-count=%d" % max_commits]
    env = dict(os.environ)
    env.update(BASE_ENV)
    try:
        p = subprocess.Popen(args, cwd=folder, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=env,
                             creationflags=NO_WINDOW)
    except (FileNotFoundError, OSError):
        return
    try:
        while True:
            if cancel is not None and cancel.is_set():
                p.kill()
                return
            trozo = p.stdout.read(1 << 16)
            if not trozo:
                break
            yield trozo
    finally:
        try:
            p.stdout.close()
        except OSError:
            pass
        p.wait()


def iter_records(chunks):
    """Registros completos (bytes, sin el separador) a partir de trozos."""
    resto = b""
    for trozo in chunks:
        resto += trozo
        partes = resto.split(_SEP_REC)
        resto = partes.pop()
        for p in partes:
            if p:
                yield p
    if resto:
        yield resto


def parse_record(rec):
    """Un commit → (sha, autor, correo, marca, iso, [(añ, bor, viejo, nuevo)]).

    `viejo` es None salvo en los renombrados. Las líneas de binarios
    llegan como «-»: cuentan como cambio pero con 0 líneas.
    """
    cabecera, _, resto = rec.partition(b"\x00")
    campos = cabecera.split(_SEP_CAMPO)
    if len(campos) < 5:
        return None
    sha, nombre, correo, marca, iso = campos[:5]
    try:
        marca = int(marca)
    except ValueError:
        return None
    # Tras la cabecera git pone un único «\n» antes del numstat. Solo se
    # quita ese: un nombre de archivo podría empezar por salto de línea.
    if resto.startswith(b"\n"):
        resto = resto[1:]
    tokens = resto.split(b"\x00")
    cambios, i, n = [], 0, len(tokens)
    while i < n:
        tok = tokens[i]
        i += 1
        if not tok:
            continue
        partes = tok.split(b"\t", 2)
        if len(partes) != 3:
            continue
        a, d, ruta = partes
        viejo = None
        if ruta == b"":                       # renombrado: dos rutas más
            if i + 1 >= n:
                break                         # registro truncado
            viejo, ruta = tokens[i], tokens[i + 1]
            i += 2
        cambios.append((_num(a), _num(d),
                        _txt(viejo) if viejo is not None else None,
                        _txt(ruta)))
    return (_txt(sha), _txt(nombre), _txt(correo), marca, _txt(iso),
            cambios)


def _num(b):
    try:
        return int(b)
    except ValueError:
        return 0                               # «-» de los binarios


def _txt(b):
    return b.decode("utf-8", "replace")


# ------------------------------------------------------------------ #
#  Agregado                                                           #
# ------------------------------------------------------------------ #
def _local_parts(iso):
    """«2026-10-16 00:36:12 -0500» → ("2026-10", día_semana, hora)."""
    try:
        fecha, hora = iso.split(" ")[:2]
        y, m, d = (int(x) for x in fecha.split("-"))
        h = int(hora.split(":")[0])
        return "%04d-%02d" % (y, m), _dt.date(y, m, d).weekday(), h
    except (ValueError, IndexError):
        return None


def _month_range(first, last):
    """Todos los meses entre dos «AAAA-MM», también los vacíos: una línea
    de tiempo con huecos escondidos miente sobre el ritmo del proyecto."""
    y, m = (int(x) for x in first.split("-"))
    ly, lm = (int(x) for x in last.split("-"))
    out = []
    while (y, m) <= (ly, lm) and len(out) < 1200:
        out.append("%04d-%02d" % (y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def bus_factor(commit_counts, umbral=0.5):
    """Cuántas personas, de más a menos activas, hacen falta para sumar
    MÁS de la mitad de los commits. Un 1 dice que el proyecto depende de
    una sola persona; con dos personas al 50 % sale 2, porque si se va
    cualquiera de las dos se pierde la mitad, no «casi nada»."""
    total = sum(commit_counts)
    if total <= 0:
        return 0
    acumulado = 0
    for n, c in enumerate(sorted(commit_counts, reverse=True), 1):
        acumulado += c
        if acumulado > total * umbral:
            return n
    return len(commit_counts)


class _UnionFind:
    """Conjuntos disjuntos con compresión de caminos y unión por rango.

    Cada firma de autor aporta dos nodos —su nombre normalizado y su
    correo— y los une. Al final, cada componente es una persona: así
    «Ana Pérez <ana@casa>» y «ana perez <ana@trabajo>» cuentan como una,
    y también «☿ Kenneth Reitz» y «Kenneth Reitz».
    """

    __slots__ = ("padre", "rango")

    def __init__(self):
        self.padre = {}
        self.rango = {}

    def find(self, x):
        padre = self.padre
        if x not in padre:
            padre[x] = x
            self.rango[x] = 0
            return x
        raiz = x
        while padre[raiz] != raiz:
            raiz = padre[raiz]
        while padre[x] != raiz:                 # compresión del camino
            padre[x], x = raiz, padre[x]
        return raiz

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return ra
        if self.rango[ra] < self.rango[rb]:
            ra, rb = rb, ra
        self.padre[rb] = ra
        if self.rango[ra] == self.rango[rb]:
            self.rango[ra] += 1
        return ra


# Lo que NO sirve para decidir que dos firmas son la misma persona: los
# correos de relleno que mucha gente deja sin cambiar y los nombres de
# cuentas genéricas. Si unieran, fundirían a desconocidos en uno.
_GENERIC_NAMES = frozenset({"root", "user", "admin", "administrator",
                            "ubuntu", "unknown", "your name", "yourname",
                            "nombre", "git", "dev", "developer", "test"})
_GENERIC_EMAIL_PARTS = ("example.com", "example.org", "localhost",
                        "localdomain", "none", "noreply@github.com",
                        "(none)", "users.noreply.github.com.invalid")


def author_name_key(nombre):
    """«☿ Ana Pérez» → «ana perez»: sin símbolos, tildes ni mayúsculas."""
    base = unicodedata.normalize("NFKD", nombre or "")
    letras = "".join(c for c in base if c.isalnum() or c.isspace())
    return " ".join(letras.casefold().split())


def _useful_email(correo):
    c = (correo or "").strip().casefold()
    if not c or "@" not in c:
        return ""
    if any(p in c for p in _GENERIC_EMAIL_PARTS):
        return ""
    return c


class _Firma:
    """Lo acumulado por una firma concreta (nombre normalizado, correo)."""

    __slots__ = ("stats", "nombres", "nodo")

    def __init__(self, nombre, correo, marca):
        self.stats = AuthorStats(nombre, correo, first=marca, last=marca)
        self.nombres = {}                    # nombre → último uso (marca)
        self.nodo = ""


def _merge_authors(firmas):
    """{(clave_nombre, correo): _Firma} → [AuthorStats].

    Une con union-find las firmas que comparten nombre normalizado o
    correo útil y suma sus cifras. Se muestra el nombre que la persona
    usó MÁS RECIENTEMENTE (la gente cambia de nombre, y es el que quiere
    que se use) y el correo con más commits.
    """
    uf = _UnionFind()
    for (nombre_k, correo_k) in firmas:
        nodos = []
        if nombre_k and nombre_k not in _GENERIC_NAMES:
            nodos.append("n:" + nombre_k)
        correo_util = _useful_email(correo_k)
        if correo_util:
            nodos.append("e:" + correo_util)
        if not nodos:                       # firma sin nada que la una
            nodos.append("f:%s|%s" % (nombre_k, correo_k))
        for otro in nodos[1:]:
            uf.union(nodos[0], otro)
        uf.find(nodos[0])
        firmas[(nombre_k, correo_k)].nodo = nodos[0]
    grupos = defaultdict(list)
    for firma in firmas.values():
        grupos[uf.find(firma.nodo)].append(firma)
    personas = []
    for partes in grupos.values():
        nombres, correos = {}, defaultdict(int)
        total = AuthorStats("", "",
                            first=min(f.stats.first for f in partes),
                            last=max(f.stats.last for f in partes))
        for f in partes:
            total.commits += f.stats.commits
            total.added += f.stats.added
            total.deleted += f.stats.deleted
            for n, cuando in f.nombres.items():
                nombres[n] = max(nombres.get(n, cuando), cuando)
            if f.stats.email:
                correos[f.stats.email] += f.stats.commits
        total.name = max(nombres.items(), key=lambda kv: (kv[1], kv[0]))[0]
        total.email = max(correos.items(),
                          key=lambda kv: (kv[1], kv[0]))[0] if correos else ""
        personas.append(total)
    return personas


def aggregate(records, existing=None, top_churn=25):
    """Construye el `GitReport` a partir de registros ya parseados.

    `existing` es el conjunto de rutas que hay hoy en el proyecto, para
    marcar qué archivos muy tocados ya no existen.
    """
    rep = GitReport()
    firmas = {}                    # (nombre normalizado, correo) → parcial
    meses = defaultdict(int)
    heat = [[0] * 24 for _ in range(7)]
    churn = {}
    alias = {}                     # nombre antiguo → nombre de hoy

    for sha, nombre, correo, marca, iso, cambios in records:
        rep.commits += 1
        rep.first = marca if not rep.first else min(rep.first, marca)
        rep.last = max(rep.last, marca)
        clave = (author_name_key(nombre), (correo or "").strip().casefold())
        firma = firmas.get(clave)
        if firma is None:
            firma = firmas[clave] = _Firma(nombre, correo, marca)
        firma.nombres[nombre] = max(firma.nombres.get(nombre, marca), marca)
        a = firma.stats
        a.commits += 1
        a.first = min(a.first, marca)
        a.last = max(a.last, marca)
        partes = _local_parts(iso)
        if partes:
            mes, dia, hora = partes
            meses[mes] += 1
            if 0 <= hora < 24:
                heat[dia][hora] += 1
        for anadidas, borradas, viejo, nuevo in cambios:
            a.added += anadidas
            a.deleted += borradas
            rep.added += anadidas
            rep.deleted += borradas
            actual = alias.get(nuevo, nuevo)
            if viejo is not None:
                alias[viejo] = actual
            ch = churn.get(actual)
            if ch is None:
                ch = churn[actual] = FileChurn(actual)
            ch.commits += 1
            ch.added += anadidas
            ch.deleted += borradas

    rep.authors = sorted(_merge_authors(firmas),
                         key=lambda x: (-x.commits, x.name.lower()))
    if meses:
        orden = sorted(meses)
        rep.months = [(m, meses.get(m, 0))
                      for m in _month_range(orden[0], orden[-1])]
    rep.heatmap = heat
    lista = sorted(churn.values(),
                   key=lambda c: (-c.commits, -c.lines_changed, c.path))
    if existing is not None:
        for c in lista:
            c.exists = c.path in existing
    rep.churn = lista[:top_churn]
    rep.bus_factor = bus_factor([a.commits for a in rep.authors])
    return rep


def analyze_history(folder, existing=None, max_commits=DEFAULT_MAX_COMMITS,
                    cancel=None, git=None, chunks=None):
    """`GitReport` de la carpeta, o None si no es un repositorio con
    commits. `chunks` existe para las pruebas (salida de git fingida)."""
    t0 = time.time()
    if chunks is None:
        if not os.path.isdir(os.path.join(folder, ".git")):
            return None
        chunks = _git_log_chunks(folder, max_commits, cancel,
                                 exe=getattr(git, "exe", "git"))
    registros = (r for r in (parse_record(x) for x in iter_records(chunks))
                 if r is not None)
    rep = aggregate(registros, existing)
    if rep.commits == 0:
        return None
    rep.truncated = rep.commits >= max_commits
    rep.shallow = os.path.isfile(os.path.join(folder, ".git", "shallow"))
    rep.seconds = time.time() - t0
    return rep
