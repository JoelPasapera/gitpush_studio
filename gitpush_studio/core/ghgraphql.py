# -*- coding: utf-8 -*-
"""Lo que GitHub solo da issue a issue o pull request a pull request,
pedido de muchos en muchos con GraphQL.

Con la API de siempre (REST), las revisiones, los commits y las
menciones de cada pull request o issue cuestan una consulta CADA UNO: en
un repositorio con 8.000 issues y 2.000 pull requests son 12.000
consultas, y GitHub solo da 5.000 por hora. Con GraphQL una sola
consulta pide lo de muchos a la vez, nombrando a cada uno con un alias,
y TODO lo que le falta a cada uno en el mismo alias:

    query($owner: String!, $name: String!) {
      repository(owner: $owner, name: $name) {
        p12: pullRequest(number: 12) { ...Rv ...Cm ...Tp }
        n13: issueOrPullRequest(number: 13) { __typename ...Ti ...Tp }
      }
    }
    fragment Rv on PullRequest { reviews(first: 100) { … } }
    fragment Cm on PullRequest { commits(first: 100) { … } }
    fragment Tp on PullRequest { timelineItems(first: 100, …) { … } }
    fragment Ti on Issue { timelineItems(first: 100, …) { … } }

Los campos se escriben UNA vez, en los fragmentos, y cada alias solo los
nombra: una consulta de 50 issues pasa de ~38 KB de texto a ~3 KB (menos
que subir y menos que analizar para GitHub). GraphQL tiene además su
propia cuota (no gasta la de REST).

Cuántos van en cada consulta se ajusta solo (`BatchSizer`): se mide lo
que tarda GitHub por cada lista pedida y se busca que cada consulta
tarde unos pocos segundos (GitHub corta las que pasan de 10).

Lo que se recibe se convierte en EXACTAMENTE las mismas entradas que
daría REST (`ghrecords.review_entry`, `commit_entry`, `mention_entry`):
el TXT es el mismo por un camino o por el otro. Las diferencias de
nombres entre las dos API se deshacen aquí: GraphQL llama a los bots sin
«[bot]» y no enlaza el autor de un commit hecho por un bot (REST lo
enlaza por su correo).

Lo que no cabe en una consulta (más de 100 revisiones, commits o
menciones) se marca para pedirlo por REST, entero. Para saber si una
lista vino entera se mira `pageInfo.hasNextPage` (la señal de paginación
de siempre), NUNCA `totalCount`: en la historia de un issue, GitHub
cuenta en `totalCount` TODOS sus eventos aunque se pidan solo las
menciones, así que casi ninguna «cabría» y todo acabaría yendo, uno a
uno, por REST.

Si un lote falla (por ejemplo, GitHub corta una consulta que tarda
demasiado), se parte en dos y se reintenta; un único elemento que sigue
fallando, también va por REST. GraphQL exige token: sin él, todo va por
REST.
"""

import re
from dataclasses import dataclass, field
from typing import (Callable, Deque, Dict, List, Optional, Sequence, Set,
                    Tuple)

from .errors import StepError
from .ghrecords import Record, commit_entry, mention_entry, review_entry
from .ghsession import ApiSession
from .ghstore import Entry

GRAPHQL_PATH = "/graphql"
NESTED = 100                  # revisiones / commits / menciones por cada uno
# Un 502 de GraphQL suele ser una consulta que tardó demasiado: se
# reintenta una vez y, si no, el lote se parte (no seis veces).
SERVER_ATTEMPTS = 2

# Las partes que se piden de cada uno (son también el prefijo de lo hecho
# en el almacén: «reviews:12», «commits:12», «timeline:13»).
REVIEWS = "reviews"
COMMITS = "commits"
TIMELINE = "timeline"
PARTS = (REVIEWS, COMMITS, TIMELINE)

_AUTHOR = "author { login __typename }"
_REVIEW = (f"databaseId state body submittedAt url authorAssociation {_AUTHOR} "
           "commit { oid }")
_COMMIT = ("commit { oid message committedDate authoredDate "
           "author { name email user { login } } }")
# Tipos de cuenta que REST nombra igual («type»); el resto es «User».
_ACCOUNT_TYPES = ("Bot", "Organization", "Mannequin")
# Correo con el que firman sus commits las cuentas de bot de GitHub
# («49699333+dependabot[bot]@users.noreply.github.com»).
_BOT_EMAIL = re.compile(r"(?:\d+\+)?([A-Za-z0-9-]+\[bot\])"
                        r"@users\.noreply\.github\.com", re.IGNORECASE)
# `state` es IssueState en un issue y PullRequestState en un PR: GraphQL
# no deja pedir con el mismo nombre dos campos de tipos distintos, así
# que cada uno lleva su alias.
_SOURCE = ("... on Issue { number title issueState: state repository "
           "{ nameWithOwner } } "
           "... on PullRequest { number title prState: state repository "
           "{ nameWithOwner } }")
# ¿Vino entera la lista? (Ver el comentario del módulo: no `totalCount`.)
_PAGE = "pageInfo { hasNextPage }"
_TIMELINE = (f"timelineItems(first: {NESTED}, itemTypes: "
             f"[CROSS_REFERENCED_EVENT]) {{ {_PAGE} nodes {{ "
             "... on CrossReferencedEvent { createdAt actor { login "
             f"__typename }} source {{ __typename {_SOURCE} }} }} }}")
_HEAD = ("query($owner: String!, $name: String!) { "
         "repository(owner: $owner, name: $name) { ")


class GraphQLUnavailable(StepError):
    """GraphQL no se puede usar (sin token, o el token no lo permite, o
    GitHub no acepta la consulta): todo va por REST."""


class GraphQLBatchError(StepError):
    """Un lote concreto falló (se parte en dos y se reintenta)."""


@dataclass(frozen=True)
class BatchItem:
    """Un issue o pull request de un lote y lo que le falta (`parts`, de
    `PARTS`; las revisiones y los commits, solo si es un pull request)."""
    number: int
    is_pr: bool
    parts: Tuple[str, ...]

    @property
    def units(self) -> int:
        """Listas que se piden de él (lo que pesa en la consulta)."""
        return len(self.parts)

    @property
    def alias(self) -> str:
        return f"p{self.number}" if self.is_pr else f"n{self.number}"


@dataclass
class ItemResult:
    """Lo que se sacó de un pull request o issue: sus entradas, qué
    partes hay que pedir por REST (no cabían o fallaron) y si GraphQL no
    pudo con él (`failed`: un error, no una lista larga)."""
    entries: List[Entry] = field(default_factory=list)
    fallback: Set[str] = field(default_factory=set)
    failed: bool = False


# ------------------------------------------------------------------ #
#  Consultas                                                          #
# ------------------------------------------------------------------ #
def _fragments() -> Dict[str, Tuple[str, str]]:
    """Nombre → (tipo, campos) de cada fragmento. Se arma al usarlo (no
    al importar) para que los campos sean siempre los de este módulo."""
    return {
        "Rv": ("PullRequest", f"reviews(first: {NESTED}) {{ {_PAGE} "
                              f"nodes {{ {_REVIEW} }} }}"),
        "Cm": ("PullRequest", f"commits(first: {NESTED}) {{ {_PAGE} "
                              f"nodes {{ {_COMMIT} }} }}"),
        "Tp": ("PullRequest", _TIMELINE),
        "Ti": ("Issue", _TIMELINE),
    }


_PR_FRAGMENT = {REVIEWS: "Rv", COMMITS: "Cm", TIMELINE: "Tp"}


def build_query(items: Sequence[BatchItem]) -> str:
    """La consulta de un lote: un alias por elemento, que nombra los
    fragmentos de lo que le falta. Solo se escriben los fragmentos que se
    usan (GraphQL rechaza los que sobran)."""
    usados: Set[str] = set()
    alias: List[str] = []
    for item in items:
        if item.is_pr:
            nombres = [_PR_FRAGMENT[parte] for parte in PARTS
                       if parte in item.parts]
            usados.update(nombres)
            cuerpo = " ".join("..." + nombre for nombre in nombres)
            alias.append(f"{item.alias}: pullRequest(number: {item.number}) "
                         f"{{ {cuerpo} }}")
        else:
            # Solo sus menciones. `issueOrPullRequest` vale aunque al final
            # fuera un pull request.
            usados.update(("Ti", "Tp"))
            alias.append(f"{item.alias}: issueOrPullRequest(number: "
                         f"{item.number}) {{ __typename ...Ti ...Tp }}")
    fragmentos = _fragments()
    definiciones = [f"fragment {nombre} on {fragmentos[nombre][0]} "
                    f"{{ {fragmentos[nombre][1]} }}"
                    for nombre in sorted(usados)]
    return _HEAD + " ".join(alias) + " } } " + " ".join(definiciones)


def _error_path(error: object) -> List[object]:
    path = error.get("path") if isinstance(error, dict) else None
    return path if isinstance(path, list) else []


def _error_type(error: object) -> str:
    return str(error.get("type") or "") if isinstance(error, dict) else ""


def run_query(session: ApiSession, owner: str, repo: str, query: str
              ) -> Dict[str, object]:
    """Los alias de `repository` de una consulta. Un alias que no existe
    (un número que no es un pull request) llega como None.

    GraphQLUnavailable si GraphQL no sirve para este repositorio (sin
    permiso, no lo ve con este token, o no acepta la consulta: todo irá
    por REST); GraphQLBatchError si falló este lote (se partirá en dos).
    """
    response = session.post_json(GRAPHQL_PATH, {
        "query": query, "variables": {"owner": owner, "name": repo}},
        server_attempts=SERVER_ATTEMPTS)
    if response.status in (401, 403):
        raise GraphQLUnavailable("GitHub no deja usar GraphQL con este token "
                                 f"({response.status}).")
    if response.status in (404, 405, 501):
        raise GraphQLUnavailable("Este servidor no tiene GraphQL "
                                 f"({response.status}).")
    if not response.ok or not isinstance(response.data, dict):
        raise GraphQLBatchError(f"GraphQL respondió {response.status}.")
    errores = response.data.get("errors") or []
    if not isinstance(errores, list):
        errores = [errores]
    if "data" not in response.data:
        # Ni se ejecutó (así responde GraphQL a una consulta que no
        # acepta): partir el lote no arreglaría nada.
        primero = errores[0] if errores and isinstance(errores[0], dict) \
            else {}
        raise GraphQLUnavailable("GitHub no acepta la consulta GraphQL: "
                                 f"{primero.get('message') or '¿?'}")
    datos = response.data.get("data")
    repositorio = datos.get("repository") if isinstance(datos, dict) else None
    # El repositorio entero no se ve por GraphQL: partir lotes no sirve.
    if repositorio is None and any(
            _error_type(e) in ("NOT_FOUND", "FORBIDDEN")
            and len(_error_path(e)) <= 1 for e in errores):
        raise GraphQLUnavailable("GraphQL no ve este repositorio con este "
                                 "token.")
    graves = [e for e in errores
              if not (_error_type(e) == "NOT_FOUND"
                      and len(_error_path(e)) > 1)]
    if not isinstance(repositorio, dict) or graves:
        mensaje = ""
        if graves and isinstance(graves[0], dict):
            mensaje = str(graves[0].get("message") or "")
        raise GraphQLBatchError(f"GraphQL no pudo con el lote: {mensaje}")
    return repositorio


# ------------------------------------------------------------------ #
#  De GraphQL a las mismas entradas que REST                          #
# ------------------------------------------------------------------ #
def _person(node: object) -> Optional[Record]:
    """El autor como lo da REST: {login, type}. GraphQL nombra a los bots
    sin «[bot]» («dependabot»); REST, con él («dependabot[bot]»)."""
    if not isinstance(node, dict) or not isinstance(node.get("login"), str) \
            or not node["login"]:
        return None
    login = node["login"]
    tipo = node.get("__typename")
    tipo = tipo if tipo in _ACCOUNT_TYPES else "User"
    if tipo == "Bot" and not login.endswith("[bot]"):
        login += "[bot]"
    return {"login": login, "type": tipo}


def _commit_author(autor: Dict[str, object]) -> Optional[Record]:
    """La cuenta del autor de un commit, como la da REST. GraphQL solo
    enlaza cuentas de persona; la de un bot se reconoce por su correo
    («…+dependabot[bot]@users.noreply.github.com»), igual que hace REST."""
    usuario = autor.get("user") if isinstance(autor.get("user"), dict) \
        else None
    if usuario and isinstance(usuario.get("login"), str) and usuario["login"]:
        return {"login": usuario["login"], "type": "User"}
    correo = autor.get("email") if isinstance(autor.get("email"), str) \
        else ""
    bot = _BOT_EMAIL.fullmatch(correo.strip())
    if bot:
        return {"login": bot.group(1), "type": "Bot"}
    return None


def review_from(number: int, node: Dict[str, object]) -> Optional[Entry]:
    commit = node.get("commit") if isinstance(node.get("commit"), dict) \
        else {}
    raw = {"id": node.get("databaseId"),
           "user": _person(node.get("author")),
           "author_association": node.get("authorAssociation"),
           "state": node.get("state"), "body": node.get("body"),
           "submitted_at": node.get("submittedAt"),
           "commit_id": commit.get("oid"), "html_url": node.get("url")}
    return review_entry(number, raw)


def commit_from(number: int, node: Dict[str, object]) -> Optional[Entry]:
    commit = node.get("commit") if isinstance(node.get("commit"), dict) \
        else {}
    autor = commit.get("author") if isinstance(commit.get("author"), dict) \
        else {}
    raw = {"sha": commit.get("oid"),
           "author": _commit_author(autor),
           "commit": {"message": commit.get("message"),
                      "author": {"name": autor.get("name"),
                                 "date": commit.get("authoredDate")},
                      "committer": {"date": commit.get("committedDate")}}}
    return commit_entry(number, raw)


def mention_from(number: int, node: Dict[str, object]) -> Optional[Entry]:
    source = node.get("source") if isinstance(node.get("source"), dict) \
        else {}
    if source.get("__typename") not in ("Issue", "PullRequest"):
        return None
    repositorio = source.get("repository") \
        if isinstance(source.get("repository"), dict) else {}
    estado = str(source.get("issueState") or source.get("prState")
                 or "").lower()
    raw = {"event": "cross-referenced",
           "actor": _person(node.get("actor")),
           "created_at": node.get("createdAt"),
           "source": {"issue": {
               "number": source.get("number"), "title": source.get("title"),
               "state": "closed" if estado == "merged" else estado,
               "pull_request": {} if source.get("__typename") ==
               "PullRequest" else None,
               "repository": {"full_name": repositorio.get("nameWithOwner")}}}}
    return mention_entry(number, raw)


# Cada parte: (la lista en la respuesta, cómo se convierte cada nodo).
_CONVERTERS: Dict[str, Tuple[str, Callable[[int, Dict[str, object]],
                                           Optional[Entry]]]] = {
    REVIEWS: ("reviews", review_from),
    COMMITS: ("commits", commit_from),
    TIMELINE: ("timelineItems", mention_from),
}


def complete(connection: object) -> bool:
    """¿Vino entera una lista anidada? Solo si GitHub dice que no hay
    página siguiente. (Sin esa señal, se da por incompleta: mejor pedir
    algo de más por REST que perder algo.)"""
    pagina = connection.get("pageInfo") if isinstance(connection, dict) \
        else None
    return isinstance(pagina, dict) and pagina.get("hasNextPage") is False


def _connection(node: Dict[str, object], name: str, number: int,
                convert: Callable[[int, Dict[str, object]], Optional[Entry]],
                result: ItemResult, part: str) -> None:
    """Las entradas de una lista anidada; si no venía entera, esa parte
    se pide por REST."""
    conexion = node.get(name) if isinstance(node.get(name), dict) else {}
    nodos = [n for n in conexion.get("nodes") or [] if isinstance(n, dict)]
    if not complete(conexion):
        result.fallback.add(part)
        return
    for nodo in nodos:
        entry = convert(number, nodo)
        if entry is not None:
            result.entries.append(entry)


def items_results(data: Dict[str, object], items: Sequence[BatchItem]
                  ) -> Dict[int, ItemResult]:
    """Lo de cada elemento de un lote. Sin nodo (no existe, o no es lo
    que se pidió) queda vacío y hecho, como un 404 de REST."""
    out: Dict[int, ItemResult] = {}
    for item in items:
        result = ItemResult()
        node = data.get(item.alias)
        if isinstance(node, dict):
            for part in item.parts:
                nombre, convert = _CONVERTERS[part]
                _connection(node, nombre, item.number, convert, result, part)
        out[item.number] = result
    return out


# ------------------------------------------------------------------ #
#  Lotes que se parten si fallan                                      #
# ------------------------------------------------------------------ #
@dataclass
class BatchOutcome:
    """Lo que trajo un lote: lo de cada uno, si hubo que partirlo y, si
    algo falló, lo último que dijo GitHub (para explicar el porqué si
    GraphQL deja de funcionar del todo)."""
    results: Dict[int, ItemResult]
    split: bool
    error: str = ""


def fetch_items(session: ApiSession, owner: str, repo: str,
                items: Sequence[BatchItem]) -> BatchOutcome:
    """Un lote; si GitHub no puede con él, sus dos mitades por separado,
    y un elemento que falla solo va entero por REST."""
    try:
        data = run_query(session, owner, repo, build_query(items))
    except GraphQLBatchError as error:
        if len(items) == 1:
            unico = items[0]
            return BatchOutcome({unico.number: ItemResult(
                fallback=set(unico.parts), failed=True)}, split=True,
                error=str(error))
        mitad = len(items) // 2
        primera = fetch_items(session, owner, repo, items[:mitad])
        segunda = fetch_items(session, owner, repo, items[mitad:])
        primera.results.update(segunda.results)
        return BatchOutcome(primera.results, split=True,
                            error=segunda.error or primera.error)
    return BatchOutcome(items_results(data, items), split=False)


# ------------------------------------------------------------------ #
#  Cuánto en cada lote                                                #
# ------------------------------------------------------------------ #
class BatchSizer:
    """Cuántas listas pedir en cada consulta: las que GitHub responda en
    unos `target` segundos.

    Mide los segundos por lista de cada lote (media móvil, para no
    saltar por uno raro) y calcula el siguiente; nunca crece más del
    doble de una vez, y si un lote hubo que partirlo, el siguiente es la
    mitad. Como mucho `max_aliases` elementos por consulta (los nodos que
    puede pedir una consulta de GitHub tienen un tope).
    """

    def __init__(self, initial: int = 60, target: float = 3.0,
                 minimum: int = 8, maximum: int = 300,
                 max_aliases: int = 100) -> None:
        self.units = initial
        self.target = target
        self.minimum = minimum
        self.maximum = maximum
        self.max_aliases = max_aliases
        self._seconds_per_unit: Optional[float] = None

    def take(self, queue: Deque[BatchItem]) -> List[BatchItem]:
        """Saca de la cola el siguiente lote (al menos un elemento)."""
        lote: List[BatchItem] = []
        total = 0
        while queue and len(lote) < self.max_aliases:
            siguiente = queue[0]
            if lote and total + siguiente.units > self.units:
                break
            lote.append(queue.popleft())
            total += siguiente.units
        return lote

    def observe(self, units: int, seconds: float, split: bool) -> None:
        """Lo que tardó un lote de `units` listas."""
        if split:
            self.units = max(self.minimum, self.units // 2)
            return
        por_unidad = max(1e-4, seconds / max(1, units))
        if self._seconds_per_unit is None:
            self._seconds_per_unit = por_unidad
        else:
            self._seconds_per_unit = (0.7 * self._seconds_per_unit
                                      + 0.3 * por_unidad)
        ideal = int(self.target / self._seconds_per_unit)
        self.units = max(self.minimum, min(self.maximum, ideal,
                                           self.units * 2))
