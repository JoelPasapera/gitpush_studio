# -*- coding: utf-8 -*-
"""Versiones: ver el historial, volver a una versión anterior sin
reescribir nada, y describir cambios para el mensaje del commit."""

import re
from collections import Counter
from dataclasses import dataclass

from .errors import StepError
from .git import Git

_STAT_RX = re.compile(r"(\d+) files? changed"
                      r"(?:, (\d+) insertions?\(\+\))?"
                      r"(?:, (\d+) deletions?\(-\))?")
_LOG_FORMAT = "%x1e%H%x1f%h%x1f%ad%x1f%an%x1f%s%x1f%P"
MERGE_SUBJECT = "Combinación con lo que había en GitHub"
# for-each-ref escapa con %xx (dos dígitos hex), no con %xNN como git log
_REF_FORMAT = ("%(refname:short)%1f%(objectname)%1f%(objectname:short)"
               "%1f%(committerdate:format:%Y-%m-%d %H:%M)%1f%(subject)")


@dataclass
class Version:
    sha: str
    short: str
    date: str
    author: str
    subject: str
    files: int = 0
    insertions: int = 0
    deletions: int = 0
    branch: str = ""          # nombre de la rama de respaldo, si lo es
    is_merge: bool = False

    @property
    def is_backup(self):
        return bool(self.branch)


@dataclass
class RestoreResult:
    status: str               # ok | error
    message: str = ""
    backup_branch: str = ""
    commit_short: str = ""


def list_versions(folder, limit=30, git=None):
    """Versiones de la rama actual (la más reciente primero) seguidas de
    las ramas de respaldo que haya."""
    git = git or Git()
    if not git.is_repo(folder) or not git.has_commits(folder):
        return []
    p = git.run(["log", f"-n{limit}", "--date=format:%Y-%m-%d %H:%M",
                 f"--format={_LOG_FORMAT}", "--shortstat"], cwd=folder,
                check=False, echo=False)
    out = []
    for chunk in (p.stdout or "").split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        head, _, rest = chunk.partition("\n")
        fields = head.split("\x1f")
        if len(fields) < 5:
            continue
        v = Version(*fields[:5])
        if len(fields) > 5 and len(fields[5].split()) > 1:
            # Los merges que crea la app tienen asuntos técnicos
            # («Merge branch 'main' of …»): se muestran con su sentido.
            v.is_merge = True
            v.subject = MERGE_SUBJECT
        m = _STAT_RX.search(rest)
        if m:
            v.files = int(m.group(1))
            v.insertions = int(m.group(2) or 0)
            v.deletions = int(m.group(3) or 0)
        out.append(v)
    p = git.run(["for-each-ref", f"--format={_REF_FORMAT}",
                 "--sort=-committerdate", "refs/heads/respaldo-*"],
                cwd=folder, check=False, echo=False)
    for line in (p.stdout or "").splitlines():
        fields = line.split("\x1f")
        if len(fields) == 5:
            name, sha, short, date, subject = fields
            out.append(Version(sha, short, date, "", subject, branch=name))
    return out


def restore_version(folder, sha, git=None, log=None, identity=None):
    """Deja la carpeta como estaba en `sha` creando un commit NUEVO
    (el historial no se reescribe; después basta con publicar). Antes,
    el estado actual queda en una rama de respaldo."""
    git = git or Git(log=log)
    say = log or (lambda text, tag="dim": None)
    try:
        if not git.is_repo(folder) or not git.has_commits(folder):
            raise StepError("La carpeta no tiene versiones todavía.")
        p = git.run(["rev-parse", "--verify", "-q", f"{sha}^{{commit}}"],
                    cwd=folder, check=False, echo=False)
        if p.returncode != 0:
            raise StepError("Esa versión no existe en este repositorio.")
        full = (p.stdout or "").strip()
        short = full[:7]
        say("🛟 Guardando tu versión actual en una rama de respaldo…", "out")
        backup = git.backup(folder, "antes de restaurar una versión",
                            identity=identity)
        say(f"⏪ Restaurando la versión {short}…", "out")
        git.run(["read-tree", "--reset", "-u", full], cwd=folder)
        p = git.run_committing(["commit", "-q", "-m",
                                f"Restaurar versión {short}"], cwd=folder,
                               identity=identity)
        if p.returncode != 0:
            out = ((p.stdout or "") + (p.stderr or "")).lower()
            if "nothing to commit" in out or "no changes" in out:
                say("  La carpeta ya estaba exactamente así.", "dim")
                return RestoreResult("ok", "La carpeta ya estaba exactamente "
                                     "en esa versión.", backup, short)
            raise StepError("No pude crear el commit de restauración. "
                            "Revisa la consola.")
        msg = (f"Carpeta restaurada a la versión {short}. Pulsa «Publicar» "
               "para que GitHub también vuelva a ella.")
        if backup:
            msg += f" Lo que tenías antes está en la rama «{backup}»."
        say("✔ " + msg, "ok")
        return RestoreResult("ok", msg, backup, short)
    except StepError as e:
        say("✖ " + str(e), "err")
        return RestoreResult("error", str(e))
    except Exception as e:                                  # noqa: BLE001
        say(f"✖ Error inesperado: {e}", "err")
        return RestoreResult("error", f"Error inesperado: {e}")


# ------------------------------------------------------------------ #
#  Mensaje de commit automático                                       #
# ------------------------------------------------------------------ #
_KINDS = (
    ("imagen", "imágenes",
     {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico"}),
    ("estilo", "estilos", {".css", ".scss", ".sass", ".less"}),
    ("script", "scripts", {".js", ".ts", ".jsx", ".tsx", ".mjs"}),
    ("página", "páginas", {".html", ".htm"}),
    ("archivo Python", "archivos Python", {".py"}),
    ("documento", "documentos", {".md", ".txt", ".rst"}),
    ("archivo de datos", "archivos de datos",
     {".json", ".csv", ".xml", ".yml", ".yaml", ".toml"}),
)
SUBJECT_MAX = 72


def _top_folders(paths, limit=2):
    c = Counter(p.split("/", 1)[0] + "/" for p in paths if "/" in p)
    return [name for name, _ in c.most_common(limit)]


def _ext(path):
    name = path.rsplit("/", 1)[-1]
    return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name[1:] else ""


def _phrase(verb, paths):
    """«añade 3 imágenes» si el 70 % o más son de un mismo tipo;
    si no, «añade 3 archivos»."""
    n = len(paths)
    exts = Counter(_ext(p) for p in paths)
    for singular, plural, group in _KINDS:
        if sum(cnt for ext, cnt in exts.items() if ext in group) >= n * 0.7:
            return f"{verb} {n} {singular if n == 1 else plural}"
    return f"{verb} {n} {'archivo' if n == 1 else 'archivos'}"


def describe_changes(added, modified, deleted):
    """(asunto, cuerpo) para el commit a partir de lo que cambió.

    Asunto: «Añade 2 imágenes y actualiza 3 archivos en src/ y docs/».
    Cuerpo: hasta 20 rutas marcadas con +, ~ y -.
    """
    added, modified, deleted = list(added), list(modified), list(deleted)
    if not (added or modified or deleted):
        return "", ""
    parts = []
    if added:
        parts.append(_phrase("añade", added))
    if modified:
        parts.append(_phrase("actualiza", modified))
    if deleted:
        parts.append(_phrase("elimina", deleted))
    if len(parts) > 1:
        body_parts = ", ".join(parts[:-1]) + " y " + parts[-1]
    else:
        body_parts = parts[0]
    subject = body_parts[0].upper() + body_parts[1:]
    folders = _top_folders(added + modified + deleted)
    if folders:
        with_folders = subject + " en " + " y ".join(folders)
        if len(with_folders) <= SUBJECT_MAX:
            subject = with_folders
    if len(subject) > SUBJECT_MAX:
        subject = subject[:SUBJECT_MAX - 1].rstrip() + "…"
    lines = ([f"+ {p}" for p in added] + [f"~ {p}" for p in modified]
             + [f"- {p}" for p in deleted])
    body = "\n".join(lines[:20])
    if len(lines) > 20:
        body += f"\n… y {len(lines) - 20} más"
    return subject, body
