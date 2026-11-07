# -*- coding: utf-8 -*-
"""Nombres de rama válidos, con las MISMAS reglas que git
(`git check-ref-format --branch`), comprobadas sin lanzar procesos.

Por qué importa: el nombre de la rama acaba en la línea de comandos de
git («git fetch <url> <rama>», «git push origin <rama>»). Un nombre que
empiece por «-» se leería como una OPCIÓN («--upload-pack=…» ejecuta un
programa) y uno con «:» o «*» cambia lo que significa el refspec. Aquí
se rechaza todo eso antes de que llegue a git, con un motivo que se
pueda enseñar tal cual.
"""

from .errors import StepError

# Caracteres que git nunca admite en un nombre de referencia.
_PROHIBIDOS = frozenset(" ~^:?*[\\")
_MAX_BYTES = 200


def branch_problem(name):
    """Cadena vacía si `name` sirve como nombre de rama; si no, el motivo
    (una frase en castellano, lista para enseñarla)."""
    if not isinstance(name, str) or not name:
        return "Escribe el nombre de la rama."
    if name != name.strip():
        return "El nombre de la rama no puede empezar ni acabar con espacios."
    try:
        crudo = name.encode("utf-8")
    except UnicodeEncodeError:
        return "El nombre de la rama tiene caracteres que no son válidos."
    if len(crudo) > _MAX_BYTES:
        return "El nombre de la rama es demasiado largo."
    if name.startswith("-"):
        return "El nombre de la rama no puede empezar por «-»."
    if name in ("@", "HEAD"):
        return f"«{name}» no puede ser el nombre de una rama."
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return "El nombre de la rama tiene caracteres de control."
    malos = sorted(set(name) & _PROHIBIDOS)
    if malos:
        vistos = " ".join("«espacio»" if c == " " else f"«{c}»" for c in malos)
        return f"El nombre de la rama no puede llevar {vistos}."
    if ".." in name:
        return "El nombre de la rama no puede llevar «..»."
    if "@{" in name:
        return "El nombre de la rama no puede llevar «@{»."
    if name.startswith("/") or name.endswith("/") or "//" in name:
        return ("El nombre de la rama no puede empezar ni acabar en «/», "
                "ni llevar «//».")
    if name.endswith("."):
        return "El nombre de la rama no puede acabar en «.»."
    for tramo in name.split("/"):
        if tramo.startswith("."):
            return "Ningún tramo del nombre de la rama puede empezar por «.»."
        if tramo.endswith(".lock"):
            return ("Ningún tramo del nombre de la rama puede acabar en "
                    "«.lock».")
    return ""


def valid_branch_name(name):
    """¿Sirve `name` como nombre de rama?"""
    return not branch_problem(name)


def require_branch(name):
    """Devuelve `name` si es válido; si no, lanza StepError con el motivo."""
    problema = branch_problem(name)
    if problema:
        raise StepError(problema)
    return name
