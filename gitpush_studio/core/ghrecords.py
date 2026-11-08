# -*- coding: utf-8 -*-
"""Lo justo de cada cosa que llega de GitHub, como se guarda en el
almacén y como lo lee el TXT.

GitHub manda de cada issue, comentario o revisión decenas de campos
(enlaces de la API, identificadores internos, avatares…). Aquí se
quedan solo los que el TXT usa, siempre con el mismo tipo (un texto que
falta es "", un número que falta es 0). Da igual que lleguen por REST o
por GraphQL (`ghgraphql` los traduce a esta misma forma): la misma
conversación produce exactamente las mismas entradas.

Una entrada es (tipo, id, número del issue/PR, cuándo, datos). El
almacén no guarda dos entradas con el mismo tipo e id (la segunda pisa a
la primera), así que el id tiene que ser ÚNICO de verdad:

  · comentarios, comentarios en el código, revisiones y eventos: el id de
    GitHub, que es único en todo GitHub;
  · commits: el MISMO commit puede estar en varios pull requests (uno
    cerrado y reabierto como otro, ramas apiladas…): su id lleva delante
    el número del pull request («12:3a4b…»);
  · menciones: un mismo comentario («Arregla #23 y #31») menciona a
    varios a la vez, desde el mismo sitio y en el mismo segundo: su id
    lleva delante el número del mencionado.

Sin eso, el commit o la mención de uno desaparecía del TXT del otro.
"""

import hashlib
import json
import urllib.parse
from typing import Dict, List, Optional, Tuple

from .ghstore import Entry

# Eventos que no son conversación sino avisos (quién se suscribió, a
# quién se mencionó: las menciones ya están en el texto).
SKIPPED_EVENTS = frozenset({"subscribed", "unsubscribed", "mentioned"})
REACTIONS = ("+1", "-1", "laugh", "hooray", "confused", "heart", "rocket",
             "eyes")

Record = Dict[str, object]


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


def item_number(raw: object) -> int:
    """El número de un issue/PR tal como llega en un listado; 0 si no es
    un issue/PR (lo que tenga 0 se descarta)."""
    return _int(raw.get("number")) if isinstance(raw, dict) else 0


def number_in_url(url: object) -> int:
    """El número final de «…/issues/123» o «…/pulls/123»; 0 si no hay."""
    path = urllib.parse.urlsplit(_text(url)).path.rstrip("/")
    return _int(path.rsplit("/", 1)[-1]) if path else 0


def _milestone(raw: object) -> str:
    return _text(raw.get("title")) if isinstance(raw, dict) else ""


def _ident(raw: Dict[str, object], number: int, data: Record) -> str:
    """El id de GitHub (único en todo GitHub). Si faltara —GitHub siempre
    lo manda—, uno sacado de lo que se guarda: dos entradas sin id nunca
    se pisan en el almacén."""
    numero = _int(raw.get("id"))
    if numero > 0:
        return str(numero)
    huella = hashlib.sha256(json.dumps(data, sort_keys=True,
                                       ensure_ascii=False).encode("utf-8"))
    return f"sin-id:{number}:{huella.hexdigest()[:20]}"


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
    return ("comment", _ident(raw, number, data), number,
            str(data["created_at"]), data)


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
    return ("code", _ident(raw, number, data), number,
            str(data["created_at"]), data)


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
    return ("event", _ident(raw, number, data), number,
            str(data["created_at"]), data)


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
    return ("review", _ident(raw, number, data), number, when, data)


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
    # El mismo commit puede estar en varios pull requests.
    return ("commit", f"{number}:{sha}", number, when, data)


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
    # Un mismo comentario menciona a varios a la vez (mismo sitio, mismo
    # segundo): el mencionado va en el id.
    ident = (f"{number}<{data['source_repo']}#{data['source_number']}"
             f"@{when}")
    return ("mention", ident, number, when, data)
