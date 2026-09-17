# -*- coding: utf-8 -*-
"""Todo lo que habla con el ejecutable git.

`Git` es un envoltorio inyectable: recibe una función `log` y todas las
operaciones pasan por `run`/`stream`, así que la interfaz, la CLI y
los tests lo usan igual. Los analizadores de salida son funciones
puras (sin git) para poder probarlos con texto fijo.
"""

import base64
import os
import shutil
import stat
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .errors import StepError
from .fs import NO_WINDOW
from .urls import http_origin

GIT_MISSING = ("Git no está instalado o no está en el PATH.\n"
               "  Descárgalo en https://git-scm.com y reinicia la app.")

# Mensajes en inglés (LC_ALL=C) para poder reconocerlos, y nunca
# quedarse colgado pidiendo credenciales en una ventana invisible.
BASE_ENV = {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "LANG": "C"}


def _noop_log(text, tag="dim"):
    pass


# ------------------------------------------------------------------ #
#  Analizadores puros                                                 #
# ------------------------------------------------------------------ #
def parse_name_status_z(text):
    """Salida de `git diff --name-status -z` → (nuevos, modificados,
    borrados). Los renombrados y copiados cuentan como modificados con
    su ruta destino."""
    added, modified, deleted = [], [], []
    parts = [x for x in (text or "").split("\0") if x]
    i, n = 0, len(parts)
    while i < n:
        code = parts[i]
        i += 1
        if i >= n:
            break
        path = parts[i]
        i += 1
        if code[:1] in ("R", "C"):
            if i < n:                      # origen y destino
                path = parts[i]
                i += 1
            modified.append(path)
        elif code[:1] == "A":
            added.append(path)
        elif code[:1] == "D":
            deleted.append(path)
        else:
            modified.append(path)
    return sorted(added), sorted(modified), sorted(deleted)


def parse_status_z(text, ignore_deletions=False):
    """Salida de `git status --porcelain -z` → rutas con cambios.

    Con `ignore_deletions` no cuentan los borrados en la carpeta: es el
    caso del modo agregar/editar, donde un archivo borrado localmente
    no se quita del repositorio y no debe disparar otra publicación.
    """
    changed = []
    tokens = (text or "").split("\0")
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        i += 1
        if len(tok) < 3:
            continue
        xy, path = tok[:2], tok[3:]
        if "R" in xy or "C" in xy:
            i += 1                         # la ruta de origen va aparte
        if ignore_deletions and xy.strip() == "D":
            continue
        changed.append(path)
    return changed


def push_error_message(output_lower):
    """Traduce el error de `git push` a una explicación con remedio."""
    o = output_lower
    if ("authentication" in o or "could not read username" in o
            or "denied" in o or "403" in o):
        return ("GitHub rechazó las credenciales.\n"
                "  • Instala/actualiza Git Credential Manager "
                "(viene con Git para Windows).\n"
                "  • O ejecuta un push manual una vez para iniciar "
                "sesión en el navegador.\n"
                "  • Verifica que tu usuario tenga permisos en ese "
                "repositorio.")
    if "src refspec" in o:
        return ("No hay nada que publicar: la carpeta está vacía o todo "
                "está en el .gitignore.")
    if "repository not found" in o:
        return ("GitHub dice que el repositorio no existe. Créalo con "
                "«➕ Crear repositorio nuevo…» o revisa la URL.")
    if "exceeds github" in o:
        return ("GitHub rechazó un archivo por tamaño. Pulsa "
                "«🛡 Analizar», exclúyelo y vuelve a publicar.")
    if "non-fast-forward" in o or "rejected" in o:
        return ("GitHub rechazó el push: alguien más subió algo "
                "mientras publicabas.\n"
                "  Vuelve a pulsar «Publicar»: la app combinará de "
                "nuevo esos cambios y esta vez debería entrar.")
    return "Falló el push. Revisa la consola."


def clone_error_message(output_lower, last_line=""):
    o = output_lower
    if ("could not read username" in o or "authentication" in o
            or "denied" in o):
        return ("GitHub pidió credenciales. Si el repositorio es privado, "
                "añade un token o inicia sesión una vez con Git "
                "Credential Manager.")
    if "already exists" in o:
        return "La carpeta destino ya existe y no está vacía."
    if "not found" in o:
        return ("Repositorio no encontrado: revisa la URL (o es privado "
                "y no tienes acceso).")
    return "Falló la clonación. Última línea: " + (last_line or "sin salida")


# ------------------------------------------------------------------ #
#  Autenticación por entorno (nunca en la línea de comandos)          #
# ------------------------------------------------------------------ #
def auth_env(url, token, base_env=None):
    """Variables GIT_CONFIG_* (git ≥ 2.31) que añaden la cabecera de
    autorización solo para el origen de `url`. No tocan argv ni
    `.git/config`, así que no hay nada que limpiar después."""
    origin = http_origin(url)
    if not (token and origin):
        return {}
    env = os.environ if base_env is None else base_env
    try:
        n = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    except ValueError:
        n = 0
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {"GIT_CONFIG_COUNT": str(n + 1),
            f"GIT_CONFIG_KEY_{n}": f"http.{origin}.extraheader",
            f"GIT_CONFIG_VALUE_{n}": f"AUTHORIZATION: basic {basic}"}


# ------------------------------------------------------------------ #
#  Envoltorio de git                                                  #
# ------------------------------------------------------------------ #
class Git:
    """Ejecuta git. `log(texto, etiqueta)` recibe el comando y su salida
    con etiquetas «cmd» y «dim» (las mismas que usa la consola de la
    interfaz)."""

    def __init__(self, log=None, exe="git"):
        self.log = log or _noop_log
        self.exe = exe

    def _env(self, extra=None):
        env = dict(os.environ)
        env.update(BASE_ENV)
        if extra:
            env.update(extra)
        return env

    def run(self, args, cwd=None, check=True, timeout=900, env_extra=None,
            echo=True, register=None):
        """Ejecuta `git *args` y devuelve un CompletedProcess.

        `register(proc)` recibe el proceso nada más arrancar (para poder
        matarlo desde fuera). Con `check`, un código distinto de cero
        lanza StepError.
        """
        if echo:
            self.log("$ git " + " ".join(args), "cmd")
        try:
            p = subprocess.Popen(
                [self.exe, *args], cwd=cwd, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", creationflags=NO_WINDOW,
                env=self._env(env_extra))
        except FileNotFoundError:
            raise StepError(GIT_MISSING) from None
        if register:
            register(p)
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
            raise StepError(f"El comando tardó demasiado: git "
                            f"{' '.join(args)}") from None
        result = subprocess.CompletedProcess(p.args, p.returncode, out, err)
        if echo:
            combined = ((out or "") + (err or "")).strip()
            for line in combined.splitlines():
                self.log("  " + line, "dim")
        if check and p.returncode != 0:
            raise StepError(f"Falló: git {' '.join(args)}")
        return result

    def stream(self, args, cwd, on_line, timeout=1800, env_extra=None,
               register=None):
        """Ejecuta git entregando cada línea de salida (progreso en
        vivo). Devuelve el código de salida."""
        try:
            p = subprocess.Popen(
                [self.exe, *args], cwd=cwd, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", creationflags=NO_WINDOW,
                env=self._env(env_extra))
        except FileNotFoundError:
            on_line(GIT_MISSING.splitlines()[0])
            return 127
        if register:
            register(p)
        start = time.time()
        assert p.stdout is not None
        with p.stdout:
            for raw in p.stdout:
                for piece in raw.replace("\r", "\n").splitlines():
                    piece = piece.strip()
                    if piece:
                        on_line(piece)
                if time.time() - start > timeout:
                    p.kill()
                    on_line("Tiempo agotado.")
                    p.wait()
                    return 1
        return p.wait()

    # ---- consultas -------------------------------------------------
    def version(self):
        p = self.run(["--version"], check=True, echo=False)
        return (p.stdout or "").strip()

    def list_files(self, folder):
        """Exactamente los archivos que `git add -A` subiría: rastreados
        más no rastreados, respetando .gitignore (raíz y anidados),
        `.git/info/exclude` y las exclusiones globales.

        Devuelve [(rel, ruta_abs, tamaño)] ordenado; los archivos que ya
        no existen en disco (borrados pendientes) y los submódulos no se
        incluyen."""
        p = self.run(["ls-files", "-z", "--cached", "--others",
                      "--exclude-standard"], cwd=folder, echo=False)
        root = os.path.abspath(os.fspath(folder))
        entries, seen = [], set()
        for rel in (p.stdout or "").split("\0"):
            if not rel or rel in seen:
                continue
            seen.add(rel)
            fp = os.path.join(root, rel)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                continue
            entries.append((rel, fp, st.st_size))
        entries.sort(key=lambda e: e[0].lower())
        return entries

    def status_changes(self, folder, ignore_deletions=False):
        """Rutas con cambios sin publicar. Usa la caché de stat del
        índice de git: mucho más rápido que recorrer el árbol, y con
        las reglas de ignorado reales."""
        p = self.run(["status", "--porcelain", "-z"], cwd=folder,
                     echo=False)
        return parse_status_z(p.stdout, ignore_deletions)

    def is_dirty(self, folder, ignore_deletions=False):
        """¿Hay algo que publicar?"""
        return bool(self.status_changes(folder, ignore_deletions))

    def is_repo(self, folder):
        return (Path(folder) / ".git").exists()

    def has_commits(self, folder):
        p = self.run(["rev-parse", "--verify", "-q", "HEAD"], cwd=folder,
                     check=False, echo=False)
        return p.returncode == 0

    def current_branch(self, folder):
        """Nombre de la rama actual (también si aún no tiene commits);
        cadena vacía si HEAD está suelto."""
        p = self.run(["symbolic-ref", "--short", "-q", "HEAD"], cwd=folder,
                     check=False, echo=False)
        return (p.stdout or "").strip() if p.returncode == 0 else ""

    def local_branches(self, folder):
        p = self.run(["branch", "--format=%(refname:short)"], cwd=folder,
                     check=False, echo=False)
        return sorted(x.strip() for x in (p.stdout or "").splitlines()
                      if x.strip()) if p.returncode == 0 else []

    def remote_branches(self, url, timeout=60):
        """Ramas que existen en el remoto, sin necesitar repositorio local."""
        p = self.run(["ls-remote", "--heads", url], check=False, echo=False,
                     timeout=timeout)
        if p.returncode != 0:
            raise StepError("No pude consultar las ramas del remoto. Revisa "
                            "la URL y tu conexión.")
        names = []
        for line in (p.stdout or "").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].startswith("refs/heads/"):
                names.append(parts[1][len("refs/heads/"):])
        return sorted(names)

    def staged_changes(self, folder):
        """(nuevos, modificados, borrados) preparados para el commit,
        frente a HEAD (o frente a nada si aún no hay commits)."""
        p = self.run(["diff", "--cached", "--name-status", "-z"], cwd=folder,
                     check=False, echo=False)
        return parse_name_status_z(p.stdout) if p.returncode == 0 \
            else ([], [], [])

    def ensure_remote(self, folder, url):
        p = self.run(["remote", "get-url", "origin"], cwd=folder, check=False)
        if p.returncode == 0:
            self.run(["remote", "set-url", "origin", url], cwd=folder)
        else:
            self.run(["remote", "add", "origin", url], cwd=folder)

    def run_committing(self, args, cwd, identity=None, check=False,
                       timeout=900):
        """Como `run`, pero si git no sabe quién es el usuario (carpeta
        recién creada, sin identidad global) reintenta con la identidad
        dada o con una genérica: los commits de respaldo y de
        restauración no pueden fallar por eso."""
        p = self.run(args, cwd=cwd, check=False, timeout=timeout)
        out = ((p.stdout or "") + (p.stderr or "")).lower()
        if p.returncode != 0 and "tell me who you are" in out:
            name, email = identity or ("GitPush Studio", "gitpush@localhost")
            env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                   "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
            p = self.run(args, cwd=cwd, check=False, timeout=timeout,
                         env_extra=env)
        if check and p.returncode != 0:
            raise StepError(f"Falló: git {' '.join(args)}")
        return p

    def backup(self, folder, reason="antes de cambiar la carpeta",
               identity=None):
        """Deja a salvo el estado actual: si hay cambios sin commit los
        registra en un commit, y crea una rama «respaldo-FECHA» apuntando
        a HEAD. Devuelve el nombre de la rama, o cadena vacía si no había
        nada que proteger (sin commits ni archivos)."""
        if self.is_dirty(folder):
            self.run(["add", "-A"], cwd=folder)
            self.run_committing(["commit", "-q", "-m",
                                 f"Respaldo automático {reason}"],
                                cwd=folder, identity=identity, check=True)
        if not self.has_commits(folder):
            return ""
        stamp = time.strftime("%Y-%m-%d-%H%M")
        existing = set(self.local_branches(folder))
        name, i = f"respaldo-{stamp}", 2
        while name in existing:
            name, i = f"respaldo-{stamp}-{i}", i + 1
        self.run(["branch", name, "HEAD"], cwd=folder)
        return name

    def is_ancestor(self, folder, ancestor, descendant):
        p = self.run(["merge-base", "--is-ancestor", ancestor, descendant],
                     cwd=folder, check=False)
        return p.returncode == 0

    def diff_vs_remote(self, folder, remote_exists):
        """(nuevos, modificados, a_borrar) comparando el índice con lo
        que hay hoy en GitHub (FETCH_HEAD). Si la rama aún no existe,
        todo lo preparado es nuevo."""
        if remote_exists:
            p = self.run(["diff", "--cached", "--name-status", "-z",
                          "FETCH_HEAD"], cwd=folder, check=False)
            if p.returncode != 0:
                return [], [], []
            return parse_name_status_z(p.stdout)
        p = self.run(["ls-files", "--cached", "-z"], cwd=folder, check=False)
        added = [x for x in (p.stdout or "").split("\0") if x] \
            if p.returncode == 0 else []
        return sorted(added), [], []


# ------------------------------------------------------------------ #
#  Clonación                                                          #
# ------------------------------------------------------------------ #
def _cleanup_partial(target, existed_before):
    """Tras un fallo o una cancelación no debe quedar una carpeta a
    medias que luego cuente como «ya existía»."""
    try:
        if not existed_before:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
        else:
            git_dir = target / ".git"
            if git_dir.exists():
                shutil.rmtree(git_dir, ignore_errors=True)
    except OSError:
        pass


def clone_repo(git, url, target, shallow=False, token="", timeout=900,
               on_line=None, register=None):
    """Clona `url` en `target`. Devuelve (estado, segundos, error) con
    estado ok | skip (ya existía y no estaba vacía) | fail."""
    target = Path(target)
    existed = target.exists()
    if existed and any(target.iterdir()):
        return "skip", 0.0, "ya existía"

    args = ["clone"]
    if shallow:
        args += ["--depth", "1", "--single-branch"]
    if on_line:
        args.append("--progress")
    args += [url, str(target)]
    env_extra = auth_env(url, token)
    t0 = time.time()
    lines = []

    try:
        if on_line:
            def tap(line):
                lines.append(line)
                on_line(line)
            rc = git.stream(args, cwd=str(target.parent), on_line=tap,
                            timeout=timeout, env_extra=env_extra,
                            register=register)
        else:
            p = git.run(args, cwd=str(target.parent), check=False,
                        timeout=timeout, env_extra=env_extra, echo=False,
                        register=register)
            rc = p.returncode
            lines = ((p.stderr or "") + (p.stdout or "")).strip().splitlines()
    except StepError as e:
        _cleanup_partial(target, existed)
        return "fail", time.time() - t0, str(e).splitlines()[0]

    if rc != 0:
        _cleanup_partial(target, existed)
        low = " ".join(lines).lower()
        last = lines[-1] if lines else ""
        msg = clone_error_message(low, last)
        if token:
            msg = msg.replace(token, "***")     # por si acaso: jamás mostrarlo
        return "fail", time.time() - t0, msg[:200]
    return "ok", time.time() - t0, ""


class BulkCloner:
    """Clona una lista de repositorios en paralelo. `stop()` cancela de
    verdad: no arranca más clonaciones y mata las que están en curso
    (sus carpetas a medias se eliminan)."""

    def __init__(self, git=None, workers=4):
        self.git = git or Git()
        self.workers = max(1, int(workers))
        self._stop = threading.Event()
        self._procs = set()
        self._lock = threading.Lock()

    @property
    def stopped(self):
        return self._stop.is_set()

    def stop(self):
        self._stop.set()
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            try:
                p.kill()
            except OSError:
                pass

    def _register(self, p):
        with self._lock:
            self._procs.add(p)
        if self._stop.is_set():             # carrera: stop() llegó antes
            try:
                p.kill()
            except OSError:
                pass

    def _forget(self, p):
        with self._lock:
            self._procs.discard(p)

    def run(self, repos, base_dir, shallow, token, on_event):
        """`repos` son dicts con name, clone_url y private (de
        github.list_user_repos). `on_event("start", nombre)` y
        `on_event("done", (nombre, estado, segundos, error, hechos,
        total))` llegan desde hilos de trabajo: la interfaz debe
        encolarlos, no tocar widgets ahí."""
        t0 = time.time()
        base_dir = Path(base_dir)
        base_dir.mkdir(parents=True, exist_ok=True)
        results = {"ok": 0, "skip": 0, "fail": 0, "cancel": 0}
        fails = []

        def job(r):
            if self._stop.is_set():
                return r["name"], "cancel", 0.0, ""
            on_event("start", r["name"])
            holder = []

            def register(p):
                holder.append(p)
                self._register(p)

            try:
                st, secs, err = clone_repo(
                    self.git, r["clone_url"], base_dir / r["name"],
                    shallow=shallow,
                    token=token if r.get("private") else "",
                    register=register)
            finally:
                for p in holder:
                    self._forget(p)
            if st == "fail" and self._stop.is_set():
                st, err = "cancel", ""
            return r["name"], st, secs, err

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futures = [ex.submit(job, r) for r in repos]
            for fut in as_completed(futures):
                name, st, secs, err = fut.result()
                results[st] += 1
                if st == "fail":
                    fails.append(f"{name}: {err}")
                done = sum(results.values())
                on_event("done", (name, st, secs, err, done, len(repos)))

        return {"seconds": time.time() - t0, "fails": fails, **results}
