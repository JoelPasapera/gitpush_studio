# -*- coding: utf-8 -*-
"""Copias de seguridad que no tocan NADA de lo tuyo, y comprobaciones
antes de cualquier operación que pueda pisar o borrar archivos.

Por qué existe: la copia de antes se hacía con `git add -A` + `git commit`
SOBRE LA RAMA ACTUAL y luego se le ponía nombre. Eso tenía tres efectos
que nadie había pedido:

  · el commit de respaldo quedaba en la rama, y al publicar llegaba a
    GitHub saltándose la vista previa, la revisión de secretos y las
    exclusiones (un contrato sin rastrear, un .env apartado…);
  · `add -A` respeta el .gitignore, así que un archivo ignorado que la
    operación iba a sobrescribir no se guardaba en ningún sitio;
  · `add` y `commit` pasan por los filtros de finales de línea y por los
    hooks del repositorio, así que lo guardado podía no ser idéntico a lo
    que había en el disco, y un hook podía bloquear o alterar la copia.

Ahora la copia se construye con fontanería de git, sin efectos laterales:

  1. un ÍNDICE TEMPORAL (`GIT_INDEX_FILE`): el del usuario no se toca;
  2. cada archivo se guarda con `hash-object --no-filters`: byte a byte;
  3. `commit-tree` con padre HEAD: no ejecuta hooks, no firma y no mueve
     ninguna rama;
  4. una rama «respaldo-FECHA» apunta a ese commit. Solo ella: lo que se
     publica (la rama actual) no lo contiene.

Entran los archivos rastreados que hay en disco, los no rastreados que no
están ignorados y, además, los que la operación concreta va a PISAR
aunque estén ignorados (`files_at_risk`).

Las rutas viajan como bytes entre git y el disco (`os.fsdecode` /
`os.fsencode`): un nombre que no es UTF-8 tiene que poder guardarse y,
sobre todo, no puede borrarse sin haberse guardado.

Y nada se borra «porque está en la copia»: antes de borrar se vuelve a
calcular, con una implementación propia (`gitobj`), el identificador
del contenido que hay en el disco, y solo se borra si coincide con lo
guardado.
"""

import os
import stat
import tempfile
import time
from pathlib import Path

from .errors import StepError
from .gitobj import algorithm_for, file_blob_id

# Lo que dice que hay una operación de git a medias. Encima de cualquiera
# de ellas, «traer cambios» o «restaurar» cerrarían algo que el usuario
# dejó abierto (un merge con conflictos, un rebase) y lo publicarían.
_IN_PROGRESS = (
    ("MERGE_HEAD", "una combinación (merge) a medias"),
    ("CHERRY_PICK_HEAD", "un cherry-pick a medias"),
    ("REVERT_HEAD", "un revert a medias"),
    ("rebase-merge", "un rebase a medias"),
    ("rebase-apply", "un rebase o un «git am» a medias"),
    ("BISECT_LOG", "una búsqueda con «git bisect»"),
)

BACKUP_PREFIX = "respaldo-"
# Marca que llevan los respaldos byte a byte (sin filtros). Los de
# versiones anteriores de la app se guardaron con filtros y se restauran
# como git restaura cualquier commit.
EXACT_TRAILER = "GitPush-Respaldo: exacto"
_HASH_BATCH = 2000


# ------------------------------------------------------------------ #
#  Rutas exactas                                                      #
# ------------------------------------------------------------------ #
def _split_z(raw):
    """Salida `-z` de git (bytes) → rutas tal como las entiende el disco."""
    return [os.fsdecode(x) for x in (raw or b"").split(b"\0") if x]


def _abs(folder, rel):
    return os.path.join(os.fspath(folder), rel)


def git_dir(git, folder):
    """Carpeta real de git (también si `.git` es un archivo, como en los
    worktrees y los submódulos)."""
    p = git.run(["rev-parse", "--absolute-git-dir"], cwd=folder, check=False,
                echo=False)
    ruta = (p.stdout or "").strip()
    return Path(ruta) if p.returncode == 0 and ruta else Path(folder) / ".git"


# ------------------------------------------------------------------ #
#  ¿Se puede tocar esta carpeta ahora?                                #
# ------------------------------------------------------------------ #
def operation_in_progress(folder, git=None):
    """Descripción de la operación de git a medias, o ""."""
    gd = git_dir(git, folder) if git is not None else Path(folder) / ".git"
    for nombre, que in _IN_PROGRESS:
        if (gd / nombre).exists():
            return que
    return ""


def unmerged_paths(git, folder):
    """Rutas con conflictos sin resolver en el índice."""
    p = git.run(["ls-files", "-u", "-z"], cwd=folder, check=False,
                echo=False, binary=True)
    rutas = set()
    for item in (p.stdout or b"").split(b"\0"):
        _, sep, ruta = item.partition(b"\t")
        if sep:
            rutas.add(os.fsdecode(ruta))
    return sorted(rutas)


def require_quiet_repo(folder, git=None):
    """Lanza StepError si hay una operación de git sin terminar o
    archivos con conflictos sin resolver."""
    que = operation_in_progress(folder, git)
    if que:
        raise StepError(
            f"La carpeta tiene {que}. Termínala o cancélala primero (en "
            "una terminal: «git merge --abort», «git rebase --abort»…); "
            "no la toco para no cerrarla por ti.")
    if git is not None:
        conflictos = unmerged_paths(git, folder)
        if conflictos:
            raise StepError(
                f"Hay {len(conflictos)} archivo(s) con conflictos sin "
                f"resolver (por ejemplo «{conflictos[0]}»). Resuélvelos "
                "primero; no los toco para no decidir por ti.")


def require_branch_checked_out(git, folder):
    """La rama actual. Lanza StepError si HEAD está suelto: actualizarlo
    movería un commit que no pertenece a ninguna rama."""
    rama = git.current_branch(folder)
    if not rama:
        raise StepError(
            "Tu carpeta no está en ninguna rama (HEAD suelto). Vuelve a "
            "una rama primero (por ejemplo con «git switch main»).")
    return rama


# ------------------------------------------------------------------ #
#  ¿Cambió algo mientras se preparaba la operación?                   #
# ------------------------------------------------------------------ #
CHANGED_WHILE_WORKING = (
    "Algo cambió en la carpeta mientras preparaba la operación (¿un editor "
    "guardando, una sincronización?): «{}». No he tocado nada; vuelve a "
    "intentarlo cuando la carpeta esté quieta.")


def stat_signature(info):
    """Lo que cambia cuando alguien escribe en una ruta (None: no existe)."""
    if info is None:
        return None
    return (info.st_mode, info.st_size, info.st_mtime_ns)


def _signature_of(folder, rel):
    try:
        return stat_signature(os.lstat(_abs(folder, rel)))
    except OSError:
        return None


class FolderGuard:
    """Comprueba, justo antes de pisar o borrar nada, que lo que la
    operación va a tocar sigue como estaba cuando se preparó.

    Entre mirar la carpeta, hacer la copia y actuar pasan segundos. Un
    editor que guarda en ese momento dejaría su cambio fuera de la copia,
    y la operación lo pisaría. Por cada ruta vigilada se comparan dos
    cosas baratas:

      · su estado en `git status` (limpia, modificada…) desde que se
        creó el guardián: un archivo limpio que pasa a tener cambios;
      · tipo, tamaño y fecha (lstat): un archivo con cambios que vuelve
        a cambiar, o uno que aparece donde se va a escribir.

    Solo se vigila lo que la operación toca: un editor que guarda un
    archivo que no tiene nada que ver no debe impedir nada.
    """

    def __init__(self, git, folder):
        self.git = git
        self.folder = os.fspath(folder)
        self.status = self._tracked_status()
        self.seen = {}

    def _tracked_status(self):
        p = self.git.run(["status", "--porcelain", "-z",
                          "--untracked-files=no", "--no-renames"],
                         cwd=self.folder, check=True, echo=False, binary=True)
        out = {}
        for item in (p.stdout or b"").split(b"\0"):
            if len(item) > 3:
                out[os.fsdecode(item[3:])] = item[:2]
        return out

    def watch(self, rutas):
        """Vigila estas rutas tal como están AHORA."""
        for rel in rutas:
            if rel not in self.seen:
                self.seen[rel] = _signature_of(self.folder, rel)

    def expect(self, firmas):
        """Vigila rutas tal como se vieron antes ({ruta: firma}): lo que
        cambiara desde entonces también cuenta."""
        for rel, firma in firmas.items():
            self.seen.setdefault(rel, firma)

    def verify(self):
        """StepError si algo vigilado cambió."""
        ahora = self._tracked_status()
        for rel in sorted(self.seen):
            if ahora.get(rel) != self.status.get(rel) or \
                    _signature_of(self.folder, rel) != self.seen[rel]:
                raise StepError(CHANGED_WHILE_WORKING.format(rel))


# ------------------------------------------------------------------ #
#  Qué hay en la carpeta                                              #
# ------------------------------------------------------------------ #
def tracked_index(git, folder):
    """{ruta: (modo, sha)} del índice actual (lo rastreado)."""
    p = git.run(["ls-files", "-s", "-z"], cwd=folder, check=False,
                echo=False, binary=True)
    out = {}
    for item in (p.stdout or b"").split(b"\0"):
        meta, sep, ruta = item.partition(b"\t")
        if not sep:
            continue
        partes = meta.split()
        if len(partes) >= 3 and partes[2] in (b"0", b"2"):
            out[os.fsdecode(ruta)] = (partes[0].decode("ascii"),
                                      partes[1].decode("ascii"))
    return out


def untracked(git, folder):
    """No rastreados que NO están ignorados (lo que vería «git status»,
    aunque el usuario haya puesto status.showUntrackedFiles=no)."""
    p = git.run(["ls-files", "-z", "--others", "--exclude-standard"],
                cwd=folder, check=False, echo=False, binary=True)
    return _split_z(p.stdout)


def tree_entries(git, folder, tree, check=True):
    """{ruta: (modo, sha)} de todos los archivos de un árbol/commit.

    Con `check=False`, un árbol que no existe devuelve None en vez de
    lanzar StepError."""
    p = git.run(["ls-tree", "-r", "-z", "--full-tree", tree], cwd=folder,
                check=check, echo=False, binary=True)
    if p.returncode != 0:
        return None
    out = {}
    for item in (p.stdout or b"").split(b"\0"):
        meta, sep, ruta = item.partition(b"\t")
        if not sep:
            continue
        partes = meta.split()
        if len(partes) >= 3:
            out[os.fsdecode(ruta)] = (partes[0].decode("ascii"),
                                      partes[2].decode("ascii"))
    return out


class RepoInTheWay(StepError):
    """Otro repositorio de git ocupa una carpeta que la otra versión
    convierte en archivo.

    Git borraría esa carpeta ENTERA para escribir el archivo (`reset
    --hard`, `read-tree -u` y `checkout-index -f` lo hacen sin
    preguntar), y con ella su carpeta .git: historial, ramas, lo no
    publicado. Eso no cabe en un respaldo (git no admite rutas con un
    tramo «.git»), así que la única opción segura es no empezar.
    """

    def __init__(self, paths):
        self.paths = sorted(paths)
        ejemplos = "», «".join(self.paths[:3])
        mas = (f" (y {len(self.paths) - 3} más)"
               if len(self.paths) > 3 else "")
        super().__init__(
            f"No sigo: «{ejemplos}»{mas} es otro repositorio de git (tiene "
            "su propia carpeta .git) y la otra versión pone un archivo en "
            "ese sitio. Git borraría esa carpeta entera, con su historial, "
            "y eso no se puede guardar en un respaldo. No he tocado nada: "
            "mueve esa carpeta fuera del proyecto y vuelve a intentarlo.")


def files_at_risk(git, folder, target, strict=True):
    """Archivos de la carpeta que NO están en el índice y que dejar la
    carpeta como `target` sobrescribiría o borraría.

    Git trata los archivos ignorados como desechables: un `reset --hard`,
    un `read-tree -u` o un merge los pisan sin avisar. Aquí se buscan las
    tres colisiones posibles con cada ruta P del árbol destino:

      · en disco hay un ARCHIVO en P            → se sobrescribiría;
      · en disco hay una CARPETA en P           → se borraría entera;
      · en disco hay un ARCHIVO donde P necesita una carpeta (un tramo
        de su ruta)                              → se borraría.

    Devuelve rutas de archivo (las carpetas, expandidas a su contenido;
    un enlace dentro de ellas cuenta como archivo: git lo borraría igual).

    Si una carpeta que se borraría entera contiene OTRO repositorio
    (una entrada «.git» a cualquier profundidad), lanza `RepoInTheWay`:
    su historial no se puede respaldar. `strict=False` es para quien
    escribe archivo a archivo y nunca borra una carpeta con contenido
    (`restore_snapshot`): ahí ese repositorio no corre peligro.
    Cuando P es un submódulo en el destino, git deja la carpeta en paz y
    no se cuenta.
    """
    destino = tree_entries(git, folder, target, check=False)
    if destino is None:
        return []
    rastreados = set(tracked_index(git, folder))
    raiz = os.fspath(folder)
    riesgo, vistos_dirs, repos = set(), set(), []

    def expande(rel, ruta):
        """Todo lo que hay dentro de una carpeta que git borraría."""
        for base, dirs, archivos in os.walk(ruta):
            rel_base = (rel if base == ruta else
                        os.path.relpath(base, raiz).replace(os.sep, "/"))
            if ".git" in dirs or ".git" in archivos:
                repos.append(rel_base)
            hijos = []
            for d in dirs:
                if d == ".git":
                    continue
                if os.path.islink(os.path.join(base, d)):
                    archivos.append(d)      # se guarda como enlace
                else:
                    hijos.append(d)
            dirs[:] = hijos
            for a in archivos:
                if a == ".git":
                    continue
                r = rel_base + "/" + a
                if r not in rastreados:
                    riesgo.add(r)

    def vigila(rel, modo):
        if rel in riesgo:
            return
        ruta = _abs(raiz, rel)
        try:
            info = os.lstat(ruta)
        except OSError:
            return
        if not stat.S_ISDIR(info.st_mode):
            if rel not in rastreados:
                riesgo.add(rel)
        elif modo != "160000":
            expande(rel, ruta)

    for rel, (modo, _sha) in destino.items():
        vigila(rel, modo)
        # Tramos de la ruta: si en disco son archivos, estorban.
        partes = rel.split("/")
        for i in range(1, len(partes)):
            tramo = "/".join(partes[:i])
            if tramo in vistos_dirs:
                continue
            vistos_dirs.add(tramo)
            try:
                info = os.lstat(_abs(raiz, tramo))
            except OSError:
                continue
            if not stat.S_ISDIR(info.st_mode):
                vigila(tramo, "")
    if repos and strict:
        raise RepoInTheWay(repos)
    return sorted(riesgo)


# ------------------------------------------------------------------ #
#  La copia                                                           #
# ------------------------------------------------------------------ #
def _mode_for(info, en_indice, windows):
    if stat.S_ISLNK(info.st_mode):
        return "120000"
    if windows:
        # Windows no tiene bit de ejecución (Python lo deduce de la
        # extensión): se respeta lo que diga el índice, y si no, normal.
        return en_indice[0] if en_indice and en_indice[0] in (
            "100644", "100755") else "100644"
    return "100755" if info.st_mode & 0o111 else "100644"


def _needs_own_call(rel):
    """`--stdin-paths` lee líneas y desentrecomilla las que empiezan por
    comillas: esos nombres van uno a uno, como argumento."""
    return "\n" in rel or "\r" in rel or rel.startswith('"')


def _hash_files(git, folder, rutas):
    """{ruta: sha} guardando el contenido EXACTO (sin filtros)."""
    out = {}
    normales = [r for r in rutas if not _needs_own_call(r)]
    raras = [r for r in rutas if _needs_own_call(r)]
    for i in range(0, len(normales), _HASH_BATCH):
        lote = normales[i:i + _HASH_BATCH]
        p = git.run(["hash-object", "-w", "--no-filters", "--stdin-paths"],
                    cwd=folder, check=False, echo=False, binary=True,
                    input=b"\n".join(os.fsencode(r) for r in lote) + b"\n")
        shas = (p.stdout or b"").decode("ascii", "replace").split()
        if p.returncode != 0 or len(shas) != len(lote):
            # Uno a uno, para poder decir CUÁL no se deja leer.
            for r in lote:
                out[r] = _hash_one(git, folder, r)
            continue
        out.update(zip(lote, shas))
    for r in raras:
        out[r] = _hash_one(git, folder, r)
    return out


def _hash_one(git, folder, rel):
    p = git.run(["hash-object", "-w", "--no-filters", "--", rel],
                cwd=folder, check=False, echo=False, binary=True)
    sha = (p.stdout or b"").decode("ascii", "replace").strip()
    if p.returncode != 0 or not sha:
        raise StepError(
            f"No pude leer «{rel}» para guardarlo en la copia de seguridad "
            "(¿está abierto en otro programa o sin permisos?). No he "
            "tocado nada.")
    return sha


def _hash_link(git, folder, rel):
    destino = os.fsencode(os.readlink(_abs(folder, rel)))
    p = git.run(["hash-object", "-w", "--stdin"], cwd=folder, check=True,
                echo=False, binary=True, input=destino)
    return (p.stdout or b"").decode("ascii").strip()


def snapshot_tree(git, folder, extra=()):
    """Árbol con el estado EXACTO de la carpeta. Devuelve (sha del
    árbol, nº de entradas); ("", 0) si no hay nada que guardar."""
    raiz = os.fspath(folder)
    indice = tracked_index(git, folder)
    rutas = set(indice) | set(untracked(git, folder)) | set(extra)
    windows = os.name == "nt"
    entradas, a_hashear, enlaces = [], [], []
    for rel in sorted(rutas):
        try:
            info = os.lstat(_abs(raiz, rel))
        except OSError:
            continue                       # borrado en disco: no se guarda
        en_indice = indice.get(rel)
        if stat.S_ISDIR(info.st_mode):
            # Submódulo o repositorio anidado: se guarda el enlace que
            # git ya conoce; su contenido es de ese otro repositorio.
            if en_indice and en_indice[0] == "160000":
                entradas.append((en_indice[0], en_indice[1], rel))
            continue
        modo = _mode_for(info, en_indice, windows)
        if modo == "120000":
            enlaces.append(rel)
        elif stat.S_ISREG(info.st_mode):
            a_hashear.append((rel, modo))
        # FIFOs, sockets y dispositivos no son contenido: no se leen.
    shas = _hash_files(git, folder, [r for r, _ in a_hashear])
    for rel, modo in a_hashear:
        entradas.append((modo, shas[rel], rel))
    for rel in enlaces:
        try:
            entradas.append(("120000", _hash_link(git, folder, rel), rel))
        except OSError:
            continue
    if not entradas:
        return "", 0

    gd = git_dir(git, folder)
    fd, tmp_index = tempfile.mkstemp(prefix="gitpush-indice-", dir=str(gd))
    os.close(fd)
    os.unlink(tmp_index)                   # git lo crea a su gusto
    env = {"GIT_INDEX_FILE": tmp_index}
    try:
        info = b"".join(b"%s %s\t%s\0" % (m.encode("ascii"),
                                          s.encode("ascii"),
                                          os.fsencode(r))
                        for m, s, r in entradas)
        git.run(["update-index", "-z", "--index-info"], cwd=folder,
                check=True, echo=False, env_extra=env, input=info,
                binary=True)
        p = git.run(["write-tree"], cwd=folder, check=True, echo=False,
                    env_extra=env)
        return (p.stdout or "").strip(), len(entradas)
    finally:
        for resto in (tmp_index, tmp_index + ".lock"):
            try:
                os.unlink(resto)
            except OSError:
                pass


def snapshot_commit(git, folder, message, identity=None, extra=()):
    """Commit con el estado EXACTO de la carpeta, sin tocar nada.

    Devuelve (sha, nº de archivos) o ("", 0) si no hay nada que guardar.
    Si la carpeta está idéntica a HEAD, devuelve HEAD (no hace falta un
    commit nuevo para recordar algo que ya está en el historial).
    """
    arbol, n = snapshot_tree(git, folder, extra)
    head = git.sha(folder)
    if not arbol:
        return (head, 0) if head else ("", 0)
    if head and arbol == git.sha(folder, "HEAD^{tree}"):
        return head, n
    sha = git.commit_tree(folder, arbol, [head] if head else [],
                          [message, EXACT_TRAILER], identity=identity,
                          sign=False)
    return sha, n


def free_backup_name(git, folder):
    stamp = time.strftime("%Y-%m-%d-%H%M")
    existentes = set(git.local_branches(folder))
    nombre, i = BACKUP_PREFIX + stamp, 2
    while nombre in existentes:
        nombre, i = "%s%s-%d" % (BACKUP_PREFIX, stamp, i), i + 1
    return nombre


def backup(git, folder, reason="antes de cambiar la carpeta", identity=None,
           at_risk=()):
    """Rama «respaldo-FECHA» con el estado exacto de la carpeta.

    No mueve la rama actual, no toca el índice ni los archivos, no
    ejecuta hooks. Devuelve el nombre de la rama, o "" si no había nada
    que guardar (carpeta vacía y sin commits).
    """
    sha, _ = snapshot_commit(git, folder, "Respaldo automático " + reason,
                             identity=identity, extra=at_risk)
    if not sha:
        return ""
    nombre = free_backup_name(git, folder)
    git.run(["branch", nombre, sha], cwd=folder, check=True, echo=False)
    return nombre


def is_exact_backup(git, folder, ref):
    """¿Es `ref` un respaldo byte a byte (de esta versión de la app)?"""
    p = git.run(["cat-file", "commit", ref], cwd=folder, check=False,
                echo=False)
    return p.returncode == 0 and EXACT_TRAILER in (p.stdout or "")


# ------------------------------------------------------------------ #
#  Borrar solo lo que está a salvo                                    #
# ------------------------------------------------------------------ #
def prune_empty_dirs(folder, removed):
    """Quita las carpetas que se quedaron vacías al borrar `removed`,
    subiendo hacia la raíz. Nunca la raíz ni nada dentro de .git."""
    raiz = os.path.abspath(os.fspath(folder))
    padres = set()
    for rel in removed:
        partes = rel.split("/")[:-1]
        for i in range(len(partes), 0, -1):
            padres.add("/".join(partes[:i]))
    for rel in sorted(padres, key=lambda r: r.count("/"), reverse=True):
        if rel == ".git" or rel.startswith(".git/"):
            continue
        try:
            os.rmdir(_abs(raiz, rel))
        except OSError:
            pass                           # no vacía (o ya no existe)


def remove_verified(git, folder, snapshot, rutas):
    """Borra cada ruta SOLO si `snapshot` guarda un contenido idéntico
    al que hay ahora en el disco (comprobado byte a byte, con una
    implementación propia del identificador de git).

    Devuelve (borradas, conservadas). Lo conservado se queda tal cual:
    o no estaba en la copia o cambió después de hacerla.
    """
    rutas = list(rutas)
    if not rutas:
        return [], []
    guardado = tree_entries(git, folder, snapshot) if snapshot else {}
    borradas, conservadas = [], []
    for rel in rutas:
        entrada = guardado.get(rel)
        ruta = _abs(folder, rel)
        if not entrada or entrada[0] == "160000":
            conservadas.append(rel)
            continue
        try:
            actual = file_blob_id(ruta, algorithm_for(entrada[1]))
        except (OSError, ValueError):
            conservadas.append(rel)
            continue
        if actual != entrada[1]:
            conservadas.append(rel)
            continue
        try:
            os.unlink(ruta)
        except OSError:
            conservadas.append(rel)
            continue
        borradas.append(rel)
    prune_empty_dirs(folder, borradas)
    return borradas, conservadas


# ------------------------------------------------------------------ #
#  Volver a un respaldo                                               #
# ------------------------------------------------------------------ #
def _write_exact(ruta, datos, modo):
    """Escribe `datos` tal cual (archivo temporal + reemplazo atómico)."""
    carpeta = os.path.dirname(ruta)
    os.makedirs(carpeta, exist_ok=True)
    if modo == "120000":
        try:
            if os.path.lexists(ruta):
                os.unlink(ruta)
            os.symlink(os.fsdecode(datos), ruta)
            return
        except (OSError, NotImplementedError):
            pass                 # sin permiso para enlaces: como git, texto
    fd, tmp = tempfile.mkstemp(prefix=".gitpush-", dir=carpeta)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(datos)
            fh.flush()
            os.fsync(fh.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o755 if modo == "100755" else 0o644)
        if os.path.islink(ruta):
            os.unlink(ruta)
        os.replace(tmp, ruta)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def restore_snapshot(git, folder, snapshot, safety):
    """Deja en la carpeta los archivos de `snapshot` tal como se guardaron,
    SIN hacer commit y sin tocar el índice ni la rama: lo recuperado
    queda como cambios tuyos sin publicar (pasará por la vista previa y
    la revisión de secretos si luego publicas).

    `safety` es el respaldo del estado actual (hecho por quien llama):
    solo se borra o se pisa lo que está guardado allí, verificado byte a
    byte. Los archivos ignorados que no estaban en el respaldo se quedan.

    Devuelve (escritos, borrados, conservados).
    """
    raiz = os.fspath(folder)
    objetivo = tree_entries(git, folder, snapshot)
    exacto = is_exact_backup(git, folder, snapshot)

    # 1. Lo que sobra: rastreado o no ignorado que no estaba entonces,
    #    y lo que estorba donde hace falta una carpeta.
    actuales = set(tracked_index(git, folder)) | set(untracked(git, folder))
    sobra = {r for r in actuales if r not in objetivo}
    for rel in objetivo:
        partes = rel.split("/")
        for i in range(1, len(partes)):
            tramo = "/".join(partes[:i])
            try:
                if not stat.S_ISDIR(os.lstat(_abs(raiz, tramo)).st_mode):
                    sobra.add(tramo)
            except OSError:
                pass
    sobra = {r for r in sobra if os.path.lexists(_abs(raiz, r))}
    borrados, conservados = remove_verified(git, folder, safety, sorted(sobra))

    # 2. Lo que hay que escribir: solo lo que no está ya idéntico.
    pendientes = []
    for rel, (modo, sha) in sorted(objetivo.items()):
        if modo == "160000":
            continue
        ruta = _abs(raiz, rel)
        try:
            info = os.lstat(ruta)
        except OSError:
            info = None
        if info is not None and stat.S_ISDIR(info.st_mode):
            try:
                os.rmdir(ruta)             # solo si está vacía
            except OSError:
                conservados.append(rel)
                continue
        elif info is not None and exacto:
            try:
                if file_blob_id(ruta, algorithm_for(sha)) == sha:
                    continue
            except (OSError, ValueError):
                pass
        pendientes.append((rel, modo, sha))

    if exacto:
        datos = git.read_blobs(folder, [s for _, _, s in pendientes])
        for rel, modo, sha in pendientes:
            _write_exact(_abs(raiz, rel), datos[sha], modo)
    elif pendientes:
        _checkout_with_filters(git, folder, pendientes)
    return [r for r, _, _ in pendientes], borrados, conservados


def _checkout_with_filters(git, folder, entradas):
    """Escribe entradas (ruta, modo, sha) con los filtros de git (finales
    de línea, LFS…), como haría un checkout, usando un índice temporal:
    el índice del usuario no se toca."""
    gd = git_dir(git, folder)
    fd, tmp_index = tempfile.mkstemp(prefix="gitpush-indice-", dir=str(gd))
    os.close(fd)
    os.unlink(tmp_index)
    env = {"GIT_INDEX_FILE": tmp_index}
    try:
        info = b"".join(b"%s %s\t%s\0" % (m.encode("ascii"), s.encode("ascii"),
                                          os.fsencode(r))
                        for r, m, s in entradas)
        git.run(["update-index", "-z", "--index-info"], cwd=folder,
                check=True, echo=False, env_extra=env, input=info,
                binary=True)
        rutas = b"".join(os.fsencode(r) + b"\0" for r, _, _ in entradas)
        git.run(["checkout-index", "-f", "-z", "--stdin"], cwd=folder,
                check=True, echo=False, env_extra=env, input=rutas,
                binary=True)
    finally:
        for resto in (tmp_index, tmp_index + ".lock"):
            try:
                os.unlink(resto)
            except OSError:
                pass
