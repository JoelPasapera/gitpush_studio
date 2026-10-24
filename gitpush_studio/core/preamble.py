# -*- coding: utf-8 -*-
"""Texto de presentación de los TXT consolidados.

Quien abre uno de estos archivos —o se lo pasa a otra herramienta— no
tiene por qué saber cómo está montado. Estas funciones escriben, en
prosa, qué contiene, cómo está ordenado, qué significan las marcas y
qué se quedó fuera, para que nada se malinterprete: un archivo omitido
por binario no es un archivo vacío.
"""

import textwrap
from datetime import datetime

from .. import APP_NAME, APP_VERSION
from .fs import fmt_size

WIDTH = 78

MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre")


def _cuando(when=None):
    d = when or datetime.now()
    return f"{d.day} de {MESES[d.month - 1]} de {d.year} a las {d:%H:%M}"


def _parrafos(*textos):
    return "\n\n".join(textwrap.fill(t, WIDTH) for t in textos if t) + "\n"


def _firma():
    return f"{APP_NAME} {APP_VERSION}"


def _convenciones(que):
    return (
        f"El contenido de cada archivo empieza con una línea «MODULO: "
        f"ruta/relativa» seguida de una raya, y termina con una línea de "
        f"signos «=» que lo separa del siguiente, así que cualquier "
        f"archivo {que} se localiza buscando su ruta tal como aparece en "
        f"el árbol.")


def _omisiones(max_file_bytes):
    return (
        "Quedan fuera del volcado lo que ignora el .gitignore del "
        "proyecto, la carpeta .git con el historial, los archivos "
        "binarios (imágenes, vídeos, fuentes, ejecutables) y los que "
        f"superan {fmt_size(max_file_bytes)}. En su lugar aparece una nota "
        "con el motivo y el tamaño, para que su ausencia no se confunda "
        "con un archivo vacío.")


def profile_text(username, profile_url, repo_count, max_file_bytes,
                 when=None):
    """Presentación del TXT que reúne un perfil entero."""
    return _parrafos(
        f"Este archivo reúne en texto plano el contenido de los "
        f"{repo_count} repositorios del perfil de GitHub «{username}» "
        f"({profile_url}), tal y como estaban el {_cuando(when)}. Lo generó "
        f"{_firma()} a partir de una copia local de cada repositorio, así "
        f"que es una foto fija: no refleja los cambios posteriores ni "
        f"incluye el historial de versiones, las ramas, las incidencias ni "
        f"ningún otro dato del repositorio que no sean sus archivos.",

        "Está ordenado de lo general a lo concreto. Primero va el árbol "
        "del perfil, donde la raíz es el usuario y cada rama un "
        "repositorio. Después viene un bloque por repositorio, en orden "
        "alfabético y encabezado por «REPOSITORIO n/total», con su propio "
        "árbol de carpetas y, a continuación, el contenido de sus "
        "archivos.",

        _convenciones("de cualquier repositorio"),
        _omisiones(max_file_bytes),
        "Al final del archivo hay un resumen con las cifras totales.")


def project_text(project_name, file_count, max_file_bytes, when=None):
    """Presentación del TXT de un solo proyecto."""
    return _parrafos(
        f"Este archivo reúne en texto plano el contenido del proyecto "
        f"«{project_name}»: {file_count} archivos, tal y como estaban el "
        f"{_cuando(when)}. Lo generó {_firma()} a partir de la carpeta del "
        f"proyecto, así que es una foto fija y no incluye el historial de "
        f"versiones ni nada que no sean sus archivos.",

        "Primero va el árbol de carpetas completo y después el contenido "
        "de cada archivo, en el mismo orden.",

        _convenciones("del proyecto"),
        _omisiones(max_file_bytes),
        "Al final del archivo hay un resumen con las cifras totales.")


def _plural(n, singular, plural):
    return f"{n} {singular if n == 1 else plural}"


def _duracion(segundos):
    if segundos < 1:
        return "en menos de un segundo"
    if segundos < 60:
        return f"en {segundos:.1f} segundos"
    return f"en {int(segundos // 60)} min {int(segundos % 60)} s"


def summary_text(stats, truncated=False):
    """Resumen final, también en prosa."""
    partes = []
    if "repos" in stats:
        partes.append(_plural(stats["repos"], "repositorio", "repositorios"))
    partes.append(_plural(stats["files"], "archivo volcado",
                          "archivos volcados"))
    if stats.get("binaries"):
        partes.append(_plural(stats["binaries"], "binario omitido",
                              "binarios omitidos"))
    if stats.get("big"):
        partes.append(_plural(stats["big"], "omitido por tamaño",
                              "omitidos por tamaño"))
    if stats.get("ignored"):
        partes.append(_plural(stats["ignored"], "ignorado", "ignorados"))
    if len(partes) > 1:
        lista = ", ".join(partes[:-1]) + " y " + partes[-1]
    else:
        lista = partes[0]
    cuerpo = (f"Este archivo contiene {lista}. Ocupa "
              f"{fmt_size(stats['bytes'])} y se generó "
              f"{_duracion(stats['seconds'])}.")
    if truncated:
        cuerpo += (" Aviso: se alcanzó el límite de tamaño, así que el "
                   "volcado está cortado y faltan los últimos archivos.")
    return ("#" * 80 + "\nRESUMEN\n" + "#" * 80 + "\n\n"
            + textwrap.fill(cuerpo, WIDTH) + "\n")
