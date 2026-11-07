# -*- coding: utf-8 -*-
"""Lo descargado de GitHub, guardado en disco mientras dura el volcado.

Volcar un repositorio grande son cientos o miles de consultas: GitHub
puede pedir una hora de espera a mitad, la conexión puede caerse o el
usuario puede pulsar «Detener». Nada de eso hace perder lo descargado:
cada página se guarda aquí, junto con el punto por el que va cada
listado, en UNA transacción (o todo o nada: nunca una página a medias
con el cursor ya adelantado). Al volver a empezar se sigue desde ahí.

Es una base SQLite (de la biblioteca estándar) en
`~/.gitpush_studio/historiales/`, con lo justo de cada cosa (no la
respuesta entera de GitHub). Además, el TXT se escribe leyendo de aquí
issue a issue, sin cargar el repositorio entero en memoria. Cuando el
TXT queda escrito, la base se borra: no quedan copias escondidas.

Tablas:
    meta     identidad del volcado (formato, repositorio, opciones): si
             no coincide con la que se pide, se empieza de cero
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
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .config import CONFIG_DIR

STORE_DIR = CONFIG_DIR / "historiales"
# Cambia si cambia lo que se guarda: un almacén de otro formato se
# descarta en vez de mezclarse.
FORMAT = 1

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
    """Un almacén abierto. Usar desde un solo hilo."""

    def __init__(self, path: Path, identity: Dict[str, object]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), timeout=30)
        self._db.execute("PRAGMA synchronous=NORMAL")
        with self._db:
            for statement in _SCHEMA:
                self._db.execute(statement)
        self.resumed = self._adopt(identity)

    # ---- identidad ------------------------------------------------------
    def _adopt(self, identity: Dict[str, object]) -> bool:
        """True si lo guardado es de ESTE volcado (se retoma); si no, se
        vacía y se apunta la identidad nueva."""
        row = self._db.execute(
            "SELECT value FROM meta WHERE key = 'identity'").fetchone()
        wanted = _dumps(identity)
        if row is not None and row[0] == wanted:
            return self.has_progress()
        with self._db:
            for table in _TABLES:
                self._db.execute(f"DELETE FROM {table}")
            self._db.execute("INSERT INTO meta (key, value) VALUES "
                             "('identity', ?)", (wanted,))
        return False

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
        """Guarda una página y por dónde va su parte, en UNA transacción.

        `items` sustituye lo que hubiera; `new_items` solo añade los que
        aún no estaban (los pull requests que llegan de su listado no
        pisan lo que ya trajo el de issues, que es más completo).
        """
        with self._db:
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
        try:
            self._db.close()
        except sqlite3.Error:
            pass

    def remove(self) -> None:
        """Cierra y borra el almacén (y su diario, si quedó)."""
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
