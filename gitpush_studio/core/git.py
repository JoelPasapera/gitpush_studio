# -*- coding: utf-8 -*-
"""Todo lo que habla con el ejecutable git.

`Git` es un envoltorio inyectable: recibe una función `log` y todas las
operaciones pasan por `run`/`stream`, así que la interfaz, la CLI y
los tests lo usan igual. Los analizadores de salida son funciones
puras (sin git) para poder probarlos con texto fijo.
"""

import base64
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .errors import StepError
from .fs import NO_WINDOW
from .refnames import branch_problem
from .safekeeping import backup as safe_backup
from .safekeeping import (RepoInTheWay, files_at_risk,
                          operation_in_progress, unmerged_paths)
from .snapshot import fetch_snapshot, update_snapshot

GIT_MISSING = ("Git no está instalado o no está en el PATH.\n"
               "  Descárgalo en https://git-scm.com y reinicia la app.")

# Mensajes en inglés (LC_ALL=C) para poder reconocerlos, y nunca
# quedarse colgado pidiendo credenciales en una ventana invisible.
BASE_ENV = {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "LANG": "C"}


def _noop_log(text, tag="dim"):
    pass


def _as_text(data):
    """Salida de git como texto, venga en bytes o ya descodificada."""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data or ""


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


# Un código numérico solo cuenta donde git lo pone («returned error:
# 429», «status 500»), nunca suelto por la salida: una carpeta llamada
# /tmp/tmp.k429xq no es un límite de peticiones, y confundirlos llevaba a
# dar un mensaje falso y a reintentar tres veces un error definitivo.
_HTTP_CODE = re.compile(r"(?:error|status|code|http|returned)\D{0,6}"
                        r"(\d{3})(?!\d)")

# Fallos que no son culpa de nadie y suelen arreglarse solos: un corte
# de red, el servidor ocupado, o GitHub pidiendo que bajemos el ritmo.
TRANSIENT_HINTS = ("too many requests", "rate limit", "try again",
                   "timed out", "timeout", "connection reset",
                   "connection refused", "could not resolve host",
                   "early eof", "rpc failed", "unexpected disconnect",
                   "the remote end hung up", "gnutls_handshake",
                   "ssl_read", "empty reply from server")
RATE_LIMIT_HINTS = ("too many requests", "rate limit")

AUTH_HINTS = ("authentication", "could not read username",
              "could not read password", "invalid username or password",
              "bad credentials")


def http_code(output_lower):
    """Código HTTP que git menciona, o 0 si no menciona ninguno."""
    m = _HTTP_CODE.search(output_lower)
    return int(m.group(1)) if m else 0


def looks_like_auth_error(output_lower):
    # «denied» a secas aparece en cualquier ruta (/home/denied/…), así que
    # se exige que venga acompañado: git siempre habla de permiso o acceso
    # («Permission to X denied to Y», «Permission denied (publickey)»).
    denegado = "denied" in output_lower and (
        "permission" in output_lower or "access" in output_lower)
    return (denegado or any(h in output_lower for h in AUTH_HINTS)
            or http_code(output_lower) in (401, 403))


def looks_transient(output_lower):
    """¿Merece la pena reintentar? Un fallo de credenciales o un
    repositorio inexistente no se arregla insistiendo."""
    if looks_like_auth_error(output_lower) or "not found" in output_lower:
        return False
    return (any(h in output_lower for h in TRANSIENT_HINTS)
            or looks_rate_limited(output_lower)
            or 500 <= http_code(output_lower) < 600)


def looks_rate_limited(output_lower):
    return (any(h in output_lower for h in RATE_LIMIT_HINTS)
            or http_code(output_lower) == 429)


def push_error_message(output_lower):
    """Traduce el error de `git push` a una explicación con remedio."""
    o = output_lower
    if looks_like_auth_error(o):
        return ("GitHub rechazó las credenciales.\n"
                "  • Instala/actualiza Git Credential Manager "
                "(viene con Git para Windows).\n"
                "  • O ejecuta un push manual una vez para iniciar "
                "sesión en el navegador.\n"
                "  • Verifica que tu usuario tenga permisos en ese "
                "repositorio.")
    if looks_rate_limited(o):
        return ("GitHub pidió bajar el ritmo (demasiadas peticiones "
                "seguidas). Espera un momento, baja «Descargas a la vez» y "
                "vuelve a intentarlo.")
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
    if looks_rate_limited(o):
        return ("GitHub pidió bajar el ritmo (demasiadas descargas "
                "seguidas). Baja «Descargas a la vez» y vuelve a "
                "intentarlo.")
    if looks_like_auth_error(o):
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
def github_origin(url):
    """«https://github.com/» si `url` es de github.com por https (sin
    usuario ni puerto raro); cadena vacía en cualquier otro caso."""
    try:
        partes = urllib.parse.urlsplit((url or "").strip())
        puerto = partes.port
    except ValueError:
        return ""
    if partes.scheme.lower() != "https" or partes.username is not None \
            or partes.password is not None or puerto not in (None, 443):
        return ""
    if (partes.hostname or "").lower() != "github.com":
        return ""
    return "https://github.com/"


def auth_env(url, token, base_env=None):
    """Variables GIT_CONFIG_* (git ≥ 2.31) que añaden la cabecera de
    autorización solo para el origen de `url`. No tocan argv ni
    `.git/config`, así que no hay nada que limpiar después.

    Solo para github.com: el token es de GitHub, y un remoto que apunte a
    otro servidor (escrito a mano, heredado de un repositorio clonado)
    no debe recibirlo nunca."""
    origin = github_origin(url)
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
            echo=True, register=None, input=None, binary=False):
        """Ejecuta `git *args` y devuelve un CompletedProcess.

        `register(proc)` recibe el proceso nada más arrancar (para poder
        matarlo desde fuera). Con `check`, un código distinto de cero
        lanza StepError. `input` se manda por la entrada estándar.

        Con `binary`, la entrada y la salida son bytes SIN descodificar:
        es lo que hay que usar cuando la salida son rutas que luego se
        tocan en el disco. Descodificando con «reemplazar», un nombre que
        no es UTF-8 llega con «�» y ya no corresponde a ningún archivo.
        """
        if echo:
            self.log("$ git " + " ".join(args), "cmd")
        texto = {} if binary else {"text": True, "encoding": "utf-8",
                                   "errors": "replace"}
        try:
            p = subprocess.Popen(
                [self.exe, *args], cwd=cwd, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE if input is not None
                else subprocess.DEVNULL,
                creationflags=NO_WINDOW, env=self._env(env_extra), **texto)
        except FileNotFoundError:
            raise StepError(GIT_MISSING) from None
        if register:
            register(p)
        try:
            out, err = p.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
            raise StepError(f"El comando tardó demasiado: git "
                            f"{' '.join(args)}") from None
        result = subprocess.CompletedProcess(p.args, p.returncode, out, err)
        if echo:
            combined = (_as_text(out) + _as_text(err)).strip()
            for line in combined.splitlines():
                self.log("  " + line, "dim")
        if check and p.returncode != 0:
            raise StepError(f"Falló: git {' '.join(args)}")
        return result

    def read_blobs(self, folder, shas):
        """{sha: bytes} de varios blobs con un solo `git cat-file --batch`
        (un proceso por archivo sería lentísimo en Windows)."""
        pendientes = list(dict.fromkeys(shas))
        if not pendientes:
            return {}
        try:
            p = subprocess.Popen(
                [self.exe, "cat-file", "--batch"], cwd=folder,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, creationflags=NO_WINDOW,
                env=self._env())
        except FileNotFoundError:
            raise StepError(GIT_MISSING) from None
        out = {}
        try:
            # Por lotes que caben en el búfer de la tubería: git responde
            # en el mismo orden y nunca quedan los dos esperando.
            for i in range(0, len(pendientes), 256):
                lote = pendientes[i:i + 256]
                p.stdin.write(b"".join(s.encode("ascii") + b"\n"
                                       for s in lote))
                p.stdin.flush()
                for sha in lote:
                    cab = p.stdout.readline().split()
                    if len(cab) < 3 or cab[1] == b"missing":
                        raise StepError(f"Falta el objeto {sha} en el "
                                        "repositorio.")
                    tam = int(cab[2])
                    datos = p.stdout.read(tam)
                    if len(datos) != tam:
                        raise StepError("git cortó la lectura de un objeto.")
                    p.stdout.read(1)              # salto de línea final
                    out[sha] = datos
        finally:
            for tubo in (p.stdin, p.stdout):
                try:
                    tubo.close()
                except OSError:
                    pass
            p.wait()
        return out

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
        # El tiempo máximo lo vigila un temporizador aparte: si git se
        # queda colgado SIN escribir nada (una red que no responde), un
        # bucle que solo mira el reloj al recibir líneas no se enteraría.
        agotado = threading.Event()

        def vencer():
            agotado.set()
            try:
                p.kill()
            except OSError:
                pass

        reloj = threading.Timer(timeout, vencer)
        reloj.daemon = True
        reloj.start()
        assert p.stdout is not None
        try:
            with p.stdout:
                for raw in p.stdout:
                    for piece in raw.replace("\r", "\n").splitlines():
                        piece = piece.strip()
                        if piece:
                            on_line(piece)
            codigo = p.wait()
        finally:
            reloj.cancel()
        if agotado.is_set():
            on_line("Tiempo agotado.")
            return 1
        return codigo

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
        ilegibles = 0
        for rel in (p.stdout or "").split("\0"):
            if not rel or rel in seen:
                continue
            seen.add(rel)
            fp = os.path.join(root, rel)
            try:
                # lstat: un enlace es un enlace (git sube el texto de su
                # destino, no lo que hay al otro lado). Así ni la revisión
                # ni el TXT leen archivos de fuera del proyecto.
                st = os.lstat(fp)
            except OSError:
                # Un borrado pendiente es normal y se salta sin más. Pero
                # si el nombre llegó con «�», lo que falló es la
                # descodificación: git SÍ va a subir ese archivo y la
                # revisión de seguridad no podrá mirarlo. Callarlo sería
                # dar un «✔ sin hallazgos» que no es verdad.
                if "�" in rel:
                    ilegibles += 1
                continue
            if stat.S_ISDIR(st.st_mode):
                continue
            entries.append((rel, fp, st.st_size))
        if ilegibles:
            self.log(f"⚠ {ilegibles} archivo(s) tienen en el nombre "
                     "caracteres que no son UTF-8. Git los sube, pero la "
                     "revisión de seguridad no puede leerlos ni el TXT "
                     "incluirlos: renómbralos si pueden contener algo "
                     "sensible.", "warn")
        entries.sort(key=lambda e: e[0].lower())
        return entries

    def status_changes(self, folder, ignore_deletions=False):
        """Rutas con cambios sin publicar. Usa la caché de stat del
        índice de git: mucho más rápido que recorrer el árbol, y con
        las reglas de ignorado reales."""
        # «-uall»: aunque el usuario tenga status.showUntrackedFiles=no,
        # aquí hay que ver TODO lo no rastreado. Decidir «no hay cambios»
        # sin verlo es decidir que se puede pisar.
        p = self.run(["status", "--porcelain", "-z", "--untracked-files=all"],
                     cwd=folder, echo=False)
        return parse_status_z(p.stdout, ignore_deletions)

    def pending_changes(self, folder, ignore_deletions=False):
        """Lo que una publicación SUBIRÍA: como `status_changes`, sin los
        repositorios anidados («vendor/» con su propio .git), que al
        publicar se dejan fuera. Si contaran, la auto-publicación vería
        «cambios» en cada vuelta y publicaría para nada."""
        raiz = os.fspath(folder)
        return [rel for rel in self.status_changes(folder, ignore_deletions)
                if not (rel.endswith("/") and os.path.lexists(
                    os.path.join(raiz, rel, ".git")))]

    def tracked_files(self, folder):
        """Archivos que git ya está rastreando en esta carpeta."""
        p = self.run(["ls-files", "-z", "--cached"], cwd=folder, check=False,
                     echo=False)
        if p.returncode != 0:
            return []
        return [x for x in (p.stdout or "").split("\0") if x]

    def tracked_matching(self, folder, patterns):
        """Archivos YA rastreados que casan con `patterns`, según git.

        Lo decide git, no un comparador propio: los patrones de .gitignore
        tienen reglas que no son `fnmatch` (`**/`, negaciones, escapes,
        anclado, sensibilidad a mayúsculas según el sistema de archivos).
        Equivocarse aquí significa dejar en GitHub un archivo que el
        usuario mandó excluir, así que se pregunta a quien manda.

        Devuelve None si no se pudo preguntar (git viejo, error de E/S);
        quien llama decide entonces si usa un respaldo.
        """
        limpios = [p for p in (patterns or ()) if p and p.strip()]
        if not limpios:
            return []
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix="gpignore-", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(limpios) + "\n")
            p = self.run(["ls-files", "-z", "--cached", "--ignored",
                          "--exclude-from=" + tmp],
                         cwd=folder, check=False, echo=False)
            if p.returncode != 0:
                return None
            return [x for x in (p.stdout or "").split("\0") if x]
        except OSError:
            return None
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

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

    def remote_sha(self, url, branch, timeout=60, token=""):
        """Sha de una rama del remoto sin clonar y sin gastar API."""
        p = self.run(["ls-remote", url, f"refs/heads/{branch}"], check=False,
                     echo=False, timeout=timeout,
                     env_extra=auth_env(url, token))
        if p.returncode != 0:
            return ""
        primera = (p.stdout or "").split("\n")[0].split()
        return primera[0] if primera else ""

    def sha(self, folder, ref="HEAD"):
        p = self.run(["rev-parse", "--verify", "-q", ref], cwd=folder,
                     check=False, echo=False)
        return (p.stdout or "").strip() if p.returncode == 0 else ""

    def is_shallow(self, folder):
        p = self.run(["rev-parse", "--is-shallow-repository"], cwd=folder,
                     check=False, echo=False)
        return (p.stdout or "").strip() == "true"

    def remote_url(self, folder):
        p = self.run(["remote", "get-url", "origin"], cwd=folder, check=False,
                     echo=False)
        return (p.stdout or "").strip() if p.returncode == 0 else ""

    def remote_branches(self, url, timeout=60, token=""):
        """Ramas que existen en el remoto, sin necesitar repositorio local."""
        p = self.run(["ls-remote", "--heads", url], check=False, echo=False,
                     timeout=timeout, env_extra=auth_env(url, token))
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

    def wants_signature(self, folder):
        """¿Pide el usuario firmar sus commits (commit.gpgsign)?"""
        p = self.run(["config", "--bool", "commit.gpgsign"], cwd=folder,
                     check=False, echo=False)
        return (p.stdout or "").strip() == "true"

    def commit_tree(self, folder, tree, parents, message, identity=None,
                    sign=None):
        """Crea un commit con `commit-tree` (sin hooks, sin tocar el índice
        ni la carpeta, sin mover ninguna rama) y devuelve su sha.

        `sign=None` respeta commit.gpgsign como haría `git commit` (a
        diferencia de commit-tree, que nunca firma por su cuenta): un
        commit que va a publicarse sin la firma que el usuario exige lo
        rechazaría GitHub. `sign=False` no firma nunca (respaldos).
        """
        if sign is None:
            sign = self.wants_signature(folder)
        args = ["commit-tree", "-S" if sign else "--no-gpg-sign", tree]
        for padre in parents:
            args += ["-p", padre]
        args += ["-m", message] if isinstance(message, str) else \
            [a for parte in message for a in ("-m", parte)]
        p = self.run_committing(args, cwd=folder, identity=identity,
                                check=True)
        return (p.stdout or "").strip()

    def backup(self, folder, reason="antes de cambiar la carpeta",
               identity=None, at_risk=()):
        """Rama «respaldo-FECHA» con el estado EXACTO de la carpeta, sin
        mover la rama actual ni tocar el índice ni los archivos (véase
        `core.safekeeping`). `at_risk` son archivos ignorados que la
        operación siguiente va a pisar: también se guardan. Devuelve el
        nombre de la rama, o "" si no había nada que proteger."""
        return safe_backup(self, folder, reason, identity=identity,
                           at_risk=at_risk)

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
               on_line=None, register=None, retries=2, on_retry=None,
               sleep=time.sleep):
    """Clona `url` en `target`. Devuelve (estado, segundos, error) con
    estado ok | skip (ya existía y no estaba vacía) | fail.

    Un corte de red o un «baja el ritmo» del servidor no se dan por
    perdidos: se limpia lo descargado a medias y se reintenta con una
    espera creciente. Los fallos que no se arreglan insistiendo (token
    inválido, repositorio inexistente) fallan a la primera.
    """
    target = Path(target)
    existed = target.exists()
    if existed and any(target.iterdir()):
        return "skip", 0.0, "ya existía"
    t_total = time.time()
    for intento in range(retries + 1):
        estado, _, error, low = _clone_once(git, url, target, existed,
                                            shallow, token, timeout, on_line,
                                            register)
        if estado == "ok" or intento == retries or not looks_transient(low):
            return estado, time.time() - t_total, error
        espera = 3 * (intento + 1)
        if on_retry:
            on_retry(espera, looks_rate_limited(low), error)
        sleep(espera)
    return estado, time.time() - t_total, error


def _clone_once(git, url, target, existed, shallow, token, timeout, on_line,
                register):
    """Un intento de clonación. Devuelve (estado, segundos, error, salida)."""
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
        return "fail", time.time() - t0, str(e).splitlines()[0], ""

    if rc != 0:
        _cleanup_partial(target, existed)
        low = " ".join(lines).lower()
        last = lines[-1] if lines else ""
        msg = clone_error_message(low, last)
        if token:
            msg = msg.replace(token, "***")     # por si acaso: jamás mostrarlo
        return "fail", time.time() - t0, msg[:200], low
    return "ok", time.time() - t0, "", ""


MODE_CLONE = "clone"
MODE_SNAPSHOT = "snapshot"
# Qué hacer con lo que ya está descargado.
ON_EXISTS_SKIP = "skip"
ON_EXISTS_UPDATE = "update"


def update_clone(git, target, branch="", token="", timeout=900,
                 on_line=None, register=None):
    """Pone al día un repositorio ya clonado, trayendo solo lo que cambió.

    Nunca pisa trabajo tuyo: si la carpeta tiene cambios sin guardar
    (también archivos nuevos), commits propios que no están en GitHub,
    una operación de git a medias, otra rama activa, o archivos
    ignorados que la versión nueva sobrescribiría, se deja como está y
    se explica por qué. Lo que se aplica es exactamente el commit que se
    acaba de descargar (fijado por su sha). Devuelve (estado, segundos,
    mensaje) con estado ok | uptodate | skip | fail.
    """
    target = Path(target)
    t0 = time.time()
    if not (target / ".git").exists():
        return "fail", 0.0, ("la carpeta existe pero no es un repositorio "
                             "(¿es una instantánea?)")
    try:
        if operation_in_progress(target, git) or unmerged_paths(git, target):
            return "skip", 0.0, "tiene una operación de git a medias"
        if git.is_dirty(target):
            return "skip", 0.0, "tiene cambios sin guardar"
        actual = git.current_branch(target)
        rama = branch or actual or "main"
        if branch_problem(rama):
            return "fail", 0.0, f"la rama «{rama}» no tiene un nombre válido"
        if not actual:
            return "skip", 0.0, "no está en ninguna rama (HEAD suelto)"
        if actual != rama:
            return "skip", 0.0, (f"está en la rama «{actual}», no en "
                                 f"«{rama}»; no la toco")
        url = git.remote_url(target)
        if not url:
            return "skip", 0.0, "no tiene remoto configurado"

        antes = git.sha(target)
        rastreada = git.sha(target, f"refs/remotes/origin/{rama}")
        args = ["fetch"] + (["--depth", "1"] if git.is_shallow(target) else [])
        args += ["origin", f"refs/heads/{rama}"]
        p = git.run(args, cwd=target, check=False, timeout=timeout,
                    env_extra=auth_env(url, token), register=register)
        if p.returncode != 0:
            salida = ((p.stdout or "") + (p.stderr or "")).lower()
            return "fail", time.time() - t0, clone_error_message(salida)

        nuevo = git.sha(target, "FETCH_HEAD^{commit}")
        if not nuevo:
            return "fail", time.time() - t0, f"no encontré la rama «{rama}»"
        if nuevo == antes:
            return "uptodate", time.time() - t0, "ya estaba al día"

        # ¿Es una copia intacta de lo que se descargó? Si el usuario ha
        # hecho commits propios, HEAD ya no coincide con lo que había en
        # GitHub la última vez, y ponerla al día los perdería.
        intacta = (antes == rastreada) if rastreada else \
            git.is_ancestor(target, antes, nuevo)
        if not intacta:
            return "skip", time.time() - t0, ("tiene commits propios; "
                                              "no la toco")
        # Git trata lo ignorado como desechable: un .env tuyo en una
        # ruta que la versión nueva trae se perdería sin aviso.
        # La descarga puede tardar minutos: lo que se comprobó al empezar
        # ya no vale. Si mientras tanto has tocado algo, no se pisa.
        if git.is_dirty(target) or git.sha(target) != antes:
            return "skip", time.time() - t0, ("cambió mientras se "
                                              "descargaba; no la toco")
        try:
            pisados = files_at_risk(git, target, nuevo)
        except RepoInTheWay as e:
            return "skip", time.time() - t0, (
                f"«{e.paths[0]}» es otro repositorio de git y la versión "
                "nueva pone un archivo en su sitio; no la toco")
        if pisados:
            return "skip", time.time() - t0, (
                f"la versión nueva sobrescribiría «{pisados[0]}»"
                + (f" y {len(pisados) - 1} más" if len(pisados) > 1 else "")
                + ", que no está en git; no la toco")
        git.run(["reset", "--hard", "-q", nuevo], cwd=target, check=True,
                timeout=timeout)
        return "ok", time.time() - t0, "actualizado"
    except StepError as e:
        return "fail", time.time() - t0, str(e).splitlines()[0]


def _actualizar(git, repo, carpeta, token, mode, register,
                should_stop=None):
    """Pone al día lo ya descargado, según cómo se bajó en su día.

    El «ok» de las funciones de actualizar se traduce a «update» para
    que el resumen distinga lo que se puso al día de lo que se descargó
    por primera vez.
    """
    rama = repo.get("default_branch") or ""
    if mode == MODE_SNAPSHOT or not (carpeta / ".git").exists():
        sha = git.remote_sha(repo["clone_url"], rama or "main",
                             token=token) if repo.get("clone_url") else ""
        estado, segundos, mensaje = update_snapshot(
            repo.get("owner") or "", repo["name"], carpeta, branch=rama,
            token=token, sha_remoto=sha, should_stop=should_stop)
    else:
        estado, segundos, mensaje = update_clone(
            git, carpeta, branch=rama, token=token, register=register)
    return ("update" if estado == "ok" else estado), segundos, mensaje


class BulkCloner:
    """Descarga una lista de repositorios en paralelo.

    Dos modos: `clone` deja un repositorio de git completo (se puede
    publicar y actualizar), y `snapshot` baja el .tar.gz y extrae solo
    los archivos (mucho más rápido, pero esa carpeta ya no es un
    repositorio). `stop()` cancela de verdad: no arranca más descargas y
    mata los procesos en curso, borrando lo que quedó a medias.
    """

    PAUSE_SECONDS = 20

    def __init__(self, git=None, workers=4, sleep=time.sleep):
        self.git = git or Git()
        self.workers = max(1, int(workers))
        self._stop = threading.Event()
        self._procs = set()
        self._lock = threading.Lock()
        self._sleep = sleep
        self._busy_until = 0.0      # GitHub pidió bajar el ritmo

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

    def _slow_down(self, on_event):
        """Un «429» no es problema de un repositorio: es de todos. Se
        frena la cola entera en vez de que los demás sigan insistiendo."""
        with self._lock:
            ya_frenado = self._busy_until > time.time()
            self._busy_until = time.time() + self.PAUSE_SECONDS
        if not ya_frenado:
            on_event("slow", self.PAUSE_SECONDS)

    def _wait_if_busy(self):
        """Espera a que pase la pausa. Sobre el evento de parada, no con
        un sleep: así «Detener» la interrumpe al instante en vez de
        dejar al usuario mirando una ventana congelada."""
        while not self._stop.is_set():
            with self._lock:
                restante = self._busy_until - time.time()
            if restante <= 0:
                return
            self._stop.wait(min(restante, 1.0))

    def run(self, repos, base_dir, shallow, token, on_event,
            mode=MODE_CLONE, on_exists=ON_EXISTS_SKIP):
        """`repos` son dicts con name, clone_url, private, owner y
        default_branch (de github.list_user_repos). `on_event("start",
        nombre)` y `on_event("done", (nombre, estado, segundos, error,
        hechos, total))` llegan desde hilos de trabajo: la interfaz debe
        encolarlos, no tocar widgets ahí."""
        t0 = time.time()
        base_dir = Path(base_dir)
        base_dir.mkdir(parents=True, exist_ok=True)
        results = {"ok": 0, "skip": 0, "fail": 0, "cancel": 0,
                   "update": 0, "uptodate": 0}
        fails = []

        def job(r):
            self._wait_if_busy()
            if self._stop.is_set():
                return str(r.get("name") or "?"), "cancel", 0.0, ""
            on_event("start", r.get("name") or "?")
            holder = []

            def register(p):
                holder.append(p)
                self._register(p)

            def on_retry(espera, limitado, error):
                if limitado:
                    self._slow_down(on_event)
                on_event("retry", (r["name"], espera, error))

            nombre = str(r.get("name") or "")
            if not nombre or nombre in (".", "..") or "/" in nombre \
                    or "\\" in nombre:
                return nombre or "?", "fail", 0.0, "nombre de repositorio no válido"
            carpeta = base_dir / nombre
            try:
                ya_estaba = carpeta.exists() and any(carpeta.iterdir())
            except OSError as e:
                return nombre, "fail", 0.0, f"no puedo leer la carpeta: {e}"
            try:
                if ya_estaba and on_exists == ON_EXISTS_UPDATE:
                    st, secs, err = _actualizar(
                        self.git, r, carpeta, token, mode, register,
                        should_stop=self._stop.is_set)
                elif mode == MODE_SNAPSHOT:
                    st, secs, err = fetch_snapshot(
                        r.get("owner") or "", r["name"], carpeta,
                        branch=r.get("default_branch", ""), token=token,
                        should_stop=self._stop.is_set, on_retry=on_retry,
                        sleep=self._sleep,
                        sha=self.git.remote_sha(r["clone_url"],
                                                r.get("default_branch")
                                                or "main", token=token)
                        if r.get("default_branch") else "")
                else:
                    st, secs, err = clone_repo(
                        self.git, r["clone_url"], carpeta,
                        shallow=shallow,
                        token=token if r.get("private") else "",
                        register=register, on_retry=on_retry,
                        sleep=self._sleep)
            except Exception as e:                          # noqa: BLE001
                # Un repositorio raro no puede tumbar la descarga de los
                # demás (ni dejar la ventana esperando para siempre).
                st, secs, err = "fail", 0.0, (f"error inesperado: "
                                              f"{type(e).__name__}: {e}")[:200]
            finally:
                for p in holder:
                    self._forget(p)
            if st == "fail" and self._stop.is_set():
                st, err = "cancel", ""
            return nombre, st, secs, err

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
