# -*- coding: utf-8 -*-
"""El TXT con todo lo que se habló en un repositorio de GitHub.

Un solo archivo, pensado para leerlo una persona y para pasárselo a una
IA sin que se confunda nada:

    HISTORIAL DE GITHUB: dueño/repositorio
    ================  (datos del repositorio y del volcado)
    ÍNDICE            (una línea por issue y por pull request)
    ================
    ISSUE #1 · cerrado (completado) · Título
    ----------------  (quién lo abrió y cuándo, etiquetas, asignados…)
    [2025-01-03 10:22:05 -05:00] @luis (colaborador) abrió el issue:
        lo que escribió, con cuatro espacios delante
    [2025-01-03 11:00:00 -05:00] @ana (propietario) comentó:
        …
    [2025-01-04 18:01:44 -05:00] @ana cerró el issue.
    ================
    PULL REQUEST #2 · fusionado · …   (y además revisiones, comentarios
                                       en el código en hilo, commits…)

La regla que lo hace inequívoco: TODO lo que escribió alguien va con
sangría (cuatro espacios o más). Una línea que empieza en la primera
columna siempre es del volcado: un comentario que contenga «=====» o
«ISSUE #9 · …» nunca parece el principio de otro issue. Los títulos y
nombres, que sí van en líneas del volcado, se dejan en una sola línea.

Lo que llega de GitHub se limpia al escribirlo: los caracteres de
control (que en una terminal pueden cambiar colores o mover el cursor)
se cambian por «�», y los que invierten el sentido del texto (con los
que una línea puede parecer otra) se escriben a la vista, «<U+202E>».
Con `redact`, los secretos (tokens, contraseñas, llaves) se tachan
también aquí, igual que en el TXT del proyecto.

Las horas van en la zona horaria de este equipo con su diferencia con
UTC (o en UTC si se pide): «2025-01-03 10:22:05 -05:00».
"""

import re
import textwrap
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .. import APP_NAME, APP_VERSION
from .ghstore import Store
from .redact import MASK, redact_bytes

TITLE_PREFIX = "HISTORIAL DE GITHUB: "
FOOTER_PREFIX = "FIN DEL HISTORIAL: "
RULE = "=" * 80
THIN = "-" * 80
INDENT = "    "
THREAD = INDENT + "│ "
THREAD_BODY = THREAD + INDENT
HUNK_LINES = 8                 # líneas del código comentado que se citan
LABEL_WIDTH = 18
RULE_WIDTH = 80

ROLES = {
    "OWNER": "propietario",
    "MEMBER": "miembro de la organización",
    "COLLABORATOR": "colaborador",
    "CONTRIBUTOR": "ya había contribuido",
    "FIRST_TIME_CONTRIBUTOR": "primera contribución",
    "FIRST_TIMER": "primera vez en GitHub",
    "MANNEQUIN": "cuenta importada",
}
REACTION_SIGNS = {"+1": "👍", "-1": "👎", "laugh": "😄", "hooray": "🎉",
                  "confused": "😕", "heart": "❤️", "rocket": "🚀",
                  "eyes": "👀"}
LOCK_REASONS = {"off-topic": "fuera de tema", "too heated": "demasiado "
                "acalorada", "resolved": "resuelta", "spam": "spam"}
CLOSE_REASONS = {"completed": "completado", "not_planned": "no planeado",
                 "duplicate": "duplicado", "reopened": ""}
REVIEW_STATES = {"APPROVED": "aprobó los cambios",
                 "CHANGES_REQUESTED": "pidió cambios",
                 "COMMENTED": "comentó",
                 "DISMISSED": "revisó (revisión descartada después)"}

_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_BIDI = re.compile("[\u202a-\u202e\u2066-\u2069]")
_SPACES = re.compile(r"\s+")


# ------------------------------------------------------------------ #
#  Texto que llega de GitHub                                          #
# ------------------------------------------------------------------ #
def clean(text: str) -> str:
    """Saltos de línea normalizados, sin caracteres de control y con los
    que invierten el texto a la vista."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("\ufffd", text)
    return _BIDI.sub(lambda m: f"<U+{ord(m.group()):04X}>", text)


def inline(text: str) -> str:
    """Para las líneas del volcado (títulos, etiquetas, nombres): una
    sola línea, sin espacios de sobra."""
    return _SPACES.sub(" ", clean(text)).strip()


class Redaction:
    """Tacha secretos (si se pidió) y cuenta cuántos."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.count = 0

    def __call__(self, text: str) -> str:
        if not self.enabled or not text:
            return text
        data, found = redact_bytes(text.encode("utf-8"), "")
        if not found:
            return text
        self.count += found
        return data.decode("utf-8", "replace")


# ------------------------------------------------------------------ #
#  Horas                                                              #
# ------------------------------------------------------------------ #
class Clock:
    """Convierte las horas de GitHub (UTC, ISO 8601) a la zona elegida:
    `tz` None = la de este equipo (con sus cambios de horario)."""

    def __init__(self, tz: Optional[tzinfo] = None) -> None:
        self.tz = tz

    def moment(self, iso: str) -> Optional[datetime]:
        if not iso:
            return None
        try:
            moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(self.tz) if self.tz is not None \
            else moment.astimezone()

    def full(self, iso: str) -> str:
        """«2025-01-03 10:22:05 -05:00»."""
        moment = self.moment(iso)
        if moment is None:
            return iso or "¿?"
        return moment.strftime("%Y-%m-%d %H:%M:%S ") + offset(moment)

    def day(self, iso: str) -> str:
        moment = self.moment(iso)
        return moment.strftime("%Y-%m-%d") if moment is not None \
            else (iso or "")[:10]


def offset(moment: datetime) -> str:
    """«-05:00», «+01:00», «+00:00»."""
    delta = moment.utcoffset()
    minutes = int(delta.total_seconds() // 60) if delta is not None else 0
    sign = "-" if minutes < 0 else "+"
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


# ------------------------------------------------------------------ #
#  Personas y adornos                                                 #
# ------------------------------------------------------------------ #
def who(person: object, association: str = "") -> str:
    """«@ana (propietario)», «@dependabot[bot] (bot)», «@ghost (cuenta
    eliminada)»."""
    if not isinstance(person, dict) or not person.get("login"):
        return "@ghost (cuenta eliminada)"
    login = inline(str(person["login"]))
    notes: List[str] = []
    if person.get("type") == "Bot" or login.endswith("[bot]"):
        notes.append("bot")
    role = ROLES.get(association or "", "")
    if role:
        notes.append(role)
    return f"@{login}" + (f" ({', '.join(notes)})" if notes else "")


def login_of(person: object) -> str:
    if isinstance(person, dict) and person.get("login"):
        return "@" + inline(str(person["login"]))
    return "@ghost"


def reactions_text(counts: object) -> str:
    """«👍 3 · ❤️ 1» (solo las que hay)."""
    if not isinstance(counts, dict):
        return ""
    parts = [f"{sign} {counts[name]}" for name, sign in REACTION_SIGNS.items()
             if isinstance(counts.get(name), int) and counts[name] > 0]
    return " · ".join(parts)


def short_sha(sha: object) -> str:
    return str(sha or "")[:7]


def _num(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def _plural(count: int, one: str, many: str) -> str:
    return f"{_num(count)} {one if count == 1 else many}"


# ------------------------------------------------------------------ #
#  Un issue o pull request                                            #
# ------------------------------------------------------------------ #
@dataclass
class Summary:
    """Lo que el índice y el resumen necesitan de cada uno."""
    number: int
    kind: str                # "issue" | "pr"
    state: str               # texto ya traducido
    bucket: str              # abierto | cerrado | fusionado | sin_fusionar
    created_at: str
    author: str
    title: str


def is_merged(item: Dict[str, object], pull: Dict[str, object]) -> bool:
    return bool(item.get("merged_at") or pull.get("merged_at"))


def state_of(item: Dict[str, object], pull: Dict[str, object]
             ) -> Tuple[str, str]:
    """(texto del estado, grupo para contar)."""
    if item.get("kind") == "pr":
        if is_merged(item, pull):
            return "fusionado", "fusionado"
        if item.get("state") == "open":
            borrador = item.get("draft") or pull.get("draft")
            return ("abierto (borrador)" if borrador else "abierto"), \
                "abierto"
        return "cerrado sin fusionar", "sin_fusionar"
    if item.get("state") == "open":
        return "abierto", "abierto"
    motivo = CLOSE_REASONS.get(str(item.get("state_reason") or ""), "")
    return (f"cerrado ({motivo})" if motivo else "cerrado"), "cerrado"


def summarize(store: Store, number: int) -> Summary:
    item = store.item(number)
    pull = store.pull(number) if item.get("kind") == "pr" else {}
    state, bucket = state_of(item, pull)
    return Summary(number=number, kind=str(item.get("kind") or "issue"),
                   state=state, bucket=bucket,
                   created_at=str(item.get("created_at") or ""),
                   author=login_of(item.get("user")),
                   title=inline(str(item.get("title") or "")))


class _Out:
    """Escribe líneas en UTF-8 y lleva la cuenta de los bytes."""

    def __init__(self, write: Callable[[bytes], object]) -> None:
        self._write = write
        self.bytes = 0
        self._pending_gap = False

    def line(self, text: str = "") -> None:
        data = (text + "\n").encode("utf-8")
        self._write(data)
        self.bytes += len(data)

    def lines(self, texts: Sequence[str]) -> None:
        for text in texts:
            self.line(text)


class _Conversation:
    """Las líneas de un issue o pull request: cabecera y conversación."""

    def __init__(self, store: Store, number: int, clock: Clock,
                 redaction: Redaction, repo_name: str = "") -> None:
        self.number = number
        self.repo_name = repo_name
        self.item = store.item(number)
        self.is_pr = self.item.get("kind") == "pr"
        self.pull = store.pull(number) if self.is_pr else {}
        self.entries = store.entries_for(number)
        self.clock = clock
        self.redact = redaction
        self.noun = "el pull request" if self.is_pr else "el issue"

    # ---- piezas -------------------------------------------------------
    def stamp(self, iso: str) -> str:
        return f"[{self.clock.full(iso)}]"

    def body(self, text: str, prefix: str = INDENT) -> List[str]:
        limpio = clean(self.redact(text or "")).strip("\n")
        if not limpio.strip():
            return [prefix + "(sin texto)"]
        return [prefix + linea for linea in limpio.split("\n")]


    def _events(self, name: str) -> List[Dict[str, object]]:
        return [d for kind, d in self.entries
                if kind == "event" and d.get("event") == name]

    # ---- cabecera -------------------------------------------------------
    def header(self) -> List[str]:
        item, pull = self.item, self.pull
        state, _bucket = state_of(item, pull)
        title = inline(str(item.get("title") or ""))
        tipo = "PULL REQUEST" if self.is_pr else "ISSUE"
        out = [f"{tipo} #{self.number} · {state} · {title}", THIN]
        autor = who(item.get("user"), str(item.get("association") or ""))
        abierto = self.clock.full(str(item.get("created_at") or ""))
        out += wrapped("Abierto por", f"{autor} · {abierto}")
        if self.is_pr:
            out += self._pr_fields()
        cerrado = str(item.get("closed_at") or "")
        if cerrado and not is_merged(item, pull):
            por = self._last_actor("closed")
            texto = self.clock.full(cerrado) + (f" por {por}" if por else "")
            out += wrapped("Cerrado", texto)
        etiquetas = [inline(str(x)) for x in item.get("labels") or []]
        if etiquetas:
            out += wrapped_items("Etiquetas", etiquetas)
        asignados = ["@" + inline(str(x)) for x in item.get("assignees") or []]
        if asignados:
            out += wrapped_items("Asignado a", asignados)
        if item.get("milestone"):
            out += wrapped("Hito", inline(str(item["milestone"])))
        if item.get("locked"):
            motivo = LOCK_REASONS.get(str(item.get("lock_reason") or ""), "")
            texto = "sí, la conversación está cerrada a comentarios"
            out += wrapped("Bloqueado",
                           texto + (f" ({motivo})" if motivo else ""))
        reacciones = reactions_text(item.get("reactions"))
        if reacciones:
            out += wrapped("Reacciones", reacciones)
        out += wrapped_items("Participan", self._participants())
        if item.get("html_url"):
            out += wrapped("Enlace", inline(str(item["html_url"])))
        out.append(THIN)
        return out

    def _pr_fields(self) -> List[str]:
        item, pull = self.item, self.pull
        out: List[str] = []
        head = inline(str(pull.get("head") or ""))
        base = inline(str(pull.get("base") or ""))
        if head or base:
            ramas = f"{head or '¿?'} → {base or '¿?'}"
            origen = inline(str(pull.get("head_repo") or ""))
            if origen and origen.lower() != self.repo_name.lower():
                ramas += f" (desde la bifurcación {origen})"
            out += wrapped("Ramas", ramas)
        if is_merged(item, pull):
            cuando = str(pull.get("merged_at") or item.get("merged_at") or "")
            por = self._last_actor("merged")
            commit = short_sha(pull.get("merge_commit"))
            texto = self.clock.full(cuando) + (f" por {por}" if por else "")
            if commit:
                texto += f" · commit {commit}"
            out += wrapped("Fusionado", texto)
        pedidos = ["@" + inline(str(x))
                   for x in pull.get("requested_reviewers") or []]
        pedidos += [f"equipo «{inline(str(x))}»"
                    for x in pull.get("requested_teams") or []]
        if pedidos:
            out += wrapped("Revisión pedida",
                           ", ".join(pedidos) + " (sin responder aún)")
        return out

    def _last_actor(self, event: str) -> str:
        eventos = self._events(event)
        return login_of(eventos[-1].get("actor")) if eventos else ""

    def _participants(self) -> List[str]:
        """Todas las personas que escribieron o hicieron algo, en el
        orden en que aparecen."""
        vistos: List[str] = [login_of(self.item.get("user"))]
        for kind, data in self.entries:
            persona = _person(kind, data)
            if kind == "commit" and persona is None:
                continue                  # autor sin cuenta de GitHub
            nombre = login_of(persona)
            if nombre not in vistos:
                vistos.append(nombre)
        return vistos

    # ---- la conversación ----------------------------------------------
    def conversation(self) -> List[List[str]]:
        """Bloques de líneas, en orden. Un bloque de una sola línea es un
        evento (van juntos); los demás llevan una línea en blanco antes."""
        blocks: List[List[str]] = [self.opening()]
        codes = [d for kind, d in self.entries if kind == "code"]
        threads, by_review, loose = _threads(codes)
        reviews_shown = {int(d.get("id") or 0) for kind, d in self.entries
                         if kind == "review" and _review_shown(
                             d, by_review.get(int(d.get("id") or 0)))}
        merged_moments = {str(d.get("created_at")) for d in
                          self._events("merged")}
        for kind, data in self.entries:
            if kind == "comment":
                blocks.append(self.comment(data))
            elif kind == "review":
                review_id = int(data.get("id") or 0)
                if review_id in reviews_shown:
                    blocks.append(self.review(
                        data, [threads[r] for r in by_review.get(review_id,
                                                                 [])]))
            elif kind == "code":
                # Solo la raíz de cada hilo; y solo si no va dentro de su
                # revisión (sin revisión, o con una que no llegó).
                root_id = int(data.get("id") or 0)
                review_id = int(data.get("review_id") or 0)
                es_raiz = root_id in threads
                suelto = root_id in loose or review_id not in reviews_shown
                if es_raiz and suelto:
                    blocks.append(self.loose_thread(threads[root_id]))
            elif kind == "event":
                if data.get("event") == "closed" and \
                        str(data.get("created_at")) in merged_moments:
                    continue          # «fusionó» ya lo dice
                blocks.append([self.event(data)])
            elif kind == "commit":
                blocks.append(self.commit(data))
            elif kind == "mention":
                blocks.append([self.mention(data)])
        return blocks

    def opening(self) -> List[str]:
        item = self.item
        autor = who(item.get("user"), str(item.get("association") or ""))
        head = (f"{self.stamp(str(item.get('created_at') or ''))} {autor} "
                f"abrió {self.noun}:")
        return [head] + self.body(str(item.get("body") or ""))

    def _changed(self, data: Dict[str, object]) -> str:
        creado = str(data.get("created_at") or "")
        cambiado = str(data.get("updated_at") or "")
        if cambiado and creado and cambiado > creado:
            return f" · actualizado el {self.clock.full(cambiado)}"
        return ""

    def _reacted(self, data: Dict[str, object]) -> str:
        texto = reactions_text(data.get("reactions"))
        return f" · {texto}" if texto else ""

    def comment(self, data: Dict[str, object]) -> List[str]:
        autor = who(data.get("user"), str(data.get("association") or ""))
        head = (f"{self.stamp(str(data.get('created_at') or ''))} {autor} "
                f"comentó{self._reacted(data)}{self._changed(data)}:")
        return [head] + self.body(str(data.get("body") or ""))

    def review(self, data: Dict[str, object],
               threads: List[List[Dict[str, object]]]) -> List[str]:
        autor = who(data.get("user"), str(data.get("association") or ""))
        estado = REVIEW_STATES.get(str(data.get("state") or ""),
                                   f"revisó ({inline(str(data.get('state')))})")
        commit = short_sha(data.get("commit"))
        head = (f"{self.stamp(str(data.get('submitted_at') or ''))} {autor} "
                f"revisó el pull request: {estado}"
                + (f" (en el commit {commit})" if commit else "") + ":")
        out = [head]
        if str(data.get("body") or "").strip() or not threads:
            out += self.body(str(data.get("body") or ""))
        for thread in threads:
            out += self.thread(thread)
        return out

    def loose_thread(self, thread: List[Dict[str, object]]) -> List[str]:
        root = thread[0]
        autor = who(root.get("user"), str(root.get("association") or ""))
        head = (f"{self.stamp(str(root.get('created_at') or ''))} {autor} "
                "comentó en el código:")
        return [head] + self.thread(thread)

    def thread(self, thread: List[Dict[str, object]]) -> List[str]:
        """Un hilo de comentarios sobre una línea (o un archivo): el
        trozo de código y cada mensaje con sus respuestas."""
        root = thread[0]
        out = [INDENT + "┌─ " + _where(root)]
        hunk = clean(self.redact(str(root.get("diff_hunk") or ""))) \
            .strip("\n").split("\n")
        hunk = [h for h in hunk if h != ""] if any(hunk) else []
        if hunk:
            cabecera = hunk[0] if hunk[0].startswith("@@") else ""
            cuerpo = hunk[1:] if cabecera else hunk
            if cabecera:
                out.append(THREAD + cabecera)
            if len(cuerpo) > HUNK_LINES:
                out.append(THREAD + f"… ({len(cuerpo) - HUNK_LINES} líneas "
                           "antes)")
                cuerpo = cuerpo[-HUNK_LINES:]
            out += [THREAD + linea for linea in cuerpo]
            out.append(THREAD.rstrip())
        for index, message in enumerate(thread):
            autor = who(message.get("user"),
                        str(message.get("association") or ""))
            verbo = "" if index == 0 else " respondió"
            out.append(THREAD + f"{self.stamp(str(message.get('created_at') or ''))} "
                       f"{autor}{verbo}{self._reacted(message)}"
                       f"{self._changed(message)}:")
            out += self.body(str(message.get("body") or ""), THREAD_BODY)
        out.append(INDENT + "└─")
        return out

    def event(self, data: Dict[str, object]) -> str:
        return f"{self.stamp(str(data.get('created_at') or ''))} " \
               f"{login_of(data.get('actor'))} {self._event_text(data)}."

    def _event_text(self, data: Dict[str, object]) -> str:
        name = str(data.get("event") or "")
        noun = self.noun
        commit = short_sha(data.get("commit"))
        con_commit = f" con el commit {commit}" if commit else ""
        if name == "closed":
            motivo = CLOSE_REASONS.get(str(data.get("state_reason") or ""), "")
            return f"cerró {noun}{con_commit}" + (f" ({motivo})" if motivo
                                                  else "")
        if name == "reopened":
            return f"reabrió {noun}"
        if name == "merged":
            return "fusionó el pull request" + (f" (commit {commit})"
                                                if commit else "")
        if name in ("labeled", "unlabeled"):
            verbo = "añadió" if name == "labeled" else "quitó"
            return f"{verbo} la etiqueta «{inline(str(data.get('label') or ''))}»"
        if name in ("assigned", "unassigned"):
            asignado = login_of(data.get("assignee"))
            if name == "assigned":
                return "se lo asignó" if asignado == login_of(data.get(
                    "actor")) else f"se lo asignó a {asignado}"
            return f"quitó la asignación a {asignado}"
        if name in ("milestoned", "demilestoned"):
            hito = inline(str(data.get("milestone") or ""))
            return (f"lo puso en el hito «{hito}»" if name == "milestoned"
                    else f"lo quitó del hito «{hito}»")
        if name == "renamed":
            rename = data.get("rename") if isinstance(data.get("rename"),
                                                      dict) else {}
            return (f"cambió el título de «{inline(str(rename.get('from') or ''))}» "
                    f"a «{inline(str(rename.get('to') or ''))}»")
        if name in ("locked", "unlocked"):
            motivo = LOCK_REASONS.get(str(data.get("lock_reason") or ""), "")
            if name == "unlocked":
                return "desbloqueó la conversación"
            return "bloqueó la conversación" + (f" ({motivo})" if motivo
                                                else "")
        if name == "referenced":
            return f"lo mencionó en el commit {commit}" if commit \
                else "lo mencionó en un commit"
        if name in ("review_requested", "review_request_removed"):
            quien = login_of(data.get("reviewer")) if data.get("reviewer") \
                else f"el equipo «{inline(str(data.get('team') or ''))}»"
            return (f"pidió la revisión de {quien}"
                    if name == "review_requested"
                    else f"retiró la petición de revisión a {quien}")
        if name == "review_dismissed":
            dismissed = data.get("dismissed") \
                if isinstance(data.get("dismissed"), dict) else {}
            motivo = inline(str(dismissed.get("message") or ""))
            return "descartó una revisión" + (f": «{motivo}»" if motivo
                                              else "")
        simples = {
            "head_ref_deleted": "borró la rama del pull request",
            "head_ref_restored": "restauró la rama del pull request",
            "head_ref_force_pushed": "subió la rama forzando (git push -f): "
                                     "los commits anteriores se reescribieron",
            "base_ref_changed": "cambió la rama de destino",
            "base_ref_force_pushed": "forzó la rama de destino",
            "convert_to_draft": "lo pasó a borrador",
            "ready_for_review": "lo marcó como listo para revisar",
            "marked_as_duplicate": "lo marcó como duplicado",
            "unmarked_as_duplicate": "quitó la marca de duplicado",
            "pinned": "lo fijó en el repositorio",
            "unpinned": "lo quitó de los fijados",
            "transferred": "lo trajo desde otro repositorio",
            "comment_deleted": "borró un comentario",
            "connected": "lo enlazó con otro issue o pull request",
            "disconnected": "quitó un enlace con otro issue o pull request",
            "auto_merge_enabled": "activó la fusión automática",
            "auto_merge_disabled": "desactivó la fusión automática",
            "auto_squash_enabled": "activó la fusión automática (squash)",
            "auto_rebase_enabled": "activó la fusión automática (rebase)",
            "added_to_project": "lo añadió a un proyecto",
            "moved_columns_in_project": "lo movió de columna en un proyecto",
            "removed_from_project": "lo quitó de un proyecto",
            "converted_note_to_issue": "convirtió una nota de un proyecto "
                                       "en este issue",
            "deployed": "lo desplegó",
            "deployment_environment_changed": "cambió el entorno de "
                                              "despliegue",
            "user_blocked": "bloqueó a un usuario",
        }
        if name in simples:
            return simples[name]
        return f"hizo «{inline(name)}»"

    def commit(self, data: Dict[str, object]) -> List[str]:
        autor = login_of(data.get("author")) if data.get("author") \
            else f"«{inline(str(data.get('author_name') or '¿?'))}» (sin " \
                 "cuenta de GitHub)"
        mensaje = clean(self.redact(str(data.get("message") or ""))) \
            .strip("\n").split("\n")
        primera = inline(mensaje[0]) if mensaje else ""
        when = str(data.get("committed_at") or data.get("authored_at") or "")
        head = (f"{self.stamp(when)} {autor} añadió el commit "
                f"{short_sha(data.get('sha'))}: «{primera}»")
        resto = [m for m in mensaje[1:]]
        while resto and not resto[0].strip():
            resto = resto[1:]
        if not "".join(resto).strip():
            return [head]
        return [head + " —"] + [INDENT + linea for linea in resto]

    def mention(self, data: Dict[str, object]) -> str:
        tipo = "el pull request" if data.get("source_is_pr") else "el issue"
        titulo = inline(str(data.get("source_title") or ""))
        origen = inline(str(data.get("source_repo") or ""))
        numero = int(data.get("source_number") or 0)
        donde = f"{tipo} #{numero}" + (f" de {origen}" if origen else "")
        return (f"{self.stamp(str(data.get('created_at') or ''))} "
                f"{login_of(data.get('actor'))} lo mencionó desde {donde}"
                + (f" «{titulo}»" if titulo else "") + ".")


def _person(kind: str, data: Dict[str, object]) -> object:
    """Quién hizo cada cosa: el actor de un evento o una mención, el
    autor de un commit, el usuario de lo demás."""
    if kind in ("event", "mention"):
        return data.get("actor")
    if kind == "commit":
        return data.get("author")
    return data.get("user")


def _where(root: Dict[str, object]) -> str:
    """«src/arm.c · línea 42», «… · líneas 40 a 42», «… (el archivo)»."""
    ruta = inline(str(root.get("path") or "¿?"))
    if root.get("subject") == "file":
        texto = f"{ruta} (sobre el archivo entero)"
    else:
        line = int(root.get("line") or 0)
        start = int(root.get("start_line") or 0)
        if start and line and start != line:
            texto = f"{ruta} · líneas {start} a {line}"
        elif line:
            texto = f"{ruta} · línea {line}"
        else:
            texto = ruta
    if root.get("outdated"):
        texto += " · ese código cambió después"
    return texto


def _threads(codes: List[Dict[str, object]]
             ) -> Tuple[Dict[int, List[Dict[str, object]]],
                        Dict[int, List[int]], set]:
    """Agrupa los comentarios en el código en hilos.

    Devuelve ({id de la raíz: [raíz, respuestas…]}, {id de la revisión:
    [ids de las raíces que abrió]}, {ids de raíces sin revisión}).
    """
    by_id = {int(c.get("id") or 0): c for c in codes}
    threads: Dict[int, List[Dict[str, object]]] = {}
    replies: Dict[int, List[Dict[str, object]]] = defaultdict(list)
    for code in codes:
        root_id = _root_of(code, by_id)
        if root_id == int(code.get("id") or 0):
            threads[root_id] = [code]
        else:
            replies[root_id].append(code)
    for root_id, extra in replies.items():
        if root_id in threads:
            threads[root_id].extend(sorted(extra, key=_when_id))
        else:
            # La raíz no llegó (borrada): la respuesta más antigua abre.
            ordenadas = sorted(extra, key=_when_id)
            threads[int(ordenadas[0].get("id") or 0)] = ordenadas
    by_review: Dict[int, List[int]] = defaultdict(list)
    loose = set()
    for root_id, thread in sorted(threads.items(),
                                  key=lambda kv: _when_id(kv[1][0])):
        review_id = int(thread[0].get("review_id") or 0)
        if review_id:
            by_review[review_id].append(root_id)
        else:
            loose.add(root_id)
    return threads, dict(by_review), loose


def _root_of(code: Dict[str, object], by_id: Dict[int, Dict[str, object]]
             ) -> int:
    """La raíz del hilo (GitHub apunta a ella; por si acaso, se sube por
    la cadena, sin quedarse en un ciclo)."""
    current = code
    seen = set()
    while True:
        parent = int(current.get("in_reply_to") or 0)
        ident = int(current.get("id") or 0)
        if not parent or parent not in by_id or ident in seen:
            return ident
        seen.add(ident)
        current = by_id[parent]


def _when_id(code: Dict[str, object]) -> Tuple[str, int]:
    return str(code.get("created_at") or ""), int(code.get("id") or 0)


def _review_shown(review: Dict[str, object],
                  roots: Optional[List[int]]) -> bool:
    """Una revisión «comentó» vacía y sin hilos propios es la que GitHub
    crea por cada respuesta en un hilo: ya sale en su hilo."""
    if roots:
        return True
    if str(review.get("body") or "").strip():
        return True
    return str(review.get("state") or "") != "COMMENTED"


# ------------------------------------------------------------------ #
#  El archivo entero                                                  #
# ------------------------------------------------------------------ #
@dataclass
class RenderStats:
    items: int = 0
    issues: int = 0
    pulls: int = 0
    bytes: int = 0
    redacted: int = 0


@dataclass(frozen=True)
class RenderOptions:
    """Lo que dice la cabecera del volcado sobre lo que incluye."""
    commits: bool = True
    mentions: bool = False
    redact: bool = False


def _counts_text(summaries: List[Summary], store: Store) -> List[str]:
    issues = [s for s in summaries if s.kind != "pr"]
    pulls = [s for s in summaries if s.kind == "pr"]

    def grupos(items: List[Summary], orden: Sequence[Tuple[str, str, str]]
               ) -> str:
        partes = []
        for bucket, uno, varios in orden:
            n = sum(1 for s in items if s.bucket == bucket)
            if n:
                partes.append(_plural(n, uno, varios))
        return f" ({', '.join(partes)})" if partes else ""

    de_issues = _plural(len(issues), "issue", "issues") + grupos(
        issues, (("abierto", "abierto", "abiertos"),
                 ("cerrado", "cerrado", "cerrados")))
    de_pulls = _plural(len(pulls), "pull request", "pull requests") + grupos(
        pulls, (("abierto", "abierto", "abiertos"),
                ("fusionado", "fusionado", "fusionados"),
                ("sin_fusionar", "cerrado sin fusionar",
                 "cerrados sin fusionar")))
    return [de_issues, de_pulls,
            _plural(store.count("comment"), "comentario", "comentarios"),
            _plural(store.count("review"), "revisión", "revisiones"),
            _plural(store.count("code"), "comentario en el código",
                    "comentarios en el código"),
            _plural(store.count("commit"), "commit", "commits"),
            _plural(store.count("event"), "evento", "eventos"),
            _plural(store.count("mention"), "mención", "menciones")]


def _field(label: str, value: str) -> str:
    return f"{label + ':':<{LABEL_WIDTH}}{value}"


def _continued(value: str) -> str:
    return " " * LABEL_WIDTH + value


def wrapped_items(label: str, items: Sequence[str], sep: str = ", "
                  ) -> List[str]:
    """Como `wrapped`, pero partiendo solo entre elementos de la lista
    (nunca a mitad de «2 pull requests (1 fusionado, …)»)."""
    ancho = RULE_WIDTH - LABEL_WIDTH
    lineas: List[str] = []
    actual = ""
    for item in items:
        junto = f"{actual}{sep}{item}" if actual else item
        if actual and len(junto) > ancho:
            lineas.append(actual + sep.rstrip())
            actual = item
        else:
            actual = junto
    lineas.append(actual)
    return [_field(label, lineas[0])] + [_continued(x) for x in lineas[1:]]


def wrapped(label: str, text: str) -> List[str]:
    """Un campo «Etiqueta:  valor» partido en líneas de 80 columnas; las
    de continuación, alineadas bajo el valor."""
    lineas = textwrap.wrap(text, width=RULE_WIDTH - LABEL_WIDTH,
                           break_long_words=False, break_on_hyphens=False)
    if not lineas:
        lineas = [""]
    return [_field(label, lineas[0])] + [_continued(x) for x in lineas[1:]]


EXCLUDED_TEXT = ("quién puso cada reacción (solo cuántas hay), las versiones "
                 "anteriores de un texto editado (solo cuándo se cambió por "
                 "última vez), lo borrado y los avisos de suscripción y de "
                 "mención.")
READING_TEXT = ("cada issue y pull request empieza con una línea de «=» y "
                "su título; dentro va todo lo que pasó, en orden. Lo que "
                "escribió cada persona va con cuatro espacios (o más) "
                "delante: una línea que empieza en la primera columna "
                "siempre es del volcado, nunca de lo que escribió alguien. "
                "La última línea dice «" + FOOTER_PREFIX.strip() + "»: si "
                "falta, el archivo está incompleto.")


def file_header(repo: Dict[str, object], summaries: List[Summary],
                store: Store, clock: Clock, now: datetime,
                options: RenderOptions) -> List[str]:
    nombre = inline(str(repo.get("full_name") or ""))
    out = [TITLE_PREFIX + nombre, RULE]
    out.append(_field("Repositorio", inline(str(repo.get("html_url") or ""))))
    if repo.get("description"):
        out += wrapped("Descripción", inline(str(repo["description"])))
    detalles = ["privado" if repo.get("private") else "público"]
    if repo.get("default_branch"):
        rama = inline(str(repo["default_branch"]))
        detalles.append(f"rama principal «{rama}»")
    detalles.append(f"★ {_num(int(repo.get('stargazers_count') or 0))}")
    detalles.append(_plural(int(repo.get("forks_count") or 0), "bifurcación",
                            "bifurcaciones"))
    if repo.get("archived"):
        detalles.append("archivado")
    out += wrapped("Tipo", " · ".join(detalles))
    if repo.get("created_at"):
        out.append(_field("Creado", clock.full(str(repo["created_at"]))))
    momento = now.strftime("%Y-%m-%d %H:%M:%S ") + offset(now)
    out.append(_field("Volcado", f"{momento} · {APP_NAME} {APP_VERSION}"))
    out += wrapped_items("Contenido", _counts_text(summaries, store), " · ")
    if options.redact:
        out += wrapped("Secretos", f"tachados como «{MASK}» (cuántos, en la "
                                   "última línea)")
    zona = offset(now)
    if zona == "+00:00" and clock.tz is not None:
        out.append(_field("Horas", "en UTC"))
    else:
        out += wrapped("Horas", f"las de este equipo, con su diferencia con "
                                f"UTC (ahora {zona})")
    incluye = ["el texto de cada issue y pull request", "los comentarios",
               "las revisiones", "los comentarios en el código (en hilo)",
               "los eventos"]
    if options.commits:
        incluye.append("los commits de cada pull request")
    if options.mentions:
        incluye.append("las menciones desde otros issues y pull requests")
    out += wrapped("Incluye", ", ".join(incluye) + ".")
    out += wrapped("No incluye", EXCLUDED_TEXT)
    out += wrapped("Cómo leerlo", READING_TEXT)
    out.append(RULE)
    return out


def index_lines(summaries: List[Summary], clock: Clock) -> List[str]:
    out = [f"ÍNDICE ({_num(len(summaries))})", ""]
    ancho = max([len(str(s.number)) for s in summaries] + [1]) + 1
    autor = min(24, max([len(s.author) for s in summaries] + [6]))
    for s in summaries:
        tipo = "PR" if s.kind == "pr" else "issue"
        out.append(f"  {'#' + str(s.number):<{ancho + 1}} {tipo:<5} "
                   f"{s.state:<22} {clock.day(s.created_at):<10}  "
                   f"{s.author:<{autor}}  {s.title}")
    out.append(RULE)
    return out


def render(store: Store, repo: Dict[str, object],
           write: Callable[[bytes], object], tz: Optional[tzinfo] = None,
           options: RenderOptions = RenderOptions(),
           now: Optional[datetime] = None,
           check_cancel: Optional[Callable[[], None]] = None,
           on_item: Optional[Callable[[int, int], None]] = None
           ) -> RenderStats:
    """Escribe el TXT entero con `write` (bytes UTF-8), leyendo issue a
    issue del almacén. `check_cancel` se llama entre issues (puede lanzar
    Cancelled); `on_item(hechos, total)` informa del avance."""
    clock = Clock(tz)
    momento = (now or datetime.now(timezone.utc)).astimezone(tz) \
        if tz is not None else (now or datetime.now()).astimezone()
    redaction = Redaction(options.redact)
    numbers = store.numbers()
    summaries = [summarize(store, n) for n in numbers]
    out = _Out(write)
    out.lines(file_header(repo, summaries, store, clock, momento, options))
    out.line()
    out.lines(index_lines(summaries, clock))
    stats = RenderStats(items=len(numbers),
                        issues=sum(1 for s in summaries if s.kind != "pr"),
                        pulls=sum(1 for s in summaries if s.kind == "pr"))
    for index, number in enumerate(numbers, 1):
        if check_cancel is not None:
            check_cancel()
        conversation = _Conversation(store, number, clock, redaction,
                                     str(repo.get("full_name") or ""))
        out.line()
        out.lines(conversation.header())
        previo_bloque = True
        for block in conversation.conversation():
            es_bloque = len(block) > 1
            if es_bloque or previo_bloque:
                out.line()
            out.lines(block)
            previo_bloque = es_bloque
        out.line()
        out.line(RULE)
        if on_item is not None:
            on_item(index, len(numbers))
    out.line()
    out.line(footer(repo, len(numbers), options, redaction.count))
    stats.bytes = out.bytes
    stats.redacted = redaction.count
    return stats


def footer(repo: Dict[str, object], items: int, options: RenderOptions,
           redacted: int) -> str:
    """La última línea: si falta, el archivo está incompleto."""
    texto = (f"{FOOTER_PREFIX}{inline(str(repo.get('full_name') or ''))} · "
             + _plural(items, "issue o pull request",
                       "issues y pull requests"))
    if options.redact:
        texto += " · " + _plural(redacted, "secreto tachado",
                                 "secretos tachados")
    return texto
