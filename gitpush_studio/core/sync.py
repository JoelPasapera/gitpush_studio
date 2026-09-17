# -*- coding: utf-8 -*-
"""Traer cambios de GitHub a la carpeta local (la operación inversa a
publicar), en dos fases:

    inspect()  no toca nada: qué llegaría y qué cambios locales hay.
    apply()    deja a salvo lo local en una rama «respaldo-FECHA» y
               trae los cambios.

Modos de `apply`:
    merge   combina: lo que solo tienes tú se conserva; donde GitHub y
            tú tocasteis lo mismo, gana GitHub (-X theirs).
    mirror  la carpeta queda EXACTAMENTE como GitHub; lo local queda en
            la rama de respaldo.
"""

from dataclasses import dataclass, field

from .errors import StepError
from .git import Git, parse_name_status_z

MODES = ("merge", "mirror")


@dataclass
class SyncReport:
    remote_exists: bool = True
    unborn: bool = False              # aún no hay commits locales
    up_to_date: bool = False
    added: list = field(default_factory=list)      # llegarán nuevos
    modified: list = field(default_factory=list)   # cambiarán
    deleted: list = field(default_factory=list)    # desaparecerán
    local_changes: list = field(default_factory=list)   # sin commit
    local_ahead: bool = False         # commits locales que no están en GitHub

    @property
    def incoming(self):
        return len(self.added) + len(self.modified) + len(self.deleted)

    @property
    def needs_backup(self):
        return bool(self.local_changes) or self.local_ahead


@dataclass
class SyncResult:
    status: str                       # ok | error
    message: str = ""
    backup_branch: str = ""
    changed: int = 0


def _require_repo(git, folder):
    if not git.is_repo(folder):
        raise StepError("La carpeta aún no es un repositorio. Si el proyecto "
                        "ya está en GitHub, usa «⬇ Clonar repositorio…».")


def _fetch(git, folder, url, branch):
    git.ensure_remote(folder, url)
    p = git.run(["fetch", "origin", branch], cwd=folder, check=False,
                timeout=900)
    return p.returncode == 0


def inspect(folder, url, branch="main", git=None, fetch=True):
    """Qué pasaría al traer cambios. No modifica la carpeta. Con
    `fetch=False` reutiliza el FETCH_HEAD de un fetch reciente."""
    git = git or Git()
    _require_repo(git, folder)
    rep = SyncReport()
    if fetch and not _fetch(git, folder, url, branch):
        rep.remote_exists = False
        return rep
    rep.local_changes = git.status_changes(folder)
    if not git.has_commits(folder):
        rep.unborn = True
        p = git.run(["ls-tree", "-r", "--name-only", "-z", "FETCH_HEAD"],
                    cwd=folder, check=False, echo=False)
        rep.added = sorted(x for x in (p.stdout or "").split("\0") if x)
        return rep
    p = git.run(["diff", "--name-status", "-z", "HEAD", "FETCH_HEAD"],
                cwd=folder, check=False, echo=False)
    rep.added, rep.modified, rep.deleted = parse_name_status_z(p.stdout)
    rep.local_ahead = not git.is_ancestor(folder, "HEAD", "FETCH_HEAD")
    rep.up_to_date = (git.is_ancestor(folder, "FETCH_HEAD", "HEAD")
                      and rep.incoming == 0)
    return rep


def apply(folder, url, branch="main", mode="merge", git=None, log=None,
          identity=None):
    """Trae los cambios. Nunca lanza: devuelve un SyncResult."""
    git = git or Git(log=log)
    say = log or (lambda text, tag="dim": None)
    if mode not in MODES:
        return SyncResult("error", "Modo desconocido.")
    try:
        _require_repo(git, folder)
        say("⬇⬆ Consultando GitHub…", "out")
        if not _fetch(git, folder, url, branch):
            raise StepError(f"La rama «{branch}» no existe en GitHub. "
                            "Publica primero, o elige otra rama.")
        rep = inspect(folder, url, branch, git, fetch=False)
        if rep.up_to_date:
            say("✔ Tu carpeta ya estaba al día.", "ok")
            return SyncResult("ok", "Tu carpeta ya estaba al día.")

        backup = ""
        if rep.needs_backup:
            say("🛟 Guardando tu versión actual en una rama de respaldo…",
                "out")
            backup = git.backup(folder, "antes de traer cambios de GitHub",
                                identity=identity)
            if backup:
                say(f"  Rama de respaldo: {backup}", "dim")

        say("⬇⬆ Trayendo los cambios…", "out")
        if mode == "mirror" or not git.has_commits(folder):
            git.run(["reset", "--hard", "FETCH_HEAD"], cwd=folder)
        else:
            p = git.run_committing(["merge", "--allow-unrelated-histories",
                                    "-X", "theirs", "--no-edit",
                                    "FETCH_HEAD"], cwd=folder,
                                   identity=identity)
            if p.returncode != 0:
                git.run(["merge", "--abort"], cwd=folder, check=False)
                extra = (f" Tu versión está intacta en la rama «{backup}»."
                         if backup else "")
                raise StepError("No pude combinar automáticamente (hay "
                                "cambios incompatibles, por ejemplo un "
                                "archivo borrado en un lado y editado en el "
                                "otro). Prueba el modo «Espejo de GitHub»."
                                + extra)
        msg = (f"{rep.incoming} archivo(s) actualizados desde GitHub."
               if rep.incoming else "Carpeta sincronizada con GitHub.")
        if backup:
            msg += f" Tu versión anterior está en la rama «{backup}»."
        say("✔ " + msg, "ok")
        return SyncResult("ok", msg, backup, rep.incoming)
    except StepError as e:
        say("✖ " + str(e), "err")
        return SyncResult("error", str(e))
    except Exception as e:                                  # noqa: BLE001
        say(f"✖ Error inesperado: {e}", "err")
        return SyncResult("error", f"Error inesperado: {e}")
