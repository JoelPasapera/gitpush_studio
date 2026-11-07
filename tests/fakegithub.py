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
  · fallos a la carta (`fault`): cuota agotada, límite secundario con
    Retry-After, 502, conexión cortada sin respuesta, redirecciones
    (dentro y fuera), un «siguiente» que apunta a otro servidor, una
    bomba gzip y una respuesta que no es JSON.

Anota cada consulta con sus cabeceras (`requests`) para comprobar, por
ejemplo, a quién se mandó el token.
"""

import gzip
import json
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple


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


class FakeGitHub:
    """El servidor. `with FakeGitHub() as hub:` lo arranca y lo para."""

    def __init__(self, page_size: int = 2, gzip_responses: bool = True
                 ) -> None:
        self.page_size = page_size
        self.gzip = gzip_responses
        self.repos: Dict[str, FakeRepo] = {}
        self.requests: List[Tuple[str, Dict[str, str]]] = []
        self.connections = 0
        self.faults: List[Fault] = []
        self.remaining = 5000
        self.reset_at = int(time.time()) + 3600
        self._lock = threading.Lock()
        hub = self

        class Handler(_Handler):
            server_hub = hub
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
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

    def do_GET(self) -> None:                 # noqa: N802
        hub = self.server_hub
        hub.requests.append((self.path, {k.lower(): v for k, v
                                         in self.headers.items()}))
        fault = hub.take_fault(self.path)
        if fault is not None:
            self._fault(fault)
            return
        status, data, headers = hub.respond(self.path)
        self._send(status, json.dumps(data).encode("utf-8"), headers)

    def _send(self, status: int, body: bytes, headers: Dict[str, str],
              remaining: Optional[int] = None, reset: Optional[int] = None,
              force_gzip: bool = False, plain: bool = False) -> None:
        hub = self.server_hub
        pide_gzip = "gzip" in self.headers.get("Accept-Encoding", "")
        comprimir = force_gzip or (hub.gzip and pide_gzip and not plain)
        if comprimir and not force_gzip:
            body = gzip.compress(body)
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
