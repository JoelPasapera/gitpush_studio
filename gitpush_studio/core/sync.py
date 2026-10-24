# -*- coding: utf-8 -*-
"""Traer cambios de GitHub a la carpeta (la operación inversa a
publicar), en dos fases:

    inspect()  no toca nada: qué llegaría, qué cambios locales hay y qué
               pasaría con ellos en cada modo.
    apply()    guarda una copia EXACTA de la carpeta en una rama
               «respaldo-FECHA» (que no se publica) y trae los cambios.

Modos de `apply`:
    merge   combina: lo que solo tienes tú se queda como está, SIN commit
            (al publicar pasará por la vista previa y la revisión); donde
            GitHub y tú tocasteis el mismo archivo se juntan las líneas y,
            si chocan, gana GitHub. Si un lado borró lo que el otro
            cambió, no se adivina: no se toca nada.
    mirror  la carpeta queda EXACTAMENTE como GitHub; lo tuyo queda en la
            rama de respaldo. Los archivos ignorados que no estorban se
            quedan donde están.

Lo que se trae es siempre el commit que se vio al comprobar: el sha se
fija nada más descargarlo y, si se pasa `expected`, se exige que GitHub
no haya cambiado entre la comprobación y el momento de aplicar.
"""

import os
import time
from dataclasses import dataclass, field

from . import integrate
from .errors import StepError
from .git import (Git, auth_env, looks_like_auth_error, looks_transient)
from .refnames import require_branch
from .safekeeping import (FolderGuard, RepoInTheWay, backup as make_backup,
                          files_at_risk, remove_verified,
                          require_branch_checked_out, require_quiet_repo,
                          tracked_index, tree_entries, untracked)

MODES = ("merge", "mirror")


@dataclass
class SyncReport:
    remote_exists: bool = True
    error: str = ""                   # no se pudo consultar GitHub
    remote_sha: str = ""              # lo que se vio (para exigirlo al aplicar)
    branch: str = ""
    unborn: bool = False              # aún no hay commits locales
    up_to_date: bool = False          # ya tienes todo lo de GitHub
    identical: bool = False           # …y la carpeta es idéntica a GitHub
    # Modo combinar: lo que cambiará en la carpeta.
    added: list = field(default_factory=list)
    modified: list = field(default_factory=list)
    deleted: list = field(default_factory=list)
    combined: list = field(default_factory=list)     # tus cambios + los de GitHub
    overwritten: list = field(default_factory=list)  # gana GitHub
    conflicts: list = field(default_factory=list)    # (ruta, motivo)
    merge_unknown: bool = False       # se sabrá al aplicar (git antiguo)
    # Modo espejo: lo que cambiará respecto a tu última versión.
    mirror_added: list = field(default_factory=list)
    mirror_modified: list = field(default_factory=list)
    mirror_deleted: list = field(default_factory=list)
    local_changes: list = field(default_factory=list)   # sin commit
    local_ahead: bool = False         # commits locales que no están en GitHub
    # Por qué el modo espejo no se puede aplicar sin destruir algo que no
    # cabe en un respaldo (otro repositorio de git donde llega un
    # archivo). El modo combinar nunca borra carpetas: ahí es un choque
    # más, en `conflicts`.
    mirror_blocked: str = ""

    def blocked(self, mode):
        return self.mirror_blocked if mode == "mirror" else ""

    @property
    def incoming(self):
        return len(self.added) + len(self.modified) + len(self.deleted)

    @property
    def mirror_incoming(self):
        return (len(self.mirror_added) + len(self.mirror_modified)
                + len(self.mirror_deleted))

    @property
    def needs_backup(self):
        return bool(self.local_changes) or self.local_ahead


@dataclass
class SyncResult:
    status: str                       # ok | error
    message: str = ""
    backup_branch: str = ""
    changed: int = 0


@dataclass
class FetchResult:
    sha: str = ""
    missing: bool = False             # la rama no existe en GitHub
    error: str = ""                   # otro fallo (red, acceso, URL)


def _require_repo(git, folder):
    if not git.is_repo(folder):
        raise StepError("La carpeta aún no es un repositorio. Si el proyecto "
                        "ya está en GitHub, usa «⬇ Clonar repositorio…».")


def _fetch_error(salida):
    o = salida.lower()
    if "couldn't find remote ref" in o or "could not find remote ref" in o:
        return ""
    if looks_like_auth_error(o):
        return ("GitHub rechazó las credenciales o no tienes acceso a ese "
                "repositorio.")
    if "repository not found" in o or "does not appear to be a git" in o \
            or "not found" in o:
        return ("GitHub dice que ese repositorio no existe (o es privado y "
                "no tienes acceso). Revisa la URL.")
    if looks_transient(o):
        return ("No pude conectar con GitHub. Revisa tu conexión y vuelve a "
                "intentarlo.")
    return "No pude traer la rama de GitHub. Revisa la consola."


def fetch_branch(git, folder, url, branch, token=""):
    """Descarga `refs/heads/<branch>` de `url` y fija su sha.

    No cambia la configuración del repositorio (ni el remoto «origin» ni
    sus ramas de seguimiento): se descarga a una referencia temporal que
    se borra en cuanto se ha leído. El nombre de la rama se valida antes
    (un «-» delante sería una opción de git) y va siempre dentro de un
    refspec completo.
    """
    require_branch(branch)
    if not url or url.startswith("-"):
        raise StepError("La URL del repositorio no es válida.")
    temporal = "refs/gitpush/entrante-%d-%d" % (os.getpid(),
                                                int(time.time() * 1000))
    refspec = "+refs/heads/%s:%s" % (branch, temporal)
    args = ["fetch", "--no-tags", "--no-recurse-submodules", url, refspec]
    env = auth_env(url, token)
    p = git.run(args, cwd=folder, check=False, timeout=900, env_extra=env)
    salida = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0 and env and looks_like_auth_error(salida.lower()):
        # El token puede haber caducado: que decida el gestor de
        # credenciales de siempre.
        p = git.run(args, cwd=folder, check=False, timeout=900)
        salida = (p.stdout or "") + (p.stderr or "")
    try:
        if p.returncode != 0:
            error = _fetch_error(salida)
            return FetchResult(missing=not error, error=error)
        sha = git.sha(folder, temporal + "^{commit}")
        if not sha:
            return FetchResult(error="GitHub no devolvió un commit válido.")
        return FetchResult(sha=sha)
    finally:
        git.run(["update-ref", "-d", temporal], cwd=folder, check=False,
                echo=False)


def inspect(folder, url, branch="main", git=None, token=""):
    """Qué pasaría al traer cambios, en los dos modos. No modifica la
    carpeta (como mucho guarda objetos internos de git, invisibles)."""
    git = git or Git()
    _require_repo(git, folder)
    rep = SyncReport(branch=branch)
    f = fetch_branch(git, folder, url, branch, token)
    if f.missing:
        rep.remote_exists = False
        return rep
    if f.error:
        rep.error = f.error
        return rep
    remoto = rep.remote_sha = f.sha
    head = git.sha(folder)
    rep.unborn = not head
    rep.local_changes = git.pending_changes(folder)
    if head:
        rep.up_to_date = git.is_ancestor(folder, remoto, head)
        rep.local_ahead = not git.is_ancestor(folder, head, remoto)
    rep.identical = head == remoto and not rep.local_changes

    origen = head or integrate.empty_tree(git, folder)
    espejo = integrate.Plan(changes=integrate.diff_trees(git, folder, origen,
                                                         remoto))
    (rep.mirror_added, rep.mirror_modified,
     rep.mirror_deleted) = espejo.split()
    try:
        files_at_risk(git, folder, remoto)
    except RepoInTheWay as e:
        rep.mirror_blocked = str(e)

    plan = integrate.plan_merge(git, folder, remoto)
    rep.merge_unknown = plan.unknown
    rep.conflicts = list(plan.conflicts)
    if plan.unknown:
        # Sin merge-tree: lo que GitHub cambió desde vuestro punto común.
        p = git.run(["merge-base", head, remoto], cwd=folder, check=False,
                    echo=False)
        base = (p.stdout or "").strip() or integrate.empty_tree(git, folder)
        aprox = integrate.Plan(changes=integrate.diff_trees(git, folder,
                                                            base, remoto))
        rep.added, rep.modified, rep.deleted = aprox.split()
    else:
        rep.added, rep.modified, rep.deleted = plan.split()
        rep.combined = sorted(plan.combined)
        rep.overwritten = sorted(plan.overwritten)
    return rep


def _conflict_message(conflictos):
    ejemplos = "; ".join(f"«{r}»: {m}" for r, m in conflictos[:3])
    mas = f" (y {len(conflictos) - 3} más)" if len(conflictos) > 3 else ""
    return (f"No pude combinar automáticamente {len(conflictos)} archivo(s) "
            f"— {ejemplos}{mas}. No he tocado nada. Prueba el modo «Espejo "
            "de GitHub»: deja la carpeta igual que GitHub y guarda tu versión "
            "en una rama de respaldo.")


def apply(folder, url, branch="main", mode="merge", git=None, log=None,
          identity=None, token="", expected=""):
    """Trae los cambios. Nunca lanza: devuelve un SyncResult.

    `expected` es el sha que se enseñó al comprobar: si GitHub ya no está
    ahí, no se aplica nada (lo que se aplicaría no es lo que se vio).
    """
    git = git or Git(log=log)
    say = log or (lambda text, tag="dim": None)
    if mode not in MODES:
        return SyncResult("error", "Modo desconocido.")
    respaldo = [""]
    try:
        _require_repo(git, folder)
        require_branch(branch)
        require_quiet_repo(folder, git)
        require_branch_checked_out(git, folder)
        say("⬇⬆ Consultando GitHub…", "out")
        f = fetch_branch(git, folder, url, branch, token)
        if f.missing:
            raise StepError(f"La rama «{branch}» no existe en GitHub. "
                            "Publica primero, o elige otra rama.")
        if f.error:
            raise StepError(f.error)
        if expected and f.sha != expected:
            raise StepError("GitHub cambió desde que lo comprobaste (alguien "
                            "publicó algo nuevo). Vuelve a pulsar "
                            "«Comprobar» para ver qué llegaría ahora.")
        if mode == "mirror":
            return _apply_mirror(git, folder, f.sha, identity, say, respaldo)
        return _apply_merge(git, folder, f.sha, identity, say, respaldo)
    except StepError as e:
        msg = str(e)
        if respaldo[0]:
            msg += (f" Tu versión anterior está completa en la rama "
                    f"«{respaldo[0]}».")
        say("✖ " + msg, "err")
        return SyncResult("error", msg, respaldo[0])
    except Exception as e:                                  # noqa: BLE001
        msg = f"Error inesperado: {e}"
        if respaldo[0]:
            msg += (f" Tu versión anterior está completa en la rama "
                    f"«{respaldo[0]}».")
        say("✖ " + msg, "err")
        return SyncResult("error", msg, respaldo[0])


def _backup(git, folder, identity, at_risk, say, respaldo):
    say("🛟 Guardando una copia exacta de tu carpeta en una rama de "
        "respaldo…", "out")
    nombre = make_backup(git, folder, "antes de traer cambios de GitHub",
                         identity=identity, at_risk=at_risk)
    respaldo[0] = nombre
    if nombre:
        say(f"  Rama de respaldo: {nombre}", "dim")
    return nombre


def _apply_mirror(git, folder, remoto, identity, say, respaldo):
    guardia = FolderGuard(git, folder)
    head = git.sha(folder)
    locales = git.pending_changes(folder)
    if head == remoto and not locales:
        say("✔ Tu carpeta ya estaba igual que GitHub.", "ok")
        return SyncResult("ok", "Tu carpeta ya estaba igual que GitHub.")
    origen = head or integrate.empty_tree(git, folder)
    cambios = integrate.diff_trees(git, folder, origen, remoto)
    # El espejo descarta cualquier cambio en lo rastreado y escribe el
    # árbol entero de GitHub: todo eso queda vigilado hasta el final.
    guardia.watch(tracked_index(git, folder))
    guardia.watch(tree_entries(git, folder, remoto))
    en_riesgo = files_at_risk(git, folder, remoto)
    guardia.watch(en_riesgo)
    adelantada = bool(head) and not git.is_ancestor(folder, head, remoto)
    copia = ""
    if locales or en_riesgo or adelantada:
        copia = _backup(git, folder, identity, en_riesgo, say, respaldo)
    guardia.verify()

    say("⬇⬆ Dejando la carpeta igual que GitHub…", "out")
    git.run(["reset", "--hard", "-q", remoto], cwd=folder, check=True)
    sobran = untracked(git, folder)
    conservados = []
    if sobran:
        _, conservados = remove_verified(git, folder, copia, sobran)
    msg = (f"{len(cambios)} archivo(s) actualizados desde GitHub."
           if cambios else "Carpeta igual que GitHub.")
    if copia:
        msg += f" Tu versión anterior está en la rama «{copia}»."
    if conservados:
        msg += (f" {len(conservados)} archivo(s) tuyos no se borraron porque "
                "cambiaron mientras trabajaba (siguen en tu carpeta).")
    say("✔ " + msg, "ok")
    return SyncResult("ok", msg, copia, len(cambios))


def _apply_merge(git, folder, remoto, identity, say, respaldo):
    guardia = FolderGuard(git, folder)
    plan = integrate.plan_merge(git, folder, remoto, allow_slow=True,
                                identity=identity)
    if plan.conflicts:
        raise StepError(_conflict_message(plan.conflicts))
    if plan.nothing_incoming:
        say("✔ Tu carpeta ya estaba al día.", "ok")
        return SyncResult("ok", "Tu carpeta ya estaba al día.")
    # Solo lo que llega se toca: se vigila tal como lo vio el plan.
    guardia.expect(plan.seen)
    guardia.watch(plan.at_risk)
    locales = git.pending_changes(folder)
    adelantada = bool(plan.head) and not plan.fast_forward
    copia = ""
    if locales or plan.at_risk or adelantada:
        copia = _backup(git, folder, identity, plan.at_risk, say, respaldo)

    say("⬇⬆ Trayendo los cambios…", "out")
    integrate.execute(git, folder, plan, identity=identity, guard=guardia)
    n = len(plan.changes)
    msg = (f"{n} archivo(s) actualizados desde GitHub." if n
           else "Carpeta sincronizada con GitHub.")
    if plan.combined:
        msg += (f" En {len(plan.combined)} se juntaron tus cambios con los "
                "de GitHub (lo tuyo sigue sin publicar).")
    if plan.overwritten:
        msg += (f" En {len(plan.overwritten)} ganó la versión de GitHub.")
    if copia:
        msg += f" Tu versión anterior está en la rama «{copia}»."
    say("✔ " + msg, "ok")
    return SyncResult("ok", msg, copia, n)
