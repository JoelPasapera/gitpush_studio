# -*- coding: utf-8 -*-
"""
GitPush Studio
==============
Publica, clona y empaqueta tus proyectos de GitHub sin tocar la terminal.

Requisitos
----------
1. Python 3.9 o superior
2. Git instalado (https://git-scm.com)
3. La librería customtkinter:

       pip install customtkinter

Uso
---
       python gitpush_studio.py

Novedades 1.5
-------------
* «⬇⬇ Clonar perfil completo…»: descarga TODOS los repositorios de
  un perfil de GitHub a una carpeta con el nombre del usuario, en
  paralelo (2–8 descargas a la vez) y en modo ligero (--depth 1,
  solo la última versión). Omite forks y lo ya clonado, y
  opcionalmente une todos los proyectos en un único TXT al terminar
  (escritura en streaming, tope de 200 MB).

Novedades 1.4
-------------
* «⬇ Clonar repositorio…»: descarga cualquier repositorio de GitHub a
  la carpeta que elijas, con progreso en vivo, y lo deja cargado como
  proyecto actual.
* «📄 Consolidar a TXT…»: empaqueta todo el proyecto (estructura en
  árbol + contenido de cada archivo) en un único .txt. Por defecto se
  llama como el proyecto y se guarda junto a él, pero puedes elegir
  nombre y ubicación. Respeta el .gitignore y el cuadro de ignorar,
  omite binarios y archivos enormes, y nunca se incluye a sí mismo.

Novedades 1.3
-------------
* «🌍 Publicar como web…» (GitHub Pages con enlace real) y
  auto-publicación vigilando la carpeta.

Novedades 1.2
-------------
* Revisión de seguridad (archivos de más de 100 MB y posibles
  secretos) automática antes de publicar + botón «🛡 Analizar».

Novedades 1.1
-------------
* «➕ Crear repositorio nuevo…» con token personal (permiso «repo»).
  El token se guarda cifrado con tu cuenta de Windows (DPAPI).

Los perfiles se guardan en:  ~/.gitpush_studio/profiles.json
"""

import os
import re
import sys
import json
import time
import queue
import base64
import fnmatch
import hashlib
import threading
import subprocess
import webbrowser
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox

# ------------------------------------------------------------------ #
#  Dependencia externa                                                #
# ------------------------------------------------------------------ #
try:
    import customtkinter as ctk
except ImportError:
    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror(
        "GitPush Studio",
        "Falta la librería 'customtkinter'.\n\n"
        "Instálala con este comando y vuelve a abrir la app:\n\n"
        "    pip install customtkinter",
    )
    sys.exit(1)

# ------------------------------------------------------------------ #
#  Constantes y tema (inspirado en el modo oscuro de GitHub)          #
# ------------------------------------------------------------------ #
APP_NAME = "GitPush Studio"
APP_VERSION = "1.5"

CONFIG_DIR = Path.home() / ".gitpush_studio"
CONFIG_FILE = CONFIG_DIR / "profiles.json"
GITHUB_API = "https://api.github.com"

BG      = "#0d1117"   # fondo general
PANEL   = "#161b22"   # tarjetas / sidebar
PANEL2  = "#21262d"   # hover / seleccionado
INPUT   = "#0d1117"   # fondo de campos
BORDER  = "#30363d"   # bordes
TEXT    = "#e6edf3"   # texto principal
MUTED   = "#8b949e"   # texto secundario
GREEN   = "#238636"   # acción principal (como el botón de GitHub)
GREEN_H = "#2ea043"
BLUE    = "#388bfd"
BLUE_D  = "#1f6feb"
RED     = "#f85149"
RED_D   = "#da3633"
AMBER   = "#d29922"
BADGE_RED_BG   = "#3d1d1f"
BADGE_RED_FG   = "#f09595"
BADGE_AMBER_BG = "#3a2d10"
BADGE_AMBER_FG = "#fac775"

MONO = "Consolas" if os.name == "nt" else "Menlo"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

TOTAL_STEPS = 9

# --- Límites de la revisión de seguridad --------------------------- #
MB = 1024 * 1024
LIMIT_HARD   = 100 * MB     # GitHub rechaza archivos mayores
LIMIT_WARN   = 50 * MB      # GitHub avisa a partir de aquí
MAX_CONTENT  = 1 * MB       # solo se lee el contenido de textos pequeños
CONTENT_BUDGET = 80 * MB    # tope total de lectura para no tardar
MAX_FINDINGS = 300

# --- Límites del consolidador a TXT -------------------------------- #
PACK_MAX_FILE  = 2 * MB          # contenido máximo por archivo
PACK_MAX_TOTAL = 100 * MB        # tamaño máximo del TXT (un proyecto)
PACK_MAX_TOTAL_MULTI = 200 * MB  # tamaño máximo del TXT (perfil entero)

BIN_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tif",
    ".pdf", ".zip", ".rar", ".7z", ".gz", ".tar", ".bz2", ".xz",
    ".mp3", ".wav", ".ogg", ".flac", ".mp4", ".avi", ".mov", ".mkv",
    ".webm", ".exe", ".dll", ".so", ".dylib", ".bin", ".dat", ".iso",
    ".dmg", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".psd", ".ai",
    ".sketch", ".pyc", ".class", ".jar", ".xlsx", ".docx", ".pptx",
}

SECRET_NAME_EXCLUDE = (".env.example", ".env.sample", ".env.template",
                       "*.pub")
SECRET_NAME_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "*.ppk",
    "*.keystore", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "credentials.json", "service-account*.json", "secrets.json",
    "secrets.yml", "secrets.yaml", ".npmrc", ".netrc", ".htpasswd",
)

SECRET_CONTENT_PATTERNS = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
     "llave privada dentro"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "token de GitHub dentro"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
     "token de GitHub dentro"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "clave de AWS dentro"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "clave de Google dentro"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
     "token de Slack dentro"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"), "clave API (sk-…) dentro"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\b\s*[:=]\s*"
                r"['\"][^'\"\n]{8,}['\"]"),
     "posible clave o contraseña escrita"),
)


class StepError(Exception):
    """Error controlado durante el proceso de publicación."""


# ------------------------------------------------------------------ #
#  Utilidades generales                                               #
# ------------------------------------------------------------------ #
def fmt_size(n):
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.0f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def parse_owner_repo(url):
    """Extrae (usuario, repositorio) de una URL de GitHub."""
    m = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$",
                  (url or "").strip())
    if m:
        return m.group(1), m.group(2)
    return None, None


def repo_name_from_url(url):
    owner, repo = parse_owner_repo(url)
    if repo:
        return repo
    tail = Path((url or "").rstrip("/")).name
    return tail[:-4] if tail.endswith(".git") else tail


def parse_username(text):
    """Acepta «usuario» o «https://github.com/usuario» y devuelve el
    nombre de usuario, o cadena vacía si no es válido."""
    t = (text or "").strip().rstrip("/")
    m = re.search(r"github\.com[:/]+([^/\s]+)", t)
    if m:
        t = m.group(1)
    return t if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", t) \
        else ""


def open_in_explorer(path):
    """Abre un archivo o carpeta con el programa predeterminado."""
    try:
        if os.name == "nt":
            os.startfile(path)                     # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception:
        pass


def place_dialog(win, parent, w, base_h, min_w=520, min_h=420):
    """Centra una ventana sobre su padre y ACOTA su alto a la pantalla,
    para que sus botones inferiores nunca queden fuera de vista.
    La ventana se hace redimensionable como red de seguridad."""
    try:
        win.resizable(True, True)
    except Exception:
        pass
    try:
        screen_h = win.winfo_screenheight()
    except Exception:
        screen_h = 720
    h = min(base_h, max(min_h, screen_h - 120))
    try:
        x = parent.winfo_x() + (parent.winfo_width() - w) // 2
        y = parent.winfo_y() + (parent.winfo_height() - h) // 2
        win.geometry(f"{w}x{h}+{max(x, 20)}+{max(y, 20)}")
    except Exception:
        win.geometry(f"{w}x{h}")
    try:
        win.minsize(min_w, min_h)
    except Exception:
        pass


# ------------------------------------------------------------------ #
#  Coincidencia estilo .gitignore (simplificada) y patrones           #
# ------------------------------------------------------------------ #
def _pattern_matches(pat, rel, name):
    p = pat.strip().rstrip("/")
    if not p or p.startswith("#") or p.startswith("!"):
        return False
    if "/" in p:
        p = p.lstrip("/")
        return (fnmatch.fnmatch(rel, p)
                or fnmatch.fnmatch(rel, p + "/*")
                or rel.startswith(p + "/"))
    if fnmatch.fnmatch(name, p):
        return True
    return any(fnmatch.fnmatch(seg, p) for seg in rel.split("/")[:-1])


def _is_ignored(rel, name, patterns):
    return any(_pattern_matches(p, rel, name) for p in patterns)


def _looks_secret_name(name):
    low = name.lower()
    for pat in SECRET_NAME_EXCLUDE:
        if fnmatch.fnmatch(low, pat):
            return False
    return any(fnmatch.fnmatch(low, pat) for pat in SECRET_NAME_PATTERNS)


def _merged_patterns(folder, extra_patterns):
    patterns = [p.strip() for p in extra_patterns if p.strip()]
    gi = Path(folder) / ".gitignore"
    if gi.exists():
        try:
            for ln in gi.read_text(encoding="utf-8",
                                   errors="replace").splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#") and ln not in patterns:
                    patterns.append(ln)
        except Exception:
            pass
    return patterns


# ------------------------------------------------------------------ #
#  Revisión de seguridad: archivos grandes y posibles secretos        #
# ------------------------------------------------------------------ #
def scan_project(folder, extra_patterns):
    """Devuelve (hallazgos, estadísticas).

    Cada hallazgo: {"path", "size", "kind": huge|big|secret, "reason"}.
    """
    t0 = time.time()
    folder = Path(folder)
    patterns = _merged_patterns(folder, extra_patterns)

    findings, truncated = [], False
    files_seen = content_read = 0
    budget = CONTENT_BUDGET

    for root, dirs, files in os.walk(folder):
        relroot = Path(root).relative_to(folder).as_posix()
        if relroot == ".":
            relroot = ""
        dirs[:] = [d for d in dirs
                   if d != ".git"
                   and not _is_ignored((relroot + "/" + d).lstrip("/"),
                                       d, patterns)]
        for name in files:
            rel = (relroot + "/" + name).lstrip("/")
            if _is_ignored(rel, name, patterns):
                continue
            fp = os.path.join(root, name)
            try:
                size = os.stat(fp).st_size
            except OSError:
                continue
            files_seen += 1

            if size > LIMIT_HARD:
                findings.append({"path": rel, "size": size, "kind": "huge",
                                 "reason": f"{fmt_size(size)} · GitHub lo "
                                           f"rechaza (más de 100 MB)"})
            elif size > LIMIT_WARN:
                findings.append({"path": rel, "size": size, "kind": "big",
                                 "reason": f"{fmt_size(size)} · aviso "
                                           f"(más de 50 MB)"})
            elif _looks_secret_name(name):
                findings.append({"path": rel, "size": size,
                                 "kind": "secret",
                                 "reason": "nombre típico de archivo "
                                           "con claves"})
            else:
                ext = os.path.splitext(name)[1].lower()
                if (size <= MAX_CONTENT and size > 0
                        and ext not in BIN_EXTS and budget > 0):
                    try:
                        with open(fp, "rb") as fh:
                            raw = fh.read(MAX_CONTENT)
                    except OSError:
                        raw = b""
                    budget -= len(raw)
                    if raw and b"\x00" not in raw[:4096]:
                        content_read += 1
                        txt = raw.decode("utf-8", "replace")
                        for rx, reason in SECRET_CONTENT_PATTERNS:
                            if rx.search(txt):
                                findings.append({"path": rel, "size": size,
                                                 "kind": "secret",
                                                 "reason": reason})
                                break

            if len(findings) >= MAX_FINDINGS:
                truncated = True
                break
        if truncated:
            break

    order = {"huge": 0, "secret": 1, "big": 2}
    findings.sort(key=lambda f: (order[f["kind"]], -f["size"]))
    stats = {"files": files_seen, "content": content_read,
             "seconds": time.time() - t0, "truncated": truncated}
    return findings, stats


def folder_fingerprint(folder, extra_patterns):
    """Huella rápida del contenido (rutas + tamaños + fechas)."""
    folder = Path(folder)
    if not folder.is_dir():
        return None
    patterns = _merged_patterns(folder, extra_patterns)
    h = hashlib.md5()
    for root, dirs, files in os.walk(folder):
        relroot = Path(root).relative_to(folder).as_posix()
        if relroot == ".":
            relroot = ""
        dirs[:] = sorted(d for d in dirs
                         if d != ".git"
                         and not _is_ignored((relroot + "/" + d).lstrip("/"),
                                             d, patterns))
        for name in sorted(files):
            rel = (relroot + "/" + name).lstrip("/")
            if _is_ignored(rel, name, patterns):
                continue
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                continue
            h.update(f"{rel}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


# ------------------------------------------------------------------ #
#  Consolidar el proyecto en un único TXT                             #
# ------------------------------------------------------------------ #
def render_tree(root_name, rel_paths):
    """Dibuja el árbol del proyecto a partir de las rutas incluidas."""
    tree = {}
    for rel in rel_paths:
        node = tree
        parts = rel.split("/")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = None
    lines = [root_name + "/"]

    def walk(node, prefix):
        dirs = sorted((k for k, v in node.items() if isinstance(v, dict)),
                      key=str.lower)
        files = sorted((k for k, v in node.items() if v is None),
                       key=str.lower)
        entries = dirs + files
        for i, name in enumerate(entries):
            last = i == len(entries) - 1
            branch = "└── " if last else "├── "
            suffix = "/" if isinstance(node[name], dict) else ""
            lines.append(prefix + branch + name + suffix)
            if isinstance(node[name], dict):
                walk(node[name], prefix + ("    " if last else "│   "))

    walk(tree, "")
    return "\n".join(lines)


def _collect_included(project_dir, patterns, exclude_file=None):
    """Recorre el proyecto una sola vez y devuelve los archivos a
    incluir como (ruta_relativa, ruta, tamaño), más los ignorados."""
    project_dir = Path(project_dir)
    included, skipped = [], 0
    for root, dirs, files in os.walk(project_dir):
        relroot = Path(root).relative_to(project_dir).as_posix()
        if relroot == ".":
            relroot = ""
        dirs[:] = [d for d in dirs
                   if d != ".git"
                   and not _is_ignored((relroot + "/" + d).lstrip("/"),
                                       d, patterns)]
        for name in files:
            rel = (relroot + "/" + name).lstrip("/")
            fp = Path(root) / name
            try:
                if exclude_file is not None \
                        and fp.resolve() == exclude_file:
                    continue                  # jamás incluirse a sí mismo
                size = fp.stat().st_size
            except OSError:
                continue
            if _is_ignored(rel, name, patterns):
                skipped += 1
                continue
            included.append((rel, fp, size))
    included.sort(key=lambda x: x[0].lower())
    return included, skipped


def _write_modules(out, included, counters, max_file_bytes,
                   max_total_bytes):
    """Vuelca los módulos al TXT en streaming. True si se truncó."""
    for rel, fp, size in included:
        out.write(f"MODULO: {rel}\n")
        out.write("-" * 80 + "\n")
        if size > max_file_bytes:
            out.write(f"<ARCHIVO OMITIDO POR TAMAÑO "
                      f"({fmt_size(size)})>\n")
            counters["big"] += 1
        else:
            raw = None
            try:
                raw = fp.read_bytes()
            except Exception as e:
                out.write(f"<ERROR AL LEER ARCHIVO: {e}>\n")
            if raw is not None:
                if raw and b"\x00" in raw[:4096]:
                    out.write(f"<ARCHIVO BINARIO OMITIDO "
                              f"({fmt_size(size)})>\n")
                    counters["binaries"] += 1
                else:
                    out.write(raw.decode("utf-8", "replace").rstrip()
                              + "\n")
        out.write("\n" + "=" * 80 + "\n\n")
        if out.tell() > max_total_bytes:
            out.write("<SALIDA TRUNCADA: se alcanzó el límite de "
                      f"{fmt_size(max_total_bytes)}>\n")
            return True
    return False


def consolidate_project(project_dir, output_file, extra_patterns,
                        respect_ignores=True,
                        max_file_bytes=PACK_MAX_FILE,
                        max_total_bytes=PACK_MAX_TOTAL):
    """Empaqueta un proyecto en un solo TXT (árbol + contenido).

    Respeta el .gitignore y los patrones dados, omite .git, marca los
    binarios y los archivos enormes, y nunca se incluye a sí mismo.
    Devuelve estadísticas del proceso.
    """
    t0 = time.time()
    project_dir = Path(project_dir).resolve()
    output_file = Path(output_file).resolve()
    if not project_dir.is_dir():
        raise FileNotFoundError(f"Directorio no válido: {project_dir}")

    if respect_ignores:
        patterns = _merged_patterns(project_dir, extra_patterns)
    else:
        patterns = [p.strip() for p in extra_patterns if p.strip()]

    included, skipped_ignored = _collect_included(
        project_dir, patterns, exclude_file=output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    counters = {"binaries": 0, "big": 0}
    with output_file.open("w", encoding="utf-8", newline="\n") as out:
        out.write(f"MODULO RAIZ: {project_dir.name}\n")
        out.write(f"GENERADO: {datetime.now():%Y-%m-%d %H:%M}   ·   "
                  f"ARCHIVOS INCLUIDOS: {len(included)}\n\n")
        out.write("ESTRUCTURA DEL PROYECTO\n")
        out.write("-" * 30 + "\n")
        out.write(render_tree(project_dir.name, [r for r, _, _ in included]))
        out.write("\n\n" + "=" * 80 + "\n\n")
        truncated = _write_modules(out, included, counters,
                                   max_file_bytes, max_total_bytes)

    return {"files": len(included), "binaries": counters["binaries"],
            "big": counters["big"], "ignored": skipped_ignored,
            "bytes": output_file.stat().st_size,
            "seconds": time.time() - t0, "truncated": truncated}


def render_profile_tree(username, repo_names):
    """Árbol general del perfil: la raíz es el usuario y cada
    repositorio es una rama."""
    lines = [username + "/"]
    names = sorted(repo_names, key=str.lower)
    for i, name in enumerate(names):
        last = i == len(names) - 1
        lines.append(("└── " if last else "├── ") + name + "/")
    return "\n".join(lines)


def consolidate_many(username, repo_dirs, output_file, extra_patterns,
                     respect_ignores=True, profile_url="",
                     max_file_bytes=PACK_MAX_FILE,
                     max_total_bytes=PACK_MAX_TOTAL_MULTI):
    """Une los repositorios de un PERFIL de GitHub en un único TXT, en
    streaming (nunca carga los proyectos en memoria).

    El TXT abre con una cabecera de perfil y un árbol general donde la
    raíz es el usuario y cada repositorio es una rama; después va el
    contenido detallado de cada repositorio, uno tras otro."""
    t0 = time.time()
    output_file = Path(output_file).resolve()
    output_file.parent.mkdir(parents=True, exist_ok=True)
    repo_dirs = sorted((Path(d) for d in repo_dirs),
                       key=lambda p: p.name.lower())
    repo_names = [d.name for d in repo_dirs]
    if not profile_url:
        profile_url = f"https://github.com/{username}"

    counters = {"binaries": 0, "big": 0}
    total_files = total_ignored = 0
    truncated = False
    with output_file.open("w", encoding="utf-8", newline="\n") as out:
        # -------- Cabecera del perfil + árbol general ----------------
        out.write("#" * 80 + "\n")
        out.write(f"PERFIL DE GITHUB: {username}\n")
        out.write("#" * 80 + "\n\n")
        out.write(f"Perfil: {profile_url}\n")
        out.write(f"Repositorios: {len(repo_dirs)}   ·   "
                  f"Generado: {datetime.now():%Y-%m-%d %H:%M}\n\n")
        out.write("ESTRUCTURA DEL PERFIL "
                  "(raíz: usuario · ramas: repositorios)\n")
        out.write("-" * 30 + "\n")
        out.write(render_profile_tree(username, repo_names))
        out.write("\n\n" + "=" * 80 + "\n\n")

        # -------- Contenido detallado de cada repositorio ------------
        for i, d in enumerate(repo_dirs, 1):
            if respect_ignores:
                patterns = _merged_patterns(d, extra_patterns)
            else:
                patterns = [p.strip() for p in extra_patterns
                            if p.strip()]
            included, ign = _collect_included(d, patterns,
                                              exclude_file=output_file)
            total_files += len(included)
            total_ignored += ign
            out.write("#" * 80 + "\n")
            out.write(f"REPOSITORIO {i}/{len(repo_dirs)}: "
                      f"{username}/{d.name}\n")
            out.write("#" * 80 + "\n\n")
            out.write("ESTRUCTURA DEL PROYECTO\n")
            out.write("-" * 30 + "\n")
            out.write(render_tree(d.name, [r for r, _, _ in included]))
            out.write("\n\n" + "=" * 80 + "\n\n")
            if _write_modules(out, included, counters,
                              max_file_bytes, max_total_bytes):
                truncated = True
                break

    return {"repos": len(repo_dirs), "files": total_files,
            "binaries": counters["binaries"], "big": counters["big"],
            "ignored": total_ignored,
            "bytes": output_file.stat().st_size,
            "seconds": time.time() - t0, "truncated": truncated}


# ------------------------------------------------------------------ #
#  Git con salida en vivo (para clonar)                               #
# ------------------------------------------------------------------ #
def run_git_stream(args, cwd, on_line, timeout=1800):
    """Ejecuta git mostrando cada línea de salida. Devuelve el código."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        p = subprocess.Popen(
            ["git", *args], cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", creationflags=NO_WINDOW, env=env)
    except FileNotFoundError:
        on_line("Git no está instalado o no está en el PATH.")
        return 127
    start = time.time()
    assert p.stdout is not None
    for raw in p.stdout:
        for piece in raw.replace("\r", "\n").splitlines():
            piece = piece.strip()
            if piece:
                on_line(piece)
        if time.time() - start > timeout:
            p.kill()
            on_line("Tiempo agotado.")
            return 1
    return p.wait()


# ------------------------------------------------------------------ #
#  Clonación masiva de un perfil (en paralelo)                        #
# ------------------------------------------------------------------ #
def list_user_repos(username, token=""):
    """Lista los repositorios de un perfil vía API (paginado).

    Con token, y si el perfil es el del propio token, incluye también
    los privados. Devuelve una lista de dicts con name, clone_url,
    fork, private y size_kb.
    """
    login = ""
    if token:
        code, me = github_api("GET", "/user", token)
        if code == 200:
            login = (me.get("login") or "")
    own = bool(token) and login.lower() == username.lower()

    repos, page = [], 1
    while True:
        if own:
            path = (f"/user/repos?per_page=100&page={page}"
                    f"&affiliation=owner&sort=full_name")
        else:
            path = (f"/users/{username}/repos?per_page=100&page={page}"
                    f"&sort=full_name")
        code, body = github_api("GET", path, token)
        if code == 404:
            raise StepError(f"El usuario «{username}» no existe en "
                            "GitHub.")
        if code == 403:
            raise StepError("GitHub limitó las peticiones (403). "
                            "Añade un token para subir el límite.")
        if code != 200 or not isinstance(body, list):
            msg = body.get("message", "") if isinstance(body, dict) else ""
            raise StepError(f"GitHub respondió {code}: {msg}")
        for r in body:
            repos.append({"name": r.get("name", ""),
                          "clone_url": r.get("clone_url", ""),
                          "fork": bool(r.get("fork")),
                          "private": bool(r.get("private")),
                          "size_kb": int(r.get("size") or 0)})
        if len(body) < 100 or page >= 30:
            break
        page += 1
    return repos


def clone_single_repo(repo, base_dir, shallow, token, timeout=900):
    """Clona un repositorio. Devuelve (nombre, estado, segundos, error).

    Estados: ok | skip (ya existía) | fail.
    """
    name = repo["name"]
    target = Path(base_dir) / name
    if target.exists() and any(target.iterdir()):
        return name, "skip", 0.0, "ya existía"

    url = repo["clone_url"]
    auth_url = url
    if repo.get("private") and token:
        # Autenticación solo durante la descarga; luego se limpia.
        auth_url = url.replace("https://",
                               f"https://x-access-token:{token}@", 1)
    args = ["clone"]
    if shallow:
        args += ["--depth", "1", "--single-branch"]
    args += [auth_url, str(target)]

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    t0 = time.time()
    try:
        p = subprocess.run(["git", *args], cwd=str(base_dir),
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=NO_WINDOW,
                           env=env)
    except subprocess.TimeoutExpired:
        return name, "fail", time.time() - t0, "tiempo agotado"
    except FileNotFoundError:
        return name, "fail", 0.0, "git no está instalado"

    if p.returncode != 0:
        tail = ((p.stderr or "") + (p.stdout or "")).strip().splitlines()
        msg = tail[-1] if tail else "error desconocido"
        if token:
            msg = msg.replace(token, "***")   # jamás mostrar el token
        return name, "fail", time.time() - t0, msg[:160]

    if auth_url != url:
        # Dejar el remoto limpio, sin el token dentro.
        subprocess.run(["git", "remote", "set-url", "origin", url],
                       cwd=str(target), capture_output=True,
                       creationflags=NO_WINDOW)
    return name, "ok", time.time() - t0, ""


def bulk_clone(repos, base_dir, shallow, token, workers, on_event,
               stop_event):
    """Clona una lista de repos en paralelo con un grupo de hilos."""
    t0 = time.time()
    base_dir = Path(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    results = {"ok": 0, "skip": 0, "fail": 0, "cancel": 0}
    fails = []

    def job(r):
        if stop_event.is_set():
            return r["name"], "cancel", 0.0, ""
        on_event("start", r["name"])
        return clone_single_repo(r, base_dir, shallow, token)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(job, r) for r in repos]
        for fut in as_completed(futures):
            name, st, secs, err = fut.result()
            results[st] += 1
            if st == "fail":
                fails.append(f"{name}: {err}")
            done = sum(results.values())
            on_event("done", (name, st, secs, err, done, len(repos)))

    return {"seconds": time.time() - t0, "fails": fails, **results}


# ------------------------------------------------------------------ #
#  Token: guardado cifrado (DPAPI en Windows, codificado en el resto) #
# ------------------------------------------------------------------ #
def _dpapi(data: bytes, encrypt: bool) -> bytes:
    """Cifra/descifra con la cuenta de usuario de Windows (DPAPI)."""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_byte))]

    buf_in = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data),
                        ctypes.cast(buf_in, ctypes.POINTER(ctypes.c_byte)))
    blob_out = DATA_BLOB()
    fn = (ctypes.windll.crypt32.CryptProtectData if encrypt
          else ctypes.windll.crypt32.CryptUnprotectData)
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI falló")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def protect_token(token: str) -> dict:
    raw = token.encode("utf-8")
    if os.name == "nt":
        try:
            return {"v": base64.b64encode(_dpapi(raw, True)).decode(),
                    "mode": "dpapi"}
        except Exception:
            pass
    return {"v": base64.b64encode(raw).decode(), "mode": "b64"}


def unprotect_token(entry) -> str:
    if not isinstance(entry, dict):
        return ""
    try:
        raw = base64.b64decode(entry.get("v", ""))
        if entry.get("mode") == "dpapi":
            if os.name != "nt":
                return ""
            raw = _dpapi(raw, False)
        return raw.decode("utf-8")
    except Exception:
        return ""


# ------------------------------------------------------------------ #
#  Cliente mínimo de la API de GitHub (sin dependencias extra)        #
# ------------------------------------------------------------------ #
def github_api(method, path, token, payload=None, timeout=30):
    """Devuelve (código_http, cuerpo_json). Lanza URLError si no hay red."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "GitPush-Studio",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        GITHUB_API + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, (json.loads(body) if body.strip() else {})
        except Exception:
            return e.code, {"message": body[:200]}


# ================================================================== #
#  Aplicación                                                        #
# ================================================================== #
class GitPushStudio(ctk.CTk):

    # ------------------------------------------------------------- #
    #  Construcción de la ventana                                    #
    # ------------------------------------------------------------- #
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")

        self.title(f"{APP_NAME} — Publica en GitHub sin terminal")
        self.geometry("1080x800")
        self.minsize(940, 700)
        self.configure(fg_color=BG)

        # Fuentes
        self.f_logo    = ctk.CTkFont(size=22, weight="bold")
        self.f_title   = ctk.CTkFont(size=19, weight="bold")
        self.f_section = ctk.CTkFont(size=14, weight="bold")
        self.f_body    = ctk.CTkFont(size=13)
        self.f_small   = ctk.CTkFont(size=11)
        self.f_btn     = ctk.CTkFont(size=15, weight="bold")
        self.f_mono    = ctk.CTkFont(family=MONO, size=12)
        self.f_mono_s  = ctk.CTkFont(family=MONO, size=11)

        # Estado interno
        self.q = queue.Queue()
        self.working = False
        self.scanning = False
        self.last_repo_web = ""
        self.last_pages_url = ""
        self.config_data = self._load_config()
        self.selected_profile = None
        self._profile_buttons = {}
        self._repo_dlg = None
        self._pages_dlg = None
        self._clone_dlg = None
        self._pack_dlg = None
        self._bulk_dlg = None
        self._review_event = threading.Event()
        self._review_result = ("cancel", [])
        self._auto_job = None
        self._last_pub_fp = None
        self._auto_fp = None

        # Estructura general: sidebar + panel principal
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_main()

        # Restaurar la última sesión
        self._apply_state(self.config_data.get("last_state", {}))
        last = self.config_data.get("last_profile")
        if last and last in self.config_data.get("profiles", {}):
            self._select_profile(last, load=False)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll_queue)

        self._log("Bienvenido a GitPush Studio.", "ok")
        self._log("Publica con «🚀», clona con «⬇» (o un perfil entero "
                  "con «⬇⬇»), empaqueta a TXT con «📄», activa tu web "
                  "con «🌍», o deja la auto-publicación vigilando por ti.",
                  "dim")

    # ------------------------------------------------------------- #
    #  Sidebar (perfiles y herramientas)                             #
    # ------------------------------------------------------------- #
    def _build_sidebar(self):
        sb = ctk.CTkFrame(self, width=250, corner_radius=0, fg_color=PANEL,
                          border_width=0)
        sb.grid(row=0, column=0, sticky="nsew")
        sb.grid_propagate(False)
        sb.grid_columnconfigure(0, weight=1)
        sb.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(sb, text="⬆ GitPush Studio", font=self.f_logo,
                     text_color=TEXT).grid(row=0, column=0, sticky="w",
                                           padx=18, pady=(20, 0))
        ctk.CTkLabel(sb, text="Tu proyecto, en GitHub, sin comandos.",
                     font=self.f_small, text_color=MUTED, justify="left"
                     ).grid(row=1, column=0, sticky="w", padx=18, pady=(2, 14))

        ctk.CTkFrame(sb, height=1, fg_color=BORDER).grid(
            row=2, column=0, sticky="ew", padx=14)

        ctk.CTkLabel(sb, text="PERFILES GUARDADOS", font=self.f_small,
                     text_color=MUTED).grid(row=3, column=0, sticky="w",
                                            padx=18, pady=(14, 4))

        self.profile_list = ctk.CTkScrollableFrame(
            sb, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=BORDER, scrollbar_button_hover_color=MUTED)
        self.profile_list.grid(row=4, column=0, sticky="nsew", padx=8)

        btns = ctk.CTkFrame(sb, fg_color="transparent")
        btns.grid(row=5, column=0, sticky="ew", padx=14, pady=(8, 4))
        btns.grid_columnconfigure(0, weight=1)

        ctk.CTkButton(btns, text="💾  Guardar perfil actual", height=34,
                      font=self.f_body, corner_radius=8,
                      fg_color=PANEL2, hover_color=BORDER,
                      border_width=1, border_color=BORDER,
                      text_color=TEXT, command=self._save_profile
                      ).grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ctk.CTkButton(btns, text="🗑  Eliminar perfil", height=30,
                      font=self.f_small, corner_radius=8,
                      fg_color="transparent", hover_color="#3d1d1f",
                      border_width=1, border_color=BORDER,
                      text_color=MUTED, command=self._delete_profile
                      ).grid(row=1, column=0, sticky="ew")

        ctk.CTkFrame(sb, height=1, fg_color=BORDER).grid(
            row=6, column=0, sticky="ew", padx=14, pady=(10, 0))
        ctk.CTkLabel(sb, text="HERRAMIENTAS", font=self.f_small,
                     text_color=MUTED).grid(row=7, column=0, sticky="w",
                                            padx=18, pady=(10, 4))
        tools = ctk.CTkFrame(sb, fg_color="transparent")
        tools.grid(row=8, column=0, sticky="ew", padx=14)
        tools.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(tools, text="⬇  Clonar repositorio…", height=34,
                      anchor="w", font=self.f_body, corner_radius=8,
                      fg_color=PANEL2, hover_color=BORDER,
                      border_width=1, border_color=BORDER,
                      text_color=TEXT, command=self._open_clone
                      ).grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ctk.CTkButton(tools, text="⬇⬇ Clonar perfil completo…", height=34,
                      anchor="w", font=self.f_body, corner_radius=8,
                      fg_color=PANEL2, hover_color=BORDER,
                      border_width=1, border_color=BORDER,
                      text_color=TEXT, command=self._open_bulk
                      ).grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ctk.CTkButton(tools, text="📄  Consolidar a TXT…", height=34,
                      anchor="w", font=self.f_body, corner_radius=8,
                      fg_color=PANEL2, hover_color=BORDER,
                      border_width=1, border_color=BORDER,
                      text_color=TEXT, command=self._open_consolidate
                      ).grid(row=2, column=0, sticky="ew")

        ctk.CTkLabel(sb, text="Los perfiles se guardan en\n~/.gitpush_studio",
                     font=self.f_small, text_color=MUTED, justify="left"
                     ).grid(row=9, column=0, sticky="w", padx=18,
                            pady=(12, 2))
        ctk.CTkLabel(sb, text=f"v{APP_VERSION}", font=self.f_small,
                     text_color=BORDER).grid(row=10, column=0, sticky="w",
                                             padx=18, pady=(0, 12))
        self._refresh_profile_list()

    # ------------------------------------------------------------- #
    #  Panel principal                                               #
    # ------------------------------------------------------------- #
    def _build_main(self):
        main = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)

        # --- Cabecera ------------------------------------------------
        head = ctk.CTkFrame(main, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=24, pady=(18, 8))
        head.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(head, text="Publicar proyecto", font=self.f_title,
                     text_color=TEXT).grid(row=0, column=0, sticky="w")
        self.auto_pill = ctk.CTkLabel(
            head, text="🤖 auto", font=self.f_small, text_color=MUTED,
            fg_color="transparent", corner_radius=12, width=70, height=26)
        self.auto_pill.grid(row=0, column=1, sticky="e", padx=(0, 8))
        self.status_pill = ctk.CTkLabel(
            head, text="●  Listo", font=self.f_small, text_color=MUTED,
            fg_color=PANEL, corner_radius=12, width=150, height=26)
        self.status_pill.grid(row=0, column=2, sticky="e")

        # --- Formulario (desplazable) --------------------------------
        form = ctk.CTkScrollableFrame(
            main, fg_color="transparent",
            scrollbar_button_color=BORDER, scrollbar_button_hover_color=MUTED)
        form.grid(row=1, column=0, sticky="nsew", padx=20)
        self._build_form(form)

        # --- Barra de acción -----------------------------------------
        act = ctk.CTkFrame(main, fg_color="transparent")
        act.grid(row=2, column=0, sticky="ew", padx=24, pady=(6, 6))
        act.grid_columnconfigure(0, weight=1)

        row = ctk.CTkFrame(act, fg_color="transparent")
        row.grid(row=0, column=0, sticky="ew")
        row.grid_columnconfigure(0, weight=1)

        self.btn_publish = ctk.CTkButton(
            row, text="🚀  Publicar en GitHub", height=46,
            font=self.f_btn, corner_radius=10,
            fg_color=GREEN, hover_color=GREEN_H, text_color="#ffffff",
            command=self._start_publish)
        self.btn_publish.grid(row=0, column=0, sticky="ew", padx=(0, 10))

        self.btn_scan = ctk.CTkButton(
            row, text="🛡  Analizar", height=46, width=140,
            font=self.f_body, corner_radius=10,
            fg_color="transparent", hover_color=PANEL2,
            border_width=1, border_color=BORDER, text_color=AMBER,
            command=self._start_scan)
        self.btn_scan.grid(row=0, column=1, padx=(0, 10))

        self.btn_open = ctk.CTkButton(
            row, text="🌐  Abrir repositorio", height=46, width=190,
            font=self.f_body, corner_radius=10,
            fg_color="transparent", hover_color=PANEL2,
            border_width=1, border_color=BORDER, text_color=BLUE,
            command=self._open_repo)
        self.btn_open.grid(row=0, column=2)

        self.progress = ctk.CTkProgressBar(
            act, height=6, corner_radius=3,
            fg_color=PANEL2, progress_color=GREEN_H)
        self.progress.set(0)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(10, 0))

        # --- Consola de actividad ------------------------------------
        logf = ctk.CTkFrame(main, fg_color=PANEL, corner_radius=12,
                            border_width=1, border_color=BORDER)
        logf.grid(row=3, column=0, sticky="ew", padx=24, pady=(6, 18))
        logf.grid_columnconfigure(0, weight=1)

        bar = ctk.CTkFrame(logf, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 0))
        bar.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(bar, text="CONSOLA DE ACTIVIDAD", font=self.f_small,
                     text_color=MUTED).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(bar, text="Limpiar", width=70, height=22,
                      font=self.f_small, corner_radius=6,
                      fg_color="transparent", hover_color=PANEL2,
                      border_width=1, border_color=BORDER, text_color=MUTED,
                      command=self._clear_log).grid(row=0, column=1)

        self.log_box = ctk.CTkTextbox(
            logf, height=180, font=self.f_mono, corner_radius=8,
            fg_color="#010409", text_color="#c9d1d9",
            border_width=0, wrap="word",
            scrollbar_button_color=BORDER, scrollbar_button_hover_color=MUTED)
        self.log_box.grid(row=1, column=0, sticky="ew", padx=12, pady=(6, 12))
        self.log_box.configure(state="disabled")
        self._setup_log_tags()

    # ------------------------------------------------------------- #
    #  Formulario                                                    #
    # ------------------------------------------------------------- #
    def _card(self, parent, title):
        c = ctk.CTkFrame(parent, fg_color=PANEL, corner_radius=12,
                         border_width=1, border_color=BORDER)
        c.pack(fill="x", padx=4, pady=(0, 12))
        ctk.CTkLabel(c, text=title, font=self.f_section, text_color=TEXT
                     ).pack(anchor="w", padx=16, pady=(12, 2))
        return c

    def _entry(self, parent, placeholder="", show=None):
        e = ctk.CTkEntry(parent, placeholder_text=placeholder, height=36,
                         font=self.f_body, corner_radius=8,
                         fg_color=INPUT, border_color=BORDER,
                         border_width=1, text_color=TEXT,
                         placeholder_text_color=MUTED)
        if show:
            e.configure(show=show)
        return e

    def _hint(self, parent, text):
        ctk.CTkLabel(parent, text=text, font=self.f_small, text_color=MUTED,
                     justify="left", wraplength=640
                     ).pack(anchor="w", padx=16, pady=(2, 12))

    def _build_form(self, form):
        # 1) Carpeta del proyecto -------------------------------------
        c = self._card(form, "📁  Carpeta del proyecto")
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(6, 0))
        row.grid_columnconfigure(0, weight=1)
        self.e_folder = self._entry(
            row, r"C:\Users\PC\Downloads\cartas-al-tiempo")
        self.e_folder.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(row, text="Examinar…", width=110, height=36,
                      font=self.f_body, corner_radius=8,
                      fg_color=PANEL2, hover_color=BORDER,
                      border_width=1, border_color=BORDER, text_color=TEXT,
                      command=self._browse_folder).grid(row=0, column=1)
        self._hint(c, "La carpeta local que quieres subir. "
                      "También puedes pegar rutas estilo Git Bash (/c/Users/…). "
                      "¿Aún no la tienes? Usa «⬇ Clonar repositorio…» en la "
                      "barra lateral.")

        # 2) Identidad Git --------------------------------------------
        c = self._card(form, "👤  Identidad Git")
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(6, 0))
        row.grid_columnconfigure((0, 1), weight=1)
        self.e_user = self._entry(row, "Nombre de usuario (ej. JoelPasapera)")
        self.e_user.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.e_email = self._entry(row, "Correo (ej. joelpasapera101@gmail.com)")
        self.e_email.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.v_local = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(c, text="Aplicar la identidad solo a este repositorio "
                                "(recomendado)",
                        variable=self.v_local, font=self.f_small,
                        text_color=MUTED, fg_color=GREEN, hover_color=GREEN_H,
                        border_color=BORDER, checkbox_width=18,
                        checkbox_height=18).pack(anchor="w", padx=16,
                                                 pady=(8, 0))
        self._hint(c, "Si dejas los campos vacíos se usará la identidad ya "
                      "configurada en tu equipo.")

        # 3) Repositorio ----------------------------------------------
        c = self._card(form, "🔗  Repositorio de GitHub")
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(6, 0))
        row.grid_columnconfigure(0, weight=3)
        row.grid_columnconfigure(1, weight=1)
        self.e_url = self._entry(
            row, "https://github.com/usuario/mi-repositorio.git")
        self.e_url.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.e_branch = self._entry(row, "main")
        self.e_branch.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.e_branch.insert(0, "main")
        row2 = ctk.CTkFrame(c, fg_color="transparent")
        row2.pack(fill="x", padx=16, pady=(8, 0))
        ctk.CTkButton(row2, text="➕  Crear repositorio nuevo…",
                      height=32, width=220, font=self.f_body,
                      corner_radius=8, fg_color=PANEL2, hover_color=BORDER,
                      border_width=1, border_color=BORDER, text_color=TEXT,
                      command=self._open_create_repo).pack(side="left")
        ctk.CTkButton(row2, text="🌍  Publicar como web…",
                      height=32, width=200, font=self.f_body,
                      corner_radius=8, fg_color=PANEL2, hover_color=BORDER,
                      border_width=1, border_color=BORDER, text_color=TEXT,
                      command=self._open_pages).pack(side="left", padx=(8, 0))
        self._hint(c, "«➕» crea el repositorio con tu token y rellena la "
                      "URL sola. «🌍» activa GitHub Pages para que tu "
                      "proyecto sea una página web con enlace propio "
                      "(requiere repositorio público y haber publicado "
                      "antes). A la derecha, la rama (normalmente «main»).")

        # 4) Archivos a ignorar ---------------------------------------
        c = self._card(form, "🙈  Archivos y carpetas a ignorar (.gitignore)")
        self.t_ignore = ctk.CTkTextbox(
            c, height=84, font=self.f_mono, corner_radius=8,
            fg_color=INPUT, border_color=BORDER, border_width=1,
            text_color=TEXT, wrap="none",
            scrollbar_button_color=BORDER)
        self.t_ignore.pack(fill="x", padx=16, pady=(6, 0))
        self.t_ignore.insert("1.0", "node_modules/\n")
        self._hint(c, "Un patrón por línea. Se añadirán al .gitignore sin "
                      "borrar lo que ya tenga. La revisión de seguridad "
                      "también puede añadir líneas aquí, y el consolidador "
                      "a TXT respeta esta misma lista.")

        # 5) Commit -----------------------------------------------------
        c = self._card(form, "📝  Mensaje del commit")
        self.e_msg = self._entry(c, "Actualizar proyecto completo")
        self.e_msg.pack(fill="x", padx=16, pady=(6, 0))
        self.e_msg.insert(0, "Actualizar proyecto completo")
        self.v_stamp = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(c, text="Añadir fecha y hora al mensaje",
                        variable=self.v_stamp, font=self.f_small,
                        text_color=MUTED, fg_color=GREEN, hover_color=GREEN_H,
                        border_color=BORDER, checkbox_width=18,
                        checkbox_height=18).pack(anchor="w", padx=16,
                                                 pady=(8, 12))

        # 6) Auto-publicación -------------------------------------------
        c = self._card(form, "🤖  Auto-publicación")
        rowa = ctk.CTkFrame(c, fg_color="transparent")
        rowa.pack(fill="x", padx=16, pady=(6, 0))
        self.v_auto = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(rowa, text="Vigilar la carpeta y publicar sola",
                      variable=self.v_auto, font=self.f_body,
                      text_color=TEXT, progress_color=GREEN,
                      button_color=TEXT, button_hover_color="#ffffff",
                      fg_color=PANEL2, command=self._toggle_auto
                      ).pack(side="left")
        ctk.CTkLabel(rowa, text="   Comprobar cada", font=self.f_small,
                     text_color=MUTED).pack(side="left")
        self.om_interval = ctk.CTkOptionMenu(
            rowa, values=["2 min", "5 min", "10 min", "30 min"],
            width=95, height=28, font=self.f_small,
            fg_color=PANEL2, button_color=BORDER,
            button_hover_color=MUTED, text_color=TEXT,
            dropdown_fg_color=PANEL, dropdown_text_color=TEXT,
            dropdown_hover_color=PANEL2, command=self._interval_changed)
        self.om_interval.set("10 min")
        self.om_interval.pack(side="left", padx=(8, 0))
        self._hint(c, "Si hubo cambios y ya dejaste de guardar, publica "
                      "sola (con la revisión de seguridad incluida: si "
                      "encuentra algo, se pausa y te avisa en vez de subir). "
                      "Por seguridad empieza apagada cada vez que abres "
                      "la app.")

        # 7) Opciones ----------------------------------------------------
        c = self._card(form, "⚙️  Opciones")
        self.v_force = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(c, text="Push forzado (-f): sobrescribe el historial "
                                "del repositorio remoto",
                        variable=self.v_force, font=self.f_small,
                        text_color=AMBER, fg_color=RED_D, hover_color=RED,
                        border_color=BORDER, checkbox_width=18,
                        checkbox_height=18).pack(anchor="w", padx=16,
                                                 pady=(6, 0))
        self.v_check = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(c, text="Revisión de seguridad antes de publicar "
                                "(archivos grandes y secretos)",
                        variable=self.v_check, font=self.f_small,
                        text_color=MUTED, fg_color=GREEN, hover_color=GREEN_H,
                        border_color=BORDER, checkbox_width=18,
                        checkbox_height=18).pack(anchor="w", padx=16,
                                                 pady=(8, 0))
        self._hint(c, "El push forzado reemplaza lo que haya en GitHub por "
                      "tu versión local: úsalo solo si sabes lo que haces.")

    # ------------------------------------------------------------- #
    #  Consola de actividad                                          #
    # ------------------------------------------------------------- #
    def _setup_log_tags(self):
        colors = {"cmd": "#79c0ff", "ok": "#3fb950", "err": "#f85149",
                  "warn": "#d29922", "dim": "#8b949e", "out": "#c9d1d9"}
        try:
            for tag, color in colors.items():
                self.log_box.tag_config(tag, foreground=color)
        except Exception:
            pass  # si la versión de customtkinter no soporta tags, sin color

    def _log(self, text, tag="out"):
        self.log_box.configure(state="normal")
        try:
            self.log_box.insert("end", text + "\n", tag)
        except Exception:
            self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    # ------------------------------------------------------------- #
    #  Estado / perfiles                                             #
    # ------------------------------------------------------------- #
    def _collect_state(self):
        return {
            "folder": self.e_folder.get().strip().strip('"'),
            "user": self.e_user.get().strip(),
            "email": self.e_email.get().strip(),
            "url": self.e_url.get().strip(),
            "branch": self.e_branch.get().strip() or "main",
            "ignore": self.t_ignore.get("1.0", "end").rstrip("\n"),
            "message": self.e_msg.get().strip(),
            "local_identity": bool(self.v_local.get()),
            "stamp": bool(self.v_stamp.get()),
            "force": bool(self.v_force.get()),
            "check": bool(self.v_check.get()),
            "auto_interval": self._interval_minutes(),
        }

    def _apply_state(self, s):
        if not s:
            return
        def put(entry, value):
            entry.delete(0, "end")
            if value:
                entry.insert(0, value)
        put(self.e_folder, s.get("folder", ""))
        put(self.e_user, s.get("user", ""))
        put(self.e_email, s.get("email", ""))
        put(self.e_url, s.get("url", ""))
        put(self.e_branch, s.get("branch", "main") or "main")
        put(self.e_msg, s.get("message", "Actualizar proyecto completo"))
        self.t_ignore.delete("1.0", "end")
        self.t_ignore.insert("1.0", s.get("ignore", "node_modules/"))
        self.v_local.set(s.get("local_identity", True))
        self.v_stamp.set(s.get("stamp", False))
        self.v_force.set(s.get("force", False))
        self.v_check.set(s.get("check", True))
        self.om_interval.set(f"{int(s.get('auto_interval', 10))} min")

    def _load_config(self):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("profiles", {})
                return data
        except Exception:
            pass
        return {"profiles": {}, "last_profile": None, "last_state": {}}

    def _save_config(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config_data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            self._log(f"⚠ No se pudo guardar la configuración: {exc}", "warn")

    def _refresh_profile_list(self):
        for w in self.profile_list.winfo_children():
            w.destroy()
        self._profile_buttons = {}
        profiles = sorted(self.config_data.get("profiles", {}))
        if not profiles:
            ctk.CTkLabel(self.profile_list,
                         text="Aún no hay perfiles.\nGuarda uno para "
                              "reutilizar\nesta configuración.",
                         font=self.f_small, text_color=MUTED,
                         justify="left").pack(anchor="w", padx=10, pady=8)
            return
        for name in profiles:
            b = ctk.CTkButton(
                self.profile_list, text="  " + name, anchor="w", height=32,
                font=self.f_body, corner_radius=8,
                fg_color=PANEL2 if name == self.selected_profile
                else "transparent",
                hover_color=PANEL2, text_color=TEXT,
                command=lambda n=name: self._select_profile(n))
            b.pack(fill="x", pady=2, padx=2)
            self._profile_buttons[name] = b

    def _select_profile(self, name, load=True):
        self.selected_profile = name
        self.config_data["last_profile"] = name
        if load:
            if self.v_auto.get():
                self.v_auto.set(False)
                self._toggle_auto()
            self._apply_state(self.config_data["profiles"].get(name, {}))
            self._log(f"Perfil «{name}» cargado.", "dim")
        self._refresh_profile_list()

    def _save_profile(self):
        dialog = ctk.CTkInputDialog(
            title="Guardar perfil",
            text="Nombre del perfil (ej. cartas-al-tiempo):")
        name = (dialog.get_input() or "").strip()
        if not name:
            return
        self.config_data["profiles"][name] = self._collect_state()
        self.selected_profile = name
        self.config_data["last_profile"] = name
        self._save_config()
        self._refresh_profile_list()
        self._log(f"Perfil «{name}» guardado.", "ok")

    def _delete_profile(self):
        name = self.selected_profile
        if not name or name not in self.config_data.get("profiles", {}):
            messagebox.showinfo(APP_NAME, "Selecciona primero un perfil "
                                          "de la lista.")
            return
        if not messagebox.askyesno(APP_NAME,
                                   f"¿Eliminar el perfil «{name}»?"):
            return
        self.config_data["profiles"].pop(name, None)
        if self.config_data.get("last_profile") == name:
            self.config_data["last_profile"] = None
        self.selected_profile = None
        self._save_config()
        self._refresh_profile_list()
        self._log(f"Perfil «{name}» eliminado.", "warn")

    # ------------------------------------------------------------- #
    #  Token de GitHub (recordado)                                   #
    # ------------------------------------------------------------- #
    def get_saved_token(self):
        return unprotect_token(self.config_data.get("github_token"))

    def store_token(self, token, remember):
        if remember and token:
            self.config_data["github_token"] = protect_token(token)
        else:
            self.config_data.pop("github_token", None)
        self._save_config()

    # ------------------------------------------------------------- #
    #  Apertura de diálogos                                          #
    # ------------------------------------------------------------- #
    def _singleton_dialog(self, attr, factory):
        dlg = getattr(self, attr)
        if dlg is not None and dlg.winfo_exists():
            dlg.lift()
            dlg.focus()
            return
        setattr(self, attr, factory())

    def _open_create_repo(self):
        self._singleton_dialog("_repo_dlg", lambda: CreateRepoDialog(self))

    def _open_pages(self):
        owner, repo = parse_owner_repo(self.e_url.get())
        if not owner:
            messagebox.showwarning(APP_NAME, "Escribe primero la URL del "
                                             "repositorio de GitHub.")
            return
        self._singleton_dialog("_pages_dlg",
                               lambda: PagesDialog(self, owner, repo))

    def _open_clone(self):
        self._singleton_dialog("_clone_dlg", lambda: CloneDialog(self))

    def _open_bulk(self):
        self._singleton_dialog("_bulk_dlg", lambda: BulkCloneDialog(self))

    def _open_consolidate(self):
        self._singleton_dialog("_pack_dlg",
                               lambda: ConsolidateDialog(self))

    # ------------------------------------------------------------- #
    #  Utilidades                                                    #
    # ------------------------------------------------------------- #
    @staticmethod
    def _normalize_path(p):
        """Acepta rutas de Windows y rutas estilo Git Bash (/c/Users/…)."""
        p = p.strip().strip('"').strip("'")
        m = re.match(r"^/([a-zA-Z])/(.*)$", p)
        if m and os.name == "nt":
            p = f"{m.group(1).upper()}:/{m.group(2)}"
        return str(Path(p))

    @staticmethod
    def _repo_web_url(url):
        """Convierte la URL del remoto en una URL navegable."""
        u = url.strip()
        m = re.match(r"^git@([^:]+):(.+?)(\.git)?$", u)
        if m:
            return f"https://{m.group(1)}/{m.group(2)}"
        return re.sub(r"\.git/?$", "", u)

    def _browse_folder(self):
        folder = filedialog.askdirectory(title="Selecciona la carpeta "
                                               "del proyecto")
        if folder:
            self.e_folder.delete(0, "end")
            self.e_folder.insert(0, str(Path(folder)))

    def _open_repo(self):
        url = self.last_repo_web or self._repo_web_url(self.e_url.get())
        if url.startswith("http"):
            webbrowser.open(url)
        else:
            messagebox.showinfo(APP_NAME, "Escribe primero la URL del "
                                          "repositorio.")

    def _set_status(self, text, kind="idle"):
        colors = {"idle": (PANEL, MUTED), "work": ("#132339", BLUE),
                  "ok": ("#12261e", "#3fb950"), "err": ("#2d1215", RED),
                  "warn": ("#2d2412", AMBER)}
        bg, fg = colors.get(kind, colors["idle"])
        self.status_pill.configure(text=text, fg_color=bg, text_color=fg)

    def _ignore_lines(self):
        return [ln.strip() for ln in
                self.t_ignore.get("1.0", "end").splitlines() if ln.strip()]

    def _add_ignore_patterns(self, paths):
        """Añade rutas al cuadro de ignorar, sin duplicados."""
        current = self._ignore_lines()
        new = [p for p in paths if p not in current]
        if not new:
            return
        self.t_ignore.delete("1.0", "end")
        self.t_ignore.insert("1.0", "\n".join(current + new) + "\n")
        self._log("Añadido al cuadro de ignorar: " + ", ".join(new), "dim")

    def set_current_project(self, folder, url):
        """Carga carpeta y URL en el formulario (p. ej. tras clonar)."""
        if self.v_auto.get():
            self.v_auto.set(False)
            self._toggle_auto()
        self.e_folder.delete(0, "end")
        self.e_folder.insert(0, str(folder))
        if url:
            self.e_url.delete(0, "end")
            self.e_url.insert(0, url)
        self._log("Proyecto cargado en el formulario.", "dim")

    # ------------------------------------------------------------- #
    #  Auto-publicación                                              #
    # ------------------------------------------------------------- #
    def _interval_minutes(self):
        try:
            return int(self.om_interval.get().split()[0])
        except Exception:
            return 10

    def _interval_changed(self, _value=None):
        if self.v_auto.get():
            self._schedule_auto()
            self._log(f"🤖 Intervalo cambiado: cada "
                      f"{self._interval_minutes()} min.", "dim")

    def _toggle_auto(self):
        if self.v_auto.get():
            folder = self._normalize_path(self.e_folder.get())
            if not folder or not Path(folder).is_dir():
                self.v_auto.set(False)
                messagebox.showwarning(APP_NAME, "Indica primero una "
                                                 "carpeta que exista.")
                return
            self.auto_pill.configure(fg_color="#12261e",
                                     text_color="#3fb950")
            self._log(f"🤖 Auto-publicación activada: comprobaré la carpeta "
                      f"cada {self._interval_minutes()} min.", "ok")
            patterns = self._ignore_lines()
            threading.Thread(
                target=lambda: self.q.put(
                    ("set_fp", folder_fingerprint(folder, patterns))),
                daemon=True).start()
            self._schedule_auto()
        else:
            self._cancel_auto()
            self.auto_pill.configure(fg_color="transparent",
                                     text_color=MUTED)
            self._log("🤖 Auto-publicación desactivada.", "dim")

    def _schedule_auto(self):
        self._cancel_auto()
        self._auto_job = self.after(self._interval_minutes() * 60 * 1000,
                                    self._auto_tick)

    def _cancel_auto(self):
        if self._auto_job is not None:
            try:
                self.after_cancel(self._auto_job)
            except Exception:
                pass
            self._auto_job = None

    def _auto_tick(self):
        self._auto_job = None
        if not self.v_auto.get():
            return
        if self.working or self.scanning:
            self._schedule_auto()
            return
        folder = self._normalize_path(self.e_folder.get())
        patterns = self._ignore_lines()
        last_fp = self._last_pub_fp
        threading.Thread(target=self._auto_check,
                         args=(folder, patterns, last_fp),
                         daemon=True).start()

    def _auto_check(self, folder, patterns, last_fp):
        fp1 = folder_fingerprint(folder, patterns)
        if fp1 is None:
            self.q.put(("auto_result", "missing", None))
            return
        if last_fp is not None and fp1 == last_fp:
            self.q.put(("auto_result", "idle", fp1))
            return
        time.sleep(5)  # deja que los cambios se asienten
        fp2 = folder_fingerprint(folder, patterns)
        if fp2 != fp1:
            self.q.put(("auto_result", "unstable", None))
        else:
            self.q.put(("auto_result", "publish", fp1))

    def _handle_auto_result(self, action, fp):
        if not self.v_auto.get():
            return
        if action == "missing":
            self._log("🤖 La carpeta ya no existe; auto-publicación "
                      "desactivada.", "warn")
            self.v_auto.set(False)
            self._toggle_auto()
            return
        if action == "publish" and not self.working and not self.scanning:
            self._log("🤖 Cambios detectados — publicando "
                      "automáticamente…", "out")
            self._auto_fp = fp
            self._start_publish(auto=True)
        elif action == "unstable":
            self._log("🤖 Aún estás guardando cambios; lo intento en la "
                      "próxima ronda.", "dim")
        self._schedule_auto()

    def _auto_abort(self, reason):
        """Apaga la auto-publicación con un aviso claro."""
        self._log(f"🤖 {reason} Auto-publicación en pausa.", "warn")
        if self.v_auto.get():
            self.v_auto.set(False)
            self._cancel_auto()
            self.auto_pill.configure(fg_color="transparent",
                                     text_color=MUTED)

    # ------------------------------------------------------------- #
    #  Análisis manual («🛡 Analizar»)                               #
    # ------------------------------------------------------------- #
    def _start_scan(self):
        if self.working or self.scanning:
            return
        folder = self._normalize_path(self.e_folder.get())
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning(APP_NAME, "Indica primero una carpeta "
                                             "de proyecto que exista.")
            return
        self.scanning = True
        self.btn_scan.configure(state="disabled", text="⏳  Analizando…")
        self.btn_publish.configure(state="disabled")
        self._set_status("●  Analizando…", "work")
        self._log("─" * 60, "dim")
        self._log("🛡 Analizando el proyecto en busca de archivos grandes "
                  "y secretos…", "out")
        patterns = self._ignore_lines()
        threading.Thread(target=self._scan_worker,
                         args=(folder, patterns), daemon=True).start()

    def _scan_worker(self, folder, patterns):
        try:
            findings, stats = scan_project(folder, patterns)
            self.q.put(("scan_result", findings, stats))
        except Exception as e:
            self.q.put(("log", f"✖ Falló el análisis: {e}", "err"))
            self.q.put(("scan_result", [], {"files": 0, "content": 0,
                                            "seconds": 0,
                                            "truncated": False}))

    def _finish_scan(self, findings, stats):
        self.scanning = False
        self.btn_scan.configure(state="normal", text="🛡  Analizar")
        self.btn_publish.configure(state="normal")
        self._log(f"  Revisados {stats['files']} archivos en "
                  f"{stats['seconds']:.1f} s.", "dim")
        if stats.get("truncated"):
            self._log("  ⚠ Hay más de 300 hallazgos; se muestran los "
                      "primeros.", "warn")
        if not findings:
            self._log("✔ Sin archivos grandes ni secretos a la vista.", "ok")
            self._set_status("●  Sin riesgos", "ok")
            return
        self._log(f"⚠ {len(findings)} hallazgo(s). Revisa la ventana.",
                  "warn")
        self._set_status("●  Hallazgos — revisa", "warn")
        ReviewDialog(self, findings, mode="manual",
                     on_done=self._manual_review_done)

    def _manual_review_done(self, decision, selected):
        if decision == "ignore" and selected:
            self._add_ignore_patterns(selected)
            self._log("✔ Se excluirán en la próxima publicación.", "ok")
        self._set_status("●  Listo", "idle")

    def _publish_review_done(self, decision, selected):
        self._review_result = (decision, selected)
        self._review_event.set()

    # ------------------------------------------------------------- #
    #  Publicación (hilo de trabajo + cola de mensajes)              #
    # ------------------------------------------------------------- #
    def _start_publish(self, auto=False):
        if self.working or self.scanning:
            return
        cfg = self._collect_state()
        cfg["folder"] = self._normalize_path(cfg["folder"])
        cfg["auto"] = auto

        # --- Validaciones ------------------------------------------
        def fail(msg):
            if auto:
                self._auto_abort(msg)
            else:
                messagebox.showwarning(APP_NAME, msg)

        if not cfg["folder"] or not Path(cfg["folder"]).is_dir():
            fail("Indica una carpeta de proyecto que exista.")
            return
        if not cfg["url"]:
            fail("Indica la URL del repositorio de GitHub, o créalo con "
                 "«➕ Crear repositorio nuevo…».")
            return
        if not (cfg["url"].startswith("https://")
                or cfg["url"].startswith("git@")):
            fail("La URL debe empezar por https:// o git@ — ejemplo: "
                 "https://github.com/usuario/repo.git")
            return
        if bool(cfg["user"]) != bool(cfg["email"]):
            fail("Completa nombre Y correo, o deja ambos vacíos.")
            return
        if not cfg["message"]:
            cfg["message"] = "Actualizar proyecto completo"
        if cfg["stamp"] or auto:
            cfg["message"] += datetime.now().strftime(" — %Y-%m-%d %H:%M")
        if cfg["force"] and not auto:
            if not messagebox.askyesno(
                    APP_NAME,
                    "Has activado el push forzado (-f).\n\n"
                    "Todo lo que haya en el repositorio remoto se "
                    "reemplazará por tu versión local.\n\n¿Continuar?"):
                return

        # Guardar como última sesión
        self.config_data["last_state"] = self._collect_state()
        self._save_config()

        self.working = True
        self.last_repo_web = ""
        self.btn_publish.configure(state="disabled", text="⏳  Publicando…")
        self.btn_scan.configure(state="disabled")
        self.progress.set(0)
        self._set_status("●  Trabajando…", "work")
        self._log("─" * 60, "dim")
        if auto:
            self._log("🤖 Publicación automática en marcha.", "dim")

        threading.Thread(target=self._worker, args=(cfg,),
                         daemon=True).start()

    # ---------- helpers del hilo de trabajo -----------------------
    def _run_git(self, args, cwd, check=True, timeout=900):
        self.q.put(("log", "$ git " + " ".join(args), "cmd"))
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"   # nunca colgarse pidiendo datos
        try:
            p = subprocess.run(
                ["git", *args], cwd=cwd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
                creationflags=NO_WINDOW, env=env)
        except FileNotFoundError:
            raise StepError("Git no está instalado o no está en el PATH.\n"
                            "  Descárgalo en https://git-scm.com y "
                            "reinicia la app.")
        except subprocess.TimeoutExpired:
            raise StepError(f"El comando tardó demasiado: git "
                            f"{' '.join(args)}")
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        if out:
            for line in out.splitlines():
                self.q.put(("log", "  " + line, "dim"))
        if check and p.returncode != 0:
            raise StepError(f"Falló: git {' '.join(args)}")
        return p

    def _step(self, n, text):
        self.q.put(("progress", n / TOTAL_STEPS))
        self.q.put(("log", f"[{n}/{TOTAL_STEPS}] {text}", "out"))

    def _worker(self, c):
        t0 = time.time()
        folder = c["folder"]
        to_untrack = []
        patterns = [ln.strip() for ln in c["ignore"].splitlines()
                    if ln.strip()]
        try:
            # 1) Comprobar Git
            self._step(1, "Comprobando que Git esté instalado…")
            self._run_git(["--version"], cwd=None)

            # 2) Inicializar el repositorio si hace falta
            self._step(2, "Preparando el repositorio local…")
            if (Path(folder) / ".git").exists():
                self.q.put(("log", "  El repositorio ya estaba "
                                   "inicializado.", "dim"))
            else:
                self._run_git(["init"], cwd=folder)

            # 3) Identidad
            self._step(3, "Configurando la identidad Git…")
            if c["user"] and c["email"]:
                scope = [] if c["local_identity"] else ["--global"]
                self._run_git(["config", *scope, "user.name", c["user"]],
                              cwd=folder)
                self._run_git(["config", *scope, "user.email", c["email"]],
                              cwd=folder)
            else:
                self.q.put(("log", "  Sin cambios: se usa la identidad ya "
                                   "configurada.", "dim"))

            # 4) Revisión de seguridad
            self._step(4, "Revisión de seguridad (archivos grandes y "
                          "secretos)…")
            if not c.get("check", True):
                self.q.put(("log", "  Desactivada en Opciones; se omite.",
                            "warn"))
            else:
                findings, stats = scan_project(folder, patterns)
                self.q.put(("log", f"  Revisados {stats['files']} archivos "
                                   f"en {stats['seconds']:.1f} s.", "dim"))
                if not findings:
                    self.q.put(("log", "  ✔ Sin archivos grandes ni "
                                       "secretos a la vista.", "ok"))
                elif c.get("auto"):
                    # En modo automático nunca subimos con hallazgos ni
                    # abrimos ventanas: se pausa y se avisa.
                    self.q.put(("log", f"  ⚠ {len(findings)} hallazgo(s).",
                                "warn"))
                    self.q.put(("auto_blocked",))
                    return
                else:
                    self.q.put(("log", f"  ⚠ {len(findings)} hallazgo(s). "
                                       "Esperando tu decisión…", "warn"))
                    self._review_event.clear()
                    self.q.put(("review", findings))
                    self._review_event.wait()
                    decision, selected = self._review_result
                    if decision == "cancel":
                        self.q.put(("log", "✋ Publicación cancelada. No se "
                                           "ha subido nada.", "warn"))
                        self.q.put(("cancelled",))
                        return
                    if decision == "ignore" and selected:
                        patterns.extend(p for p in selected
                                        if p not in patterns)
                        to_untrack = list(selected)
                        self.q.put(("add_ignores", list(selected)))
                        self.q.put(("log", f"  Se excluirán "
                                           f"{len(selected)} elemento(s).",
                                    "dim"))
                    else:
                        self.q.put(("log", "  Continuamos sin excluir "
                                           "nada.", "warn"))

            # 5) .gitignore
            self._step(5, "Actualizando .gitignore…")
            gi = Path(folder) / ".gitignore"
            existing = []
            if gi.exists():
                existing = gi.read_text(encoding="utf-8",
                                        errors="replace").splitlines()
            existing_set = {ln.strip() for ln in existing}
            new = [p for p in patterns if p not in existing_set]
            if new:
                text = "\n".join(existing + new).strip() + "\n"
                gi.write_text(text, encoding="utf-8")
                self.q.put(("log", f"  Añadido: {', '.join(new)}", "dim"))
            else:
                self.q.put(("log", "  Sin cambios necesarios.", "dim"))
            if to_untrack:
                # Si algún archivo excluido ya estaba en un commit anterior,
                # hay que dejar de rastrearlo (el archivo sigue en tu PC).
                for i in range(0, len(to_untrack), 50):
                    chunk = to_untrack[i:i + 50]
                    self._run_git(["rm", "-r", "--cached",
                                   "--ignore-unmatch", "--", *chunk],
                                  cwd=folder, check=False)

            # 6) git add
            self._step(6, "Añadiendo archivos (git add -A)…")
            self._run_git(["add", "-A"], cwd=folder)

            # 7) git commit
            self._step(7, "Creando el commit…")
            p = self._run_git(["commit", "-m", c["message"]],
                              cwd=folder, check=False)
            if p.returncode != 0:
                out = (p.stdout or "") + (p.stderr or "")
                if ("nothing to commit" in out
                        or "nada para hacer commit" in out
                        or "nada agregado al commit" in out):
                    self.q.put(("log", "  ⚠ No hay cambios nuevos; se "
                                       "intentará el push igualmente.",
                                "warn"))
                elif "tell me who you are" in out.lower():
                    raise StepError("Git no sabe quién eres. Completa "
                                    "nombre y correo en «Identidad Git».")
                else:
                    raise StepError("Falló el commit. Revisa la consola.")

            # 8) Rama y remoto
            self._step(8, f"Fijando la rama «{c['branch']}» y el remoto…")
            p = self._run_git(["branch", "-M", c["branch"]],
                              cwd=folder, check=False)
            if p.returncode != 0:
                self._run_git(["checkout", "-B", c["branch"]], cwd=folder)
            p = self._run_git(["remote", "get-url", "origin"],
                              cwd=folder, check=False)
            if p.returncode == 0:
                self._run_git(["remote", "set-url", "origin", c["url"]],
                              cwd=folder)
            else:
                self._run_git(["remote", "add", "origin", c["url"]],
                              cwd=folder)

            # 9) git push
            self._step(9, "Subiendo a GitHub (git push)…")
            args = ["push", "-u"] + (["-f"] if c["force"] else []) \
                   + ["origin", c["branch"]]
            p = self._run_git(args, cwd=folder, check=False, timeout=1800)
            if p.returncode != 0:
                out = ((p.stdout or "") + (p.stderr or "")).lower()
                if ("authentication" in out or "could not read username"
                        in out or "denied" in out or "403" in out):
                    raise StepError(
                        "GitHub rechazó las credenciales.\n"
                        "  • Instala/actualiza Git Credential Manager "
                        "(viene con Git para Windows).\n"
                        "  • O ejecuta un push manual una vez para iniciar "
                        "sesión en el navegador.\n"
                        "  • Verifica que tu usuario tenga permisos en ese "
                        "repositorio.")
                if "repository not found" in out:
                    raise StepError("GitHub dice que el repositorio no "
                                    "existe. Créalo con «➕ Crear "
                                    "repositorio nuevo…» o revisa la URL.")
                if ("this exceeds github's file size limit" in out
                        or "exceeds github" in out):
                    raise StepError(
                        "GitHub rechazó un archivo por tamaño. Pulsa "
                        "«🛡 Analizar», exclúyelo y vuelve a publicar.")
                if "non-fast-forward" in out or "rejected" in out:
                    raise StepError(
                        "El remoto tiene commits que no tienes en local.\n"
                        "  Opciones: haz un «git pull» primero, o activa "
                        "el push forzado si quieres reemplazarlo todo.")
                raise StepError("Falló el push. Revisa la consola.")

            # Éxito ------------------------------------------------
            web = self._repo_web_url(c["url"])
            secs = time.time() - t0
            self.q.put(("log", f"✔ ¡Proyecto publicado en {secs:.1f} s!",
                        "ok"))
            self.q.put(("log", f"  {web}", "cmd"))
            self.q.put(("done", True, web))

        except StepError as e:
            self.q.put(("log", f"✖ {e}", "err"))
            self.q.put(("done", False, ""))
        except Exception as e:                              # imprevisto
            self.q.put(("log", f"✖ Error inesperado: {e}", "err"))
            self.q.put(("done", False, ""))

    # ---------- cola de mensajes hacia la interfaz -----------------
    def _restore_buttons(self):
        self.working = False
        self.btn_publish.configure(state="normal",
                                   text="🚀  Publicar en GitHub")
        self.btn_scan.configure(state="normal")

    def _poll_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1], msg[2])
                elif kind == "progress":
                    self.progress.set(msg[1])
                elif kind == "review":
                    ReviewDialog(self, msg[1], mode="publish",
                                 on_done=self._publish_review_done)
                elif kind == "add_ignores":
                    self._add_ignore_patterns(msg[1])
                elif kind == "scan_result":
                    self._finish_scan(msg[1], msg[2])
                elif kind == "set_fp":
                    self._last_pub_fp = msg[1]
                elif kind == "auto_result":
                    self._handle_auto_result(msg[1], msg[2])
                elif kind == "auto_blocked":
                    self._restore_buttons()
                    self.progress.set(0)
                    self._set_status("●  Hallazgos — revisa con 🛡", "warn")
                    self._auto_abort("La revisión encontró archivos "
                                     "grandes o secretos: no he subido "
                                     "nada. Revísalos con «🛡 Analizar».")
                elif kind == "cancelled":
                    self._restore_buttons()
                    self.progress.set(0)
                    self._set_status("●  Listo", "idle")
                elif kind == "done":
                    ok, web = msg[1], msg[2]
                    self._restore_buttons()
                    if ok:
                        self.progress.set(1)
                        self.last_repo_web = web
                        self._set_status("●  Publicado con éxito", "ok")
                        if self._auto_fp:
                            self._last_pub_fp = self._auto_fp
                            self._auto_fp = None
                        else:
                            folder = self._normalize_path(
                                self.e_folder.get())
                            patterns = self._ignore_lines()
                            threading.Thread(
                                target=lambda: self.q.put(
                                    ("set_fp",
                                     folder_fingerprint(folder, patterns))),
                                daemon=True).start()
                        if not self.last_pages_url:
                            self._log("  ¿Es una página web? Actívala con "
                                      "«🌍 Publicar como web…».", "dim")
                        self.bell()
                    else:
                        self._auto_fp = None
                        self._set_status("●  Error — revisa la consola",
                                         "err")
        except queue.Empty:
            pass
        self.after(80, self._poll_queue)

    # ------------------------------------------------------------- #
    def _on_close(self):
        self.config_data["last_state"] = self._collect_state()
        self._save_config()
        self.destroy()


# ================================================================== #
#  Diálogo: clonar repositorio                                       #
# ================================================================== #
class CloneDialog(ctk.CTkToplevel):

    def __init__(self, app: "GitPushStudio"):
        super().__init__(app)
        self.app = app
        self.q = queue.Queue()
        self.cloned_path = ""

        self.title("Clonar repositorio de GitHub")
        self.configure(fg_color=BG)
        self.transient(app)
        self.after(150, self._safe_grab)
        place_dialog(self, app, 580, 480, min_w=520, min_h=420)

        pad = {"padx": 22}

        ctk.CTkLabel(self, text="⬇  Clonar repositorio",
                     font=app.f_title, text_color=TEXT
                     ).pack(anchor="w", pady=(18, 0), **pad)
        ctk.CTkLabel(self, text="Descarga cualquier repositorio de GitHub "
                                "a la carpeta que elijas.",
                     font=app.f_small, text_color=MUTED
                     ).pack(anchor="w", pady=(2, 12), **pad)

        c1 = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=12,
                          border_width=1, border_color=BORDER)
        c1.pack(fill="x", **pad)
        ctk.CTkLabel(c1, text="🔗  Enlace del repositorio",
                     font=app.f_section, text_color=TEXT
                     ).pack(anchor="w", padx=16, pady=(12, 2))
        self.e_url = app._entry(
            c1, "https://github.com/usuario/repositorio.git")
        self.e_url.pack(fill="x", padx=16, pady=(6, 12))
        prev = app.e_url.get().strip()
        if prev:
            self.e_url.insert(0, prev)

        c2 = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=12,
                          border_width=1, border_color=BORDER)
        c2.pack(fill="x", pady=(12, 0), **pad)
        ctk.CTkLabel(c2, text="📁  Guardar en",
                     font=app.f_section, text_color=TEXT
                     ).pack(anchor="w", padx=16, pady=(12, 2))
        rowd = ctk.CTkFrame(c2, fg_color="transparent")
        rowd.pack(fill="x", padx=16, pady=(6, 0))
        rowd.grid_columnconfigure(0, weight=1)
        self.e_dest = app._entry(rowd, str(Path.home() / "Downloads"))
        self.e_dest.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(rowd, text="Examinar…", width=110, height=36,
                      font=app.f_body, corner_radius=8,
                      fg_color=PANEL2, hover_color=BORDER,
                      border_width=1, border_color=BORDER, text_color=TEXT,
                      command=self._browse).grid(row=0, column=1)
        dflt = app.config_data.get("clone_dest") \
               or str(Path.home() / "Downloads"
                      if (Path.home() / "Downloads").is_dir()
                      else Path.home())
        self.e_dest.insert(0, dflt)
        self.v_use = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(c2, text="Usarlo como proyecto actual al terminar",
                        variable=self.v_use, font=app.f_small,
                        text_color=MUTED, fg_color=GREEN,
                        hover_color=GREEN_H, border_color=BORDER,
                        checkbox_width=18, checkbox_height=18
                        ).pack(anchor="w", padx=16, pady=(8, 0))
        ctk.CTkLabel(c2, text="El proyecto se creará como una subcarpeta "
                             "con el nombre del repositorio. Los "
                             "repositorios privados requieren haber "
                             "iniciado sesión con Git Credential Manager.",
                     font=app.f_small, text_color=MUTED, justify="left",
                     wraplength=500).pack(anchor="w", padx=16, pady=(6, 12))

        self.lbl_status = ctk.CTkLabel(self, text="", font=app.f_mono_s,
                                       text_color=MUTED, justify="left",
                                       wraplength=520)
        self.lbl_status.pack(anchor="w", pady=(12, 0), **pad)

        rowb = ctk.CTkFrame(self, fg_color="transparent")
        rowb.pack(fill="x", pady=(10, 18), **pad)
        rowb.grid_columnconfigure(0, weight=1)
        self.btn_clone = ctk.CTkButton(
            rowb, text="Clonar", height=42, font=app.f_btn,
            corner_radius=10, fg_color=GREEN, hover_color=GREEN_H,
            text_color="#ffffff", command=self._clone)
        self.btn_clone.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.btn_folder = ctk.CTkButton(
            rowb, text="Abrir carpeta", height=42, width=130,
            font=app.f_body, corner_radius=10, fg_color="transparent",
            hover_color=PANEL2, border_width=1, border_color=BORDER,
            text_color=BLUE, state="disabled",
            command=lambda: open_in_explorer(self.cloned_path))
        self.btn_folder.grid(row=0, column=1, padx=(0, 10))
        ctk.CTkButton(rowb, text="Cerrar", height=42, width=90,
                      font=app.f_body, corner_radius=10,
                      fg_color="transparent", hover_color=PANEL2,
                      border_width=1, border_color=BORDER,
                      text_color=MUTED, command=self.destroy
                      ).grid(row=0, column=2)

        self.after(80, self._poll)

    # ------------------------------------------------------------- #
    def _safe_grab(self):
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _browse(self):
        folder = filedialog.askdirectory(
            title="¿Dónde guardar el proyecto clonado?")
        if folder:
            self.e_dest.delete(0, "end")
            self.e_dest.insert(0, str(Path(folder)))

    def _status(self, text, color=MUTED):
        self.lbl_status.configure(text=text, text_color=color)

    # ------------------------------------------------------------- #
    def _clone(self):
        url = self.e_url.get().strip()
        dest = self.app._normalize_path(self.e_dest.get())
        if not (url.startswith("https://") or url.startswith("git@")):
            self._status("La URL debe empezar por https:// o git@", RED)
            return
        name = repo_name_from_url(url)
        if not name:
            self._status("No pude deducir el nombre del repositorio "
                         "desde la URL.", RED)
            return
        try:
            Path(dest).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self._status(f"No puedo usar esa carpeta destino: {e}", RED)
            return
        target = Path(dest) / name
        if target.exists() and any(target.iterdir()):
            self._status(f"Ya existe y no está vacía:\n{target}", RED)
            return

        self.btn_clone.configure(state="disabled", text="Clonando…")
        self._status("Conectando con GitHub…", MUTED)
        self.app._log("─" * 60, "dim")
        self.app._log(f"$ git clone {url}", "cmd")
        threading.Thread(target=self._worker,
                         args=(url, dest, target, bool(self.v_use.get())),
                         daemon=True).start()

    def _worker(self, url, dest, target, use_as_current):
        lines = []

        def on_line(ln):
            lines.append(ln)
            self.q.put(("st", ln))

        rc = run_git_stream(["clone", "--progress", url, str(target)],
                            cwd=dest, on_line=on_line)
        if rc == 0:
            self.q.put(("ok", str(target), url, use_as_current, dest))
        else:
            low = " ".join(lines).lower()
            if ("could not read username" in low
                    or "authentication" in low or "denied" in low):
                msg = ("GitHub pidió credenciales. Si el repositorio es "
                       "privado, inicia sesión una vez con Git Credential "
                       "Manager (o comprueba tus permisos).")
            elif "already exists" in low:
                msg = "La carpeta destino ya existe y no está vacía."
            elif "not found" in low:
                msg = ("Repositorio no encontrado: revisa la URL "
                       "(o es privado y no tienes acceso).")
            else:
                msg = "Falló la clonación. Última línea: " + \
                      (lines[-1] if lines else "sin salida")
            self.q.put(("err", msg))

    # ------------------------------------------------------------- #
    def _poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "st":
                    self._status(m[1], MUTED)
                elif m[0] == "err":
                    self._status("✖ " + m[1], RED)
                    self.btn_clone.configure(state="normal", text="Clonar")
                    self.app._log("✖ " + m[1], "err")
                elif m[0] == "ok":
                    _, target, url, use_as_current, dest = m
                    self.cloned_path = target
                    self.app.config_data["clone_dest"] = dest
                    self.app._save_config()
                    self._status(f"✔ Clonado en:\n{target}", "#3fb950")
                    self.btn_clone.configure(state="normal", text="Clonar")
                    self.btn_folder.configure(state="normal")
                    self.app._log(f"✔ Repositorio clonado en {target}",
                                  "ok")
                    if use_as_current:
                        self.app.set_current_project(target, url)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(80, self._poll)


# ================================================================== #
#  Diálogo: clonar un perfil completo                                #
# ================================================================== #
class BulkCloneDialog(ctk.CTkToplevel):

    def __init__(self, app: "GitPushStudio"):
        super().__init__(app)
        self.app = app
        self.q = queue.Queue()
        self._stop = threading.Event()
        self._token_visible = False
        self.base_path = ""
        self.running = False

        self.title("Clonar un perfil completo de GitHub")
        self.configure(fg_color=BG)
        self.resizable(True, True)
        self.transient(app)
        self.after(150, self._safe_grab)
        self.protocol("WM_DELETE_WINDOW", self._close)

        # Alto acotado a la pantalla para que los botones siempre entren.
        w = 620
        try:
            screen_h = self.winfo_screenheight()
        except Exception:
            screen_h = 720
        h = min(640, max(520, screen_h - 120))
        try:
            x = app.winfo_x() + (app.winfo_width() - w) // 2
            y = app.winfo_y() + (app.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(x, 20)}+{max(y, 20)}")
        except Exception:
            self.geometry(f"{w}x{h}")
        self.minsize(560, 480)

        # Estructura: cabecera fija · contenido desplazable · pie fijo
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        pad = {"padx": 22}

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(header, text="⬇⬇  Clonar perfil completo",
                     font=app.f_title, text_color=TEXT
                     ).pack(anchor="w", pady=(18, 0), **pad)
        ctk.CTkLabel(header, text="Descarga todos los repositorios de un "
                                  "perfil, en paralelo y en modo ligero.",
                     font=app.f_small, text_color=MUTED
                     ).pack(anchor="w", pady=(2, 12), **pad)

        body = ctk.CTkScrollableFrame(
            self, fg_color="transparent",
            scrollbar_button_color=BORDER,
            scrollbar_button_hover_color=MUTED)
        body.grid(row=1, column=0, sticky="nsew")

        # --- Perfil y token ---------------------------------------------
        c1 = ctk.CTkFrame(body, fg_color=PANEL, corner_radius=12,
                          border_width=1, border_color=BORDER)
        c1.pack(fill="x", **pad)
        ctk.CTkLabel(c1, text="👤  Perfil de GitHub",
                     font=app.f_section, text_color=TEXT
                     ).pack(anchor="w", padx=16, pady=(12, 2))
        self.e_user = app._entry(
            c1, "usuario  o  https://github.com/usuario")
        self.e_user.pack(fill="x", padx=16, pady=(6, 0))
        rowt = ctk.CTkFrame(c1, fg_color="transparent")
        rowt.pack(fill="x", padx=16, pady=(8, 0))
        rowt.grid_columnconfigure(0, weight=1)
        self.e_token = app._entry(rowt, "Token (opcional)", show="•")
        self.e_token.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(rowt, text="👁", width=42, height=36,
                      font=app.f_body, corner_radius=8,
                      fg_color=PANEL2, hover_color=BORDER,
                      border_width=1, border_color=BORDER, text_color=TEXT,
                      command=self._toggle_token).grid(row=0, column=1)
        ctk.CTkLabel(c1, text="El token es opcional: sube el límite de "
                             "peticiones y, si el perfil es el tuyo, "
                             "incluye también tus repositorios privados.",
                     font=app.f_small, text_color=MUTED, justify="left",
                     wraplength=540).pack(anchor="w", padx=16, pady=(6, 12))

        # --- Destino y opciones ------------------------------------------
        c2 = ctk.CTkFrame(body, fg_color=PANEL, corner_radius=12,
                          border_width=1, border_color=BORDER)
        c2.pack(fill="x", pady=(12, 0), **pad)
        ctk.CTkLabel(c2, text="📁  Destino y opciones",
                     font=app.f_section, text_color=TEXT
                     ).pack(anchor="w", padx=16, pady=(12, 2))
        rowd = ctk.CTkFrame(c2, fg_color="transparent")
        rowd.pack(fill="x", padx=16, pady=(6, 0))
        rowd.grid_columnconfigure(0, weight=1)
        self.e_dest = app._entry(rowd, str(Path.home() / "Downloads"))
        self.e_dest.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(rowd, text="Examinar…", width=110, height=36,
                      font=app.f_body, corner_radius=8,
                      fg_color=PANEL2, hover_color=BORDER,
                      border_width=1, border_color=BORDER, text_color=TEXT,
                      command=self._browse).grid(row=0, column=1)
        dflt = app.config_data.get("clone_dest") \
               or str(Path.home() / "Downloads"
                      if (Path.home() / "Downloads").is_dir()
                      else Path.home())
        self.e_dest.insert(0, dflt)

        rowo = ctk.CTkFrame(c2, fg_color="transparent")
        rowo.pack(fill="x", padx=16, pady=(8, 0))
        self.v_shallow = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(rowo, text="Clonación ligera (solo la última "
                                   "versión, mucho más rápida)",
                        variable=self.v_shallow, font=app.f_small,
                        text_color=MUTED, fg_color=GREEN,
                        hover_color=GREEN_H, border_color=BORDER,
                        checkbox_width=18, checkbox_height=18
                        ).pack(anchor="w")
        self.v_forks = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(rowo, text="Omitir forks (copias de proyectos "
                                   "ajenos)",
                        variable=self.v_forks, font=app.f_small,
                        text_color=MUTED, fg_color=GREEN,
                        hover_color=GREEN_H, border_color=BORDER,
                        checkbox_width=18, checkbox_height=18
                        ).pack(anchor="w", pady=(6, 0))
        self.v_pack = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(rowo, text="Al terminar, unir todos los proyectos "
                                   "en un solo TXT",
                        variable=self.v_pack, font=app.f_small,
                        text_color=MUTED, fg_color=GREEN,
                        hover_color=GREEN_H, border_color=BORDER,
                        checkbox_width=18, checkbox_height=18
                        ).pack(anchor="w", pady=(6, 0))
        roww = ctk.CTkFrame(c2, fg_color="transparent")
        roww.pack(fill="x", padx=16, pady=(8, 0))
        ctk.CTkLabel(roww, text="Descargas a la vez:", font=app.f_small,
                     text_color=MUTED).pack(side="left")
        self.om_workers = ctk.CTkOptionMenu(
            roww, values=["2", "4", "6", "8"], width=70, height=28,
            font=app.f_small, fg_color=PANEL2, button_color=BORDER,
            button_hover_color=MUTED, text_color=TEXT,
            dropdown_fg_color=PANEL, dropdown_text_color=TEXT,
            dropdown_hover_color=PANEL2)
        self.om_workers.set("4")
        self.om_workers.pack(side="left", padx=(8, 0))
        ctk.CTkLabel(c2, text="Todo se guarda en una subcarpeta con el "
                             "nombre del usuario. Lo ya clonado se omite, "
                             "así puedes repetir la descarga sin rehacer "
                             "trabajo.",
                     font=app.f_small, text_color=MUTED, justify="left",
                     wraplength=540).pack(anchor="w", padx=16, pady=(8, 12))

        # --- Pie fijo: progreso y botones (siempre visibles) ------------
        footer = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0,
                              border_width=0)
        footer.grid(row=2, column=0, sticky="ew")
        ctk.CTkFrame(footer, height=1, fg_color=BORDER).pack(
            fill="x", side="top")

        self.pbar = ctk.CTkProgressBar(footer, height=6, corner_radius=3,
                                       fg_color=PANEL2,
                                       progress_color=GREEN_H)
        self.pbar.set(0)
        self.pbar.pack(fill="x", pady=(12, 0), **pad)
        self.lbl_counts = ctk.CTkLabel(footer, text="", font=app.f_small,
                                       text_color=MUTED)
        self.lbl_counts.pack(anchor="w", pady=(6, 0), **pad)
        self.lbl_status = ctk.CTkLabel(footer, text="", font=app.f_small,
                                       text_color=MUTED, justify="left",
                                       wraplength=560)
        self.lbl_status.pack(anchor="w", pady=(2, 0), **pad)

        rowb = ctk.CTkFrame(footer, fg_color="transparent")
        rowb.pack(fill="x", pady=(12, 16), **pad)
        rowb.grid_columnconfigure(0, weight=1)
        self.btn_go = ctk.CTkButton(
            rowb, text="Descargar todo", height=42, font=app.f_btn,
            corner_radius=10, fg_color=GREEN, hover_color=GREEN_H,
            text_color="#ffffff", command=self._start)
        self.btn_go.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.btn_stop = ctk.CTkButton(
            rowb, text="Detener", height=42, width=100,
            font=app.f_body, corner_radius=10, fg_color="transparent",
            hover_color=PANEL2, border_width=1, border_color=BORDER,
            text_color=AMBER, state="disabled", command=self._stop_now)
        self.btn_stop.grid(row=0, column=1, padx=(0, 10))
        self.btn_folder = ctk.CTkButton(
            rowb, text="Abrir carpeta", height=42, width=130,
            font=app.f_body, corner_radius=10, fg_color="transparent",
            hover_color=PANEL2, border_width=1, border_color=BORDER,
            text_color=BLUE, state="disabled",
            command=lambda: open_in_explorer(self.base_path))
        self.btn_folder.grid(row=0, column=2, padx=(0, 10))
        ctk.CTkButton(rowb, text="Cerrar", height=42, width=90,
                      font=app.f_body, corner_radius=10,
                      fg_color="transparent", hover_color=PANEL2,
                      border_width=1, border_color=BORDER,
                      text_color=MUTED, command=self._close
                      ).grid(row=0, column=3)

        saved = app.get_saved_token()
        if saved:
            self.e_token.insert(0, saved)

        self.after(80, self._poll)

    # ------------------------------------------------------------- #
    def _safe_grab(self):
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _toggle_token(self):
        self._token_visible = not self._token_visible
        self.e_token.configure(show="" if self._token_visible else "•")

    def _browse(self):
        folder = filedialog.askdirectory(
            title="¿Dónde guardar la carpeta del perfil?")
        if folder:
            self.e_dest.delete(0, "end")
            self.e_dest.insert(0, str(Path(folder)))

    def _status(self, text, color=MUTED):
        self.lbl_status.configure(text=text, text_color=color)

    def _close(self):
        self._stop.set()
        self.destroy()

    def _stop_now(self):
        self._stop.set()
        self.btn_stop.configure(state="disabled")
        self._status("Deteniendo… se completa lo que ya está en marcha.",
                     AMBER)

    # ------------------------------------------------------------- #
    def _start(self):
        if self.running:
            return
        username = parse_username(self.e_user.get())
        if not username:
            self._status("Escribe un usuario válido de GitHub (o pega "
                         "el enlace de su perfil).", RED)
            return
        dest = self.app._normalize_path(self.e_dest.get())
        try:
            Path(dest).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self._status(f"No puedo usar esa carpeta destino: {e}", RED)
            return
        token = self.e_token.get().strip()
        workers = int(self.om_workers.get())

        self.running = True
        self._stop.clear()
        self.n_ok = self.n_skip = self.n_fail = 0
        self.pbar.set(0)
        self.lbl_counts.configure(text="")
        self.btn_go.configure(state="disabled", text="Descargando…")
        self.btn_stop.configure(state="normal")
        self.btn_folder.configure(state="disabled")
        self._status("Buscando los repositorios del perfil…", MUTED)
        self.app._log("─" * 60, "dim")
        self.app._log(f"⬇⬇ Clonando el perfil «{username}»…", "out")

        threading.Thread(
            target=self._worker,
            args=(username, token, dest, bool(self.v_shallow.get()),
                  bool(self.v_forks.get()), workers,
                  bool(self.v_pack.get()), self.app._ignore_lines()),
            daemon=True).start()

    def _worker(self, username, token, dest, shallow, skip_forks,
                workers, do_pack, patterns):
        try:
            repos = list_user_repos(username, token)
            total_before = len(repos)
            if skip_forks:
                repos = [r for r in repos if not r["fork"]]
            if not repos:
                raise StepError("Ese perfil no tiene repositorios para "
                                "descargar"
                                + (" (todos eran forks)."
                                   if total_before else "."))
            est_kb = sum(r["size_kb"] for r in repos)
            base = Path(dest) / username
            self.q.put(("plan", len(repos), est_kb, str(base)))

            summary = bulk_clone(
                repos, base, shallow, token, workers,
                on_event=lambda k, p: self.q.put((k, p)),
                stop_event=self._stop)

            pack_stats, out = None, ""
            if do_pack and not self._stop.is_set():
                self.q.put(("st", "Uniendo todos los proyectos en un "
                                  "solo TXT…"))
                dirs = [base / r["name"] for r in repos
                        if (base / r["name"]).is_dir()]
                out = str(base / f"{username}.txt")
                pack_stats = consolidate_many(
                    username, dirs, out, patterns,
                    profile_url=f"https://github.com/{username}")
            self.q.put(("bulk_done", str(base), summary, pack_stats, out))
        except StepError as e:
            self.q.put(("err", str(e)))
        except Exception as e:
            self.q.put(("err", f"Error: {e}"))

    # ------------------------------------------------------------- #
    def _update_counts(self):
        self.lbl_counts.configure(
            text=f"✓ {self.n_ok} clonados   ·   ↷ {self.n_skip} ya "
                 f"existían   ·   ✖ {self.n_fail} fallidos")

    def _poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                kind = m[0]
                if kind == "st":
                    self._status(m[1], MUTED)
                elif kind == "plan":
                    _, n, est_kb, base = m
                    self.base_path = base
                    self._status(f"{n} repositorios encontrados "
                                 f"(~{fmt_size(est_kb * 1024)} en GitHub "
                                 "con historial completo).", MUTED)
                    self.app._log(f"  {n} repositorios por descargar.",
                                  "dim")
                elif kind == "start":
                    self._status(f"Clonando: {m[1]}…", MUTED)
                elif kind == "done":
                    name, st, secs, err, done, total = m[1]
                    self.pbar.set(done / max(1, total))
                    if st == "ok":
                        self.n_ok += 1
                        self.app._log(f"  ✓ {name} ({secs:.1f} s)", "dim")
                    elif st == "skip":
                        self.n_skip += 1
                        self.app._log(f"  ↷ {name} (ya existía)", "dim")
                    elif st == "cancel":
                        pass
                    else:
                        self.n_fail += 1
                        self.app._log(f"  ✖ {name}: {err}", "err")
                    self._update_counts()
                elif kind == "err":
                    self.running = False
                    self._status("✖ " + m[1], RED)
                    self.btn_go.configure(state="normal",
                                          text="Descargar todo")
                    self.btn_stop.configure(state="disabled")
                    self.app._log("✖ " + m[1], "err")
                elif kind == "bulk_done":
                    _, base, s, ps, out = m
                    self.running = False
                    self.base_path = base
                    self.app.config_data["clone_dest"] = \
                        str(Path(base).parent)
                    self.app._save_config()
                    self.btn_go.configure(state="normal",
                                          text="Descargar todo")
                    self.btn_stop.configure(state="disabled")
                    self.btn_folder.configure(state="normal")
                    self.pbar.set(1)
                    stopped = s.get("cancel", 0) > 0
                    txt_line = ""
                    if ps:
                        txt_line = (f"\nTXT combinado: {ps['files']} "
                                    f"archivos de {ps['repos']} repos, "
                                    f"{fmt_size(ps['bytes'])} en "
                                    f"{ps['seconds']:.1f} s"
                                    + ("  ·  ⚠ truncado"
                                       if ps.get("truncated") else ""))
                    color = "#3fb950" if not s["fails"] and not stopped \
                        else AMBER
                    head = ("✋ Detenido a medias." if stopped
                            else "✔ ¡Perfil descargado!")
                    self._status(
                        f"{head}  {s['ok']} clonados, {s['skip']} ya "
                        f"existían, {s['fail']} fallidos en "
                        f"{s['seconds']:.1f} s.{txt_line}\n{base}", color)
                    self.app._log(f"✔ Perfil completo: {s['ok']} "
                                  f"clonados, {s['skip']} omitidos, "
                                  f"{s['fail']} fallidos en "
                                  f"{s['seconds']:.1f} s → {base}",
                                  "ok" if not s["fails"] else "warn")
                    if ps and out:
                        self.app._log(f"✔ TXT combinado: {out} "
                                      f"({fmt_size(ps['bytes'])}).", "ok")
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(80, self._poll)


# ================================================================== #
#  Diálogo: consolidar el proyecto a un TXT                          #
# ================================================================== #
class ConsolidateDialog(ctk.CTkToplevel):

    def __init__(self, app: "GitPushStudio"):
        super().__init__(app)
        self.app = app
        self.q = queue.Queue()
        self.out_path = ""

        self.title("Consolidar proyecto a TXT")
        self.configure(fg_color=BG)
        self.transient(app)
        self.after(150, self._safe_grab)
        place_dialog(self, app, 600, 520, min_w=540, min_h=440)

        pad = {"padx": 22}

        ctk.CTkLabel(self, text="📄  Consolidar a TXT",
                     font=app.f_title, text_color=TEXT
                     ).pack(anchor="w", pady=(18, 0), **pad)
        ctk.CTkLabel(self, text="Todo el proyecto (árbol + contenido) en "
                                "un solo archivo de texto.",
                     font=app.f_small, text_color=MUTED
                     ).pack(anchor="w", pady=(2, 12), **pad)

        c1 = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=12,
                          border_width=1, border_color=BORDER)
        c1.pack(fill="x", **pad)
        ctk.CTkLabel(c1, text="📁  Proyecto a empaquetar",
                     font=app.f_section, text_color=TEXT
                     ).pack(anchor="w", padx=16, pady=(12, 2))
        rowp = ctk.CTkFrame(c1, fg_color="transparent")
        rowp.pack(fill="x", padx=16, pady=(6, 12))
        rowp.grid_columnconfigure(0, weight=1)
        self.e_proj = app._entry(rowp, "Carpeta del proyecto")
        self.e_proj.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(rowp, text="Examinar…", width=110, height=36,
                      font=app.f_body, corner_radius=8,
                      fg_color=PANEL2, hover_color=BORDER,
                      border_width=1, border_color=BORDER, text_color=TEXT,
                      command=self._browse_proj).grid(row=0, column=1)

        c2 = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=12,
                          border_width=1, border_color=BORDER)
        c2.pack(fill="x", pady=(12, 0), **pad)
        ctk.CTkLabel(c2, text="💾  Archivo TXT de salida",
                     font=app.f_section, text_color=TEXT
                     ).pack(anchor="w", padx=16, pady=(12, 2))
        rowo = ctk.CTkFrame(c2, fg_color="transparent")
        rowo.pack(fill="x", padx=16, pady=(6, 0))
        rowo.grid_columnconfigure(0, weight=1)
        self.e_out = app._entry(rowo, "ruta\\del\\proyecto.txt")
        self.e_out.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(rowo, text="Elegir…", width=110, height=36,
                      font=app.f_body, corner_radius=8,
                      fg_color=PANEL2, hover_color=BORDER,
                      border_width=1, border_color=BORDER, text_color=TEXT,
                      command=self._browse_out).grid(row=0, column=1)
        self.v_ign = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(c2, text="Respetar .gitignore y el cuadro de "
                                 "ignorar (recomendado)",
                        variable=self.v_ign, font=app.f_small,
                        text_color=MUTED, fg_color=GREEN,
                        hover_color=GREEN_H, border_color=BORDER,
                        checkbox_width=18, checkbox_height=18
                        ).pack(anchor="w", padx=16, pady=(8, 0))
        ctk.CTkLabel(c2, text="Por defecto se llama como el proyecto y se "
                             "guarda junto a él. Los binarios y los "
                             "archivos de más de 2 MB se marcan pero no "
                             "se vuelcan; el TXT nunca se incluye a sí "
                             "mismo.",
                     font=app.f_small, text_color=MUTED, justify="left",
                     wraplength=520).pack(anchor="w", padx=16, pady=(6, 12))

        self.lbl_status = ctk.CTkLabel(self, text="", font=app.f_small,
                                       text_color=MUTED, justify="left",
                                       wraplength=540)
        self.lbl_status.pack(anchor="w", pady=(12, 0), **pad)

        rowb = ctk.CTkFrame(self, fg_color="transparent")
        rowb.pack(fill="x", pady=(10, 18), **pad)
        rowb.grid_columnconfigure(0, weight=1)
        self.btn_go = ctk.CTkButton(
            rowb, text="Generar TXT", height=42, font=app.f_btn,
            corner_radius=10, fg_color=GREEN, hover_color=GREEN_H,
            text_color="#ffffff", command=self._generate)
        self.btn_go.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.btn_open = ctk.CTkButton(
            rowb, text="Abrir TXT", height=42, width=110,
            font=app.f_body, corner_radius=10, fg_color="transparent",
            hover_color=PANEL2, border_width=1, border_color=BORDER,
            text_color=BLUE, state="disabled",
            command=lambda: open_in_explorer(self.out_path))
        self.btn_open.grid(row=0, column=1, padx=(0, 10))
        ctk.CTkButton(rowb, text="Cerrar", height=42, width=90,
                      font=app.f_body, corner_radius=10,
                      fg_color="transparent", hover_color=PANEL2,
                      border_width=1, border_color=BORDER,
                      text_color=MUTED, command=self.destroy
                      ).grid(row=0, column=2)

        # Prellenar con el proyecto actual
        proj = app._normalize_path(app.e_folder.get())
        if proj and Path(proj).is_dir():
            self.e_proj.insert(0, proj)
            self._suggest_output()

        self.after(80, self._poll)

    # ------------------------------------------------------------- #
    def _safe_grab(self):
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _status(self, text, color=MUTED):
        self.lbl_status.configure(text=text, text_color=color)

    def _suggest_output(self):
        proj = self.app._normalize_path(self.e_proj.get())
        p = Path(proj)
        if proj and p.is_dir():
            self.e_out.delete(0, "end")
            self.e_out.insert(0, str(p.parent / (p.name + ".txt")))

    def _browse_proj(self):
        folder = filedialog.askdirectory(title="Proyecto a empaquetar")
        if folder:
            self.e_proj.delete(0, "end")
            self.e_proj.insert(0, str(Path(folder)))
            self._suggest_output()

    def _browse_out(self):
        proj = Path(self.app._normalize_path(self.e_proj.get() or "."))
        name = (proj.name or "proyecto") + ".txt"
        chosen = filedialog.asksaveasfilename(
            title="¿Dónde guardar el TXT?", defaultextension=".txt",
            initialfile=name,
            initialdir=str(proj.parent if proj.parent.is_dir()
                           else Path.home()),
            filetypes=[("Archivo de texto", "*.txt")])
        if chosen:
            self.e_out.delete(0, "end")
            self.e_out.insert(0, str(Path(chosen)))

    # ------------------------------------------------------------- #
    def _generate(self):
        proj = self.app._normalize_path(self.e_proj.get())
        if not proj or not Path(proj).is_dir():
            self._status("Indica una carpeta de proyecto que exista.", RED)
            return
        out = self.app._normalize_path(self.e_out.get())
        if not out:
            p = Path(proj)
            out = str(p.parent / (p.name + ".txt"))
            self.e_out.insert(0, out)
        patterns = self.app._ignore_lines()
        respect = bool(self.v_ign.get())

        self.btn_go.configure(state="disabled", text="Generando…")
        self._status("Empaquetando el proyecto…", MUTED)
        threading.Thread(target=self._worker,
                         args=(proj, out, patterns, respect),
                         daemon=True).start()

    def _worker(self, proj, out, patterns, respect):
        try:
            stats = consolidate_project(proj, out, patterns,
                                        respect_ignores=respect)
            self.q.put(("ok", out, stats,
                        Path(out).resolve().is_relative_to(
                            Path(proj).resolve())
                        if hasattr(Path, "is_relative_to")
                        else str(Path(out).resolve()).startswith(
                            str(Path(proj).resolve()))))
        except Exception as e:
            self.q.put(("err", str(e)))

    # ------------------------------------------------------------- #
    def _poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "err":
                    self._status("✖ " + m[1], RED)
                    self.btn_go.configure(state="normal",
                                          text="Generar TXT")
                elif m[0] == "ok":
                    _, out, st, inside = m
                    self.out_path = out
                    extra = ""
                    if st["binaries"] or st["big"]:
                        extra = (f"  ·  omitidos: {st['binaries']} "
                                 f"binarios, {st['big']} enormes")
                    if st.get("truncated"):
                        extra += "  ·  ⚠ salida truncada"
                    self._status(f"✔ TXT generado: {st['files']} archivos, "
                                 f"{fmt_size(st['bytes'])} en "
                                 f"{st['seconds']:.1f} s{extra}\n{out}",
                                 "#3fb950")
                    self.btn_go.configure(state="normal",
                                          text="Generar TXT")
                    self.btn_open.configure(state="normal")
                    self.app._log(f"✔ Proyecto consolidado en {out} "
                                  f"({st['files']} archivos, "
                                  f"{fmt_size(st['bytes'])}).", "ok")
                    if inside:
                        base = Path(out).name
                        self.app._add_ignore_patterns([base])
                        self.app._log("  El TXT quedó dentro del proyecto: "
                                      "lo añadí al cuadro de ignorar para "
                                      "que no se suba a GitHub.", "warn")
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(80, self._poll)


# ================================================================== #
#  Diálogo: revisión de seguridad                                    #
# ================================================================== #
class ReviewDialog(ctk.CTkToplevel):
    """Muestra los hallazgos y deja excluirlos con un clic.

    mode="publish": pausa la publicación hasta decidir.
    mode="manual":  solo añade patrones al cuadro de ignorar.
    """

    def __init__(self, app: "GitPushStudio", findings, mode, on_done):
        super().__init__(app)
        self.app = app
        self.mode = mode
        self.on_done = on_done
        self.findings = findings
        self._answered = False
        self._vars = []

        self.title("Revisión antes de publicar")
        self.configure(fg_color=BG)
        self.transient(app)
        self.after(150, self._safe_grab)
        self.protocol("WM_DELETE_WINDOW", lambda: self._finish(
            "cancel" if self.mode == "publish" else "close"))

        w, h = 640, 560
        try:
            x = app.winfo_x() + (app.winfo_width() - w) // 2
            y = app.winfo_y() + (app.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            self.geometry(f"{w}x{h}")
        self.minsize(560, 460)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=22, pady=(18, 0))
        ctk.CTkLabel(head, text="🛡  Revisión antes de publicar",
                     font=app.f_title, text_color=TEXT).pack(anchor="w")
        ctk.CTkLabel(head,
                     text=f"Encontré {len(findings)} cosa(s) que conviene "
                          "revisar. Marca las que quieras excluir.",
                     font=app.f_small, text_color=MUTED).pack(anchor="w",
                                                              pady=(2, 0))
        ctk.CTkFrame(self, height=1, fg_color=BORDER).grid(
            row=1, column=0, sticky="ew", padx=22, pady=(12, 0))

        body = ctk.CTkScrollableFrame(
            self, fg_color="transparent",
            scrollbar_button_color=BORDER, scrollbar_button_hover_color=MUTED)
        body.grid(row=2, column=0, sticky="nsew", padx=16, pady=(8, 0))

        bigs = [f for f in findings if f["kind"] in ("huge", "big")]
        secrets = [f for f in findings if f["kind"] == "secret"]
        if bigs:
            self._group(body, "ARCHIVOS GRANDES", bigs)
        if secrets:
            self._group(body, "POSIBLES SECRETOS", secrets)

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=3, column=0, sticky="ew", padx=22, pady=(10, 16))
        foot.grid_columnconfigure(0, weight=1)

        note = ("Ignorar no borra nada: los archivos siguen en tu PC, "
                "solo no se suben a GitHub.")
        if mode == "manual":
            note += " Se aplicará en la próxima publicación."
        ctk.CTkLabel(foot, text=note, font=app.f_small, text_color=MUTED,
                     justify="left", wraplength=580
                     ).grid(row=0, column=0, columnspan=3, sticky="w",
                            pady=(0, 10))

        main_txt = ("Ignorar marcados y continuar" if mode == "publish"
                    else "Añadir marcados al .gitignore")
        ctk.CTkButton(foot, text=main_txt, height=40, font=app.f_btn,
                      corner_radius=10, fg_color=GREEN,
                      hover_color=GREEN_H, text_color="#ffffff",
                      command=lambda: self._finish("ignore")
                      ).grid(row=1, column=0, sticky="ew", padx=(0, 8))
        if mode == "publish":
            ctk.CTkButton(foot, text="Continuar igualmente", height=40,
                          width=170, font=app.f_body, corner_radius=10,
                          fg_color="transparent", hover_color=PANEL2,
                          border_width=1, border_color=BORDER,
                          text_color=AMBER,
                          command=lambda: self._finish("continue")
                          ).grid(row=1, column=1, padx=(0, 8))
        ctk.CTkButton(foot, text="Cancelar" if mode == "publish"
                      else "Cerrar",
                      height=40, width=110, font=app.f_body,
                      corner_radius=10, fg_color="transparent",
                      hover_color=PANEL2, border_width=1,
                      border_color=BORDER, text_color=MUTED,
                      command=lambda: self._finish(
                          "cancel" if self.mode == "publish" else "close")
                      ).grid(row=1, column=2)

    # ------------------------------------------------------------- #
    def _group(self, parent, title, items):
        ctk.CTkLabel(parent, text=title, font=self.app.f_small,
                     text_color=MUTED).pack(anchor="w", padx=6, pady=(10, 4))
        box = ctk.CTkFrame(parent, fg_color=PANEL, corner_radius=10,
                           border_width=1, border_color=BORDER)
        box.pack(fill="x", padx=4)
        for i, f in enumerate(items):
            rowf = ctk.CTkFrame(box, fg_color="transparent")
            rowf.pack(fill="x", padx=10, pady=(8 if i == 0 else 4,
                                               8 if i == len(items) - 1
                                               else 4))
            rowf.grid_columnconfigure(1, weight=1)
            var = ctk.BooleanVar(value=True)
            self._vars.append((f, var))
            ctk.CTkCheckBox(rowf, text="", variable=var, width=22,
                            checkbox_width=18, checkbox_height=18,
                            fg_color=GREEN, hover_color=GREEN_H,
                            border_color=BORDER
                            ).grid(row=0, column=0, sticky="w")
            rel = f["path"]
            if len(rel) > 58:
                rel = "…" + rel[-57:]
            ctk.CTkLabel(rowf, text=rel, font=self.app.f_mono_s,
                         text_color=TEXT, anchor="w"
                         ).grid(row=0, column=1, sticky="ew", padx=(4, 8))
            danger = f["kind"] in ("huge", "secret")
            ctk.CTkLabel(rowf, text=" " + f["reason"] + " ",
                         font=self.app.f_small, corner_radius=10,
                         fg_color=BADGE_RED_BG if danger else BADGE_AMBER_BG,
                         text_color=BADGE_RED_FG if danger
                         else BADGE_AMBER_FG,
                         height=22).grid(row=0, column=2, sticky="e")

    # ------------------------------------------------------------- #
    def _safe_grab(self):
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _finish(self, decision):
        if self._answered:
            return
        self._answered = True
        selected = [f["path"] for f, var in self._vars if var.get()]
        try:
            self.on_done(decision, selected)
        finally:
            self.destroy()


# ================================================================== #
#  Diálogo: GitHub Pages (publicar como web)                         #
# ================================================================== #
class PagesDialog(ctk.CTkToplevel):

    def __init__(self, app: "GitPushStudio", owner, repo):
        super().__init__(app)
        self.app = app
        self.owner = owner
        self.repo = repo
        self.q = queue.Queue()
        self.site_url = ""
        self._token_visible = False

        self.title("Publicar como web (GitHub Pages)")
        self.configure(fg_color=BG)
        self.transient(app)
        self.after(150, self._safe_grab)
        place_dialog(self, app, 560, 560, min_w=520, min_h=460)

        pad = {"padx": 22}

        ctk.CTkLabel(self, text="🌍  Publicar como web",
                     font=app.f_title, text_color=TEXT
                     ).pack(anchor="w", pady=(18, 0), **pad)
        ctk.CTkLabel(self, text=f"Repositorio: {owner}/{repo}   ·   "
                                "Activa GitHub Pages y te doy el enlace.",
                     font=app.f_small, text_color=MUTED
                     ).pack(anchor="w", pady=(2, 12), **pad)

        # --- Token ----------------------------------------------------
        c1 = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=12,
                          border_width=1, border_color=BORDER)
        c1.pack(fill="x", **pad)
        ctk.CTkLabel(c1, text="🔑  Token de acceso personal",
                     font=app.f_section, text_color=TEXT
                     ).pack(anchor="w", padx=16, pady=(12, 2))
        rowt = ctk.CTkFrame(c1, fg_color="transparent")
        rowt.pack(fill="x", padx=16, pady=(6, 0))
        rowt.grid_columnconfigure(0, weight=1)
        self.e_token = app._entry(rowt, "ghp_…", show="•")
        self.e_token.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(rowt, text="👁", width=42, height=36,
                      font=app.f_body, corner_radius=8,
                      fg_color=PANEL2, hover_color=BORDER,
                      border_width=1, border_color=BORDER, text_color=TEXT,
                      command=self._toggle_token).grid(row=0, column=1)
        ctk.CTkLabel(c1, text="El mismo token del botón «➕» (permiso "
                             "«repo»). Se usa solo para activar Pages.",
                     font=app.f_small, text_color=MUTED, justify="left"
                     ).pack(anchor="w", padx=16, pady=(6, 12))

        # --- Origen de la web -------------------------------------------
        c2 = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=12,
                          border_width=1, border_color=BORDER)
        c2.pack(fill="x", pady=(12, 0), **pad)
        ctk.CTkLabel(c2, text="📄  Origen de la página",
                     font=app.f_section, text_color=TEXT
                     ).pack(anchor="w", padx=16, pady=(12, 2))
        rowo = ctk.CTkFrame(c2, fg_color="transparent")
        rowo.pack(fill="x", padx=16, pady=(6, 0))
        ctk.CTkLabel(rowo, text="Rama:", font=self.app.f_small,
                     text_color=MUTED).pack(side="left")
        self.e_branch = app._entry(rowo, "main")
        self.e_branch.configure(width=120)
        self.e_branch.pack(side="left", padx=(6, 14))
        self.e_branch.insert(0, app.e_branch.get().strip() or "main")
        ctk.CTkLabel(rowo, text="Carpeta:", font=self.app.f_small,
                     text_color=MUTED).pack(side="left")
        self.seg_path = ctk.CTkSegmentedButton(
            rowo, values=["/ (raíz)", "/docs"], font=app.f_small,
            fg_color=INPUT, selected_color=BLUE_D,
            selected_hover_color=BLUE, unselected_color=PANEL2,
            unselected_hover_color=BORDER, text_color=TEXT,
            corner_radius=8, height=30)
        self.seg_path.set("/ (raíz)")
        self.seg_path.pack(side="left", padx=(6, 0))
        ctk.CTkLabel(c2, text="Tu web quedará en:  "
                             f"https://{owner}.github.io/{repo}/\n"
                             "Requisitos: repositorio público y rama ya "
                             "publicada.",
                     font=app.f_small, text_color=MUTED, justify="left"
                     ).pack(anchor="w", padx=16, pady=(8, 12))

        # --- Estado y botones ------------------------------------------
        self.lbl_status = ctk.CTkLabel(self, text="", font=app.f_small,
                                       text_color=MUTED, justify="left",
                                       wraplength=500)
        self.lbl_status.pack(anchor="w", pady=(12, 0), **pad)

        rowb = ctk.CTkFrame(self, fg_color="transparent")
        rowb.pack(fill="x", pady=(10, 18), **pad)
        rowb.grid_columnconfigure(0, weight=1)
        self.btn_go = ctk.CTkButton(
            rowb, text="Activar la web", height=42, font=app.f_btn,
            corner_radius=10, fg_color=GREEN, hover_color=GREEN_H,
            text_color="#ffffff", command=self._activate)
        self.btn_go.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.btn_visit = ctk.CTkButton(
            rowb, text="Abrir la web", height=42, width=130,
            font=app.f_body, corner_radius=10, fg_color="transparent",
            hover_color=PANEL2, border_width=1, border_color=BORDER,
            text_color=BLUE, state="disabled", command=self._visit)
        self.btn_visit.grid(row=0, column=1, padx=(0, 10))
        ctk.CTkButton(rowb, text="Cerrar", height=42, width=90,
                      font=app.f_body, corner_radius=10,
                      fg_color="transparent", hover_color=PANEL2,
                      border_width=1, border_color=BORDER,
                      text_color=MUTED, command=self.destroy
                      ).grid(row=0, column=2)

        saved = app.get_saved_token()
        if saved:
            self.e_token.insert(0, saved)

        self.after(80, self._poll)

    # ------------------------------------------------------------- #
    def _safe_grab(self):
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _toggle_token(self):
        self._token_visible = not self._token_visible
        self.e_token.configure(show="" if self._token_visible else "•")

    def _status(self, text, color=MUTED):
        self.lbl_status.configure(text=text, text_color=color)

    def _visit(self):
        if self.site_url:
            webbrowser.open(self.site_url)

    # ------------------------------------------------------------- #
    def _activate(self):
        token = self.e_token.get().strip()
        branch = self.e_branch.get().strip() or "main"
        path = "/docs" if "docs" in self.seg_path.get() else "/"
        if not token:
            self._status("Escribe tu token de GitHub.", RED)
            return
        self.btn_go.configure(state="disabled", text="Activando…")
        self._status("Conectando con GitHub…", MUTED)
        threading.Thread(target=self._worker,
                         args=(token, branch, path), daemon=True).start()

    def _worker(self, token, branch, path):
        base = f"/repos/{self.owner}/{self.repo}"
        try:
            code, info = github_api("GET", base, token)
            if code == 401:
                raise StepError("Token inválido o caducado.")
            if code == 404:
                raise StepError("No encuentro el repositorio. ¿Ya lo "
                                "publicaste con «🚀»?")
            if code != 200:
                raise StepError(f"GitHub respondió {code}: "
                                f"{info.get('message', '')}")
            if info.get("private"):
                raise StepError("El repositorio es privado y GitHub Pages "
                                "gratuito solo funciona con públicos. "
                                "Hazlo público en github.com → Settings "
                                "y vuelve a intentarlo.")

            src = {"source": {"branch": branch, "path": path}}
            code, pages = github_api("GET", base + "/pages", token)
            if code == 404:
                self.q.put(("st", "Activando GitHub Pages…"))
                code, pages = github_api("POST", base + "/pages", token,
                                         src)
                if code == 409:
                    code, pages = github_api("GET", base + "/pages", token)
                elif code == 422:
                    raise StepError("GitHub no aceptó la configuración. "
                                    f"¿Existe la rama «{branch}» en el "
                                    "repositorio? Publica primero con «🚀».")
                elif code not in (201, 200):
                    raise StepError(f"No se pudo activar ({code}): "
                                    f"{pages.get('message', '')}")
            elif code == 200:
                cur = pages.get("source") or {}
                if (cur.get("branch") != branch
                        or cur.get("path") != path):
                    self.q.put(("st", "Actualizando el origen de la web…"))
                    github_api("PUT", base + "/pages", token, src)
            else:
                raise StepError(f"GitHub respondió {code}: "
                                f"{pages.get('message', '')}")

            # Esperar a que la web se construya
            url = (pages or {}).get("html_url") \
                  or f"https://{self.owner}.github.io/{self.repo}/"
            self.q.put(("st", "La web se está construyendo… (suele "
                              "tardar menos de un minuto)"))
            for _ in range(30):
                time.sleep(3)
                code, pages = github_api("GET", base + "/pages", token)
                if code == 200:
                    url = pages.get("html_url") or url
                    st = (pages.get("status") or "").lower()
                    if st == "built":
                        break
                    if st == "errored":
                        raise StepError("GitHub no pudo construir la web. "
                                        "Revisa que haya un index.html en "
                                        "el origen elegido.")
            self.q.put(("ok", url))
        except StepError as e:
            self.q.put(("err", str(e)))
        except Exception as e:
            self.q.put(("err", f"Error de red: {e}"))

    # ------------------------------------------------------------- #
    def _poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "st":
                    self._status(m[1], MUTED)
                elif m[0] == "err":
                    self._status("✖ " + m[1], RED)
                    self.btn_go.configure(state="normal",
                                          text="Activar la web")
                elif m[0] == "ok":
                    self.site_url = m[1]
                    self.app.last_pages_url = m[1]
                    self._status("✔ ¡Tu web está en línea!\n" + m[1],
                                 "#3fb950")
                    self.btn_go.configure(state="normal",
                                          text="Activar la web")
                    self.btn_visit.configure(state="normal")
                    self.app._log(f"✔ Web activada: {m[1]}", "ok")
                    self.app._log("  Si acabas de publicar, puede tardar "
                                  "1-2 min en verse la última versión.",
                                  "dim")
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(80, self._poll)


# ================================================================== #
#  Diálogo: crear repositorio en GitHub                              #
# ================================================================== #
class CreateRepoDialog(ctk.CTkToplevel):

    def __init__(self, app: GitPushStudio):
        super().__init__(app)
        self.app = app
        self.q = queue.Queue()
        self._token_visible = False

        self.title("Crear repositorio en GitHub")
        self.configure(fg_color=BG)
        self.transient(app)
        self.after(150, self._safe_grab)
        place_dialog(self, app, 560, 640, min_w=520, min_h=460)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        pad = {"padx": 22}

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(header, text="➕  Crear repositorio en GitHub",
                     font=app.f_title, text_color=TEXT
                     ).pack(anchor="w", pady=(18, 0), **pad)
        ctk.CTkLabel(header, text="Se crea en tu cuenta personal usando "
                                  "la API oficial de GitHub.",
                     font=app.f_small, text_color=MUTED
                     ).pack(anchor="w", pady=(2, 12), **pad)

        body = ctk.CTkScrollableFrame(
            self, fg_color="transparent",
            scrollbar_button_color=BORDER,
            scrollbar_button_hover_color=MUTED)
        body.grid(row=1, column=0, sticky="nsew")

        # --- Token ----------------------------------------------------
        c1 = ctk.CTkFrame(body, fg_color=PANEL, corner_radius=12,
                          border_width=1, border_color=BORDER)
        c1.pack(fill="x", **pad)
        ctk.CTkLabel(c1, text="🔑  Token de acceso personal",
                     font=app.f_section, text_color=TEXT
                     ).pack(anchor="w", padx=16, pady=(12, 2))
        rowt = ctk.CTkFrame(c1, fg_color="transparent")
        rowt.pack(fill="x", padx=16, pady=(6, 0))
        rowt.grid_columnconfigure(0, weight=1)
        self.e_token = app._entry(rowt, "ghp_…", show="•")
        self.e_token.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(rowt, text="👁", width=42, height=36,
                      font=app.f_body, corner_radius=8,
                      fg_color=PANEL2, hover_color=BORDER,
                      border_width=1, border_color=BORDER, text_color=TEXT,
                      command=self._toggle_token).grid(row=0, column=1)
        self.v_remember = ctk.BooleanVar(
            value=bool(app.get_saved_token()))
        ctk.CTkCheckBox(c1, text="Recordar el token en este equipo",
                        variable=self.v_remember, font=app.f_small,
                        text_color=MUTED, fg_color=GREEN,
                        hover_color=GREEN_H, border_color=BORDER,
                        checkbox_width=18, checkbox_height=18
                        ).pack(anchor="w", padx=16, pady=(8, 0))
        storage = ("Se guarda cifrado con tu cuenta de Windows (DPAPI)."
                   if os.name == "nt"
                   else "Se guarda codificado en ~/.gitpush_studio.")
        ctk.CTkLabel(
            c1, justify="left", font=app.f_small, text_color=MUTED,
            text=("Créalo en github.com → Settings → Developer settings →\n"
                  "Personal access tokens (classic) → Generate new token,\n"
                  "marcando el permiso «repo».  " + storage)
        ).pack(anchor="w", padx=16, pady=(6, 12))

        # --- Datos del repositorio -------------------------------------
        c2 = ctk.CTkFrame(body, fg_color=PANEL, corner_radius=12,
                          border_width=1, border_color=BORDER)
        c2.pack(fill="x", pady=(12, 0), **pad)
        ctk.CTkLabel(c2, text="📦  Datos del repositorio",
                     font=app.f_section, text_color=TEXT
                     ).pack(anchor="w", padx=16, pady=(12, 2))
        self.e_name = app._entry(c2, "nombre-del-repositorio")
        self.e_name.pack(fill="x", padx=16, pady=(6, 0))
        self.e_desc = app._entry(c2, "Descripción (opcional)")
        self.e_desc.pack(fill="x", padx=16, pady=(8, 0))
        self.seg_vis = ctk.CTkSegmentedButton(
            c2, values=["Público", "Privado"], font=app.f_body,
            fg_color=INPUT, selected_color=BLUE_D,
            selected_hover_color=BLUE, unselected_color=PANEL2,
            unselected_hover_color=BORDER, text_color=TEXT,
            corner_radius=8, height=32)
        self.seg_vis.set("Público")
        self.seg_vis.pack(anchor="w", padx=16, pady=(10, 0))
        ctk.CTkLabel(c2, text="Consejo: GitHub Pages gratuito necesita un "
                             "repositorio público.",
                     font=app.f_small, text_color=MUTED
                     ).pack(anchor="w", padx=16, pady=(6, 12))

        # Sugerir el nombre a partir de la carpeta del proyecto
        base = app.e_folder.get().strip().strip('"')
        if base:
            sug = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(base).name)
            sug = sug.strip("-.")
            if sug:
                self.e_name.insert(0, sug)
        saved = app.get_saved_token()
        if saved:
            self.e_token.insert(0, saved)

        # --- Pie fijo: estado y botones (siempre visibles) --------------
        footer = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0)
        footer.grid(row=2, column=0, sticky="ew")
        ctk.CTkFrame(footer, height=1, fg_color=BORDER).pack(
            fill="x", side="top")
        self.lbl_status = ctk.CTkLabel(footer, text="", font=app.f_small,
                                       text_color=MUTED, justify="left",
                                       wraplength=500)
        self.lbl_status.pack(anchor="w", pady=(12, 0), **pad)

        rowb = ctk.CTkFrame(footer, fg_color="transparent")
        rowb.pack(fill="x", pady=(10, 16), **pad)
        rowb.grid_columnconfigure(0, weight=1)
        self.btn_create = ctk.CTkButton(
            rowb, text="Crear repositorio", height=42, font=app.f_btn,
            corner_radius=10, fg_color=GREEN, hover_color=GREEN_H,
            text_color="#ffffff", command=self._create)
        self.btn_create.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        ctk.CTkButton(rowb, text="Cancelar", height=42, width=120,
                      font=app.f_body, corner_radius=10,
                      fg_color="transparent", hover_color=PANEL2,
                      border_width=1, border_color=BORDER,
                      text_color=MUTED, command=self.destroy
                      ).grid(row=0, column=1)

        self.after(80, self._poll)

    # ------------------------------------------------------------- #
    def _safe_grab(self):
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _toggle_token(self):
        self._token_visible = not self._token_visible
        self.e_token.configure(show="" if self._token_visible else "•")

    def _status(self, text, color=MUTED):
        self.lbl_status.configure(text=text, text_color=color)

    # ------------------------------------------------------------- #
    def _create(self):
        token = self.e_token.get().strip()
        name = self.e_name.get().strip()
        desc = self.e_desc.get().strip()
        private = self.seg_vis.get() == "Privado"
        remember = bool(self.v_remember.get())

        if not token:
            self._status("Escribe tu token de GitHub.", RED)
            return
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name or ""):
            self._status("Nombre inválido: usa solo letras, números, "
                         "guiones, puntos o guion bajo.", RED)
            return

        self.btn_create.configure(state="disabled", text="Creando…")
        self._status("Conectando con GitHub…", MUTED)
        threading.Thread(target=self._worker,
                         args=(token, name, desc, private, remember),
                         daemon=True).start()

    def _worker(self, token, name, desc, private, remember):
        try:
            code, me = github_api("GET", "/user", token)
            if code == 401:
                raise StepError("Token inválido o caducado. Genera uno "
                                "nuevo en GitHub.")
            if code != 200:
                raise StepError(f"GitHub respondió {code}: "
                                f"{me.get('message', 'error desconocido')}")
            login = me.get("login", "")

            payload = {"name": name, "private": private,
                       "description": desc, "auto_init": False}
            code, body = github_api("POST", "/user/repos", token, payload)

            if code == 201:
                clone = body.get("clone_url") \
                        or f"https://github.com/{login}/{name}.git"
                html = body.get("html_url") \
                       or f"https://github.com/{login}/{name}"
                self.q.put(("ok", clone, html, False, token, remember))
            elif code == 422 and "already exists" in \
                    json.dumps(body).lower():
                clone = f"https://github.com/{login}/{name}.git"
                self.q.put(("ok", clone, clone[:-4], True, token, remember))
            elif code == 403:
                raise StepError("GitHub denegó la operación (403). "
                                "¿El token tiene el permiso «repo»?")
            else:
                raise StepError(f"No se pudo crear ({code}): "
                                f"{body.get('message', 'error desconocido')}")
        except StepError as e:
            self.q.put(("err", str(e)))
        except Exception as e:
            self.q.put(("err", f"Error de red: {e}"))

    # ------------------------------------------------------------- #
    def _poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "err":
                    self._status("✖ " + m[1], RED)
                    self.btn_create.configure(state="normal",
                                              text="Crear repositorio")
                    continue

                _, clone, html, existed, token, remember = m
                self.app.store_token(token, remember)
                self.app.e_url.delete(0, "end")
                self.app.e_url.insert(0, clone)
                self.app.last_repo_web = html
                if existed:
                    self.app._log("⚠ El repositorio ya existía en tu "
                                  "cuenta; he colocado su URL en el "
                                  "formulario.", "warn")
                else:
                    self.app._log(f"✔ Repositorio creado: {html}", "ok")
                self.app._log("  URL lista. Ahora pulsa «Publicar en "
                              "GitHub».", "dim")
                self.app._set_status("●  Repositorio listo", "ok")
                self.destroy()
                return
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(80, self._poll)


# ================================================================== #
if __name__ == "__main__":
    app = GitPushStudio()
    app.mainloop()
