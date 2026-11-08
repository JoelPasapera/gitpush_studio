# -*- coding: utf-8 -*-
"""Lo descargado de GitHub, guardado en disco mientras dura el volcado.

Volcar un repositorio grande son cientos o miles de consultas: GitHub
puede pedir una hora de espera a mitad, la conexión puede caerse o el
usuario puede pulsar «Detener». Nada de eso hace perder lo descargado:
cada página se guarda aquí junto con el punto por el que va cada
listado, siempre en la MISMA transacción (o todo o nada: nunca una
página a medias con el cursor ya adelantado). Al volver a empezar se
sigue desde ahí.

Para no forzar el disco con cada página (en Windows, cada confirmación
espera a que el disco diga que lo escribió), las páginas se confirman
juntas, como mucho cada `COMMIT_EVERY` segundos, y siempre al cerrar.
Si el programa muriera de golpe, se perdería como mucho ese último
medio segundo, que se vuelve a pedir: lo confirmado nunca queda a
medias.

Es una base SQLite (de la biblioteca estándar) en
`~/.gitpush_studio/historiales/`, con lo justo de cada cosa (no la
respuesta entera de GitHub). Además, el TXT se escribe leyendo de aquí
issue a issue, sin cargar el repositorio entero en memoria. Cuando el
TXT queda escrito, la base se borra: no quedan copias escondidas.

Tablas:
    meta     identidad del volcado (formato, repositorio, opciones): si
             no coincide con la que se pide, se empieza de cero (salvo
             un formato anterior que se puede poner al día: ver
             `UPGRADES`)
    items    cada issue y cada pull request, por número
    pulls    lo propio de cada pull request (ramas, fusión, borrador)
    entries  todo lo que pasó en cada uno (comentarios, revisiones,
             comentarios en el código, commits, eventos, menciones)
    stages   por dónde va cada parte de la descarga
"""

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .config import CONFIG_DIR

STORE_DIR = CONFIG_DIR / "historiales"
# Cambia si cambia lo que se guarda: un almacén de otro formato se
# descarta en vez de mezclarse (salvo los de `UPGRADES`).
FORMAT = 2
# Formatos anteriores que se ponen al día en vez de empezar de cero: de
# cada uno, lo que guardaba mal (tipos de entrada y prefijos de etapa),
# que se borra para volver a pedirlo. Lo demás se conserva.
#   1 → 2: los commits y las menciones se guardaban sin el número del
#          pull request o issue al que pertenecen; un commit en dos pull
#          requests, o una mención a dos issues desde el mismo comentario,
#          se pisaban (y uno de los dos lo perdía). Issues, comentarios,
#          eventos y revisiones estaban bien.
UPGRADES: Dict[int, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    1: (("commit", "mention"), ("commits:", "timeline:")),
}
# Los formatos de los que se puede seguir una descarga a medias.
RESUMABLE = (FORMAT,) + tuple(UPGRADES)
# Como mucho, cada cuántos segundos se confirma lo guardado.
COMMIT_EVERY = 0.5

# Una entrada: (tipo, id, número del issue/PR, cuándo, datos).
Entry = Tuple[str, str, int, str, Dict[str, object]]

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, "
    "value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS items (number INTEGER PRIMARY KEY, "
    "data TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS pulls (number INTEGER PRIMARY KEY, "
    "data TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS entries (kind TEXT NOT NULL, "
    "id TEXT NOT NULL, number INTEGER NOT NULL, at TEXT NOT NULL, "
    "data TEXT NOT NULL, PRIMARY KEY (kind, id))",
    "CREATE INDEX IF NOT EXISTS entries_by_number ON entries "
    "(number, at)",
    "CREATE TABLE IF NOT EXISTS stages (name TEXT PRIMARY KEY, "
    "cursor TEXT NOT NULL, done INTEGER NOT NULL)",
)
_TABLES = ("meta", "items", "pulls", "entries", "stages")
_SAFE_NAME = re.compile(r"[^a-z0-9._-]+")


def store_path(owner: str, repo: str, base: Optional[Path] = None) -> Path:
    """Dónde se guarda la descarga de `owner/repo` (GitHub no distingue
    mayúsculas: «Ana/App» y «ana/app» comparten almacén)."""
    nombre = _SAFE_NAME.sub("_", f"{owner}__{repo}".lower()).strip("._")
    return Path(base or STORE_DIR) / f"{nombre or 'repositorio'}.sqlite3"


def _dumps(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


class Store:
    """Un almacén abierto. Usar desde un solo hilo.

    `commit_every` y `clock` (segundos) se pueden cambiar en las pruebas.
    """

    def __init__(self, path: Path, identity: Dict[str, object],
                 commit_every: float = COMMIT_EVERY,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), timeout=30)
        self._db.execute("PRAGMA synchronous=NORMAL")
        with self._db:
            for statement in _SCHEMA:
                self._db.execute(statement)
        self.upgraded = False          # se puso al día un formato anterior
        self.resumed = self._adopt(identity)
        self._commit_every = commit_every
        self._clock = clock
        self._last_commit = clock()
        self.commits = 0               # confirmaciones (para las pruebas)

    # ---- identidad ------------------------------------------------------
    def _adopt(self, identity: Dict[str, object]) -> bool:
        """True si lo guardado es de ESTE volcado (se retoma); si no, se
        vacía y se apunta la identidad nueva."""
        row = self._db.execute(
            "SELECT value FROM meta WHERE key = 'identity'").fetchone()
        wanted = _dumps(identity)
        if row is not None and row[0] == wanted:
            return self.has_progress()
        if row is not None and self._upgrade(row[0], identity):
            return self.has_progress()
        with self._db:
            for table in _TABLES:
                self._db.execute(f"DELETE FROM {table}")
            self._db.execute("INSERT INTO meta (key, value) VALUES "
                             "('identity', ?)", (wanted,))
        return False

    def _upgrade(self, stored: str, identity: Dict[str, object]) -> bool:
        """Si lo guardado es de ESTE volcado pero de un formato anterior
        que se puede poner al día (`UPGRADES`), borra lo que ese formato
        guardaba mal (para volver a pedirlo), apunta la identidad nueva y
        devuelve True. Todo en una transacción: o queda al día o como
        estaba."""
        try:
            antes = json.loads(stored)
        except ValueError:
            return False
        if not isinstance(antes, dict) or identity.get("format") != FORMAT:
            return False
        formato = antes.get("format")
        if not isinstance(formato, int) or isinstance(formato, bool):
            return False               # identidad rara: se empieza de cero
        arreglo = UPGRADES.get(formato)
        resto_antes = {k: v for k, v in antes.items() if k != "format"}
        resto_ahora = {k: v for k, v in identity.items() if k != "format"}
        if arreglo is None or resto_antes != resto_ahora:
            return False
        tipos, prefijos = arreglo
        with self._db:
            for tipo in tipos:
                self._db.execute("DELETE FROM entries WHERE kind = ?", (tipo,))
            for prefijo in prefijos:
                self._db.execute("DELETE FROM stages WHERE name LIKE ? "
                                 "ESCAPE '\\'", (_like_prefix(prefijo),))
            self._db.execute("INSERT OR REPLACE INTO meta (key, value) "
                             "VALUES ('identity', ?)", (_dumps(identity),))
        self.upgraded = True
        return True

    def has_progress(self) -> bool:
        return self._db.execute("SELECT 1 FROM stages LIMIT 1").fetchone() \
            is not None

    def set_meta(self, key: str, value: object) -> None:
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO meta (key, value) "
                             "VALUES (?, ?)", (key, _dumps(value)))

    def meta(self, key: str, default: object = None) -> object:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?",
                               (key,)).fetchone()
        return json.loads(row[0]) if row is not None else default

    # ---- progreso ---------------------------------------------------------
    def stage(self, name: str) -> Tuple[str, bool]:
        """(cursor, terminada) de una parte de la descarga."""
        row = self._db.execute("SELECT cursor, done FROM stages WHERE "
                               "name = ?", (name,)).fetchone()
        return (row[0], bool(row[1])) if row is not None else ("", False)

    def done_stages(self, prefix: str) -> set:
        """Los nombres terminados que empiezan por `prefix`."""
        rows = self._db.execute("SELECT name FROM stages WHERE done = 1 "
                                "AND name LIKE ? ESCAPE '\\'",
                                (_like_prefix(prefix),))
        return {name for (name,) in rows}

    def save(self, stage: str, cursor: str = "", done: bool = False,
             items: Iterable[Tuple[int, Dict[str, object]]] = (),
             new_items: Iterable[Tuple[int, Dict[str, object]]] = (),
             pulls: Iterable[Tuple[int, Dict[str, object]]] = (),
             entries: Iterable[Entry] = ()) -> None:
        """Guarda una página y por dónde va su parte, en la MISMA
        transacción (se confirman juntas: ver `COMMIT_EVERY`).

        `items` sustituye lo que hubiera; `new_items` solo añade los que
        aún no estaban (los pull requests que llegan de su listado no
        pisan lo que ya trajo el de issues, que es más completo).
        """
        try:
            self._db.executemany(
                "INSERT OR REPLACE INTO items (number, data) VALUES (?, ?)",
                [(n, _dumps(d)) for n, d in items])
            self._db.executemany(
                "INSERT OR IGNORE INTO items (number, data) VALUES (?, ?)",
                [(n, _dumps(d)) for n, d in new_items])
            self._db.executemany(
                "INSERT OR REPLACE INTO pulls (number, data) VALUES (?, ?)",
                [(n, _dumps(d)) for n, d in pulls])
            self._db.executemany(
                "INSERT OR REPLACE INTO entries (kind, id, number, at, data) "
                "VALUES (?, ?, ?, ?, ?)",
                [(k, str(i), n, at, _dumps(d))
                 for k, i, n, at, d in entries])
            self._db.execute(
                "INSERT OR REPLACE INTO stages (name, cursor, done) VALUES "
                "(?, ?, ?)", (stage, cursor, 1 if done else 0))
        except BaseException:
            self._db.rollback()        # ni la página ni su cursor
            raise
        self._maybe_commit()

    def save_many(self, stages: Iterable[Tuple[str, str, bool]],
                  entries: Iterable[Entry] = ()) -> None:
        """Varias partes terminadas (con sus entradas) en la MISMA
        transacción: lo que llega de un lote de GraphQL queda entero o no
        queda."""
        try:
            self._db.executemany(
                "INSERT OR REPLACE INTO entries (kind, id, number, at, data) "
                "VALUES (?, ?, ?, ?, ?)",
                [(k, str(i), n, at, _dumps(d)) for k, i, n, at, d in entries])
            self._db.executemany(
                "INSERT OR REPLACE INTO stages (name, cursor, done) VALUES "
                "(?, ?, ?)",
                [(name, cursor, 1 if done else 0)
                 for name, cursor, done in stages])
        except BaseException:
            self._db.rollback()
            raise
        self._maybe_commit()

    def _maybe_commit(self) -> None:
        if self._clock() - self._last_commit >= self._commit_every:
            self.flush()

    def flush(self) -> None:
        """Confirma lo guardado hasta ahora (queda en el disco)."""
        if self._db.in_transaction:
            self._db.commit()
            self.commits += 1
        self._last_commit = self._clock()

    # ---- lectura ----------------------------------------------------------
    def numbers(self) -> List[int]:
        return [n for (n,) in self._db.execute(
            "SELECT number FROM items ORDER BY number")]

    def item(self, number: int) -> Dict[str, object]:
        row = self._db.execute("SELECT data FROM items WHERE number = ?",
                               (number,)).fetchone()
        return json.loads(row[0]) if row is not None else {}

    def pull(self, number: int) -> Dict[str, object]:
        row = self._db.execute("SELECT data FROM pulls WHERE number = ?",
                               (number,)).fetchone()
        return json.loads(row[0]) if row is not None else {}

    def pull_numbers(self) -> List[int]:
        """Los pull requests: los que trajo su listado y los que el de
        issues marcó como tales."""
        numeros = {n for (n,) in self._db.execute("SELECT number FROM pulls")}
        for number, data in self._db.execute("SELECT number, data FROM "
                                             "items"):
            if json.loads(data).get("kind") == "pr":
                numeros.add(number)
        return sorted(numeros)

    def entries_for(self, number: int) -> List[Tuple[str, Dict[str, object]]]:
        """(tipo, datos) de todo lo que pasó en un issue/PR, por fecha."""
        rows = self._db.execute(
            "SELECT kind, data FROM entries WHERE number = ? "
            "ORDER BY at, kind, id", (number,))
        return [(kind, json.loads(data)) for kind, data in rows]

    def count(self, kind: str) -> int:
        return self._db.execute("SELECT COUNT(*) FROM entries WHERE "
                                "kind = ?", (kind,)).fetchone()[0]

    def items_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    # ---- cierre -------------------------------------------------------------
    def close(self) -> None:
        """Confirma lo pendiente y cierra (lo descargado queda para la
        próxima vez)."""
        try:
            self.flush()
        except sqlite3.Error:
            pass
        try:
            self._db.close()
        except sqlite3.Error:
            pass

    def remove(self) -> None:
        """Cierra y borra el almacén (y su diario, si quedó). Lo pendiente
        no se confirma: se va a borrar."""
        try:
            self._db.rollback()
        except sqlite3.Error:
            pass
        self.close()
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                os.unlink(str(self.path) + suffix)
            except OSError:
                pass


def _like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%") \
        .replace("_", "\\_")
    return escaped + "%"


def open_store(owner: str, repo: str, identity: Dict[str, object],
               base: Optional[Path] = None) -> Store:
    """Abre (o crea) el almacén de `owner/repo` para `identity`."""
    return Store(store_path(owner, repo, base), identity)


@dataclass(frozen=True)
class Partial:
    """Una descarga a medias que hay en disco."""
    identity: Dict[str, object]
    items: int                 # issues y PR ya guardados
    stages: int                # partes de la descarga con progreso


def peek(owner: str, repo: str, base: Optional[Path] = None
         ) -> Optional[Partial]:
    """¿Hay una descarga a medias de `owner/repo`? SOLO LEE: abre la base
    en modo de solo lectura (abrirla con `Store` y otra identidad la
    vaciaría). None si no hay, si no tiene progreso o si no se puede leer
    ahora (por ejemplo, otra descarga está escribiendo en ella)."""
    ruta = store_path(owner, repo, base)
    if not ruta.is_file():
        return None
    uri = ruta.resolve().as_uri() + "?mode=ro"
    try:
        db = sqlite3.connect(uri, uri=True, timeout=2)
    except sqlite3.Error:
        return None
    try:
        fila = db.execute("SELECT value FROM meta WHERE key = 'identity'"
                          ).fetchone()
        etapas = db.execute("SELECT COUNT(*) FROM stages").fetchone()[0]
        items = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        identity = json.loads(fila[0]) if fila is not None else None
    except (sqlite3.Error, ValueError, TypeError):
        return None
    finally:
        db.close()
    if not isinstance(identity, dict) or not etapas:
        return None
    return Partial(identity=identity, items=int(items), stages=int(etapas))
