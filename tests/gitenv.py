# -*- coding: utf-8 -*-
"""Entorno git para las pruebas, independiente del equipo.

Las pruebas no pueden depender de la configuración global de quien las
ejecute. Dos ajustes cambian de una instalación a otra y rompían los
resultados:

  · `init.defaultBranch` — sin él, `git init` crea «master». Los
    repositorios «bare» que hacen de GitHub se quedaban con HEAD
    apuntando a una rama inexistente, así que clonarlos no sacaba
    ningún archivo («remote HEAD refers to nonexistent ref») y media
    docena de pruebas comparaba contra una carpeta vacía.
  · `core.autocrlf` — activado por defecto en Windows, convierte los
    finales de línea al sacar los archivos, así que el mismo contenido
    da hashes distintos según el equipo.

Aquí se fijan los dos explícitamente en cada repositorio que crean las
pruebas. La aplicación no toca ninguno: se limita a renombrar la rama
local a la que pidas (`git branch -M`), por eso funciona igual con
«master» o con «main».
"""

import shutil
import subprocess
from pathlib import Path

HAVE_GIT = shutil.which("git") is not None
BRANCH = "main"
# Identidad y finales de línea fijos: mismo resultado en cualquier equipo.
IDENT = ["-c", "user.name=test", "-c", "user.email=test@example.com",
         "-c", "core.autocrlf=false"]


def git(cwd, *args, check=True):
    """Ejecuta git en `cwd` y devuelve su salida estándar."""
    return subprocess.run(["git", *IDENT, *args], cwd=str(cwd), check=check,
                          capture_output=True, text=True,
                          encoding="utf-8").stdout


def _configure(path):
    git(path, "config", "user.name", "test")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "core.autocrlf", "false")


def init_repo(folder, branch=BRANCH):
    """Repositorio de trabajo con la rama y la identidad fijadas."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    git(folder, "init", "-q")
    git(folder, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    _configure(folder)
    return str(folder)


def init_bare(where, name="remote", branch=BRANCH):
    """Repositorio «bare» que hace de GitHub.

    GitHub apunta su HEAD a la rama por defecto en cuanto recibe el
    primer push; aquí se hace explícito para que clonarlo saque los
    archivos en cualquier equipo.
    """
    path = Path(where) / f"{name}.git"
    git(Path(where), "init", "--bare", "-q", str(path))
    git(path, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    return str(path)


def clone(source, dest, configure=True):
    """Clona sin que la configuración del equipo altere el contenido."""
    dest = Path(dest)
    subprocess.run(["git", *IDENT, "clone", "-q", str(source), str(dest)],
                   check=True, capture_output=True, text=True)
    if configure:
        _configure(dest)
    return str(dest)


def commit_all(folder, message="commit"):
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", message)
