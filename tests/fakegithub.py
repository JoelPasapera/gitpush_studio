# -*- coding: utf-8 -*-
"""Un GitHub falso para las pruebas del volcado de issues y PR.

Sirve la parte de la API que usa la app, por http en 127.0.0.1, con lo
que tiene GitHub de verdad y hay que saber manejar:

  · HTTP/1.1 con conexiones que se reutilizan (cuenta cuántas se abren);
  · respuestas comprimidas con gzip si se piden;
  · paginación por «Link»: unos listados por número de página (con
    «last»), el de eventos por cursor opaco (sin «last»), y páginas más
    pequeñas que las pedidas (`page_size`) para tener muchas con pocos
    datos;
  · cabeceras de cuota (X-RateLimit-*);
  · los eventos, como en GitHub, del más reciente al más antiguo
    (`events_paged`: por número de página, con «last»);
  · GraphQL (POST /graphql) con alias, como lo usa la app: los
    `pullRequest` con sus revisiones y commits y los
    `issueOrPullRequest` con sus menciones, con las diferencias de
    GitHub entre las dos API (los bots sin «[bot]», el autor de un
    commit de bot sin enlazar, MERGED en vez de closed), un tope de
    nodos (`graphql_cap`) para forzar lo que no cabe, y NOT_FOUND para
    lo que no existe. Como GitHub, solo da los campos pedidos:
    `pageInfo.hasNextPage` dice si quedaron nodos fuera, y el
    `totalCount` de una historia cuenta TODO lo que pasó en ella
    (comentarios, etiquetas…), no solo las menciones pedidas (lo que
    hizo ir uno a uno a la 3.6.0);
  · fallos a la carta (`fault`): cuota agotada, límite secundario con
    Retry-After, 502, conexión cortada sin respuesta, redirecciones
    (dentro y fuera), un «siguiente» o un «último» que apuntan a otro
    servidor, una bomba gzip, una respuesta que no es JSON y, en
    GraphQL, cuota agotada (RATE_LIMITED), un lote que falla y un token
    que no vale;
  · retrasos (`delay`) y avisos tras servir algo (`on_served`), para
    colar novedades a mitad de una descarga.

Anota cada consulta con sus cabeceras (`requests`) para comprobar, por
ejemplo, a quién se mandó el token, y cada consulta GraphQL
(`graphql_queries`).
"""

import gzip
import json
import re
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional, Tuple

# «p12: pullRequest(number: 12)», «n7: issueOrPullRequest(number: 7)».
_ALIAS = re.compile(r"(\w+): (pullRequest|issueOrPullRequest)"
                    r"\(number: (\d+)\)")


def token_like(prefix: str = "ghp_") -> str:
    """Algo con forma de token, armado al ejecutar (los archivos de las
    pruebas no llevan ninguno escrito)."""
    return prefix + "".join(chr(ord("a") + (i * 7) % 26) for i in range(36))


class Fault:
    def __init__(self, kind: str, match: str, times: int,
                 extra: Dict[str, object]) -> None:
        self.kind = kind
        self.match = match
        self.times = times
        self.extra = extra


class FakeRepo:
    def __init__(self, full_name: str, repo_id: int) -> None:
        owner, name = full_name.split("/")
        self.full_name = full_name
        self.meta = {
            "id": repo_id, "full_name": full_name, "name": name,
            "owner": {"login": owner, "type": "User"},
            "html_url": f"https://github.com/{full_name}",
            "description": "App de prueba", "private": False,
            "default_branch": "main", "stargazers_count": 7,
            "forks_count": 2, "archived": False,
            "created_at": "2024-01-02T15:00:00Z", "has_issues": True,
            "node_id": "R_kgDOxxx", "url": "https://api.github.com/x"}
        self.issues: List[dict] = []
        self.pulls: List[dict] = []
        self.comments: List[dict] = []
        self.code_comments: List[dict] = []
        self.events: List[dict] = []
        self.reviews: Dict[int, List[dict]] = {}
        self.commits: Dict[int, List[dict]] = {}
        self.timeline: Dict[int, List[dict]] = {}
        self.issues_disabled = False
        # Eventos por número de página (con «last»), del más reciente al
        # más antiguo; si no, por cursor y en el orden de la lista.
        self.events_paged = False

    # ---- lo mismo, como lo da GraphQL ----------------------------------
    def pull_numbers(self) -> set:
        numeros = {p["number"] for p in self.pulls}
        numeros |= {i["number"] for i in self.issues
                    if isinstance(i.get("pull_request"), dict)}
        return numeros | set(self.reviews) | set(self.commits)

    def item_numbers(self) -> set:
        return {i["number"] for i in self.issues} | self.pull_numbers()

    def merged(self, number: int) -> bool:
        return any(p["number"] == number and p.get("merged_at")
                   for p in self.pulls)

    def timeline_size(self, number: int) -> int:
        """Lo que GitHub cuenta en `totalCount` de la historia de un issue:
        TODO (eventos y comentarios), aunque se pidan solo las menciones
        (`itemTypes` no cambia ese número; sí `pageInfo` y los nodos)."""
        sufijo = f"/issues/{number}"
        comentarios = sum(1 for c in self.comments
                          if str(c.get("issue_url") or "").endswith(sufijo))
        return len(self.timeline.get(number, [])) + comentarios


class FakeGitHub:
    """El servidor. `with FakeGitHub() as hub:` lo arranca y lo para."""

    def __init__(self, page_size: int = 2, gzip_responses: bool = True
                 ) -> None:
        self.page_size = page_size
        self.gzip = gzip_responses
        self.repos: Dict[str, FakeRepo] = {}
        self.requests: List[Tuple[str, Dict[str, str]]] = []
        self.graphql_queries: List[str] = []
        # Nodos que da como mucho cada lista anidada de GraphQL (GitHub:
        # los que se pidan, hasta 100); menos, para forzar lo que no cabe.
        self.graphql_cap = 100
        self.connections = 0
        self.faults: List[Fault] = []
        self.delays: List[Tuple[str, float]] = []
        # Se llama con la consulta servida, justo después de preparar su
        # respuesta (para colar novedades «mientras tanto»).
        self.on_served: Optional[Callable[[str], None]] = None
        # Consultas atendiéndose a la vez ahora, y el máximo visto.
        self.active = 0
        self.max_active = 0
        # Si se pasa de tantas a la vez, «ve más despacio» (429), como el
        # límite secundario de GitHub. None: sin límite.
        self.max_concurrent: Optional[int] = None
        self.slow_downs = 0
        # Bytes de los cuerpos: los que manda (respuestas) y los que
        # recibe (consultas GraphQL).
        self.bytes_sent = 0
        self.bytes_received = 0
        self.remaining = 5000
        self.reset_at = int(time.time()) + 3600
        self._lock = threading.Lock()
        hub = self

        class Handler(_Handler):
            server_hub = hub
        self._server = _QuietServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self.root = f"http://127.0.0.1:{self.port}"
        # Sondeo corto: `shutdown` espera a la vuelta siguiente del bucle.
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.02),
            daemon=True)
        self._thread.start()

    def __enter__(self) -> "FakeGitHub":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # ---- a la carta ---------------------------------------------------
    def add_repo(self, full_name: str, repo_id: int = 4242) -> FakeRepo:
        repo = FakeRepo(full_name, repo_id)
        self.repos[full_name.lower()] = repo
        return repo

    def fault(self, kind: str, match: str = "", times: int = 1,
              **extra: object) -> None:
        with self._lock:
            self.faults.append(Fault(kind, match, times, dict(extra)))

    def take_fault(self, path: str) -> Optional[Fault]:
        with self._lock:
            for fault in self.faults:
                if fault.times > 0 and fault.match in path:
                    fault.times -= 1
                    return fault
        return None

    def delay(self, match: str, seconds: float) -> None:
        """Lo que contenga `match` tarda `seconds` en empezar a servirse."""
        with self._lock:
            self.delays.append((match, seconds))

    def wait_for(self, what: str) -> None:
        with self._lock:
            pausas = [s for m, s in self.delays if m in what]
        for seconds in pausas:
            time.sleep(seconds)

    def served(self, what: str) -> None:
        if self.on_served is not None:
            self.on_served(what)

    def paths(self) -> List[str]:
        return [p for p, _h in self.requests]

    # ---- la API ---------------------------------------------------------
    def respond(self, path: str) -> Tuple[int, object, Dict[str, str]]:
        parts = urllib.parse.urlsplit(path)
        query = dict(urllib.parse.parse_qsl(parts.query))
        segs = [s for s in parts.path.split("/") if s]
        if len(segs) < 3 or segs[0] != "repos":
            return 404, {"message": "Not Found"}, {}
        repo = self.repos.get(f"{segs[1]}/{segs[2]}".lower())
        if repo is None:
            return 404, {"message": "Not Found"}, {}
        rest = segs[3:]
        if not rest:
            return 200, repo.meta, {}
        listados = {
            ("issues",): repo.issues, ("pulls",): repo.pulls,
            ("issues", "comments"): repo.comments,
            ("pulls", "comments"): repo.code_comments,
        }
        if tuple(rest) == ("issues",) and repo.issues_disabled:
            return 410, {"message": "Issues are disabled for this repo"}, {}
        if tuple(rest) in listados:
            return self._page(parts.path, query, listados[tuple(rest)])
        if tuple(rest) == ("issues", "events"):
            if repo.events_paged:
                recientes = sorted(repo.events, key=lambda e: e["id"],
                                   reverse=True)
                return self._page(parts.path, query, recientes)
            return self._cursor_page(parts.path, query, repo.events)
        if len(rest) == 3 and rest[1].isdigit():
            number = int(rest[1])
            tablas = {("pulls", "reviews"): repo.reviews,
                      ("pulls", "commits"): repo.commits,
                      ("issues", "timeline"): repo.timeline}
            tabla = tablas.get((rest[0], rest[2]))
            if tabla is not None:
                if number not in tabla and rest[0] == "pulls":
                    return 404, {"message": "Not Found"}, {}
                return self._page(parts.path, query, tabla.get(number, []))
        return 404, {"message": "Not Found"}, {}

    def _per_page(self, query: Dict[str, str]) -> int:
        pedido = int(query.get("per_page", "30") or 30)
        return max(1, min(pedido, self.page_size))

    def _page(self, path: str, query: Dict[str, str], items: List[dict]
              ) -> Tuple[int, object, Dict[str, str]]:
        per = self._per_page(query)
        page = max(1, int(query.get("page", "1") or 1))
        total_pages = max(1, -(-len(items) // per))
        chunk = items[(page - 1) * per:page * per]
        links = []
        if page < total_pages:
            links.append(f'<{self._url(path, query, page=page + 1)}>; '
                         'rel="next"')
            links.append(f'<{self._url(path, query, page=total_pages)}>; '
                         'rel="last"')
        headers = {"Link": ", ".join(links)} if links else {}
        return 200, chunk, headers

    def _cursor_page(self, path: str, query: Dict[str, str],
                     items: List[dict]) -> Tuple[int, object, Dict[str, str]]:
        per = self._per_page(query)
        start = int(query.get("after", "0") or 0)
        chunk = items[start:start + per]
        headers: Dict[str, str] = {}
        if start + per < len(items):
            siguiente = dict(query)
            siguiente.pop("page", None)
            siguiente["after"] = str(start + per)
            headers["Link"] = (f"<{self.root}{path}?"
                               f"{urllib.parse.urlencode(siguiente)}>; "
                               'rel="next"')
        return 200, chunk, headers

    def _url(self, path: str, query: Dict[str, str], page: int) -> str:
        nuevo = dict(query)
        nuevo["page"] = str(page)
        return f"{self.root}{path}?{urllib.parse.urlencode(nuevo)}"

    # ---- GraphQL ----------------------------------------------------------
    def graphql(self, query: str, variables: object
                ) -> Tuple[int, object]:
        """La respuesta a una consulta con alias de la app."""
        variables = variables if isinstance(variables, dict) else {}
        nombre = f"{variables.get('owner')}/{variables.get('name')}"
        repo = self.repos.get(nombre.lower())
        if repo is None:
            return 200, {"data": {"repository": None}, "errors": [{
                "type": "NOT_FOUND", "path": ["repository"],
                "message": "Could not resolve to a Repository with the "
                           f"name '{nombre}'."}]}
        operacion, fragmentos = _split_fragments(query)
        invalida = _invalid_fragments(operacion, fragmentos) \
            or _conflicting_fields(query)
        if invalida:
            # Como GitHub: una consulta inválida ni se ejecuta (sin «data»).
            return 200, {"errors": [{"message": invalida}]}
        alias = list(_ALIAS.finditer(operacion))
        resultado: Dict[str, object] = {}
        errores: List[dict] = []
        for k, match in enumerate(alias):
            fin = alias[k + 1].start() if k + 1 < len(alias) \
                else len(operacion)
            cuerpo = _expand(operacion[match.end():fin], fragmentos)
            nombre_alias, campo, numero = match.group(1), match.group(2), \
                int(match.group(3))
            if campo == "pullRequest":
                nodo = _gql_pull(repo, numero, cuerpo, self.graphql_cap)
            else:
                nodo = _gql_item(repo, numero, cuerpo, self.graphql_cap)
            resultado[nombre_alias] = nodo
            if nodo is None:
                errores.append({
                    "type": "NOT_FOUND",
                    "path": ["repository", nombre_alias],
                    "message": f"Could not resolve to a node with the "
                               f"number of {numero}."})
        data: Dict[str, object] = {"data": {"repository": resultado}}
        if errores:
            data["errors"] = errores
        return 200, data


# ------------------------------------------------------------------ #
#  Lo de REST, como lo da GraphQL                                     #
# ------------------------------------------------------------------ #
_FRAGMENT = re.compile(r"fragment (\w+) on (\w+) \{")
_SPREAD = re.compile(r"\.\.\.(\w+)")


def _block(text: str, opening: int) -> Tuple[str, int]:
    """(lo de dentro, posición tras el cierre) de las llaves que abren en
    `opening`."""
    nivel = 0
    for posicion in range(opening, len(text)):
        if text[posicion] == "{":
            nivel += 1
        elif text[posicion] == "}":
            nivel -= 1
            if nivel == 0:
                return text[opening + 1:posicion], posicion + 1
    return text[opening + 1:], len(text)


def _split_fragments(query: str) -> Tuple[str, Dict[str, str]]:
    """(la operación, {fragmento: sus campos}) de una consulta."""
    fragmentos: Dict[str, str] = {}
    primera: Optional[int] = None
    for match in _FRAGMENT.finditer(query):
        cuerpo, _fin = _block(query, match.end() - 1)
        fragmentos[match.group(1)] = cuerpo
        if primera is None:
            primera = match.start()
    return (query if primera is None else query[:primera]), fragmentos


def _invalid_fragments(operacion: str, fragmentos: Dict[str, str]) -> str:
    """Como GitHub: un fragmento usado sin definir, o definido sin usar,
    hace inválida la consulta. Devuelve el error, o ""."""
    usados = set(_SPREAD.findall(operacion))
    for nombre in sorted(usados - set(fragmentos)):
        return f"Fragment {nombre} was used, but not defined"
    for nombre in sorted(set(fragmentos) - usados):
        return f"Fragment {nombre} was defined, but not used"
    return ""


def _expand(cuerpo: str, fragmentos: Dict[str, str]) -> str:
    """Los campos de un alias con los de los fragmentos que nombra."""
    partes = [cuerpo]
    for nombre in _SPREAD.findall(cuerpo):
        partes.append(fragmentos.get(nombre, ""))
    return " ".join(partes)


def _conflicting_fields(query: str) -> str:
    """Como GitHub, rechaza `state` sin alias en los fragmentos de Issue
    y de PullRequest de una misma selección (son de tipos distintos:
    IssueState y PullRequestState). Devuelve el error, o ""."""
    for match in re.finditer(r"source \{", query):
        dentro, _fin = _block(query, match.end() - 1)
        sin_alias = re.findall(r"(?<!: )\bstate\b", dentro)
        if len(sin_alias) >= 2:
            return ("Fields 'state' conflict because they return "
                    "conflicting types 'IssueState' and 'PullRequestState'. "
                    "Use different aliases on the fields to fetch both if "
                    "this was intentional.")
    return ""


def _first(cuerpo: str, campo: str) -> Optional[int]:
    """El `first` pedido para una lista anidada, o None si no se pidió."""
    match = re.search(campo + r"\(first: (\d+)", cuerpo)
    return int(match.group(1)) if match else None


def _top_level(text: str) -> str:
    """Una selección sin lo que va entre llaves dentro de ella (para
    saber qué campos se piden en ESE nivel)."""
    fuera: List[str] = []
    nivel = 0
    for letra in text:
        if letra == "{":
            nivel += 1
        elif letra == "}":
            nivel -= 1
        elif nivel == 0:
            fuera.append(letra)
    return "".join(fuera)


def _selection(cuerpo: str, campo: str) -> str:
    """Lo que se pide DENTRO de una lista anidada («reviews(…) { … }»)."""
    match = re.search(campo + r"\([^)]*\)\s*\{", cuerpo)
    if not match:
        return ""
    dentro, _fin = _block(cuerpo, match.end() - 1)
    return dentro


def _gql_actor(user: object) -> Optional[dict]:
    """Un usuario de REST como «Actor» de GraphQL: los bots se llaman
    sin «[bot]»."""
    if not isinstance(user, dict) or not user.get("login"):
        return None
    login = user["login"]
    tipo = user.get("type") or "User"
    if tipo == "Bot" and login.endswith("[bot]"):
        login = login[:-len("[bot]")]
    return {"login": login, "__typename": tipo}


def _gql_review(review: dict) -> dict:
    commit = review.get("commit_id")
    return {"databaseId": review.get("id"), "state": review.get("state"),
            "body": review.get("body") or "",
            "submittedAt": review.get("submitted_at"),
            "url": review.get("html_url"),
            "authorAssociation": review.get("author_association"),
            "author": _gql_actor(review.get("user")),
            "commit": {"oid": commit} if commit else None}


def _gql_commit(raw: dict) -> dict:
    """Un commit: GraphQL solo enlaza la cuenta de una PERSONA."""
    commit = raw.get("commit") or {}
    autor = commit.get("author") or {}
    quien = raw.get("author")
    usuario = {"login": quien["login"]} if isinstance(quien, dict) \
        and quien.get("type", "User") == "User" else None
    return {"commit": {
        "oid": raw.get("sha"), "message": commit.get("message"),
        "committedDate": (commit.get("committer") or {}).get("date"),
        "authoredDate": autor.get("date"),
        "author": {"name": autor.get("name"), "email": autor.get("email"),
                   "user": usuario}}}


def _gql_connection(items: List[dict], first: int, cap: int,
                    selection: str, total: Optional[int] = None) -> dict:
    """Una lista anidada como la da GitHub: solo los campos pedidos.
    `pageInfo.hasNextPage` dice si quedaron nodos fuera; `totalCount`,
    lo que diga `total` (en la historia, TODO: ver `timeline_size`)."""
    nodos = items[:min(first, cap)]
    conexion: Dict[str, object] = {"nodes": nodos}
    nivel = _top_level(selection)
    if re.search(r"\btotalCount\b", nivel):
        conexion["totalCount"] = len(items) if total is None else total
    pagina = re.search(r"\bpageInfo\s*\{([^}]*)\}", selection)
    if pagina and re.search(r"\bhasNextPage\b", pagina.group(1)):
        conexion["pageInfo"] = {"hasNextPage": len(items) > len(nodos)}
    return conexion


def _gql_pull(repo: FakeRepo, number: int, cuerpo: str, cap: int
              ) -> Optional[dict]:
    if number not in repo.pull_numbers():
        return None
    nodo: Dict[str, object] = {}
    revisiones = _first(cuerpo, "reviews")
    if revisiones is not None:
        nodo["reviews"] = _gql_connection(
            [_gql_review(r) for r in repo.reviews.get(number, [])],
            revisiones, cap, _selection(cuerpo, "reviews"))
    commits = _first(cuerpo, "commits")
    if commits is not None:
        nodo["commits"] = _gql_connection(
            [_gql_commit(c) for c in repo.commits.get(number, [])],
            commits, cap, _selection(cuerpo, "commits"))
    _add_timeline(repo, number, cuerpo, cap, nodo)
    return nodo


def _add_timeline(repo: "FakeRepo", number: int, cuerpo: str, cap: int,
                  nodo: Dict[str, object]) -> None:
    """Sus menciones desde otros issues y PR, si se piden. Como GitHub,
    `totalCount` cuenta TODA la historia (no solo las menciones)."""
    first = _first(cuerpo, "timelineItems")
    if first is not None:
        cruzadas = [_gql_mention(repo, e)
                    for e in repo.timeline.get(number, [])
                    if e.get("event") == "cross-referenced"]
        nodo["timelineItems"] = _gql_connection(
            cruzadas, first, cap, _selection(cuerpo, "timelineItems"),
            total=repo.timeline_size(number))


def _gql_mention(repo: FakeRepo, event: dict) -> dict:
    issue = (event.get("source") or {}).get("issue") or {}
    es_pr = isinstance(issue.get("pull_request"), dict)
    origen = (issue.get("repository") or {}).get("full_name")
    estado = str(issue.get("state") or "").upper()
    if es_pr and origen == repo.full_name and repo.merged(issue["number"]):
        estado = "MERGED"
    return {"createdAt": event.get("created_at"),
            "actor": _gql_actor(event.get("actor")),
            "source": {"__typename": "PullRequest" if es_pr else "Issue",
                       "number": issue.get("number"),
                       "title": issue.get("title"),
                       ("prState" if es_pr else "issueState"): estado,
                       "repository": {"nameWithOwner": origen}}}


def _gql_item(repo: FakeRepo, number: int, cuerpo: str, cap: int
              ) -> Optional[dict]:
    if number not in repo.item_numbers():
        return None
    tipo = "PullRequest" if number in repo.pull_numbers() else "Issue"
    nodo: Dict[str, object] = {"__typename": tipo}
    _add_timeline(repo, number, cuerpo, cap, nodo)
    return nodo


class _QuietServer(ThreadingHTTPServer):
    """Sin trazas por las conexiones que el cliente corta a propósito
    (al detenerse, o ante una respuesta que no quiere leer entera)."""

    def handle_error(self, request, client_address) -> None:
        import sys
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_hub: FakeGitHub = None            # type: ignore[assignment]

    def setup(self) -> None:
        super().setup()
        # Sin el algoritmo de Nagle: cabeceras y cuerpo van en dos
        # escrituras, y con él cada respuesta esperaría ~40 ms al ACK.
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with self.server_hub._lock:
            self.server_hub.connections += 1

    def log_message(self, *_args) -> None:
        pass

    def _begin(self) -> None:
        """Cuenta cuántas consultas se atienden a la vez (desde que
        llegan hasta que se responden; no las conexiones en espera)."""
        hub = self.server_hub
        with hub._lock:
            hub.active += 1
            hub.max_active = max(hub.max_active, hub.active)

    def _end(self) -> None:
        hub = self.server_hub
        with hub._lock:
            hub.active -= 1

    def do_GET(self) -> None:                 # noqa: N802
        self._begin()
        try:
            self._get()
        finally:
            self._end()

    def do_POST(self) -> None:                # noqa: N802
        self._begin()
        try:
            self._post()
        finally:
            self._end()

    def _get(self) -> None:
        hub = self.server_hub
        hub.requests.append((self.path, {k.lower(): v for k, v
                                         in self.headers.items()}))
        hub.wait_for(self.path)
        if self._too_many():
            return
        fault = hub.take_fault(self.path)
        if fault is not None:
            self._fault(fault)
            return
        status, data, headers = hub.respond(self.path)
        hub.served(self.path)
        self._send(status, json.dumps(data).encode("utf-8"), headers)

    def _too_many(self) -> bool:
        """Como el límite secundario de GitHub: con más de
        `max_concurrent` consultas a la vez, «ve más despacio»."""
        hub = self.server_hub
        if hub.max_concurrent is None:
            return False
        with hub._lock:
            demasiadas = hub.active > hub.max_concurrent
            if demasiadas:
                hub.slow_downs += 1
        if demasiadas:
            cuerpo = json.dumps({"message": "You have exceeded a secondary "
                                            "rate limit."}).encode()
            self._send(429, cuerpo, {"Retry-After": "1"})
        return demasiadas

    def _post(self) -> None:
        """GraphQL: solo con token, como en GitHub."""
        hub = self.server_hub
        cabeceras = {k.lower(): v for k, v in self.headers.items()}
        hub.requests.append((self.path, cabeceras))
        largo = int(self.headers.get("Content-Length") or 0)
        crudo = self.rfile.read(largo) if largo else b""
        with hub._lock:
            hub.bytes_received += len(crudo)
        try:
            pedido = json.loads(crudo.decode("utf-8"))
        except ValueError:
            pedido = {}
        pedido = pedido if isinstance(pedido, dict) else {}
        query = str(pedido.get("query") or "")
        with hub._lock:
            hub.graphql_queries.append(query)
        donde = f"{self.path} {query}"
        hub.wait_for(donde)
        if self._too_many():
            return
        fault = hub.take_fault(donde)
        if fault is not None:
            self._fault(fault)
            return
        graphql = {"X-RateLimit-Resource": "graphql"}
        if urllib.parse.urlsplit(self.path).path != "/graphql":
            self._send(404, json.dumps({"message": "Not Found"}).encode(), {})
            return
        if "authorization" not in cabeceras:
            self._send(401, json.dumps({
                "message": "This endpoint requires you to be "
                           "authenticated."}).encode(), graphql)
            return
        status, data = hub.graphql(query, pedido.get("variables"))
        hub.served(donde)
        self._send(status, json.dumps(data).encode("utf-8"), graphql)

    def _send(self, status: int, body: bytes, headers: Dict[str, str],
              remaining: Optional[int] = None, reset: Optional[int] = None,
              force_gzip: bool = False, plain: bool = False) -> None:
        hub = self.server_hub
        pide_gzip = "gzip" in self.headers.get("Accept-Encoding", "")
        comprimir = force_gzip or (hub.gzip and pide_gzip and not plain)
        if comprimir and not force_gzip:
            body = gzip.compress(body)
        with hub._lock:
            hub.bytes_sent += len(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if comprimir:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("X-RateLimit-Limit", "5000")
        self.send_header("X-RateLimit-Remaining",
                         str(hub.remaining if remaining is None
                             else remaining))
        self.send_header("X-RateLimit-Reset",
                         str(hub.reset_at if reset is None else reset))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _fault(self, fault: Fault) -> None:
        kind = fault.kind
        mensaje = lambda texto: json.dumps({"message": texto}).encode()  # noqa: E731
        if kind == "rate":
            reset = int(time.time()) + int(fault.extra.get("seconds", 30))
            self._send(403, mensaje("API rate limit exceeded"), {},
                       remaining=0, reset=reset)
        elif kind == "secondary":
            self._send(429, mensaje("You have exceeded a secondary rate "
                                    "limit"),
                       {"Retry-After": str(fault.extra.get("after", 2))})
        elif kind == "error":
            self._send(502, mensaje("Bad gateway"), {})
        elif kind == "drop":
            self.close_connection = True       # sin responder nada
        elif kind == "redirect":
            destino = str(fault.extra.get("to") or self.path)
            self._send(301, mensaje("Moved Permanently"),
                       {"Location": destino})
        elif kind == "foreign_next":
            status, data, _headers = self.server_hub.respond(self.path)
            otro = "http://otro-servidor.invalid:1/repos/x/y/issues?page=2"
            self._send(status, json.dumps(data).encode(),
                       {"Link": f'<{otro}>; rel="next"'})
        elif kind == "foreign_last":
            status, data, headers = self.server_hub.respond(self.path)
            otro = "http://otro-servidor.invalid:1/repos/x/y/issues?page=9"
            enlaces = [p for p in headers.get("Link", "").split(", ")
                       if 'rel="last"' not in p and p]
            enlaces.append(f'<{otro}>; rel="last"')
            self._send(status, json.dumps(data).encode(),
                       {"Link": ", ".join(enlaces)})
        elif kind == "last":
            # Una respuesta normal, pero la última de la hora.
            status, data, headers = self.server_hub.respond(self.path)
            reset = int(time.time()) + int(fault.extra.get("seconds", 30))
            self._send(status, json.dumps(data).encode(), headers,
                       remaining=0, reset=reset)
        elif kind == "graphql_rate":
            reset = int(time.time()) + int(fault.extra.get("seconds", 30))
            cuerpo = json.dumps({"errors": [{
                "type": "RATE_LIMITED",
                "message": "API rate limit exceeded for user ID 1."}]})
            self._send(200, cuerpo.encode(),
                       {"X-RateLimit-Resource": "graphql"}, remaining=0,
                       reset=reset)
        elif kind == "graphql_error":
            cuerpo = json.dumps({"data": None, "errors": [{
                "message": "Something went wrong while executing your "
                           "query. This may be the result of a timeout."}]})
            self._send(200, cuerpo.encode(),
                       {"X-RateLimit-Resource": "graphql"})
        elif kind == "graphql_unauth":
            self._send(401, mensaje("Bad credentials"),
                       {"X-RateLimit-Resource": "graphql"})
        elif kind == "graphql_norepo":
            cuerpo = json.dumps({"data": {"repository": None}, "errors": [{
                "type": "NOT_FOUND", "path": ["repository"],
                "message": "Could not resolve to a Repository with the "
                           "name 'ana/app'."}]})
            self._send(200, cuerpo.encode(),
                       {"X-RateLimit-Resource": "graphql"})
        elif kind == "bomb":
            bomba = gzip.compress(b"\0" * (40 * 1024 * 1024))
            self._send(200, bomba, {}, force_gzip=True)
        elif kind == "huge":
            enorme = b"[" + b" " * (33 * 1024 * 1024) + b"]"
            self._send(200, enorme, {}, plain=True)
        elif kind == "notjson":
            self._send(200, b"<html>no soy JSON</html>", {})
        else:
            raise AssertionError(f"fallo desconocido: {kind}")


# ------------------------------------------------------------------ #
#  Un repositorio con un poco de todo                                 #
# ------------------------------------------------------------------ #
def _user(login: Optional[str], kind: str = "User") -> Optional[dict]:
    if login is None:
        return None
    return {"login": login, "id": abs(hash(login)) % 10000, "type": kind,
            "site_admin": False, "avatar_url": "https://x/y.png"}


def _reactions(**counts: int) -> dict:
    base = {k: 0 for k in ("+1", "-1", "laugh", "hooray", "confused",
                           "heart", "rocket", "eyes")}
    for name, value in counts.items():
        base[{"mas1": "+1", "menos1": "-1"}.get(name, name)] = value
    base["total_count"] = sum(base.values())
    base["url"] = "https://api.github.com/x/reactions"
    return base


API = "https://api.github.com/repos/ana/app"


def standard_repo(hub: FakeGitHub, fillers: int = 3,
                  secret: str = "") -> FakeRepo:
    """«ana/app»: dos issues y dos pull requests con todo lo que puede
    pasar en ellos, y `fillers` issues sin más para que haya páginas."""
    repo = hub.add_repo("ana/app")
    secret = secret or token_like()
    trampa = ("Pasa al compilar:\r\n```\r\n=====\r\nISSUE #99 · falso · "
              "título\r\n```\r\nColor \x1b[31mrojo\x1b[0m y \u202eal revés")
    repo.issues = [
        {"number": 1, "title": "Error al compilar\nen Windows",
         "state": "closed", "state_reason": "completed", "locked": False,
         "user": _user("luis"), "author_association": "CONTRIBUTOR",
         "created_at": "2025-01-03T15:22:05Z",
         "updated_at": "2025-01-05T10:00:00Z",
         "closed_at": "2025-01-04T23:01:44Z",
         "labels": [{"name": "bug", "color": "f00"},
                    {"name": "windows", "color": "00f"}],
         "assignees": [_user("ana")],
         "milestone": {"title": "v1.0", "number": 1}, "comments": 3,
         "body": trampa, "reactions": _reactions(mas1=2, heart=1),
         "html_url": "https://github.com/ana/app/issues/1"},
        {"number": 2, "title": "Añadir soporte ARM", "state": "closed",
         "locked": False, "user": _user("ana"), "author_association": "OWNER",
         "created_at": "2025-01-05T14:00:00Z",
         "updated_at": "2025-01-06T17:00:00Z",
         "closed_at": "2025-01-06T17:00:00Z", "labels": [],
         "assignees": [], "milestone": None, "comments": 1,
         "body": "Compila en ARM.\n\nCierra #1", "draft": False,
         "reactions": _reactions(rocket=1),
         "html_url": "https://github.com/ana/app/pull/2",
         "pull_request": {"merged_at": "2025-01-06T17:00:00Z",
                          "url": f"{API}/pulls/2"}},
        {"number": 3, "title": "Pregunta de un usuario borrado",
         "state": "open", "locked": True, "active_lock_reason": "too heated",
         "user": None, "author_association": "NONE",
         "created_at": "2025-02-01T12:00:00Z",
         "updated_at": "2025-02-01T12:00:00Z", "closed_at": None,
         "labels": [], "assignees": [], "milestone": None, "comments": 0,
         "body": None, "reactions": _reactions(),
         "html_url": "https://github.com/ana/app/issues/3"},
        {"number": 4, "title": "Arreglo desde una bifurcación",
         "state": "closed", "locked": False, "user": _user("luis"),
         "author_association": "FIRST_TIME_CONTRIBUTOR",
         "created_at": "2025-03-01T09:00:00Z",
         "updated_at": "2025-03-02T09:00:00Z",
         "closed_at": "2025-03-02T09:00:00Z", "labels": [], "assignees": [],
         "milestone": None, "comments": 1, "body": "Arreglo pequeño.",
         "draft": False, "reactions": _reactions(),
         "html_url": "https://github.com/ana/app/pull/4",
         "pull_request": {"merged_at": None, "url": f"{API}/pulls/4"}},
    ]
    for k in range(fillers):
        numero = 5 + k
        repo.issues.append({
            "number": numero, "title": f"Relleno {numero}", "state": "open",
            "locked": False, "user": _user("ana"),
            "author_association": "OWNER",
            "created_at": f"2025-04-{k + 1:02d}T10:00:00Z",
            "updated_at": f"2025-04-{k + 1:02d}T10:00:00Z",
            "closed_at": None, "labels": [], "assignees": [],
            "milestone": None, "comments": 0, "body": f"Texto {numero}",
            "reactions": _reactions(),
            "html_url": f"https://github.com/ana/app/issues/{numero}"})
    repo.pulls = [
        {"number": 2, "title": "Añadir soporte ARM", "state": "closed",
         "user": _user("ana"), "author_association": "OWNER",
         "created_at": "2025-01-05T14:00:00Z",
         "updated_at": "2025-01-06T17:00:00Z",
         "closed_at": "2025-01-06T17:00:00Z",
         "merged_at": "2025-01-06T17:00:00Z",
         "merge_commit_sha": "abc1234def5678", "draft": False,
         "body": "Compila en ARM.\n\nCierra #1", "labels": [],
         "assignees": [], "milestone": None, "locked": False,
         "head": {"label": "ana:feature/arm", "ref": "feature/arm",
                  "repo": {"full_name": "ana/app"}},
         "base": {"label": "ana:main", "ref": "main",
                  "repo": {"full_name": "ana/app"}},
         "requested_reviewers": [], "requested_teams": [],
         "html_url": "https://github.com/ana/app/pull/2"},
        {"number": 4, "title": "Arreglo desde una bifurcación",
         "state": "closed", "user": _user("luis"),
         "author_association": "FIRST_TIME_CONTRIBUTOR",
         "created_at": "2025-03-01T09:00:00Z",
         "updated_at": "2025-03-02T09:00:00Z",
         "closed_at": "2025-03-02T09:00:00Z", "merged_at": None,
         "merge_commit_sha": None, "draft": False, "body": "Arreglo pequeño.",
         "labels": [], "assignees": [], "milestone": None, "locked": False,
         "head": {"label": "luis:fix", "ref": "fix",
                  "repo": {"full_name": "luis/app"}},
         "base": {"label": "ana:main", "ref": "main",
                  "repo": {"full_name": "ana/app"}},
         "requested_reviewers": [_user("ana")],
         "requested_teams": [{"name": "core", "slug": "core"}],
         "html_url": "https://github.com/ana/app/pull/4"},
    ]
    repo.comments = [
        {"id": 101, "issue_url": f"{API}/issues/1", "user": _user("ana"),
         "author_association": "OWNER",
         "created_at": "2025-01-03T16:00:00Z",
         "updated_at": "2025-01-03T16:05:10Z",
         "body": "¿Qué versión usas?", "reactions": _reactions(mas1=1),
         "html_url": "https://github.com/ana/app/issues/1#c101"},
        {"id": 102, "issue_url": f"{API}/issues/1", "user": _user("luis"),
         "author_association": "CONTRIBUTOR",
         "created_at": "2025-01-03T16:30:00Z",
         "updated_at": "2025-01-03T16:30:00Z", "body": "La 1.2",
         "reactions": _reactions(),
         "html_url": "https://github.com/ana/app/issues/1#c102"},
        {"id": 103, "issue_url": f"{API}/issues/1", "user": None,
         "author_association": "NONE",
         "created_at": "2025-01-04T08:00:00Z",
         "updated_at": "2025-01-04T08:00:00Z", "body": "A mí también",
         "reactions": _reactions(),
         "html_url": "https://github.com/ana/app/issues/1#c103"},
        {"id": 104, "issue_url": f"{API}/issues/2",
         "user": _user("dependabot[bot]", "Bot"),
         "author_association": "NONE",
         "created_at": "2025-01-05T14:05:00Z",
         "updated_at": "2025-01-05T14:05:00Z",
         "body": "Dependencias al día.", "reactions": _reactions(),
         "html_url": "https://github.com/ana/app/pull/2#c104"},
        {"id": 105, "issue_url": f"{API}/issues/4", "user": _user("luis"),
         "author_association": "FIRST_TIME_CONTRIBUTOR",
         "created_at": "2025-03-01T09:30:00Z",
         "updated_at": "2025-03-01T09:30:00Z",
         "body": f"Uso este token para probar: {secret}",
         "reactions": _reactions(),
         "html_url": "https://github.com/ana/app/pull/4#c105"},
    ]
    hunk = ("@@ -40,6 +40,8 @@ static int x;\n int a = 1;\n+int b = 2;\n"
            "+int c = 3;")
    repo.code_comments = [
        {"id": 201, "pull_request_url": f"{API}/pulls/2",
         "pull_request_review_id": 301, "user": _user("luis"),
         "author_association": "CONTRIBUTOR", "path": "src/arm.c",
         "line": 42, "original_line": 42, "start_line": None, "side": "RIGHT",
         "subject_type": "line", "diff_hunk": hunk,
         "created_at": "2025-01-05T15:00:00Z",
         "updated_at": "2025-01-05T15:00:00Z",
         "body": "¿Y si b es negativo?", "reactions": _reactions(),
         "html_url": "https://github.com/ana/app/pull/2#r201"},
        {"id": 202, "pull_request_url": f"{API}/pulls/2",
         "pull_request_review_id": 302, "in_reply_to_id": 201,
         "user": _user("ana"), "author_association": "OWNER",
         "path": "src/arm.c", "line": 42, "original_line": 42,
         "side": "RIGHT", "subject_type": "line", "diff_hunk": hunk,
         "created_at": "2025-01-05T16:00:00Z",
         "updated_at": "2025-01-05T16:00:00Z",
         "body": "No puede: es un contador.", "reactions": _reactions(),
         "html_url": "https://github.com/ana/app/pull/2#r202"},
        {"id": 203, "pull_request_url": f"{API}/pulls/4",
         "pull_request_review_id": None, "user": _user("ana"),
         "author_association": "OWNER", "path": "config.py", "line": None,
         "original_line": 3, "side": "RIGHT", "subject_type": "line",
         "diff_hunk": f"@@ -1,3 +1,3 @@\n-API = 'x'\n+TOKEN = '{secret}'",
         "created_at": "2025-03-01T10:00:00Z",
         "updated_at": "2025-03-01T10:00:00Z",
         "body": "Ese token no puede ir aquí.", "reactions": _reactions(),
         "html_url": "https://github.com/ana/app/pull/4#r203"},
    ]
    repo.reviews = {
        2: [{"id": 301, "user": _user("luis"),
             "author_association": "CONTRIBUTOR",
             "state": "CHANGES_REQUESTED", "body": "Faltan pruebas.",
             "submitted_at": "2025-01-05T15:00:00Z",
             "commit_id": "1111111aaaa",
             "html_url": "https://github.com/ana/app/pull/2#pr301"},
            {"id": 302, "user": _user("ana"), "author_association": "OWNER",
             "state": "COMMENTED", "body": "",
             "submitted_at": "2025-01-05T16:00:00Z",
             "commit_id": "1111111aaaa", "html_url": "x"},
            {"id": 303, "user": _user("luis"),
             "author_association": "CONTRIBUTOR", "state": "APPROVED",
             "body": "Listo.", "submitted_at": "2025-01-06T12:00:00Z",
             "commit_id": "2222222bbbb", "html_url": "x"},
            {"id": 304, "user": _user("luis"), "state": "PENDING",
             "body": "borrador", "submitted_at": None}],
        4: [],
    }
    repo.commits = {
        2: [{"sha": "1111111aaaa", "author": _user("ana"),
             "commit": {"author": {"name": "Ana Pérez",
                                   "email": "ana@ejemplo.org",
                                   "date": "2025-01-05T13:00:00Z"},
                        "committer": {"name": "Ana Pérez",
                                      "date": "2025-01-05T13:00:00Z"},
                        "message": "Soporte ARM\n\nCon NEON."}},
            {"sha": "2222222bbbb", "author": None,
             "commit": {"author": {"name": "Robot de CI",
                                   "date": "2025-01-06T11:00:00Z"},
                        "committer": {"name": "Robot de CI",
                                      "date": "2025-01-06T11:00:00Z"},
                        "message": "Pruebas"}}],
        4: [{"sha": "3333333cccc", "author": _user("luis"),
             "commit": {"author": {"name": "Luis",
                                   "date": "2025-03-01T08:50:00Z"},
                        "committer": {"name": "Luis",
                                      "date": "2025-03-01T08:50:00Z"},
                        "message": "Arreglo"}}],
    }

    def evento(ident: int, numero: int, nombre: str, actor: Optional[str],
               cuando: str, **extra: object) -> dict:
        datos = {"id": ident, "event": nombre, "actor": _user(actor),
                 "created_at": cuando, "commit_id": None,
                 "issue": {"number": numero, "title": "x"}}
        datos.update(extra)
        return datos

    repo.events = [
        evento(401, 1, "labeled", "ana", "2025-01-03T16:01:00Z",
               label={"name": "bug", "color": "f00"}),
        evento(402, 1, "assigned", "ana", "2025-01-03T16:01:30Z",
               assignee=_user("ana")),
        evento(403, 1, "milestoned", "ana", "2025-01-03T16:02:00Z",
               milestone={"title": "v1.0"}),
        evento(404, 1, "renamed", "ana", "2025-01-03T16:03:00Z",
               rename={"from": "Error compilando", "to": "Error al compilar "
                       "en Windows"}),
        evento(405, 1, "subscribed", "luis", "2025-01-03T16:04:00Z"),
        evento(406, 1, "closed", "ana", "2025-01-04T23:01:44Z",
               state_reason="completed"),
        evento(407, 1, "referenced", "ana", "2025-01-06T16:59:00Z",
               commit_id="abc1234def5678"),
        evento(408, 2, "review_requested", "ana", "2025-01-05T14:01:00Z",
               requested_reviewer=_user("luis")),
        evento(409, 2, "merged", "ana", "2025-01-06T17:00:00Z",
               commit_id="abc1234def5678"),
        evento(410, 2, "closed", "ana", "2025-01-06T17:00:00Z"),
        evento(411, 2, "head_ref_deleted", "ana", "2025-01-06T17:00:05Z"),
        evento(412, 4, "closed", "ana", "2025-03-02T09:00:00Z"),
        evento(413, 3, "locked", "ana", "2025-02-01T12:30:00Z",
               lock_reason="too heated"),
        evento(414, 2, "mentioned", "luis", "2025-01-05T14:02:00Z"),
    ]
    repo.timeline = {
        1: [{"event": "cross-referenced", "actor": _user("ana"),
             "created_at": "2025-01-05T14:00:00Z",
             "source": {"type": "issue",
                        "issue": {"number": 2, "title": "Añadir soporte ARM",
                                  "state": "closed",
                                  "pull_request": {"url": "x"},
                                  "repository": {"full_name": "ana/app"}}}},
            {"event": "commented", "actor": _user("ana"),
             "created_at": "2025-01-03T16:00:00Z", "body": "duplicado"}],
    }
    return repo


# ------------------------------------------------------------------ #
#  Un proxy de empresa (solo túneles CONNECT)                         #
# ------------------------------------------------------------------ #
class ConnectProxy:
    """Un proxy mínimo que solo abre túneles CONNECT, como el de muchas
    empresas. Anota a dónde se pidió cada túnel y con qué cabeceras."""

    def __init__(self) -> None:
        import socket
        self._socket_module = socket
        self.seen: List[Tuple[str, Dict[str, str]]] = []
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.port = self._listener.getsockname()[1]
        self._running = True
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self._running = False
        try:
            self._listener.close()
        except OSError:
            pass

    def __enter__(self) -> "ConnectProxy":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _accept(self) -> None:
        while self._running:
            try:
                client, _addr = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,),
                             daemon=True).start()

    def _serve(self, client) -> None:
        socket = self._socket_module
        data = b""
        while b"\r\n\r\n" not in data:
            piece = client.recv(4096)
            if not piece:
                client.close()
                return
            data += piece
        head, _sep, rest = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        method, target, _version = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            name, _colon, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        self.seen.append((target, headers))
        if method != "CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n"
                           b"Content-Length: 0\r\n\r\n")
            client.close()
            return
        host, _colon, port = target.rpartition(":")
        upstream = socket.create_connection((host, int(port)))
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        if rest:
            upstream.sendall(rest)

        def pipe(src, dst) -> None:
            try:
                while True:
                    chunk = src.recv(65536)
                    if not chunk:
                        break
                    dst.sendall(chunk)
            except OSError:
                pass
            finally:
                for s in (src, dst):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        other = threading.Thread(target=pipe, args=(client, upstream),
                                 daemon=True)
        other.start()
        pipe(upstream, client)
        other.join(5)
        for s in (client, upstream):
            s.close()
