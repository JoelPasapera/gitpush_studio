# -*- coding: utf-8 -*-
"""Publicar una versión: etiqueta git + GitHub Release (+ zip opcional)."""

import os
import re
import tempfile
from dataclasses import dataclass

from . import github
from .errors import StepError
from .git import Git

_VERSION_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


@dataclass
class ReleaseResult:
    status: str               # ok | error
    message: str = ""
    html_url: str = ""
    tag: str = ""
    asset: str = ""


def list_tags(folder, git=None):
    git = git or Git()
    if not git.is_repo(folder):
        return []
    p = git.run(["tag", "--list", "--sort=-v:refname"], cwd=folder,
                check=False, echo=False)
    return [t.strip() for t in (p.stdout or "").splitlines() if t.strip()] \
        if p.returncode == 0 else []


def suggest_tag(folder, git=None):
    """Siguiente versión de parche a partir de la última etiqueta
    «vX.Y.Z»; «v1.0.0» si no hay ninguna."""
    for tag in list_tags(folder, git):
        m = _VERSION_TAG.match(tag)
        if m:
            major, minor, patch = (int(x) for x in m.groups())
            return f"v{major}.{minor}.{patch + 1}"
    return "v1.0.0"


def valid_tag(tag):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", tag or ""))


def _zip_head(git, folder, name):
    """Zip de lo que hay en HEAD (exactamente lo publicado)."""
    fd, path = tempfile.mkstemp(prefix="release-", suffix=".zip")
    os.close(fd)
    prefix = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "proyecto"
    git.run(["archive", "--format=zip", f"--prefix={prefix}/", "-o", path,
             "HEAD"], cwd=folder)
    return path


def create_release(folder, url, owner, repo, token, tag, title="", notes="",
                   attach_zip=True, git=None, log=None, on_status=None,
                   branch="main"):
    """Etiqueta HEAD, sube la etiqueta y crea la release en GitHub.
    Exige que GitHub ya tenga exactamente esta versión. Nunca lanza:
    devuelve un ReleaseResult."""
    git = git or Git(log=log)
    say = log or (lambda text, tag="dim": None)
    status = on_status or (lambda text: None)
    title = (title or "").strip() or tag
    try:
        if not valid_tag(tag):
            raise StepError("Nombre de versión no válido: usa letras, números, "
                            "puntos y guiones (ej. v1.2.0).")
        if not token:
            raise StepError("Hace falta un token de GitHub con permiso «repo».")
        if not git.is_repo(folder) or not git.has_commits(folder):
            raise StepError("Publica el proyecto al menos una vez antes de "
                            "crear una versión.")
        if git.is_dirty(folder):
            raise StepError("Hay cambios sin publicar. Publica primero para "
                            "que la versión coincida con lo que hay en "
                            "GitHub.")
        if tag in list_tags(folder, git):
            raise StepError(f"La versión «{tag}» ya existe. Elige otro "
                            "nombre.")
        status("Comprobando que GitHub tenga esta versión…")
        git.ensure_remote(folder, url)
        p = git.run(["fetch", "origin", branch], cwd=folder, check=False,
                    timeout=600)
        if p.returncode != 0 or not git.is_ancestor(folder, "HEAD",
                                                     "FETCH_HEAD"):
            raise StepError("GitHub aún no tiene tu última versión en la "
                            f"rama «{branch}». Publica primero y vuelve a "
                            "intentarlo.")

        status("Creando la etiqueta…")
        say(f"🏷 Etiquetando {tag}…", "out")
        git.run(["tag", "-a", tag, "-m", title], cwd=folder)
        p = git.run(["push", "origin", f"refs/tags/{tag}"], cwd=folder,
                    check=False, timeout=600)
        if p.returncode != 0:
            git.run(["tag", "-d", tag], cwd=folder, check=False)
            raise StepError("No pude subir la etiqueta a GitHub. ¿La rama "
                            "está publicada y tienes permisos?")

        status("Creando la release en GitHub…")
        code, body = github.api("POST", f"/repos/{owner}/{repo}/releases",
                                token, {"tag_name": tag, "name": title,
                                        "body": notes or "",
                                        "draft": False, "prerelease": False})
        if code == 401:
            raise StepError("Token inválido o caducado.")
        if code == 404:
            raise StepError("GitHub no encuentra el repositorio, o el token "
                            "no tiene permiso «repo».")
        if code not in (200, 201):
            raise StepError(f"GitHub respondió {code}: "
                            f"{body.get('message', '') if isinstance(body, dict) else ''}")
        html = body.get("html_url") or \
            f"https://github.com/{owner}/{repo}/releases/tag/{tag}"
        asset = ""
        release_id = body.get("id") if isinstance(body, dict) else None
        if attach_zip and not release_id:
            say("  ⚠ GitHub no devolvió el identificador de la release; "
                "no se adjunta el zip.", "warn")
        if attach_zip and release_id:
            status("Preparando el zip del proyecto…")
            zip_path = _zip_head(git, folder, f"{repo}-{tag}")
            try:
                with open(zip_path, "rb") as fh:
                    data = fh.read()
                status("Subiendo el zip…")
                asset = f"{repo}-{tag}.zip"
                code, abody = github.upload_asset(owner, repo, release_id,
                                                  token, asset, data)
                if code not in (200, 201):
                    say(f"  ⚠ La release se creó pero no pude adjuntar el "
                        f"zip ({code}).", "warn")
                    asset = ""
            finally:
                try:
                    os.unlink(zip_path)
                except OSError:
                    pass
        msg = f"Versión {tag} publicada: {html}"
        say("✔ " + msg, "ok")
        return ReleaseResult("ok", msg, html, tag, asset)
    except StepError as e:
        say("✖ " + str(e), "err")
        return ReleaseResult("error", str(e))
    except Exception as e:                                  # noqa: BLE001
        say(f"✖ Error inesperado: {e}", "err")
        return ReleaseResult("error", f"Error inesperado: {e}")
