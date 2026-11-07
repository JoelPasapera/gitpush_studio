# -*- coding: utf-8 -*-
"""Lo que no se publica, SIN escribirlo en el .gitignore.

El cuadro «Archivos y carpetas a ignorar» se escribía en el .gitignore
del proyecto, y el .gitignore se publica: en GitHub quedaban a la vista
los nombres de lo que no querías enseñar («PROJECT_CONTEXT.md»,
«notas/», «claves.env»…). Ahora esos patrones van a la lista de
exclusión PRIVADA de git, `.git/info/exclude`: git la aplica igual que
un .gitignore (al añadir, en el estado, al listar lo que se subiría),
pero vive dentro de la carpeta .git, que nunca se sube. En GitHub no
queda ni rastro.

La lista es un bloque marcado dentro de ese archivo; lo que haya fuera
del bloque (las líneas de ejemplo que pone git, las tuyas) se conserva
byte a byte. El bloque tiene dos partes:

  · `user`: las del cuadro (también lo que excluyen la revisión de
    seguridad y la vista previa, que acaba igualmente en el cuadro);
  · `auto`: las de la propia aplicación, el TXT del proyecto y su
    temporal, que no tienen por qué llenar el cuadro.

La lista solo CRECE, salvo que se pida lo contrario (`release`):
publicar varios proyectos con el cuadro de otro, o abrir la carpeta sin
su perfil, nunca puede volver a publicar lo que ya estaba fuera. Volver
a publicar algo es una decisión explícita: quitarlo del cuadro, que al
abrir el proyecto enseña lo que ya tiene excluido, y publicar.
"""

import os
import re
from collections import namedtuple
from pathlib import Path

from .atomic import write_bytes
from .errors import StepError

START = ("# >>> GitPush Studio: no se publica (lista privada; nunca se "
         "copia al .gitignore) >>>")
USER = "# Del cuadro «Archivos y carpetas a ignorar»:"
AUTO = "# Añadidas por la aplicación (el TXT del proyecto y su temporal):"
END = "# <<< GitPush Studio <<<"

# El temporal del TXT, tal como lo escribe la publicación:
# «/carpeta/.nombre.txt.*.tmp». Su pareja es «/carpeta/nombre.txt».
_TXT_TEMP = re.compile(r"^(/(?:[^/]+/)*)\.(.+)\.\*\.tmp$")


class Private(namedtuple("Private", "user auto")):
    """Patrones de la lista privada: los del cuadro y los de la app."""

    @property
    def all(self):
        return _unique(list(self.user) + list(self.auto))

    @property
    def empty(self):
        return not (self.user or self.auto)


EMPTY = Private((), ())


def _unique(items):
    out, seen = [], set()
    for p in items:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ------------------------------------------------------------------ #
#  Dónde vive                                                         #
# ------------------------------------------------------------------ #
def exclude_path(folder, git=None):
    """La lista privada de git de `folder` (`.git/info/exclude`), o None
    si no es un repositorio.

    Si `.git` es un archivo (un worktree, un submódulo), la lista de
    verdad está en otro sitio: se le pregunta a git (`--git-path`), que
    devuelve la compartida por todos los worktrees.
    """
    punto = Path(folder) / ".git"
    try:
        if punto.is_dir() and not punto.is_symlink():
            return punto / "info" / "exclude"
        existe = os.path.lexists(punto)
    except OSError:
        return None
    if not existe or git is None:
        return None
    p = git.run(["rev-parse", "--git-path", "info/exclude"], cwd=str(folder),
                check=False, echo=False)
    ruta = (p.stdout or "").strip()
    if p.returncode != 0 or not ruta:
        return None
    ruta = Path(ruta)
    return ruta if ruta.is_absolute() else Path(folder) / ruta


# ------------------------------------------------------------------ #
#  Leer y escribir el bloque                                          #
# ------------------------------------------------------------------ #
def split_block(texto):
    """(antes, Private, después) de un archivo de exclusiones.

    Sin bloque: (texto, EMPTY, ""). Un bloque sin su marca de cierre (un
    corte a medias) llega hasta el final. Si por lo que sea hubiera
    varios, se juntan en uno.
    """
    lineas = texto.splitlines(keepends=True)
    inicio = next((i for i, ln in enumerate(lineas)
                   if ln.rstrip("\r\n") == START), None)
    if inicio is None:
        return texto, EMPTY, ""
    fin = next((j for j in range(inicio + 1, len(lineas))
                if lineas[j].rstrip("\r\n") == END), len(lineas))
    user, auto, parte = [], [], None
    for ln in lineas[inicio + 1:fin]:
        t = ln.rstrip("\r\n")
        if t == USER:
            parte = user
            continue
        if t == AUTO:
            parte = auto
            continue
        p = t.strip()
        if not p or p.startswith("#"):
            continue
        (user if parte is None else parte).append(p)
    antes = "".join(lineas[:inicio])
    despues = "".join(lineas[fin + 1:])
    if START in despues:                      # otro bloque más abajo
        medio, otro, despues = split_block(despues)
        despues = medio + despues
        user += list(otro.user)
        auto += list(otro.auto)
    return antes, Private(tuple(_unique(user)), tuple(_unique(auto))), despues


def render_block(private, nl="\n"):
    if private.empty:
        return ""
    partes = [START, USER] + list(private.user)
    if private.auto:
        partes += [AUTO] + list(private.auto)
    partes.append(END)
    return nl.join(partes) + nl


def _leer(ruta):
    try:
        return ruta.read_bytes().decode("utf-8", "surrogateescape")
    except FileNotFoundError:
        return ""
    except OSError as e:
        raise StepError(f"No pude leer la lista privada de git ({e}).") \
            from None


def read_private(folder, git=None):
    """La lista privada de `folder` (vacía si no es repositorio)."""
    ruta = exclude_path(folder, git)
    if ruta is None:
        return EMPTY
    try:
        return split_block(_leer(ruta))[1]
    except StepError:
        return EMPTY


def write_private(folder, private, git=None):
    """Escribe el bloque (de forma atómica) y deja intacto el resto del
    archivo. Devuelve True si cambió algo.

    Un enlace en su lugar no se sigue: escribir a través de él cambiaría
    un archivo de otro sitio.
    """
    ruta = exclude_path(folder, git)
    if ruta is None:
        raise StepError("La carpeta no es un repositorio de git: no hay "
                        "lista privada donde guardar lo que no se publica.")
    if os.path.islink(ruta):
        raise StepError("La lista privada de git (.git/info/exclude) es un "
                        "enlace a otro archivo: no la modifico. Cámbiala "
                        "por un archivo normal.")
    texto = _leer(ruta)
    antes, _viejo, despues = split_block(texto)
    nl = "\r\n" if "\r\n" in texto else "\n"
    if antes and not antes.endswith(("\n", "\r")):
        antes += nl
    nuevo = antes + render_block(private, nl) + despues
    if nuevo == texto:
        return False
    ruta.parent.mkdir(parents=True, exist_ok=True)
    write_bytes(ruta, nuevo.encode("utf-8", "surrogateescape"))
    return True


def merged(old, user=(), auto=(), release=()):
    """La lista nueva: lo que había, más lo nuevo, menos lo que se pidió
    volver a publicar. `release` solo afecta a lo del cuadro, y lo que
    vuelve a estar en el cuadro se queda."""
    fuera = set(release) - set(user)
    return Private(
        tuple(_unique([p for p in old.user if p not in fuera]
                      + list(user))),
        tuple(_unique(list(old.auto) + list(auto))))


# ------------------------------------------------------------------ #
#  Lo que versiones anteriores escribían en el .gitignore             #
# ------------------------------------------------------------------ #
def app_txt_lines(gitignore_lines):
    """Las líneas del TXT que la aplicación escribía en el .gitignore: el
    temporal («/.proyecto.txt.*.tmp», inconfundible) y su pareja
    («/proyecto.txt»), solo si están las dos."""
    lineas = [ln.strip() for ln in gitignore_lines]
    presentes = set(lineas)
    out = []
    for ln in lineas:
        m = _TXT_TEMP.match(ln)
        if not m:
            continue
        pareja = m.group(1) + m.group(2)
        if pareja in presentes:
            out += [pareja, ln]
    return _unique(out)
