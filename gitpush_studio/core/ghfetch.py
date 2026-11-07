# -*- coding: utf-8 -*-
"""Descargar todo lo que se habló en un repositorio de GitHub.

Lo caro de GitHub son las consultas (5.000 por hora con token, 60 sin
él), así que el orden importa:

  1. los LISTADOS DE TODO EL REPOSITORIO, de 100 en 100: issues y pull
     requests juntos, los pull requests (ramas, fusión, borrador), todos
     los comentarios, todos los comentarios en el código y todos los
     eventos. Con eso ya está casi todo, y cuesta una consulta por cada
     100 cosas en vez de una por cada issue;
  2. lo que GitHub solo da pull request a pull request: sus revisiones
     (siempre) y sus commits (opcional);
  3. las menciones desde otros issues y pull requests (opcional: una
     consulta más por cada issue y cada pull request).

Cada página se guarda en el almacén (`ghstore`) con el punto por el que
va su listado: si GitHub pide esperar una hora, se cae la conexión o se
pulsa «Detener», lo descargado no se pierde y la próxima vez se sigue
desde ahí. De cada cosa se guarda solo lo que el TXT usa.
"""

import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .ghsession import ApiError, ApiSession, Page, page_number
from .ghstore import Entry, Store

# Eventos que no son conversación sino avisos (quién se suscribió, a
# quién se mencionó: las menciones ya están en el texto).
SKIPPED_EVENTS = frozenset({"subscribed", "unsubscribed", "mentioned"})
REACTIONS = ("+1", "-1", "laugh", "hooray", "confused", "heart", "rocket",
             "eyes")

Record = Dict[str, object]
Rows = Dict[str, list]


@dataclass(frozen=True)
class FetchOptions:
    """Lo opcional de la descarga (cada cosa cuesta consultas)."""
    commits: bool = True      # los commits de cada PR: +1 consulta por PR
    mentions: bool = False    # menciones desde otros issues/PR: +1 por cada


@dataclass
class Progress:
    """Por dónde va la descarga, para enseñarlo."""
    stage: str
    requests: int
    estimate: int
    counts: Dict[str, int] = field(default_factory=dict)

    @property
    def fraction(self) -> float:
        return min(1.0, self.requests / max(1, self.estimate))


ProgressCallback = Callable[[Progress], None]


# ------------------------------------------------------------------ #
#  Lo justo de cada cosa                                              #
# ------------------------------------------------------------------ #
def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _int(value: object) -> int:
    try:
        return int(value)                      # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def user(raw: object) -> Optional[Record]:
    """{login, type} de un usuario; None si la cuenta ya no existe."""
    if not isinstance(raw, dict) or not _text(raw.get("login")):
        return None
    return {"login": raw["login"], "type": _text(raw.get("type")) or "User"}


def _logins(raws: object) -> List[str]:
    out: List[str] = []
    for raw in raws if isinstance(raws, list) else []:
        quien = user(raw)
        if quien is not None:
            out.append(str(quien["login"]))
    return out


def _labels(raws: object) -> List[str]:
    out: List[str] = []
    for raw in raws if isinstance(raws, list) else []:
        if isinstance(raw, dict) and _text(raw.get("name")):
            out.append(raw["name"])
        elif isinstance(raw, str):
            out.append(raw)
    return out


def reactions(raw: object) -> Dict[str, int]:
    """Solo las reacciones que hay (GitHub no dice quién las puso)."""
    if not isinstance(raw, dict):
        return {}
    return {name: _int(raw.get(name)) for name in REACTIONS
            if _int(raw.get(name)) > 0}


def number_in_url(url: object) -> int:
    """El número final de «…/issues/123» o «…/pulls/123»; 0 si no hay."""
    path = urllib.parse.urlsplit(_text(url)).path.rstrip("/")
    return _int(path.rsplit("/", 1)[-1]) if path else 0


def _milestone(raw: object) -> str:
    return _text(raw.get("title")) if isinstance(raw, dict) else ""


def issue_item(raw: Dict[str, object]) -> Tuple[int, Record]:
    """Un issue (o un PR) tal como llega del listado de issues."""
    pull = raw.get("pull_request")
    es_pr = isinstance(pull, dict)
    data: Record = {
        "number": _int(raw.get("number")),
        "kind": "pr" if es_pr else "issue",
        "title": _text(raw.get("title")),
        "state": _text(raw.get("state")),
        "state_reason": _text(raw.get("state_reason")),
        "locked": bool(raw.get("locked")),
        "lock_reason": _text(raw.get("active_lock_reason")),
        "user": user(raw.get("user")),
        "association": _text(raw.get("author_association")),
        "created_at": _text(raw.get("created_at")),
        "updated_at": _text(raw.get("updated_at")),
        "closed_at": _text(raw.get("closed_at")),
        "labels": _labels(raw.get("labels")),
        "assignees": _logins(raw.get("assignees")),
        "milestone": _milestone(raw.get("milestone")),
        "comments": _int(raw.get("comments")),
        "body": _text(raw.get("body")),
        "reactions": reactions(raw.get("reactions")),
        "html_url": _text(raw.get("html_url")),
        "draft": bool(raw.get("draft")),
        "merged_at": _text(pull.get("merged_at")) if es_pr else "",
    }
    return int(data["number"]), data


def pull_record(raw: Dict[str, object]) -> Tuple[int, Record]:
    """Lo propio de un pull request (del listado de pull requests)."""
    head = raw.get("head") if isinstance(raw.get("head"), dict) else {}
    base = raw.get("base") if isinstance(raw.get("base"), dict) else {}
    head_repo = head.get("repo") if isinstance(head.get("repo"), dict) \
        else {}
    teams = [_text(t.get("name")) for t in raw.get("requested_teams") or []
             if isinstance(t, dict)]
    data: Record = {
        "head": _text(head.get("label")) or _text(head.get("ref")),
        "head_repo": _text(head_repo.get("full_name")),
        "base": _text(base.get("ref")),
        "merged_at": _text(raw.get("merged_at")),
        "merge_commit": _text(raw.get("merge_commit_sha")),
        "draft": bool(raw.get("draft")),
        "requested_reviewers": _logins(raw.get("requested_reviewers")),
        "requested_teams": [t for t in teams if t],
    }
    return _int(raw.get("number")), data


def pull_as_item(raw: Dict[str, object]) -> Tuple[int, Record]:
    """Un pull request como issue, para cuando el listado de issues no
    lo trajo (los issues desactivados en el repositorio)."""
    number, data = issue_item(raw)
    data["kind"] = "pr"
    data["merged_at"] = _text(raw.get("merged_at"))
    data["comments"] = -1                     # el listado de PR no lo dice
    return number, data


def comment_entry(raw: Dict[str, object]) -> Optional[Entry]:
    number = number_in_url(raw.get("issue_url"))
    if not number:
        return None
    data: Record = {
        "user": user(raw.get("user")),
        "association": _text(raw.get("author_association")),
        "created_at": _text(raw.get("created_at")),
        "updated_at": _text(raw.get("updated_at")),
        "body": _text(raw.get("body")),
        "reactions": reactions(raw.get("reactions")),
        "html_url": _text(raw.get("html_url")),
    }
    return ("comment", str(raw.get("id")), number, str(data["created_at"]),
            data)


def code_comment_entry(raw: Dict[str, object]) -> Optional[Entry]:
    number = number_in_url(raw.get("pull_request_url"))
    if not number:
        return None
    data: Record = {
        "user": user(raw.get("user")),
        "association": _text(raw.get("author_association")),
        "created_at": _text(raw.get("created_at")),
        "updated_at": _text(raw.get("updated_at")),
        "body": _text(raw.get("body")),
        "reactions": reactions(raw.get("reactions")),
        "html_url": _text(raw.get("html_url")),
        "path": _text(raw.get("path")),
        "line": _int(raw.get("line")) or _int(raw.get("original_line")),
        "start_line": _int(raw.get("start_line"))
        or _int(raw.get("original_start_line")),
        "outdated": raw.get("line") is None
        and raw.get("original_line") is not None,
        "subject": _text(raw.get("subject_type")) or "line",
        "diff_hunk": _text(raw.get("diff_hunk")),
        "in_reply_to": _int(raw.get("in_reply_to_id")),
        "review_id": _int(raw.get("pull_request_review_id")),
        "id": _int(raw.get("id")),
    }
    return ("code", str(raw.get("id")), number, str(data["created_at"]),
            data)


def event_entry(raw: Dict[str, object]) -> Optional[Entry]:
    kind = _text(raw.get("event"))
    issue = raw.get("issue") if isinstance(raw.get("issue"), dict) else {}
    number = _int(issue.get("number"))
    if not kind or kind in SKIPPED_EVENTS or not number:
        return None
    data: Record = {"event": kind, "actor": user(raw.get("actor")),
                    "created_at": _text(raw.get("created_at"))}
    label = raw.get("label")
    if isinstance(label, dict):
        data["label"] = _text(label.get("name"))
    if raw.get("assignee") is not None:
        data["assignee"] = user(raw.get("assignee"))
    if raw.get("milestone") is not None:
        data["milestone"] = _milestone(raw.get("milestone"))
    rename = raw.get("rename")
    if isinstance(rename, dict):
        data["rename"] = {"from": _text(rename.get("from")),
                          "to": _text(rename.get("to"))}
    if _text(raw.get("commit_id")):
        data["commit"] = raw["commit_id"]
    if _text(raw.get("lock_reason")):
        data["lock_reason"] = raw["lock_reason"]
    if _text(raw.get("state_reason")):
        data["state_reason"] = raw["state_reason"]
    if raw.get("requested_reviewer") is not None:
        data["reviewer"] = user(raw.get("requested_reviewer"))
    team = raw.get("requested_team")
    if isinstance(team, dict):
        data["team"] = _text(team.get("name"))
    dismissed = raw.get("dismissed_review")
    if isinstance(dismissed, dict):
        data["dismissed"] = {"state": _text(dismissed.get("state")),
                             "message": _text(
                                 dismissed.get("dismissal_message"))}
    return ("event", str(raw.get("id")), number, str(data["created_at"]),
            data)


def review_entry(number: int, raw: Dict[str, object]) -> Optional[Entry]:
    when = _text(raw.get("submitted_at"))
    state = _text(raw.get("state"))
    if not when or state == "PENDING":           # borradores sin enviar
        return None
    data: Record = {
        "user": user(raw.get("user")),
        "association": _text(raw.get("author_association")),
        "state": state,
        "body": _text(raw.get("body")),
        "submitted_at": when,
        "commit": _text(raw.get("commit_id")),
        "html_url": _text(raw.get("html_url")),
        "id": _int(raw.get("id")),
    }
    return ("review", str(raw.get("id")), number, when, data)


def commit_entry(number: int, raw: Dict[str, object]) -> Optional[Entry]:
    sha = _text(raw.get("sha"))
    commit = raw.get("commit") if isinstance(raw.get("commit"), dict) \
        else {}
    author = commit.get("author") if isinstance(commit.get("author"),
                                                dict) else {}
    committer = commit.get("committer") \
        if isinstance(commit.get("committer"), dict) else {}
    when = _text(committer.get("date")) or _text(author.get("date"))
    if not sha or not when:
        return None
    data: Record = {
        "sha": sha,
        "author_name": _text(author.get("name")),
        "author": user(raw.get("author")),
        "authored_at": _text(author.get("date")),
        "committed_at": _text(committer.get("date")),
        "message": _text(commit.get("message")),
    }
    return ("commit", sha, number, when, data)


def mention_entry(number: int, raw: Dict[str, object]) -> Optional[Entry]:
    """Una mención desde otro issue/PR («cross-referenced» del
    historial de cada uno)."""
    if _text(raw.get("event")) != "cross-referenced":
        return None
    source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
    issue = source.get("issue") if isinstance(source.get("issue"), dict) \
        else {}
    repository = issue.get("repository") \
        if isinstance(issue.get("repository"), dict) else {}
    when = _text(raw.get("created_at"))
    if not issue or not when:
        return None
    data: Record = {
        "actor": user(raw.get("actor")),
        "created_at": when,
        "source_repo": _text(repository.get("full_name")),
        "source_number": _int(issue.get("number")),
        "source_title": _text(issue.get("title")),
        "source_is_pr": isinstance(issue.get("pull_request"), dict),
        "source_state": _text(issue.get("state")),
    }
    ident = f"{data['source_repo']}#{data['source_number']}@{when}"
    return ("mention", ident, number, when, data)


# ------------------------------------------------------------------ #
#  Los listados                                                       #
# ------------------------------------------------------------------ #
def _rows_issues(items: Sequence[object]) -> Rows:
    return {"items": [issue_item(raw) for raw in items
                      if isinstance(raw, dict) and _int(raw.get("number"))]}


def _rows_pulls(items: Sequence[object]) -> Rows:
    validos = [raw for raw in items
               if isinstance(raw, dict) and _int(raw.get("number"))]
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
    # Códigos con los que el listado se da por vacío en vez de fallar
    # (410: los issues están desactivados en ese repositorio).
    missing_ok: Tuple[int, ...] = (410,)


_ASC = (("sort", "created"), ("direction", "asc"))
LISTINGS: Tuple[Listing, ...] = (
    Listing("issues", "Issues y pull requests", "/issues",
            (("state", "all"),) + _ASC, _rows_issues, "los issues"),
    Listing("pulls", "Ramas y fusiones de los pull requests", "/pulls",
            (("state", "all"),) + _ASC, _rows_pulls, "los pull requests"),
    Listing("comments", "Comentarios", "/issues/comments", _ASC,
            _entries(comment_entry), "los comentarios"),
    Listing("code", "Comentarios en el código", "/pulls/comments", _ASC,
            _entries(code_comment_entry), "los comentarios en el código"),
    Listing("events", "Eventos (cierres, etiquetas, asignaciones…)",
            "/issues/events", (), _entries(event_entry), "los eventos"),
)


@dataclass(frozen=True)
class PerItem:
    name: str
    label: str
    template: str
    convert: Callable[[int, Dict[str, object]], Optional[Entry]]
    what: str


REVIEWS = PerItem("reviews", "Revisiones de cada pull request",
                  "/pulls/{n}/reviews", review_entry, "las revisiones")
COMMITS = PerItem("commits", "Commits de cada pull request",
                  "/pulls/{n}/commits", commit_entry, "los commits")
MENTIONS = PerItem("timeline", "Menciones desde otros issues y PR",
                   "/issues/{n}/timeline", mention_entry,
                   "el historial de un issue")

COUNTED = (("comment", "comentarios"), ("code", "en el código"),
           ("event", "eventos"), ("review", "revisiones"),
           ("commit", "commits"), ("mention", "menciones"))


# ------------------------------------------------------------------ #
#  La descarga                                                        #
# ------------------------------------------------------------------ #
class _Tracker:
    """Cuenta lo descargado y estima lo que falta (en consultas)."""

    def __init__(self, session: ApiSession, store: Store,
                 on_progress: Optional[ProgressCallback]) -> None:
        self.session = session
        self.store = store
        self.on_progress = on_progress
        self.stage = ""
        # Páginas que faltan de cada listado (1 mientras no se sepa más).
        self.pages_left: Dict[str, int] = {l.name: 1 for l in LISTINGS}
        self.pending_items = 0
        self.counts: Dict[str, int] = {}
        self.recount()

    def recount(self) -> None:
        """Lo guardado de verdad (al retomar, una página repetida no
        cuenta dos veces)."""
        self.counts["items"] = self.store.items_count()
        for kind, _label in COUNTED:
            self.counts[kind] = self.store.count(kind)

    def add(self, rows: Rows) -> None:
        for kind, _i, _n, _at, _d in rows.get("entries", ()):
            self.counts[kind] = self.counts.get(kind, 0) + 1
        if rows.get("items") or rows.get("new_items"):
            self.counts["items"] = self.store.items_count()

    def report(self) -> None:
        if self.on_progress is None:
            return
        made = self.session.requests
        estimate = made + sum(self.pages_left.values()) + self.pending_items
        self.on_progress(Progress(self.stage, made, estimate,
                                  dict(self.counts)))


def _pages_left(page: Page) -> int:
    """Páginas que faltan tras `page` (si GitHub dice cuál es la última;
    si no, al menos una mientras haya «siguiente»)."""
    current = page_number(page.url)
    if page.last_page and current:
        return max(0, page.last_page - current)
    return 1 if page.next_url else 0


def _fetch_listing(session: ApiSession, store: Store, base: str,
                   listing: Listing, tracker: _Tracker) -> None:
    cursor, done = store.stage(listing.name)
    if done:
        tracker.pages_left[listing.name] = 0
        return
    tracker.stage = listing.label
    tracker.report()
    try:
        for page in session.pages(base + listing.path, dict(listing.params),
                                  start=cursor, what=listing.what):
            rows = listing.rows(page.items)
            # El cursor es ESTA página: al retomar se vuelve a pedir (lo
            # repetido se sustituye), y si algo se borró entretanto y las
            # páginas corrieron un puesto, no se pierde nada.
            store.save(listing.name, cursor=page.url, **rows)
            tracker.add(rows)
            tracker.pages_left[listing.name] = _pages_left(page)
            tracker.report()
    except ApiError as e:
        if e.status not in listing.missing_ok:
            raise
    store.save(listing.name, cursor="", done=True)
    tracker.pages_left[listing.name] = 0
    tracker.recount()


def _fetch_per_item(session: ApiSession, store: Store, base: str,
                    what: PerItem, numbers: Iterable[int],
                    tracker: _Tracker) -> None:
    hechos = store.done_stages(what.name + ":")
    pendientes = [n for n in numbers if f"{what.name}:{n}" not in hechos]
    if not pendientes:
        return
    tracker.stage = what.label
    for number in pendientes:
        entries: List[Entry] = []
        try:
            for page in session.pages(base + what.template.format(n=number),
                                      {}, what=what.what):
                for raw in page.items:
                    if isinstance(raw, dict):
                        entry = what.convert(number, raw)
                        if entry is not None:
                            entries.append(entry)
        except ApiError as e:
            # Un pull request oculto o borrado: se sigue con los demás.
            if e.status not in (404, 410, 422):
                raise
        store.save(f"{what.name}:{number}", done=True, entries=entries)
        tracker.pending_items -= 1
        tracker.add({"entries": entries})
        tracker.report()


def fetch_all(session: ApiSession, owner: str, repo: str, store: Store,
              options: FetchOptions = FetchOptions(),
              on_progress: Optional[ProgressCallback] = None) -> None:
    """Descarga al almacén todo lo de `owner/repo` (o lo que falte, si
    se retoma). Las esperas y la cancelación las lleva la sesión."""
    base = "/repos/" + urllib.parse.quote(owner, safe="") + "/" \
        + urllib.parse.quote(repo, safe="")
    tracker = _Tracker(session, store, on_progress)
    for listing in LISTINGS:
        _fetch_listing(session, store, base, listing, tracker)
    pulls = store.pull_numbers()
    todos = store.numbers()
    trabajos: List[Tuple[PerItem, List[int]]] = [(REVIEWS, pulls)]
    if options.commits:
        trabajos.append((COMMITS, pulls))
    if options.mentions:
        trabajos.append((MENTIONS, todos))
    for what, numbers in trabajos:
        hechos = store.done_stages(what.name + ":")
        tracker.pending_items += sum(1 for n in numbers
                                     if f"{what.name}:{n}" not in hechos)
    for what, numbers in trabajos:
        _fetch_per_item(session, store, base, what, numbers, tracker)
    tracker.stage = "Listo"
    tracker.recount()
    tracker.report()
