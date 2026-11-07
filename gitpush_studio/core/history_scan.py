# -*- coding: utf-8 -*-
"""Secretos en el historial: lo que ya se publicó alguna vez.

La revisión de seguridad mira la carpeta tal como está ahora. Pero un
archivo .env que se subió hace tres meses y luego se borró sigue en
GitHub: en el commit en el que entró, al alcance de cualquiera que mire
el historial. Aquí se revisa TODO lo que git guarda en el historial de
las ramas y etiquetas (lo que está publicado o se publicará), con las
mismas reglas que la revisión normal:

  · cada contenido distinto se lee una sola vez, aunque aparezca en mil
    commits (git lo guarda una vez), con un solo proceso de git;
  · por cada hallazgo se busca el commit en el que entró (cuándo y con
    qué mensaje) y si sigue en la versión actual;
  · los respaldos de la app («respaldo-…») y lo guardado con «stash» no
    se miran: son locales y nunca se publican.

Lo que dice al final es lo importante: una clave que llegó a GitHub hay
que darla por vista. Borrarla ahora no la quita de los commits viejos;
hay que cambiarla (rotarla) en el servicio al que pertenece.
"""

import os
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set, Tuple

from . import secret_rules as rules
from .errors import Cancelled, StepError
from .fs import NO_WINDOW, fmt_size, is_binary_ext
from .git import GIT_MISSING, Git
from .textenc import to_utf8

MAX_BLOB = 8 * 1024 * 1024        # más grande: no es código ni configuración
READ_BUDGET = 1024 * 1024 * 1024  # tope de lectura de una revisión
MAX_FINDINGS = 200
_EXCLUDED_REFS = ("--exclude=refs/heads/respaldo-*", "--exclude=refs/stash")
_FIELD = "\x1f"


@dataclass
class HistoryFinding:
    """Un secreto (o un archivo de claves) en el historial."""
    path: str
    reason: str
    confidence: str
    blob: str
    line: int = 0
    first_commit: str = ""
    first_date: str = ""
    first_subject: str = ""
    still_present: bool = False

    def where(self) -> str:
        """«entró en a1b2c3d (2026-03-01: Añadir conexión)»."""
        if not self.first_commit:
            return "en el historial"
        subject = self.first_subject
        if len(subject) > 60:
            subject = subject[:57] + "…"
        return (f"entró en {self.first_commit} ({self.first_date}: "
                f"«{subject}»)")


@dataclass
class HistoryReport:
    findings: List[HistoryFinding]
    commits: int = 0
    blobs: int = 0
    read: int = 0
    skipped: int = 0
    truncated: bool = False
    incomplete: bool = False
    seconds: float = 0.0


def _objects(git: Git, folder: str) -> List[Tuple[str, str]]:
    """(sha, ruta) de cada contenido alcanzable desde ramas y etiquetas
    (salvo respaldos y stash). Cada objeto sale una vez."""
    process = git.run(["rev-list", "--objects", *_EXCLUDED_REFS, "--all"],
                      cwd=folder, check=False, echo=False, binary=True,
                      timeout=1800)
    if process.returncode != 0:
        raise StepError("No pude leer el historial del repositorio.")
    out: List[Tuple[str, str]] = []
    for line in (process.stdout or b"").splitlines():
        sha, _space, path = line.partition(b" ")
        if path:
            out.append((sha.decode("ascii"), os.fsdecode(path)))
    return out


def _sizes(git: Git, folder: str, shas: List[str]) -> Dict[str, Tuple[str,
                                                                    int]]:
    """{sha: (tipo, tamaño)} con un solo `cat-file --batch-check`."""
    request = b"".join(sha.encode("ascii") + b"\n" for sha in shas)
    process = git.run(["cat-file", "--batch-check"], cwd=folder,
                      check=False, echo=False, binary=True, input=request,
                      timeout=1800)
    out: Dict[str, Tuple[str, int]] = {}
    for line in (process.stdout or b"").splitlines():
        fields = line.split()
        if len(fields) == 3:
            out[fields[0].decode("ascii")] = (fields[1].decode("ascii"),
                                              int(fields[2]))
    return out


def _commit_count(git: Git, folder: str) -> int:
    process = git.run(["rev-list", "--count", *_EXCLUDED_REFS, "--all"],
                      cwd=folder, check=False, echo=False)
    try:
        return int((process.stdout or "0").strip() or 0)
    except ValueError:
        return 0


class _BlobReader:
    """`git cat-file --batch` para leer contenidos de uno en uno."""

    def __init__(self, git: Git, folder: str) -> None:
        try:
            self._process = subprocess.Popen(
                [git.exe, "cat-file", "--batch"], cwd=folder,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, creationflags=NO_WINDOW,
                env=git._env())
        except FileNotFoundError:
            raise StepError(GIT_MISSING) from None

    def read(self, sha: str) -> Optional[bytes]:
        self._process.stdin.write(sha.encode("ascii") + b"\n")
        self._process.stdin.flush()
        header = self._process.stdout.readline().split()
        if len(header) < 3 or header[-1] == b"missing":
            return None
        size = int(header[2])
        data = self._process.stdout.read(size)
        self._process.stdout.read(1)
        return data

    def close(self) -> None:
        for pipe in (self._process.stdin, self._process.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        self._process.wait()


def _first_commit(git: Git, folder: str, blob: str) -> Tuple[str, str, str]:
    """(commit corto, fecha, asunto) del commit más antiguo que introdujo
    ese contenido."""
    process = git.run(["log", *_EXCLUDED_REFS, "--all", "--reverse",
                       f"--format=%h{_FIELD}%ad{_FIELD}%s",
                       "--date=short", f"--find-object={blob}"],
                      cwd=folder, check=False, echo=False, timeout=600)
    for line in (process.stdout or "").splitlines():
        parts = line.split(_FIELD)
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
    return "", "", ""


def _current_blobs(git: Git, folder: str) -> Set[str]:
    """Los contenidos de la versión actual (HEAD)."""
    process = git.run(["ls-tree", "-r", "-z", "HEAD"], cwd=folder,
                      check=False, echo=False, binary=True)
    out: Set[str] = set()
    for item in (process.stdout or b"").split(b"\0"):
        fields = item.split(b"\t", 1)[0].split()
        if len(fields) == 3 and fields[1] == b"blob":
            out.add(fields[2].decode("ascii"))
    return out


def _line_of(text: bytes, position: int) -> int:
    return text.count(b"\n", 0, position) + 1


def scan_history(folder: str, git: Optional[Git] = None,
                 cancel=None,
                 on_progress: Optional[Callable[[int, int], None]] = None,
                 max_blob: int = MAX_BLOB, budget: int = READ_BUDGET
                 ) -> HistoryReport:
    """Revisa todo el historial de `folder`. `on_progress(hechos, total)`
    informa por el camino; si `cancel` (un threading.Event) se activa,
    lanza `Cancelled`."""
    t0 = time.time()
    if not os.path.isdir(os.path.join(folder, ".git")):
        raise StepError("La carpeta no es un repositorio de git: no hay "
                        "historial que revisar.")
    git = git or Git()
    if not git.has_commits(folder):
        return HistoryReport(findings=[], seconds=time.time() - t0)
    objects = _objects(git, folder)
    kinds = _sizes(git, folder, [sha for sha, _path in objects])
    by_blob: Dict[str, HistoryFinding] = {}
    to_read: List[Tuple[str, str, int]] = []
    skipped = 0
    for sha, path in objects:
        kind, size = kinds.get(sha, ("", 0))
        if kind != "blob":
            continue
        by_name = rules.name_rule(path)
        if by_name is not None:
            reason, confidence = by_name
            by_blob[sha] = HistoryFinding(path, reason, confidence, sha)
        inspect = rules.needs_inspection(path)
        if size <= 0 or (is_binary_ext(path) and not inspect):
            continue
        if size > max_blob:
            skipped += 1
            continue
        to_read.append((sha, path, size))

    report = HistoryReport(findings=[], blobs=len(to_read),
                           commits=_commit_count(git, folder))
    reader = _BlobReader(git, folder)
    spent = 0
    try:
        for done, (sha, path, size) in enumerate(to_read, 1):
            if cancel is not None and cancel.is_set():
                raise Cancelled("Revisión del historial detenida.")
            if spent + size > budget:
                report.incomplete = True
                skipped += len(to_read) - done + 1
                break
            raw = reader.read(sha)
            spent += size
            if on_progress is not None and done % 200 == 0:
                on_progress(done, len(to_read))
            if raw is None:
                continue
            report.read += 1
            text = to_utf8(raw)
            if not text:
                continue
            hit = rules.find_secrets(text, path.lower())
            if hit is None:
                continue
            reason, confidence, position = hit
            if confidence == rules.BAJA and sha in by_blob:
                continue
            by_blob[sha] = HistoryFinding(path, reason, confidence, sha,
                                          line=_line_of(text, position))
    finally:
        reader.close()
    report.skipped = skipped

    findings = sorted(by_blob.values(),
                      key=lambda f: (rules.RANK[f.confidence], f.path))
    findings = [f for f in findings if f.confidence != rules.BAJA] + \
        [f for f in findings if f.confidence == rules.BAJA]
    if len(findings) > MAX_FINDINGS:
        findings = findings[:MAX_FINDINGS]
        report.truncated = True
    current = _current_blobs(git, folder)
    for finding in findings:
        if cancel is not None and cancel.is_set():
            raise Cancelled("Revisión del historial detenida.")
        finding.still_present = finding.blob in current
        if finding.confidence != rules.BAJA:
            commit, date, subject = _first_commit(git, folder, finding.blob)
            finding.first_commit = commit
            finding.first_date = date
            finding.first_subject = subject
    report.findings = findings
    if on_progress is not None:
        on_progress(len(to_read), len(to_read))
    report.seconds = time.time() - t0
    return report


ROTATE_ADVICE = (
    "Una clave o contraseña que llegó a GitHub hay que darla por vista, "
    "aunque el repositorio sea privado o la borres ahora: sigue en los "
    "commits antiguos. Lo seguro es cambiarla (rotarla) en el servicio al "
    "que pertenece y después dejar el archivo fuera de la publicación.")


def report_lines(report: HistoryReport) -> List[Tuple[str, str]]:
    """El informe redactado: (texto, etiqueta) para la consola o la
    terminal."""
    lines: List[Tuple[str, str]] = [(
        f"Revisados {report.commits} commits: {report.read} contenidos "
        f"distintos leídos en {report.seconds:.1f} s.", "dim")]
    if report.skipped:
        lines.append((f"⚠ {report.skipped} contenido(s) no se revisaron por "
                      f"dentro (más de {fmt_size(MAX_BLOB)} o sin tiempo).",
                      "warn"))
    serious = [f for f in report.findings if f.confidence != rules.BAJA]
    if not serious:
        lines.append(("✔ Ningún secreto a la vista en el historial.", "ok"))
        return lines
    lines.append((f"⚠ {len(serious)} hallazgo(s) en el historial:", "warn"))
    for finding in serious:
        state = ("sigue en la versión actual" if finding.still_present
                 else "ya no está en la versión actual, pero sí en el "
                      "historial")
        where = f"línea {finding.line}, " if finding.line else ""
        lines.append((f"  · {finding.path} — {finding.reason} ({where}"
                      f"{finding.where()}; {state})", "warn"))
    lines.append(("  " + ROTATE_ADVICE, "dim"))
    if report.truncated:
        lines.append((f"⚠ Hay más de {MAX_FINDINGS}: se muestran los más "
                      "graves.", "warn"))
    return lines
