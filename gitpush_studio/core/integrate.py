# -*- coding: utf-8 -*-
"""Traer a la carpeta lo que cambió en GitHub SIN hacer commit de lo tuyo
(el modo «Combinar» de «Traer cambios»).

Antes se hacía un commit con todo lo de la carpeta y luego `git merge`.
Ese commit iba a la rama y, al publicar, subía a GitHub archivos que
nunca pasaron por la vista previa ni por la revisión de secretos. Ahora:

  1. Se calcula el resultado SIN tocar la carpeta: si tu rama no tiene
     nada propio, el resultado es lo de GitHub (avance rápido); si las
     dos historias se separaron, `git merge-tree` las combina en memoria
     (con «-X theirs»: donde chocan, gana GitHub).
  2. Para cada archivo que llega se mira qué tienes tú en el disco:
       · no lo tocaste            → se pone la versión nueva;
       · lo cambiaste             → se combinan tus cambios y los de
         GitHub línea a línea (`git merge-file --theirs`); si es binario,
         gana GitHub. Tus cambios que sobreviven siguen SIN commit;
       · lo borraste y GitHub lo cambió, o lo cambiaste y GitHub lo
         borró → no se adivina: se para sin tocar nada.
  3. Solo entonces se escribe: primero la copia de seguridad (la hace
     quien llama), luego los archivos, y al final se mueve la rama.

Lo que no llega de GitHub no se toca: tus cambios en otros archivos y
tus archivos nuevos siguen exactamente igual, sin commit.
"""

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field

from .errors import StepError
from .gitobj import null_oid
from .safekeeping import (files_at_risk, git_dir, prune_empty_dirs,
                          stat_signature)

MERGE_MESSAGE = "Combinación con lo que había en GitHub"
ABSENT = "000000"
GITLINK = "160000"
SYMLINK = "120000"
_BINARY_PROBE = 8000
_BATCH = 2000
# Por encima de esto no se combina línea a línea (gana GitHub entero):
# leer tres versiones de un archivo enorme en memoria no compensa.
MAX_TEXT_MERGE = 32 * 1024 * 1024


@dataclass
class Change:
    """Un archivo que cambia entre tu versión y el resultado."""
    path: str
    old_mode: str
    old_sha: str
    new_mode: str
    new_sha: str

    @property
    def added(self):
        return self.old_mode == ABSENT

    @property
    def deleted(self):
        return self.new_mode == ABSENT

    @property
    def gitlink(self):
        return GITLINK in (self.old_mode, self.new_mode)


@dataclass
class Plan:
    head: str = ""              # tu commit ("" si la rama no tiene ninguno)
    remote: str = ""            # el commit de GitHub
    tree: str = ""              # el árbol resultante ("" si no se sabe aún)
    fast_forward: bool = True   # la rama solo avanza hasta `remote`
    merge_commit: str = ""      # ya creado (solo con el método lento)
    changes: list = field(default_factory=list)     # [Change]
    write: list = field(default_factory=list)       # rutas a escribir
    remove: list = field(default_factory=list)      # rutas a borrar
    combined: dict = field(default_factory=dict)    # ruta → sha combinado
    overwritten: list = field(default_factory=list)  # gana GitHub
    conflicts: list = field(default_factory=list)   # (ruta, motivo)
    at_risk: list = field(default_factory=list)     # sin rastrear que se pisan
    unknown: bool = False       # sin `merge-tree`: se sabrá al aplicar
    # Cómo estaba en disco cada ruta que llega cuando se miró (firma de
    # lstat; None si no existía): al aplicar se exige que siga igual.
    seen: dict = field(default_factory=dict)

    @property
    def nothing_incoming(self):
        return not self.changes and not self.unknown and (
            self.head == self.remote or not self.tree)

    def split(self):
        """(nuevos, modificados, borrados) de lo que llega."""
        nuevos = sorted(c.path for c in self.changes if c.added)
        borrados = sorted(c.path for c in self.changes if c.deleted)
        cambiados = sorted(c.path for c in self.changes
                           if not c.added and not c.deleted)
        return nuevos, cambiados, borrados


# ------------------------------------------------------------------ #
#  Lecturas                                                           #
# ------------------------------------------------------------------ #
def empty_tree(git, folder):
    """El árbol vacío (sirve igual para SHA-1 y SHA-256)."""
    p = git.run(["hash-object", "-t", "tree", "-w", "--stdin"], cwd=folder,
                check=True, echo=False, input="")
    return (p.stdout or "").strip()


def diff_trees(git, folder, old, new):
    """[Change] entre dos árboles, sin detectar renombrados."""
    p = git.run(["diff-tree", "-r", "-z", "--no-renames", old, new],
                cwd=folder, check=True, echo=False, binary=True)
    partes = (p.stdout or b"").split(b"\0")
    out, i = [], 0
    while i + 1 < len(partes):
        meta = partes[i]
        if not meta.startswith(b":"):
            i += 1
            continue
        campos = meta[1:].decode("ascii").split()
        ruta = os.fsdecode(partes[i + 1])
        i += 2
        if len(campos) < 5:
            continue
        out.append(Change(ruta, campos[0], campos[2], campos[1], campos[3]))
    return out


def _status_paths(git, folder):
    """Rutas con cualquier diferencia frente a HEAD (índice o disco) y
    las no rastreadas no ignoradas."""
    p = git.run(["status", "--porcelain", "-z", "--untracked-files=all",
                 "--no-renames"], cwd=folder, check=True, echo=False,
                binary=True)
    rutas = set()
    for item in (p.stdout or b"").split(b"\0"):
        if len(item) > 3:
            rutas.add(os.fsdecode(item[3:]))
    return rutas


def _needs_own_call(rel):
    return "\n" in rel or "\r" in rel or rel.startswith('"')


def normalized_ids(git, folder, rutas, write=False):
    """{ruta: sha} del contenido del disco PASADO POR LOS FILTROS de git
    (finales de línea, LFS…): lo que git guardaría con `git add`. Es lo
    que hay que comparar con un commit para saber si tocaste algo."""
    out = {}
    base = ["hash-object"] + (["-w"] if write else [])
    normales = [r for r in rutas if not _needs_own_call(r)]
    for i in range(0, len(normales), _BATCH):
        lote = normales[i:i + _BATCH]
        p = git.run(base + ["--stdin-paths"], cwd=folder, check=False,
                    echo=False, binary=True,
                    input=b"\n".join(os.fsencode(r) for r in lote) + b"\n")
        shas = (p.stdout or b"").decode("ascii", "replace").split()
        if p.returncode == 0 and len(shas) == len(lote):
            out.update(zip(lote, shas))
        else:
            for r in lote:
                out[r] = _one_id(git, folder, base, r)
    for r in rutas:
        if _needs_own_call(r):
            out[r] = _one_id(git, folder, base, r)
    return out


def _one_id(git, folder, base, rel):
    p = git.run(base + ["--", rel], cwd=folder, check=False, echo=False,
                binary=True)
    sha = (p.stdout or b"").decode("ascii", "replace").strip()
    if p.returncode != 0 or not sha:
        raise StepError(f"No pude leer «{rel}» (¿está abierto en otro "
                        "programa o sin permisos?). No he tocado nada.")
    return sha


def _link_id(git, folder, rel):
    destino = os.fsencode(os.readlink(os.path.join(os.fspath(folder), rel)))
    p = git.run(["hash-object", "-w", "--stdin"], cwd=folder, check=True,
                echo=False, binary=True, input=destino)
    return (p.stdout or b"").decode("ascii").strip()


def _merge_attrs(git, folder, rutas):
    """{ruta: valor del atributo «merge»} (binary/unset = no combinar)."""
    if not rutas:
        return {}
    p = git.run(["check-attr", "-z", "--stdin", "merge"], cwd=folder,
                check=False, echo=False, binary=True,
                input=b"".join(os.fsencode(r) + b"\0" for r in rutas))
    if p.returncode != 0:
        return {}
    partes = (p.stdout or b"").split(b"\0")
    out = {}
    for i in range(0, len(partes) - 2, 3):
        out[os.fsdecode(partes[i])] = partes[i + 2].decode("ascii",
                                                            "replace")
    return out


# ------------------------------------------------------------------ #
#  Combinar historias sin tocar la carpeta                            #
# ------------------------------------------------------------------ #
def merge_tree(git, folder, head, remote):
    """(árbol, conflictos) de combinar `head` y `remote` con «-X theirs»,
    en memoria. None si este git no sabe hacerlo (anterior a ~2.40)."""
    p = git.run(["merge-tree", "--write-tree", "--name-only",
                 "--no-messages", "-z", "-X", "theirs",
                 "--allow-unrelated-histories", head, remote], cwd=folder,
                check=False, echo=False, binary=True)
    if p.returncode not in (0, 1):
        return None
    partes = (p.stdout or b"").split(b"\0")
    arbol = partes[0].decode("ascii", "replace").strip()
    if not arbol:
        return None
    if p.returncode == 0:
        return arbol, []
    conflictos = []
    for item in partes[1:]:
        if not item:
            break
        conflictos.append(os.fsdecode(item))
    return arbol, sorted(set(conflictos))


def merge_in_worktree(git, folder, head, remote, identity=None):
    """Lo mismo que `merge_tree` para git antiguos: combina en una copia
    de trabajo temporal FUERA de tu carpeta. Devuelve (commit,
    conflictos); el commit es "" si hubo conflictos."""
    tmp = tempfile.mkdtemp(prefix="gitpush-combinar-")
    copia = os.path.join(tmp, "w")
    sin_hooks = tempfile.mkdtemp(prefix="gitpush-sin-hooks-")
    hooks = ["-c", "core.hooksPath=" + sin_hooks]
    try:
        p = git.run(hooks + ["worktree", "add", "--detach", copia, head],
                    cwd=folder, check=False, echo=False)
        if p.returncode != 0:
            raise StepError("No pude preparar la combinación (git worktree).")
        p = git.run_committing(
            hooks + ["merge", "--no-edit", "--no-ff", "-X", "theirs",
                     "--allow-unrelated-histories", "-m", MERGE_MESSAGE,
                     remote], cwd=copia, identity=identity)
        if p.returncode != 0:
            c = git.run(["diff", "--name-only", "--diff-filter=U", "-z"],
                        cwd=copia, check=False, echo=False, binary=True)
            conflictos = sorted(os.fsdecode(x)
                                for x in (c.stdout or b"").split(b"\0") if x)
            git.run(["merge", "--abort"], cwd=copia, check=False, echo=False)
            return "", conflictos or ["(desconocido)"]
        return git.sha(copia), []
    finally:
        git.run(["worktree", "remove", "--force", copia], cwd=folder,
                check=False, echo=False)
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(sin_hooks, ignore_errors=True)
        git.run(["worktree", "prune"], cwd=folder, check=False, echo=False)


# ------------------------------------------------------------------ #
#  El plan                                                            #
# ------------------------------------------------------------------ #
def plan_merge(git, folder, remote, allow_slow=False, identity=None):
    """Qué pasaría al traer `remote` en modo combinar. No escribe en la
    carpeta (como mucho crea objetos internos de git, que no se ven).

    Con `allow_slow` y un git sin `merge-tree`, combina en una copia
    temporal (crea un commit de combinación que solo se usa al aplicar).
    """
    plan = Plan(head=git.sha(folder), remote=remote)
    head = plan.head
    if head and git.is_ancestor(folder, remote, head):
        return plan                        # ya tienes todo lo de GitHub
    if not head or git.is_ancestor(folder, head, remote):
        plan.tree = git.sha(folder, remote + "^{tree}")
    else:
        plan.fast_forward = False
        r = merge_tree(git, folder, head, remote)
        if r is None:
            if not allow_slow:
                plan.unknown = True
                return plan
            commit, conflictos = merge_in_worktree(git, folder, head, remote,
                                                   identity)
            if conflictos:
                plan.conflicts = [(c, "GitHub y tú cambiasteis esto de "
                                      "formas incompatibles") for c in conflictos]
                return plan
            plan.merge_commit = commit
            plan.tree = git.sha(folder, commit + "^{tree}")
        else:
            plan.tree, conflictos = r
            if conflictos:
                plan.conflicts = [(c, "GitHub y tú cambiasteis esto de "
                                      "formas incompatibles") for c in conflictos]
                return plan
    _classify(git, folder, plan)
    return plan


def _classify(git, folder, plan):
    """Rellena qué se escribe, qué se borra, qué se combina y qué choca,
    mirando lo que tienes en el disco."""
    raiz = os.fspath(folder)
    origen = plan.head or empty_tree(git, folder)
    plan.changes = diff_trees(git, folder, origen, plan.tree)
    if not plan.changes:
        return
    tocados = _status_paths(git, folder)
    borrados_limpios = set()

    # Estado del disco de lo que llega y que podrías haber tocado.
    revisar, enlaces, tipos = [], [], {}
    for c in plan.changes:
        ruta = os.path.join(raiz, c.path)
        try:
            info = os.lstat(ruta)
        except OSError:
            info = None
        tipos[c.path] = info
        plan.seen[c.path] = stat_signature(info)
        if info is None or c.gitlink:
            continue
        if c.path in tocados or c.added:
            if stat.S_ISLNK(info.st_mode):
                enlaces.append(c.path)
            elif stat.S_ISREG(info.st_mode):
                revisar.append(c.path)
    ids = normalized_ids(git, folder, revisar)
    for rel in enlaces:
        ids[rel] = _link_id(git, folder, rel)

    a_combinar = []
    for c in plan.changes:
        info = tipos[c.path]
        if c.gitlink:
            continue                       # solo índice: el submódulo es suyo
        tuyo_cambiado = c.path in tocados or (c.added and info is not None)
        if info is not None and stat.S_ISDIR(info.st_mode):
            if c.deleted:
                continue                   # ya no es un archivo: nada que hacer
            plan.conflicts.append((c.path, "en tu carpeta es una carpeta y en "
                                           "GitHub es un archivo"))
            continue
        if info is not None and not (stat.S_ISREG(info.st_mode)
                                     or stat.S_ISLNK(info.st_mode)):
            plan.conflicts.append((c.path, "en tu carpeta no es un archivo "
                                           "normal"))
            continue
        actual = ids.get(c.path) if info is not None else None

        if not tuyo_cambiado:
            # Está como en tu último commit: se pone lo nuevo sin más.
            if c.deleted:
                if info is not None:
                    plan.remove.append(c.path)
                    borrados_limpios.add(c.path)
            else:
                plan.write.append(c.path)
            continue

        # Lo tocaste (o hay un archivo tuyo sin rastrear en esa ruta).
        if c.deleted:
            if info is None or actual == c.old_sha:
                if info is not None:
                    plan.remove.append(c.path)
                    borrados_limpios.add(c.path)
                continue
            plan.conflicts.append((c.path, "lo cambiaste tú y en GitHub se "
                                           "borró"))
            continue
        if info is None:
            if c.added:
                plan.write.append(c.path)
            else:
                plan.conflicts.append((c.path, "lo borraste tú y en GitHub "
                                               "cambió"))
            continue
        if actual == c.new_sha:
            continue                       # ya tienes exactamente lo nuevo
        if not c.added and actual == c.old_sha:
            plan.write.append(c.path)      # tu cambio solo estaba en el índice
            continue
        if c.added or SYMLINK in (c.old_mode, c.new_mode) or \
                stat.S_ISLNK(info.st_mode) or info.st_size > MAX_TEXT_MERGE:
            plan.write.append(c.path)      # gana GitHub (tu versión: copia)
            plan.overwritten.append(c.path)
            continue
        a_combinar.append(c)

    if a_combinar:
        _combine_text(git, folder, plan, a_combinar)

    # Un archivo donde lo nuevo necesita una carpeta (salvo que ese
    # archivo se vaya a borrar justo antes, sin cambios tuyos).
    for c in plan.changes:
        if c.deleted or c.gitlink:
            continue
        partes = c.path.split("/")
        for i in range(1, len(partes)):
            tramo = "/".join(partes[:i])
            if tramo in borrados_limpios:
                continue
            try:
                info = os.lstat(os.path.join(raiz, tramo))
            except OSError:
                break
            if not stat.S_ISDIR(info.st_mode):
                plan.conflicts.append((c.path, f"«{tramo}» es un archivo en tu "
                                               "carpeta y en GitHub una "
                                               "carpeta"))
                break
    # Lo que se pisa sin estar rastreado (ignorado o nuevo tuyo): copia.
    # `strict=False`: aquí nunca se borra una carpeta (una carpeta donde
    # llega un archivo es un choque, arriba), así que un repositorio
    # anidado no corre peligro.
    plan.at_risk = files_at_risk(git, folder, plan.tree, strict=False)


def _combine_text(git, folder, plan, cambios):
    """Combina tus cambios y los de GitHub línea a línea; donde chocan,
    gana GitHub. Si alguno de los tres es binario, gana GitHub entero."""
    nuestros = normalized_ids(git, folder, [c.path for c in cambios],
                              write=True)
    atributos = _merge_attrs(git, folder, [c.path for c in cambios])
    blobs = git.read_blobs(folder, [c.old_sha for c in cambios]
                           + [c.new_sha for c in cambios]
                           + [nuestros[c.path] for c in cambios])
    tmp = tempfile.mkdtemp(prefix="gitpush-mezcla-")
    try:
        for n, c in enumerate(cambios):
            base, suyo = blobs[c.old_sha], blobs[c.new_sha]
            mio = blobs[nuestros[c.path]]
            binario = any(b"\0" in x[:_BINARY_PROBE] for x in (base, suyo, mio))
            if binario or atributos.get(c.path) in ("binary", "unset"):
                plan.write.append(c.path)
                plan.overwritten.append(c.path)
                continue
            rutas = []
            for nombre, datos in (("mio", mio), ("base", base),
                                  ("suyo", suyo)):
                r = os.path.join(tmp, "%d-%s" % (n, nombre))
                with open(r, "wb") as fh:
                    fh.write(datos)
                rutas.append(r)
            p = git.run(["merge-file", "-p", "--theirs"] + rutas, cwd=folder,
                        check=False, echo=False, binary=True)
            if p.returncode < 0 or p.returncode > 127:
                plan.write.append(c.path)
                plan.overwritten.append(c.path)
                continue
            resultado = p.stdout or b""
            if resultado == suyo:
                plan.write.append(c.path)
                plan.overwritten.append(c.path)
                continue
            h = git.run(["hash-object", "-w", "--no-filters", "--stdin"],
                        cwd=folder, check=True, echo=False, binary=True,
                        input=resultado)
            plan.combined[c.path] = (h.stdout or b"").decode("ascii").strip()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ #
#  Aplicar                                                            #
# ------------------------------------------------------------------ #
def _index_info(entradas):
    return b"".join(b"%s %s\t%s\0" % (m.encode("ascii"), s.encode("ascii"),
                                      os.fsencode(r))
                    for m, s, r in entradas)


def _attributes_first(rutas):
    """`.gitattributes` primero: decide cómo se escriben los demás."""
    return sorted(rutas, key=lambda r: (r.rsplit("/", 1)[-1]
                                        != ".gitattributes", r))


def _checkout(git, folder, rutas, env=None, update_stat=True):
    if not rutas:
        return
    args = ["checkout-index", "-f", "-z", "--stdin"]
    if update_stat:
        args.insert(1, "-u")
    git.run(args, cwd=folder, check=True, echo=False, binary=True,
            env_extra=env,
            input=b"".join(os.fsencode(r) + b"\0"
                           for r in _attributes_first(rutas)))


def execute(git, folder, plan, identity=None, guard=None):
    """Aplica un plan sin conflictos. La copia de seguridad la hace quien
    llama ANTES. Devuelve el commit al que queda apuntando la rama.

    `guard` (un `FolderGuard`) se comprueba lo más tarde posible: después
    de crear el commit (que puede esperar a una firma) y antes de tocar
    el primer archivo."""
    if plan.conflicts:
        raise StepError("Hay cambios incompatibles: no aplico nada.")
    if git.sha(folder) != plan.head:
        raise StepError("La carpeta cambió mientras preparaba la operación. "
                        "Vuelve a comprobar.")
    # 1. El commit de destino, ANTES de tocar nada: si falla (identidad,
    #    firma), la carpeta sigue intacta.
    if plan.fast_forward:
        destino = plan.remote
    elif plan.merge_commit:
        destino = plan.merge_commit
    else:
        destino = git.commit_tree(folder, plan.tree, [plan.head, plan.remote],
                                  MERGE_MESSAGE, identity=identity)
    if guard is not None:
        guard.verify()

    raiz = os.fspath(folder)
    # 2. Lo que GitHub borró y tú no tocaste.
    quitados = []
    for rel in plan.remove:
        try:
            os.unlink(os.path.join(raiz, rel))
            quitados.append(rel)
        except FileNotFoundError:
            pass
    prune_empty_dirs(folder, quitados)

    # 3. El índice: lo que llega queda como la versión nueva; lo demás
    #    (tus cambios en otros archivos) no se toca.
    nulo = null_oid(plan.remote)
    entradas = []
    for c in plan.changes:
        if c.deleted:
            entradas.append(("0", nulo, c.path))
        else:
            entradas.append((c.new_mode, c.new_sha, c.path))
    if entradas:
        git.run(["update-index", "-z", "--index-info"], cwd=folder,
                check=True, echo=False, binary=True,
                input=_index_info(entradas))

    # 4. La carpeta: versiones nuevas y, donde combinaste, el resultado.
    _checkout(git, folder, [r for r in plan.write if r not in plan.combined])
    if plan.combined:
        modos = {c.path: c.new_mode for c in plan.changes}
        _checkout_blobs(git, folder, [(modos[r], s, r)
                                      for r, s in plan.combined.items()])

    # 5. La rama, lo último.
    git.run(["update-ref", "-m", "GitPush Studio: traer cambios de GitHub",
             "HEAD", destino, plan.head or ""], cwd=folder, check=True,
            echo=False)
    git.run(["update-index", "-q", "--refresh"], cwd=folder, check=False,
            echo=False)
    return destino


def _checkout_blobs(git, folder, entradas):
    """Escribe blobs concretos con los filtros de git usando un índice
    TEMPORAL: el índice real conserva la versión de GitHub y lo tuyo
    queda como cambio sin commit."""
    gd = git_dir(git, folder)
    fd, tmp_index = tempfile.mkstemp(prefix="gitpush-indice-", dir=str(gd))
    os.close(fd)
    os.unlink(tmp_index)
    env = {"GIT_INDEX_FILE": tmp_index}
    try:
        git.run(["update-index", "-z", "--index-info"], cwd=folder,
                check=True, echo=False, binary=True, env_extra=env,
                input=_index_info(entradas))
        _checkout(git, folder, [r for _, _, r in entradas], env=env,
                  update_stat=False)
    finally:
        for resto in (tmp_index, tmp_index + ".lock"):
            try:
                os.unlink(resto)
            except OSError:
                pass
