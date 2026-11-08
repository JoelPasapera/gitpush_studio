# -*- coding: utf-8 -*-
"""Volcar en un TXT todo lo que se habló en un repositorio de GitHub.

    dump_repository("ana/app", "app-historial-github.txt", token=…)

Junta las piezas: entiende qué repositorio se pide («dueño/nombre» o
cualquier URL de GitHub), abre las conexiones con la API (`ghpool`,
`ghsession`), descarga lo que falte al almacén (`ghfetch`, por REST y
por GraphQL, `ghgraphql`; `ghstore`) y escribe el TXT
de una vez (`ghrender`) en un temporal que sustituye al destino solo al
terminar (`atomic`). Nunca pisa un archivo que no sea un historial
generado por la app: si en el destino hay otra cosa, usa otro nombre.

Si algo para la descarga (Detener, la conexión, el límite de GitHub),
lo descargado se queda en el almacén y la próxima vez se sigue desde
ahí. Cuando el TXT queda escrito entero, el almacén se borra.
"""

import os
import re
import stat
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from . import ghsession
from .atomic import atomic_open
from .errors import StepError
from .ghfetch import COUNTED, FetchOptions, ProgressCallback, fetch_all
from .ghpool import DEFAULT_WORKERS, MAX_WORKERS, make_pool
from .ghrender import TITLE_PREFIX, RenderOptions, render
from .ghsession import WaitCallback, describe_error
from .ghstore import FORMAT, RESUMABLE, open_store, peek

_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_REPO = re.compile(r"[A-Za-z0-9._-]{1,100}")
_GITHUB_HOST = re.compile(r"(?:^|[/@.])github\.com[:/]+", re.IGNORECASE)
WRITE_BUFFER = 1024 * 1024
NAME_SUFFIX = "-historial-github.txt"
MAX_NAME_TRIES = 99

WriteCallback = Callable[[int, int], None]


@dataclass(frozen=True)
class DumpOptions:
    """Qué entra en el volcado y cómo se escribe.

    `workers` y `graphql` solo cambian CÓMO se descarga (más deprisa), no
    lo que sale: no forman parte de la identidad del almacén y una
    descarga a medias se puede seguir con otros valores.
    """
    commits: bool = True       # los commits de cada pull request
    mentions: bool = False     # menciones desde otros issues y PR
    redact: bool = False       # tachar secretos
    utc: bool = False          # horas en UTC (si no, las de este equipo)
    workers: int = DEFAULT_WORKERS   # conexiones a la vez, como mucho
    graphql: bool = True       # lotes por GraphQL (solo con token)


@dataclass
class DumpResult:
    path: str
    renamed: bool              # el destino estaba ocupado por otra cosa
    repository: str
    items: int
    issues: int
    pulls: int
    counts: Dict[str, int] = field(default_factory=dict)
    requests: int = 0
    resumed: bool = False      # se siguió una descarga anterior
    seconds: float = 0.0
    bytes: int = 0
    redacted: int = 0
    # Lo que no fue como debía (p. ej. GraphQL no se pudo usar), o vacío.
    notice: str = ""


# ------------------------------------------------------------------ #
#  Qué repositorio                                                    #
# ------------------------------------------------------------------ #
def parse_repository(text: str) -> Tuple[str, str]:
    """(dueño, nombre) de «ana/app», «https://github.com/ana/app»,
    «git@github.com:ana/app.git» o cualquier página dentro de él
    («…/ana/app/pull/3»). StepError si no es un repositorio de GitHub."""
    t = (text or "").strip().strip("\"'")
    found = _GITHUB_HOST.search(t)
    if found:
        resto = t[found.end():]
    elif "://" in t or t.count("/") != 1:
        raise StepError("Escribe el repositorio como «usuario/repositorio» "
                        "o pega su dirección de GitHub.")
    else:
        resto = t
    resto = resto.split("?", 1)[0].split("#", 1)[0]
    partes = [p for p in resto.split("/") if p]
    if len(partes) < 2:
        raise StepError("Falta el nombre del repositorio: escribe "
                        "«usuario/repositorio».")
    owner, repo = partes[0], partes[1]
    if repo.lower().endswith(".git"):
        repo = repo[:-len(".git")]
    if not _OWNER.fullmatch(owner) or not _REPO.fullmatch(repo) \
            or repo in (".", ".."):
        raise StepError(f"«{owner}/{repo}» no es un nombre de repositorio "
                        "de GitHub válido.")
    return owner, repo


def default_name(repo: str) -> str:
    return f"{repo}{NAME_SUFFIX}"


# ------------------------------------------------------------------ #
#  ¿Hay una descarga a medias?                                        #
# ------------------------------------------------------------------ #
@dataclass(frozen=True)
class PartialDownload:
    """Una descarga a medias de un repositorio, y con qué opciones se
    hizo. Solo se sigue con las MISMAS (los commits y las menciones
    cambian lo que se guarda): con otras, empieza de cero."""
    repository: str
    commits: bool
    mentions: bool
    items: int                 # issues y PR ya guardados
    # Es de una versión anterior que guardaba mal algo (ver
    # `ghstore.UPGRADES`): eso se vuelve a pedir, lo demás se aprovecha.
    upgrade: bool = False

    def matches(self, options: DumpOptions) -> bool:
        return (self.commits == options.commits
                and self.mentions == options.mentions)

    def describe(self) -> str:
        """«con los commits y las menciones», «sin los commits»…"""
        partes = [("con" if self.commits else "sin") + " los commits",
                  ("con" if self.mentions else "sin") + " las menciones"]
        return " y ".join(partes)

    def upgrade_note(self) -> str:
        """Si es de una versión anterior, qué se vuelve a pedir (y por
        qué); si no, vacío."""
        if not self.upgrade:
            return ""
        return ("Es de una versión anterior, que podía perder algún commit "
                "o mención al guardarlos: esos se vuelven a pedir (con "
                "token, en pocas consultas); lo demás se aprovecha.")


def partial_download(spec: str, store_dir: Optional[Path] = None
                     ) -> Optional[PartialDownload]:
    """La descarga a medias de ese repositorio, si la hay (sin conectarse
    a nada y sin tocar el almacén: solo lo lee)."""
    try:
        owner, repo = parse_repository(spec)
    except StepError:
        return None
    previo = peek(owner, repo, base=store_dir)
    # Uno de un formato anterior que se pone al día también se sigue.
    if previo is None or previo.identity.get("format") not in RESUMABLE:
        return None
    return PartialDownload(repository=f"{owner}/{repo}",
                           commits=bool(previo.identity.get("commits")),
                           mentions=bool(previo.identity.get("mentions")),
                           items=previo.items,
                           upgrade=previo.identity.get("format") != FORMAT)


# ------------------------------------------------------------------ #
#  Dónde se escribe                                                   #
# ------------------------------------------------------------------ #
def is_dump(path: Path) -> bool:
    """¿Es un historial generado por la app? (Un enlace nunca lo es:
    rehacerlo escribiría donde apunte.)"""
    try:
        if not stat.S_ISREG(os.lstat(str(path)).st_mode):
            return False
        with open(str(path), "rb") as fh:
            cabeza = fh.read(len(TITLE_PREFIX.encode("utf-8")))
    except OSError:
        return False
    return cabeza == TITLE_PREFIX.encode("utf-8")


def _free(path: Path) -> bool:
    return not os.path.lexists(str(path)) or is_dump(path)


def free_output_path(destino: Path) -> Tuple[Path, bool]:
    """(ruta donde escribir, se_cambió_el_nombre). Un historial nuestro
    se rehace; cualquier otra cosa (un archivo tuyo, una carpeta, un
    enlace) se respeta y se busca «nombre-historial.txt»,
    «nombre-historial-2.txt»…"""
    destino = Path(destino)
    destino = destino.parent.resolve() / destino.name
    if _free(destino):
        return destino, False
    sufijo = destino.suffix or ".txt"
    for n in range(1, MAX_NAME_TRIES + 1):
        cola = "" if n == 1 else f"-{n}"
        candidato = destino.with_name(f"{destino.stem}-historial{cola}"
                                      f"{sufijo}")
        if _free(candidato):
            return candidato, True
    raise StepError(f"No encuentro un nombre libre junto a «{destino.name}»: "
                    "elige otro destino.")


# ------------------------------------------------------------------ #
#  El volcado                                                         #
# ------------------------------------------------------------------ #
def _repo_info(data: Dict[str, object]) -> Dict[str, object]:
    campos = ("id", "full_name", "html_url", "description", "private",
              "default_branch", "stargazers_count", "forks_count",
              "archived", "created_at", "has_issues")
    return {k: data.get(k) for k in campos}


def dump_repository(spec: str, output: str, token: str = "",
                    options: DumpOptions = DumpOptions(),
                    cancel=None,
                    on_progress: Optional[ProgressCallback] = None,
                    on_wait: Optional[WaitCallback] = None,
                    on_write: Optional[WriteCallback] = None,
                    api_root: Optional[str] = None,
                    store_dir: Optional[Path] = None,
                    now: Optional[datetime] = None,
                    tz: Optional[tzinfo] = None,
                    proxy=None, sleep=None) -> DumpResult:
    """Descarga (o termina de descargar) y escribe el TXT.

    Lanza StepError (con una explicación) o Cancelled; en los dos casos
    lo ya descargado se guarda para la próxima vez y el destino queda
    como estaba.
    """
    t0 = time.time()
    owner, repo = parse_repository(spec)
    # Antes de descargar nada: si no hay dónde escribir, mejor saberlo ya.
    destino, renamed = free_output_path(Path(output))
    root = api_root or ghsession.API_ROOT
    workers = max(1, min(int(options.workers or 1), MAX_WORKERS))
    pool = make_pool(token, root, cancel, on_wait=on_wait, proxy=proxy,
                     sleep=sleep, workers=workers)
    try:
        session = pool.session()
        ruta = "/repos/" + urllib.parse.quote(owner, safe="") + "/" \
            + urllib.parse.quote(repo, safe="")
        response = session.get(ruta)
        if not response.ok or not isinstance(response.data, dict):
            raise StepError(describe_error(
                response, f"el repositorio «{owner}/{repo}»"))
        info = _repo_info(response.data)
        # El nombre de verdad (mayúsculas, o el nuevo si se renombró).
        nombre = str(info.get("full_name") or f"{owner}/{repo}")
        owner, _sep, repo = nombre.partition("/")
        # Solo lo que cambia lo que SALE: con otras conexiones o sin
        # GraphQL, una descarga a medias se sigue igual.
        identity = {"format": FORMAT, "repo_id": info.get("id"),
                    "commits": options.commits,
                    "mentions": options.mentions}
        store = open_store(owner, repo, identity, base=store_dir)
        try:
            aviso = fetch_all(pool, owner, repo, store,
                              FetchOptions(commits=options.commits,
                                           mentions=options.mentions,
                                           workers=workers,
                                           # GraphQL exige token.
                                           graphql=options.graphql
                                           and bool(token)),
                              on_progress)
            zona = timezone.utc if options.utc else tz
            # Otra vez justo antes de escribir: la descarga puede haber
            # durado horas y el usuario, haber creado ese archivo.
            destino, renamed = free_output_path(Path(output))
            with atomic_open(destino, buffering=WRITE_BUFFER) as fh:
                stats = render(store, info, fh.write, tz=zona,
                               options=RenderOptions(
                                   commits=options.commits,
                                   mentions=options.mentions,
                                   redact=options.redact),
                               now=now, check_cancel=pool.check_cancel,
                               on_item=on_write)
            counts = {kind: store.count(kind) for kind, _label in COUNTED}
            resumed = store.resumed
        except BaseException:
            store.close()            # lo descargado se queda para seguir
            raise
        store.remove()
        requests = pool.requests     # antes de cerrar (cierra las sesiones)
    finally:
        pool.close()
    return DumpResult(path=str(destino), renamed=renamed, repository=nombre,
                      items=stats.items, issues=stats.issues,
                      pulls=stats.pulls, counts=counts, requests=requests,
                      resumed=resumed, seconds=time.time() - t0,
                      bytes=stats.bytes, redacted=stats.redacted,
                      notice=aviso)
