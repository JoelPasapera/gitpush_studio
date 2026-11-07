# -*- coding: utf-8 -*-
"""Versiones: ver el historial, volver a una versión anterior sin
reescribir nada, recuperar un respaldo, y describir cambios para el
mensaje del commit."""

import re
from collections import Counter
from dataclasses import dataclass

from .errors import StepError
from .git import Git
from .safekeeping import (BACKUP_PREFIX, FolderGuard, backup as make_backup,
                          files_at_risk, remove_verified,
                          require_branch_checked_out, require_quiet_repo,
                          restore_snapshot, tracked_index, tree_entries,
                          untracked)

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


def _resolve(git, folder, ref):
    """Sha completo del commit `ref`, o StepError si no existe."""
    if not ref or str(ref).startswith("-"):
        raise StepError("Esa versión no existe en este repositorio.")
    p = git.run(["rev-parse", "--verify", "-q", "--end-of-options",
                 f"{ref}^{{commit}}"], cwd=folder, check=False, echo=False)
    if p.returncode != 0:
        # git < 2.24 no conoce --end-of-options; `ref` ya no empieza por «-».
        p = git.run(["rev-parse", "--verify", "-q", f"{ref}^{{commit}}"],
                    cwd=folder, check=False, echo=False)
    full = (p.stdout or "").strip()
    if p.returncode != 0 or not full:
        raise StepError("Esa versión no existe en este repositorio.")
    return full


def _with_backup(msg, backup):
    if backup:
        msg += f" Lo que tenías antes está completo en la rama «{backup}»."
    return msg


def restore_version(folder, sha, git=None, log=None, identity=None):
    """Deja la carpeta como estaba en `sha` creando un commit NUEVO
    (el historial no se reescribe; después basta con publicar).

    Antes, una copia EXACTA de la carpeta (también lo no publicado y los
    archivos ignorados que se van a pisar) queda en una rama de respaldo
    que no forma parte de lo que se publica. El commit nuevo se prepara
    antes de tocar ningún archivo: si git no puede crearlo, no cambia
    nada. Los archivos sin rastrear que no estaban en esa versión se
    quitan solo si están en la copia, comprobado byte a byte.
    """
    git = git or Git(log=log)
    say = log or (lambda text, tag="dim": None)
    backup = ""
    try:
        if not git.is_repo(folder) or not git.has_commits(folder):
            raise StepError("La carpeta no tiene versiones todavía.")
        require_quiet_repo(folder, git)
        require_branch_checked_out(git, folder)
        guardia = FolderGuard(git, folder)
        full = _resolve(git, folder, sha)
        short = full[:7]
        head = git.sha(folder)
        arbol = git.sha(folder, full + "^{tree}")
        mismo = arbol == git.sha(folder, "HEAD^{tree}")
        locales = git.pending_changes(folder)
        en_riesgo = files_at_risk(git, folder, full)
        if mismo and not locales and not en_riesgo:
            say("  La carpeta ya estaba exactamente así.", "dim")
            return RestoreResult("ok", "La carpeta ya estaba exactamente "
                                 "en esa versión.", "", short)
        # Se descarta cualquier cambio en lo rastreado y se escribe la
        # versión entera: todo eso queda vigilado hasta el final.
        guardia.watch(tracked_index(git, folder))
        guardia.watch(tree_entries(git, folder, full))
        guardia.watch(en_riesgo)
        nuevo = ""
        if not mismo:
            nuevo = git.commit_tree(folder, arbol, [head],
                                    f"Restaurar versión {short}",
                                    identity=identity)

        say("🛟 Guardando una copia exacta de tu carpeta en una rama de "
            "respaldo…", "out")
        backup = make_backup(git, folder, "antes de restaurar una versión",
                             identity=identity, at_risk=en_riesgo)
        if backup:
            say(f"  Rama de respaldo: {backup}", "dim")
        guardia.verify()
        say(f"⏪ Restaurando la versión {short}…", "out")
        git.run(["read-tree", "--reset", "-u", full], cwd=folder)
        sobran = untracked(git, folder)
        conservados = []
        if sobran:
            _, conservados = remove_verified(git, folder, backup, sobran)
        if nuevo:
            git.run(["update-ref", "-m",
                     f"GitPush Studio: restaurar versión {short}", "HEAD",
                     nuevo, head], cwd=folder, check=True, echo=False)
        git.run(["update-index", "-q", "--refresh"], cwd=folder, check=False,
                echo=False)
        if nuevo:
            msg = (f"Carpeta restaurada a la versión {short}. Pulsa "
                   "«Publicar» para que GitHub también vuelva a ella.")
        else:
            msg = ("La carpeta ya estaba en esa versión; se han descartado "
                   "los cambios sin publicar.")
        if conservados:
            msg += (f" {len(conservados)} archivo(s) tuyos no se quitaron "
                    "porque cambiaron mientras trabajaba.")
        msg = _with_backup(msg, backup)
        say("✔ " + msg, "ok")
        return RestoreResult("ok", msg, backup, short)
    except StepError as e:
        msg = _with_backup(str(e), backup)
        say("✖ " + msg, "err")
        return RestoreResult("error", msg, backup)
    except Exception as e:                                  # noqa: BLE001
        msg = _with_backup(f"Error inesperado: {e}", backup)
        say("✖ " + msg, "err")
        return RestoreResult("error", msg, backup)


def restore_backup(folder, ref, git=None, log=None, identity=None):
    """Recupera en la carpeta los archivos de un respaldo, SIN commit.

    Un respaldo puede contener lo que nunca se publicó (un .env ignorado,
    un borrador). Si se restaurara como una versión, quedaría dentro de un
    commit y llegaría a GitHub al publicar. Aquí los archivos vuelven a la
    carpeta como cambios tuyos sin publicar: lo ignorado sigue ignorado y
    lo demás pasará por la vista previa y la revisión de secretos.
    """
    git = git or Git(log=log)
    say = log or (lambda text, tag="dim": None)
    backup = ""
    try:
        if not git.is_repo(folder):
            raise StepError("La carpeta aún no es un repositorio.")
        require_quiet_repo(folder, git)
        guardia = FolderGuard(git, folder)
        full = _resolve(git, folder, ref)
        short = full[:7]
        # Se escribe archivo a archivo y nunca se borra una carpeta con
        # contenido: un repositorio anidado no corre peligro aquí.
        en_riesgo = files_at_risk(git, folder, full, strict=False)
        # Lo que se puede pisar: cada ruta del respaldo. Lo que sobra se
        # borra solo si sigue idéntico a la copia (no hace falta vigilarlo).
        guardia.watch(tree_entries(git, folder, full))
        guardia.watch(en_riesgo)
        say("🛟 Guardando una copia exacta de tu carpeta actual…", "out")
        backup = make_backup(git, folder, "antes de recuperar un respaldo",
                             identity=identity, at_risk=en_riesgo)
        if backup:
            say(f"  Rama de respaldo: {backup}", "dim")
        guardia.verify()
        say(f"⏪ Recuperando los archivos de «{ref}»…", "out")
        escritos, _, conservados = restore_snapshot(git, folder, full, backup)
        if escritos:
            msg = (f"Recuperados {len(escritos)} archivo(s) de «{ref}». "
                   "Están en tu carpeta sin publicar: GitHub no cambia hasta "
                   "que publiques, y antes pasarán por la vista previa y la "
                   "revisión.")
        else:
            msg = f"La carpeta ya tenía los archivos de «{ref}»."
        if conservados:
            msg += (f" {len(conservados)} archivo(s) no se tocaron (cambiaron "
                    "mientras trabajaba o no se podían reemplazar).")
        msg = _with_backup(msg, backup)
        say("✔ " + msg, "ok")
        return RestoreResult("ok", msg, backup, short)
    except StepError as e:
        msg = _with_backup(str(e), backup)
        say("✖ " + msg, "err")
        return RestoreResult("error", msg, backup)
    except Exception as e:                                  # noqa: BLE001
        msg = _with_backup(f"Error inesperado: {e}", backup)
        say("✖ " + msg, "err")
        return RestoreResult("error", msg, backup)


def restore(folder, ref, git=None, log=None, identity=None):
    """Restaura una versión o recupera un respaldo, según lo que sea `ref`."""
    git = git or Git(log=log)
    if str(ref).startswith(BACKUP_PREFIX) and \
            ref in set(git.local_branches(folder)):
        return restore_backup(folder, ref, git=git, log=log,
                              identity=identity)
    return restore_version(folder, ref, git=git, log=log, identity=identity)


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
