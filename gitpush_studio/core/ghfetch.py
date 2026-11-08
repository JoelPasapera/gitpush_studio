# -*- coding: utf-8 -*-
"""Descargar todo lo que se habló en un repositorio de GitHub.

Lo caro de GitHub es el tiempo de cada consulta y la cuota (5.000 por
hora con token, 60 sin él), así que el plan es:

  1. los LISTADOS DE TODO EL REPOSITORIO, de 100 en 100: issues y pull
     requests juntos, los pull requests (ramas, fusión, borrador), todos
     los comentarios, todos los comentarios en el código y todos los
     eventos. En cuanto la primera página dice cuál es la última, las
     demás se piden A LA VEZ;
  2. lo que GitHub solo da issue a issue —las revisiones y los commits
     de cada pull request y las menciones desde otros issues y PR—: con
     token, por GraphQL, todo lo de cada uno en un mismo alias y muchos
     por consulta (`ghgraphql`: cien veces menos consultas, y de otra
     cuota); lo que no cabe, o sin token, uno a uno por REST.

Todo eso son FUENTES de consultas (`ghsched.Source`) que se reparten las
mismas conexiones (`ghsched.Scheduler`), sin esperar unas a otras: lo de
cada pull request empieza en cuanto se sabe qué pull requests hay,
mientras siguen los demás listados. Cuántas consultas van a la vez lo
decide `ghlimits` (empieza con 4 y se adapta a lo que GitHub aguante).

Pedir páginas a la vez tiene una trampa: GitHub numera las páginas
contando desde el principio del listado, y si mientras tanto aparece
algo nuevo delante, todo corre un puesto. De una en una y en orden eso
solo repite algo (inofensivo); a la vez y desordenadas, podría saltarse
algo. Por eso (`ListingSource`):

  · los listados por fecha de creación ascendente (issues, PR,
    comentarios) reciben lo nuevo AL FINAL: no corre nada, y al terminar
    se vuelve a pedir la última página y lo que venga detrás («la
    cola»);
  · el de eventos va del más reciente al más antiguo (GitHub no deja
    ordenarlo): se pide por tandas y, tras cada tanda, se mira si el
    evento más reciente sigue siendo el mismo. Si cambió, la tanda se
    vuelve a pedir en orden, de una en una, que así no se salta nada;
  · el cursor guardado solo avanza por lo que está completo desde el
    principio, y al retomar se vuelve a pedir la página anterior (por si
    algo se borró entretanto y las páginas corrieron hacia atrás).

Cada página o lote se guarda en el almacén (`ghstore`) con lo que ya
está hecho: si GitHub pide esperar, se cae la conexión o se pulsa
«Detener», lo descargado no se pierde y la próxima vez se sigue desde
ahí. Lo que se guarda es lo mismo venga por donde venga (`ghrecords`):
el TXT no depende del camino.
"""

import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from typing import (Callable, Deque, Dict, List, Optional, Sequence, Set,
                    Tuple)

from . import ghgraphql
from .ghgraphql import BatchItem, BatchOutcome
from .ghpool import DEFAULT_WORKERS, SessionPool
from .ghrecords import (code_comment_entry, comment_entry, commit_entry,
                        event_entry, issue_item, item_number, mention_entry,
                        pull_as_item, pull_record, review_entry)
from .ghsched import Scheduler, Source, Task
from .ghsession import ApiError, ApiSession, Page, page_number
from .ghstore import Entry, Store

Rows = Dict[str, list]

# Cursor de un listado que se está pidiendo en paralelo:
# «paralelo:<primera página aún no completa>|<URL de la última página>».
PARALLEL_PREFIX = "paralelo:"
# Páginas que tienen que quedar para que merezca la pena repartirlas.
PARALLEL_MIN_PAGES = 2
# Páginas por conexión en cada tanda de un listado «más reciente
# primero» (tras cada tanda, una consulta de un elemento comprueba que
# nada se movió: con 8 conexiones, una de cada 32).
PAGES_PER_WORKER = 4
# Tandas que pueden estropearse por novedades antes de seguir ese
# listado de una en una (un repositorio con mucha actividad en ese
# momento: en paralelo se repetiría casi todo).
MAX_HEAD_CHANGES = 2
# Lotes seguidos de GraphQL en los que falla TODO antes de pasar lo que
# falta a REST (algo entre medias que no deja usar GraphQL de verdad).
# Un lote con listas que no cabían NO cuenta: eso es normal (esas listas
# van por REST y GraphQL sigue con lo demás).
MAX_FAILED_BATCHES = 3
# Lo que GitHub contesta para un issue/PR oculto o borrado: se sigue
# con los demás.
MISSING_ITEM = (404, 410, 422)


@dataclass(frozen=True)
class FetchOptions:
    """Lo opcional de la descarga (cada cosa cuesta consultas)."""
    commits: bool = True               # los commits de cada PR
    mentions: bool = False             # menciones desde otros issues/PR
    workers: int = DEFAULT_WORKERS     # conexiones a la vez, como mucho
    graphql: bool = True               # lotes por GraphQL (necesita token)


@dataclass
class Progress:
    """Por dónde va la descarga, para enseñarlo."""
    stage: str
    requests: int
    estimate: int
    counts: Dict[str, int] = field(default_factory=dict)
    active: Tuple[str, ...] = ()       # lo que se está descargando ahora
    # Si algo no fue como debía (por ejemplo, GraphQL no se pudo usar y
    # todo va más lento), qué y por qué; si no, vacío.
    notice: str = ""

    @property
    def fraction(self) -> float:
        return min(1.0, self.requests / max(1, self.estimate))


ProgressCallback = Callable[[Progress], None]


# ------------------------------------------------------------------ #
#  Los listados                                                       #
# ------------------------------------------------------------------ #
def _rows_issues(items: Sequence[object]) -> Rows:
    return {"items": [issue_item(raw) for raw in items
                      if item_number(raw)]}


def _rows_pulls(items: Sequence[object]) -> Rows:
    validos = [raw for raw in items if item_number(raw)]
    return {"pulls": [pull_record(raw) for raw in validos],
            "new_items": [pull_as_item(raw) for raw in validos]}


def _entries(convert: Callable[[Dict[str, object]], Optional[Entry]]
             ) -> Callable[[Sequence[object]], Rows]:
    def rows(items: Sequence[object]) -> Rows:
        out: List[Entry] = []
        for raw in items:
            if isinstance(raw, dict):
                entry = convert(raw)
                if entry is not None:
                    out.append(entry)
        return {"entries": out}
    return rows


@dataclass(frozen=True)
class Listing:
    name: str
    label: str
    path: str
    params: Tuple[Tuple[str, str], ...]
    rows: Callable[[Sequence[object]], Rows]
    what: str
    short: str                         # para «Descargando …»
    # Códigos con los que el listado se da por vacío en vez de fallar
    # (410: los issues están desactivados en ese repositorio).
    missing_ok: Tuple[int, ...] = (410,)
    # Lo más reciente va primero (lo nuevo aparece DELANTE y empuja todo
    # hacia las páginas siguientes).
    newest_first: bool = False


_ASC = (("sort", "created"), ("direction", "asc"))
LISTINGS: Tuple[Listing, ...] = (
    Listing("issues", "Issues y pull requests", "/issues",
            (("state", "all"),) + _ASC, _rows_issues, "los issues",
            "issues"),
    Listing("pulls", "Ramas y fusiones de los pull requests", "/pulls",
            (("state", "all"),) + _ASC, _rows_pulls, "los pull requests",
            "pull requests"),
    Listing("comments", "Comentarios", "/issues/comments", _ASC,
            _entries(comment_entry), "los comentarios", "comentarios"),
    Listing("code", "Comentarios en el código", "/pulls/comments", _ASC,
            _entries(code_comment_entry), "los comentarios en el código",
            "comentarios en el código"),
    # GitHub no deja ordenar los eventos: van del más reciente al más
    # antiguo.
    Listing("events", "Eventos (cierres, etiquetas, asignaciones…)",
            "/issues/events", (), _entries(event_entry), "los eventos",
            "eventos", newest_first=True),
)


@dataclass(frozen=True)
class PerItem:
    """Lo que GitHub da de un issue/PR concreto (una consulta cada uno
    por REST). `name` es también el prefijo de lo hecho en el almacén
    («reviews:12»)."""
    name: str
    label: str
    template: str
    convert: Callable[[int, Dict[str, object]], Optional[Entry]]
    what: str


REVIEWS = PerItem(ghgraphql.REVIEWS, "Revisiones de cada pull request",
                  "/pulls/{n}/reviews", review_entry, "las revisiones")
COMMITS = PerItem(ghgraphql.COMMITS, "Commits de cada pull request",
                  "/pulls/{n}/commits", commit_entry, "los commits")
MENTIONS = PerItem(ghgraphql.TIMELINE, "Menciones desde otros issues y PR",
                   "/issues/{n}/timeline", mention_entry,
                   "el historial de un issue")
PER_ITEM: Dict[str, PerItem] = {p.name: p for p in (REVIEWS, COMMITS,
                                                    MENTIONS)}

COUNTED = (("comment", "comentarios"), ("code", "en el código"),
           ("event", "eventos"), ("review", "revisiones"),
           ("commit", "commits"), ("mention", "menciones"))


# ------------------------------------------------------------------ #
#  Cuenta y estimación                                                #
# ------------------------------------------------------------------ #
class _Tracker:
    """Cuenta lo descargado y estima lo que falta (en consultas)."""

    def __init__(self, pool: SessionPool, store: Store,
                 on_progress: Optional[ProgressCallback]) -> None:
        self.pool = pool
        self.store = store
        self.on_progress = on_progress
        self.sources: List[Source] = []
        self.counts: Dict[str, int] = {}
        self.recount()

    def recount(self) -> None:
        """Lo guardado de verdad (una página repetida no cuenta dos
        veces)."""
        self.counts["items"] = self.store.items_count()
        for kind, _label in COUNTED:
            self.counts[kind] = self.store.count(kind)

    def add(self, rows: Rows) -> None:
        for kind, _i, _n, _at, _d in rows.get("entries", ()):
            self.counts[kind] = self.counts.get(kind, 0) + 1
        if rows.get("items") or rows.get("new_items"):
            self.counts["items"] = self.store.items_count()

    def notice(self) -> str:
        """Los avisos de las fuentes (vacío si todo va como debe)."""
        return " ".join(s.notice for s in self.sources if s.notice)

    def report(self, final: bool = False) -> None:
        if self.on_progress is None:
            return
        activas = tuple(s.label for s in self.sources
                        if not s.finished() and (s.running or s.ready()))
        if final:
            stage, activas = "Listo", ()
        elif activas:
            stage = "Descargando " + ", ".join(activas)
        else:
            stage = "Preparando"
        made = self.pool.requests
        estimate = made + sum(s.pending() for s in self.sources
                              if not s.finished())
        self.on_progress(Progress(stage, made, estimate, dict(self.counts),
                                  activas, self.notice()))


# ------------------------------------------------------------------ #
#  Piezas de los listados                                             #
# ------------------------------------------------------------------ #
def _pages_left(page: Page) -> int:
    """Páginas que faltan tras `page` (si GitHub dice cuál es la última;
    si no, al menos una mientras haya «siguiente»)."""
    current = page_number(page.url)
    if page.last_page and current:
        return max(0, page.last_page - current)
    return 1 if page.next_url else 0


def page_url(last_url: str, number: int) -> str:
    """La URL de la página `number` a partir de la de la última (la que
    da GitHub en «Link»: la misma consulta con otro «page»)."""
    partes = urllib.parse.urlsplit(last_url)
    consulta = [(k, v) for k, v in urllib.parse.parse_qsl(
        partes.query, keep_blank_values=True) if k != "page"]
    consulta.append(("page", str(number)))
    return urllib.parse.urlunsplit(
        (partes.scheme, partes.netloc, partes.path,
         urllib.parse.urlencode(consulta), ""))


def parallel_cursor(first: int, last_url: str) -> str:
    """El cursor de un listado en paralelo: completo hasta `first`-1."""
    return f"{PARALLEL_PREFIX}{first}|{last_url}"


def parse_parallel_cursor(cursor: str) -> Optional[Tuple[int, str]]:
    """(primera página no completa, URL de la última) de un cursor en
    paralelo; None si no lo es o está estropeado (se empieza de nuevo)."""
    if not cursor.startswith(PARALLEL_PREFIX):
        return None
    primera, sep, last_url = cursor[len(PARALLEL_PREFIX):].partition("|")
    if not sep or not primera.isdigit() or not last_url \
            or page_number(last_url) < 1:
        return None
    return max(1, int(primera)), last_url


# Lo que devuelve la consulta de una página que GitHub da por vacía (410).
_MISSING = object()

# Modos de un listado.
_SEQUENTIAL = "en orden"         # siguiendo «siguiente», de una en una
_PARALLEL = "a la vez"           # ascendente: todas las páginas a la vez
_BATCHES = "por tandas"          # más reciente primero: tandas + sonda
_ONE_BY_ONE = "una a una"        # números de página, de uno en uno
_TAIL = "cola"                   # la última otra vez y lo que venga
_DONE = "hecho"

# Pasos de una tanda («por tandas»).
_PROBE_FIRST = "sonda inicial"   # qué es lo más reciente antes de empezar
_PAGES = "páginas"               # las de la tanda, a la vez
_PROBE = "sonda"                 # ¿sigue siendo lo más reciente lo mismo?
_REDO = "rehacer"                # cambió: la tanda otra vez, en orden


class ListingSource(Source):
    """Un listado de todo el repositorio, como fuente de consultas.

    Empieza en orden (la primera página dice cuántas hay) y, si quedan
    varias y hay más de una conexión, pasa a pedirlas a la vez sin poder
    saltarse nada (ver el comentario del módulo). Cada página se guarda
    en cuanto llega; el cursor del almacén («completo hasta la n») solo
    avanza por lo que es seguro.
    """

    def __init__(self, listing: Listing, store: Store, base: str,
                 tracker: _Tracker, workers: int) -> None:
        super().__init__()
        self.listing = listing
        self.label = listing.short
        self.store = store
        self.base = base
        self.tracker = tracker
        self.workers = workers
        self.window = max(1, workers * PAGES_PER_WORKER)
        self._mode = _SEQUENTIAL
        self._target = ""               # la siguiente, en orden ("" = 1.ª)
        self._left = 1                  # en orden: las que se sabe que faltan
        self._last_url = ""
        self._last_page = 0
        self._committed = 1             # primera página no completa
        self._queue: Deque[int] = deque()      # páginas por pedir
        self._arrived: Set[int] = set()        # «a la vez»: ya llegadas
        self._remaining: Deque[int] = deque()  # «por tandas»: sin tanda
        self._batch: List[int] = []            # la tanda en curso
        self._step = _PROBE_FIRST
        self._head: object = None
        self._changes = 0
        cursor, done = store.stage(listing.name)
        if done:
            self._mode = _DONE
            return
        paralelo = parse_parallel_cursor(cursor)
        if paralelo is not None:
            self._start_numbered(paralelo[0], paralelo[1], overlap=True)
        elif not cursor.startswith(PARALLEL_PREFIX):
            self._target = cursor       # retomar en esta página ("" = 1.ª)

    # ---- lo que ve el planificador ------------------------------------------
    def finished(self) -> bool:
        return self._mode == _DONE

    def pending(self) -> int:
        if self._mode in (_PARALLEL, _BATCHES, _ONE_BY_ONE):
            return len(self._queue) + len(self._remaining) \
                + len(self._batch) + 1
        if self._mode == _SEQUENTIAL:
            return max(1, self._left)
        return 0 if self._mode == _DONE else 1

    def next_task(self) -> Optional[Task]:
        if self._mode == _SEQUENTIAL or self._mode == _TAIL:
            if self.running:
                return None
            return self._page_task(("orden", self._target))
        if self._mode == _PARALLEL:
            if not self._queue:
                return None
            return self._page_task(("número", self._queue.popleft()))
        if self._mode == _ONE_BY_ONE:
            if self.running or not self._queue:
                return None
            return self._page_task(("número", self._queue.popleft()))
        if self._mode == _BATCHES:
            return self._next_in_batches()
        return None

    def on_result(self, task: Task, result: object) -> None:
        tipo, valor = task.data
        if tipo == "sonda":
            self._on_probe(result)
        elif result is _MISSING:
            self._finish()
        elif self._mode == _SEQUENTIAL:
            self._on_sequential(result)
        elif self._mode == _TAIL:
            self._on_tail(result)
        elif self._mode == _PARALLEL:
            self._on_parallel(int(valor), result)
        elif self._mode == _ONE_BY_ONE:
            self._save(result, advance_to=int(valor) + 1)
            if not self._queue and not self.running:
                self._start_tail()
        elif self._mode == _BATCHES:
            self._on_batch_page(int(valor), result)

    # ---- consultas ---------------------------------------------------------
    def _page_task(self, data: Tuple[str, object]) -> Task:
        tipo, valor = data
        if tipo == "número":
            url = page_url(self._last_url, int(valor))
        else:
            url = str(valor)
        return Task(self, lambda session: self._fetch(session, url), data)

    def _fetch(self, session: ApiSession, url: str) -> object:
        """(página, filas) de una página (en un hilo de trabajo: la
        conversión de lo que llega también se reparte). "" = la primera."""
        if not url:
            consulta: Dict[str, object] = dict(self.listing.params)
            consulta["per_page"] = 100
            url = session.url(session.target(self.base + self.listing.path,
                                             consulta))
        try:
            page = session.page(url, self.listing.what)
        except ApiError as e:
            if e.status in self.listing.missing_ok:
                return _MISSING
            raise
        return page, self.listing.rows(page.items)

    def _probe_task(self) -> Task:
        return Task(self, self._head_of, ("sonda", None))

    def _head_of(self, session: ApiSession) -> object:
        """El id de lo más reciente del listado (su primer elemento),
        con una consulta de un solo elemento."""
        consulta: Dict[str, object] = dict(self.listing.params)
        consulta["per_page"] = 1
        url = session.url(session.target(self.base + self.listing.path,
                                         consulta))
        page = session.page(url, self.listing.what)
        primero = page.items[0] if page.items else None
        return primero.get("id") if isinstance(primero, dict) else None

    # ---- guardar -----------------------------------------------------------
    def _save(self, result: object, advance_to: Optional[int] = None) -> None:
        """Guarda una página «numerada»; con `advance_to`, el listado
        queda completo hasta `advance_to`-1 (en la misma transacción)."""
        _page, rows = result
        if advance_to is not None:
            self._committed = advance_to
        self.store.save(self.listing.name,
                        cursor=parallel_cursor(self._committed,
                                               self._last_url), **rows)
        self.tracker.add(rows)

    def _commit(self, first: int) -> None:
        self._committed = first
        self.store.save(self.listing.name,
                        cursor=parallel_cursor(first, self._last_url))

    def _finish(self) -> None:
        self.store.save(self.listing.name, cursor="", done=True)
        self._mode = _DONE
        self.tracker.recount()

    # ---- en orden ----------------------------------------------------------
    def _on_sequential(self, result: object) -> None:
        page, rows = result
        # El cursor es ESTA página: al retomar se vuelve a pedir (lo
        # repetido se sustituye), y si algo se borró entretanto y las
        # páginas corrieron un puesto, no se pierde nada.
        self.store.save(self.listing.name, cursor=page.url, **rows)
        self.tracker.add(rows)
        self._left = _pages_left(page)
        actual = page_number(page.url)
        if self.workers > 1 and page.last_url and actual \
                and page.last_page - actual >= PARALLEL_MIN_PAGES:
            self.store.save(self.listing.name,
                            cursor=parallel_cursor(actual + 1,
                                                   page.last_url))
            self._start_numbered(actual + 1, page.last_url, overlap=False)
        elif page.next_url:
            self._target = page.next_url
        else:
            self._finish()

    # ---- por número de página ------------------------------------------------
    def _start_numbered(self, first: int, last_url: str,
                        overlap: bool) -> None:
        """Desde `first` hasta la última. Con `overlap` (al retomar) se
        empieza una página antes."""
        inicio = max(1, first - 1) if overlap else first
        self._last_url = last_url
        self._last_page = page_number(last_url)
        self._committed = inicio
        numeros = list(range(inicio, self._last_page + 1))
        if not numeros:
            self._start_tail()
        elif self.workers == 1:
            # Una sola conexión: en orden, que siempre es seguro.
            self._mode = _ONE_BY_ONE
            self._queue = deque(numeros)
        elif self.listing.newest_first:
            self._mode = _BATCHES
            self._remaining = deque(numeros)
            self._step = _PROBE_FIRST
            self._changes = 0
        else:
            self._mode = _PARALLEL
            self._queue = deque(numeros)
            self._arrived = set()

    def _on_parallel(self, number: int, result: object) -> None:
        self._arrived.add(number)
        siguiente = self._committed
        while siguiente in self._arrived:
            siguiente += 1
        self._save(result, advance_to=siguiente)
        if not self._queue and not self.running:
            self._start_tail()

    # ---- por tandas (más reciente primero) -----------------------------------
    def _next_in_batches(self) -> Optional[Task]:
        if self._step in (_PROBE_FIRST, _PROBE):
            return None if self.running else self._probe_task()
        if self._step == _PAGES:
            if not self._queue:
                return None
            return self._page_task(("número", self._queue.popleft()))
        if self._step == _REDO:
            if self.running or not self._queue:
                return None
            return self._page_task(("número", self._queue.popleft()))
        return None

    def _next_batch(self) -> None:
        """La siguiente tanda, o la cola si ya no quedan páginas."""
        if not self._remaining:
            self._start_tail()
            return
        cuantas = min(self.window, len(self._remaining))
        self._batch = [self._remaining.popleft() for _ in range(cuantas)]
        self._queue = deque(self._batch)
        self._step = _PAGES

    def _on_probe(self, head: object) -> None:
        if self._step == _PROBE_FIRST:
            self._head = head
            self._next_batch()
            return
        if head == self._head:
            # Nada corrió de sitio mientras se pedía la tanda: completa.
            self._commit(self._batch[-1] + 1)
            self._batch = []
            self._next_batch()
            return
        # Llegó algo nuevo delante: la tanda otra vez, en orden.
        self._changes += 1
        self._step = _REDO
        self._queue = deque(self._batch)

    def _on_batch_page(self, number: int, result: object) -> None:
        if self._step == _REDO:
            self._save(result, advance_to=number + 1)
            if self._queue or self.running:
                return
            self._batch = []
            if self._changes < MAX_HEAD_CHANGES:
                if self._remaining:
                    self._step = _PROBE_FIRST    # lo más reciente, de nuevo
                else:
                    self._start_tail()
            else:
                # Mucha actividad en el repositorio: el resto, en orden.
                self._mode = _ONE_BY_ONE
                self._queue = deque(self._remaining)
                self._remaining = deque()
                if not self._queue:
                    self._start_tail()
            return
        self._save(result)              # en la tanda: aún sin avanzar
        if not self._queue and not self.running:
            self._step = _PROBE

    # ---- la cola -----------------------------------------------------------
    def _start_tail(self) -> None:
        """La última página otra vez y lo que venga detrás: lo creado
        durante la descarga (o lo que las novedades empujaron más allá
        de la que era la última) también entra."""
        self._mode = _TAIL
        self._queue = deque()
        self._target = page_url(self._last_url, self._last_page)

    def _on_tail(self, result: object) -> None:
        page, rows = result
        self.store.save(self.listing.name,
                        cursor=parallel_cursor(self._committed,
                                               self._last_url), **rows)
        self.tracker.add(rows)
        if page.next_url:
            self._target = page.next_url
        else:
            self._finish()


# ------------------------------------------------------------------ #
#  Lo de cada pull request o issue                                    #
# ------------------------------------------------------------------ #
def _pending(store: Store, what: PerItem, numbers: Sequence[int]
             ) -> List[int]:
    hechos = store.done_stages(what.name + ":")
    return [n for n in numbers if f"{what.name}:{n}" not in hechos]


def _jobs(store: Store, options: FetchOptions) -> Dict[str, List[int]]:
    """Lo que falta de cada parte: {nombre de la parte: números}."""
    pulls = store.pull_numbers()
    trabajos = {REVIEWS.name: _pending(store, REVIEWS, pulls)}
    if options.commits:
        trabajos[COMMITS.name] = _pending(store, COMMITS, pulls)
    if options.mentions:
        trabajos[MENTIONS.name] = _pending(store, MENTIONS, store.numbers())
    return trabajos


def batch_items(store: Store, options: FetchOptions) -> List[BatchItem]:
    """Lo que falta, por issue/PR: cada uno con TODAS sus partes
    pendientes (una sola entrada en la consulta para todas)."""
    trabajos = _jobs(store, options)
    pulls = set(store.pull_numbers())
    partes: Dict[int, List[str]] = {}
    for parte in ghgraphql.PARTS:
        for number in trabajos.get(parte, ()):
            partes.setdefault(number, []).append(parte)
    return [BatchItem(number, number in pulls, tuple(partes[number]))
            for number in sorted(partes)]


class RestItemsSource(Source):
    """Lo de cada issue/PR, uno a uno por REST: todo, si no hay GraphQL,
    o lo que GraphQL no pudo traer (no cabía, o falló)."""

    label = "uno a uno"

    def __init__(self, store: Store, base: str, options: FetchOptions,
                 tracker: _Tracker, needs: Sequence[Source]) -> None:
        super().__init__()
        self.store = store
        self.base = base
        self.options = options
        self.tracker = tracker
        self.needs = list(needs)
        self.feeder: Optional[Source] = None    # GraphQL, si lo hay
        self._queue: Deque[Tuple[PerItem, int]] = deque()
        self._prepared = False

    def add(self, parts: Sequence[Tuple[str, int]]) -> None:
        """Partes que GraphQL no pudo traer: (nombre de la parte, número)."""
        self._queue.extend((PER_ITEM[nombre], numero)
                           for nombre, numero in parts)

    def ready(self) -> bool:
        return all(s.finished() for s in self.needs)

    def finished(self) -> bool:
        if self.feeder is not None and not self.feeder.finished():
            return False
        return (self._prepared or self.feeder is not None) \
            and not self._queue and not self.running

    def pending(self) -> int:
        return len(self._queue)

    def next_task(self) -> Optional[Task]:
        if not self._prepared:
            self._prepared = True
            if self.feeder is None:
                # Sin GraphQL: todo lo que falte, por aquí.
                for nombre, numeros in _jobs(self.store,
                                             self.options).items():
                    self.add([(nombre, n) for n in numeros])
        if not self._queue:
            return None
        what, numero = self._queue.popleft()
        return Task(self, lambda s: self._fetch(s, what, numero),
                    (what, numero))

    def _fetch(self, session: ApiSession, what: PerItem, numero: int
               ) -> List[Entry]:
        entradas: List[Entry] = []
        try:
            for page in session.pages(self.base
                                      + what.template.format(n=numero),
                                      {}, what=what.what):
                for raw in page.items:
                    if isinstance(raw, dict):
                        entry = what.convert(numero, raw)
                        if entry is not None:
                            entradas.append(entry)
        except ApiError as e:
            # Un pull request oculto o borrado: se sigue con los demás.
            if e.status not in MISSING_ITEM:
                raise
        return entradas

    def on_result(self, task: Task, result: object) -> None:
        what, numero = task.data
        entradas = list(result)
        self.store.save(f"{what.name}:{numero}", done=True, entries=entradas)
        self.tracker.add({"entries": entradas})


@dataclass(frozen=True)
class _Unavailable:
    """Lo que devuelve un lote cuando GraphQL no se puede usar: por qué."""
    reason: str


def graphql_notice(reason: str) -> str:
    """El aviso de que GraphQL no se pudo usar, con el porqué y lo que
    significa (todo lo que falta, uno a uno: mucho más lento)."""
    motivo = reason.strip() or "GitHub no lo aceptó"
    if not motivo.endswith((".", "!", "?", ")")):
        motivo += "."
    return ("GraphQL no se pudo usar: " + motivo + " Lo que falta de cada "
            "issue y pull request va uno a uno por REST: más lento y con "
            "la cuota de 5.000 consultas por hora.")


def _all_failed(lote: Sequence[BatchItem], outcome: BatchOutcome) -> bool:
    """¿Falló GraphQL con TODOS los del lote? (Una lista que no cabía no
    es un fallo: GraphQL respondió bien, solo que hay más.)"""
    resultados = [outcome.results.get(item.number) for item in lote]
    return bool(resultados) and all(r is None or r.failed
                                    for r in resultados)


class GraphQLSource(Source):
    """Lo de cada issue/PR por lotes de GraphQL: cada uno con todas sus
    partes pendientes en un mismo alias, y tantos por consulta como
    GitHub responda en unos segundos (`ghgraphql.BatchSizer`)."""

    label = "revisiones, commits y menciones (GraphQL)"
    graphql = True

    def __init__(self, store: Store, owner: str, repo: str,
                 options: FetchOptions, tracker: _Tracker,
                 rest: RestItemsSource, needs: Sequence[Source],
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self.store = store
        self.owner = owner
        self.repo = repo
        self.options = options
        self.tracker = tracker
        self.rest = rest
        self.needs = list(needs)
        self.sizer = ghgraphql.BatchSizer()
        self._clock = clock
        self._queue: Optional[Deque[BatchItem]] = None
        self._unavailable = False
        self._failed_batches = 0       # seguidos, con todo fallido
        self._last_error = ""          # lo último que dijo GitHub

    def ready(self) -> bool:
        return all(s.finished() for s in self.needs)

    def finished(self) -> bool:
        return self._queue is not None and not self._queue \
            and not self.running

    def pending(self) -> int:
        if self._queue is None:
            return 1
        unidades = sum(item.units for item in self._queue)
        return -(-unidades // max(1, self.sizer.units))

    def next_task(self) -> Optional[Task]:
        if self._queue is None:
            self._queue = deque(batch_items(self.store, self.options))
        if self._unavailable or not self._queue:
            return None
        lote = self.sizer.take(self._queue)
        return Task(self, lambda s: self._fetch(s, lote), lote)

    def _fetch(self, session: ApiSession, lote: List[BatchItem]) -> object:
        inicio = self._clock()
        try:
            outcome = ghgraphql.fetch_items(session, self.owner, self.repo,
                                            lote)
        except ghgraphql.GraphQLUnavailable as error:
            return _Unavailable(str(error))
        return outcome, self._clock() - inicio

    def _give_up(self, lote: Sequence[BatchItem], reason: str) -> None:
        """GraphQL no sirve: este lote y lo que quedaba, por REST. La
        primera vez se deja el aviso (con el porqué) para quien mira."""
        if not self._unavailable:
            self.notice = graphql_notice(reason)
        self._unavailable = True
        quedan = list(lote) + list(self._queue or ())
        if self._queue is not None:
            self._queue.clear()
        self.rest.add([(parte, item.number) for item in quedan
                       for parte in item.parts])

    def on_result(self, task: Task, result: object) -> None:
        lote: List[BatchItem] = task.data
        if isinstance(result, _Unavailable):
            self._give_up(lote, result.reason)
            return
        outcome, segundos = result
        assert isinstance(outcome, BatchOutcome)
        self._save(lote, outcome)
        self.sizer.observe(sum(item.units for item in lote), segundos,
                           outcome.split)
        if outcome.error:
            self._last_error = outcome.error
        if _all_failed(lote, outcome):
            self._failed_batches += 1
        else:
            self._failed_batches = 0
        if self._failed_batches >= MAX_FAILED_BATCHES \
                and not self._unavailable:
            self._give_up([], self._breaker_reason())

    def _breaker_reason(self) -> str:
        """Por qué se dejó GraphQL tras varios lotes fallidos."""
        motivo = f"falló con todo en {MAX_FAILED_BATCHES} lotes seguidos"
        ultimo = self._last_error.strip().rstrip(".")
        return f"{motivo} (lo último: {ultimo})." if ultimo else motivo + "."

    def _save(self, lote: Sequence[BatchItem], outcome: BatchOutcome) -> None:
        """Lo que trajo un lote, en una transacción. Las partes que no
        cupieron (o fallaron) se quedan sin marcar y van por REST."""
        etapas: List[Tuple[str, str, bool]] = []
        entradas: List[Entry] = []
        a_rest: List[Tuple[str, int]] = []
        for item in lote:
            resultado = outcome.results.get(item.number)
            if resultado is None:
                a_rest.extend((parte, item.number) for parte in item.parts)
                continue
            entradas.extend(resultado.entries)
            for parte in item.parts:
                if parte in resultado.fallback:
                    a_rest.append((parte, item.number))
                else:
                    etapas.append((f"{parte}:{item.number}", "", True))
        self.store.save_many(etapas, entradas)
        self.tracker.add({"entries": entradas})
        self.rest.add(a_rest)


# ------------------------------------------------------------------ #
#  Todo junto                                                         #
# ------------------------------------------------------------------ #
def fetch_all(pool: SessionPool, owner: str, repo: str, store: Store,
              options: FetchOptions = FetchOptions(),
              on_progress: Optional[ProgressCallback] = None) -> str:
    """Descarga al almacén todo lo de `owner/repo` (o lo que falte, si
    se retoma). Las esperas y la cancelación las llevan las sesiones.

    Devuelve el aviso de lo que no fue como debía (por ejemplo, que
    GraphQL no se pudo usar), o vacío si todo fue bien."""
    base = "/repos/" + urllib.parse.quote(owner, safe="") + "/" \
        + urllib.parse.quote(repo, safe="")
    tracker = _Tracker(pool, store, on_progress)
    listados = {listing.name: ListingSource(listing, store, base, tracker,
                                            pool.workers)
                for listing in LISTINGS}
    # Lo de cada issue/PR necesita saber qué issues y PR hay.
    necesita = [listados["issues"], listados["pulls"]]
    rest = RestItemsSource(store, base, options, tracker, necesita)
    planificador = Scheduler(pool, on_tick=tracker.report)
    # Por prioridad: lo que abre camino a GraphQL, GraphQL, lo que va uno
    # a uno y, llenando el resto de las conexiones, los demás listados.
    planificador.add(listados["issues"])
    planificador.add(listados["pulls"])
    if options.graphql:
        graphql = GraphQLSource(store, owner, repo, options, tracker, rest,
                                necesita)
        rest.feeder = graphql
        planificador.add(graphql)
    planificador.add(rest)
    for nombre in ("comments", "code", "events"):
        planificador.add(listados[nombre])
    tracker.sources = planificador.sources
    tracker.report()
    try:
        planificador.run()
    finally:
        store.flush()
    tracker.recount()
    tracker.report(final=True)
    return tracker.notice()
