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


def _omisiones():
    return (
        "Quedan fuera del volcado lo que ignora el .gitignore del "
        "proyecto, la carpeta .git con el historial y los archivos "
        "binarios (imágenes, vídeos, fuentes, ejecutables…), que no son "
        "texto legible: en su lugar aparece una nota con su tamaño, para "
        "que su ausencia no se confunda con un archivo vacío. Los archivos "
        "de texto están completos, midan lo que midan; los que estaban en "
        "UTF-16 o en la codificación clásica de Windows (ANSI) aparecen ya "
        "convertidos a UTF-8, y un byte suelto que no era texto válido se "
        "muestra como «�».")


def profile_text(username, profile_url, repo_count, when=None):
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
        _omisiones(),
        "Al final del archivo hay un resumen con las cifras totales.")


def _para_ia(options):
    """Los párrafos que añaden las opciones pensadas para una IA."""
    textos = []
    if options is not None and options.for_ai:
        textos.append(
            "Está preparado para pasárselo a una IA: lo que no aporta "
            "(archivos de bloqueo de dependencias, minificados, generados "
            "o de terceros) aparece solo como una nota «<OMITIDO PARA IA…>» "
            "con su tamaño, para no gastar contexto.")
    if options is not None and options.redact:
        textos.append(
            "Los valores que parecían secretos (contraseñas, tokens, "
            "llaves) están tachados como «••••••••»: el archivo no los "
            "contiene. Al aplicar cambios de vuelta, GitPush Studio "
            "recupera los valores reales del original.")
    return textos


def _como_devolver():
    return ("Para proponer cambios, devuelve cada archivo modificado (o "
            "nuevo) completo y con el mismo formato: la línea «MODULO: "
            "ruta», la raya y el contenido entero, sin resumir partes con "
            "«…». Así se puede aplicar de vuelta al proyecto con GitPush "
            "Studio, que enseña las diferencias antes de tocar nada.")


def project_text(project_name, file_count, when=None, options=None):
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
        _omisiones(),
        *_para_ia(options),
        _como_devolver(),
        "Al final del archivo hay un resumen con las cifras totales.")


def changes_text(project_name, change_set, with_content, deleted,
                 options=None, when=None):
    """Presentación del TXT de «solo lo que cambió» (ver `txtchanges`)."""
    diferencias = options is not None and options.changes == "diferencias"
    if diferencias:
        forma = ("De cada archivo que cambió van solo las líneas que salen "
                 "(−) y las que entran (+), en bloques «DIFERENCIAS: ruta» "
                 "con tres líneas iguales alrededor, como los diffs de git. "
                 "No son el contenido entero de los archivos.")
    else:
        forma = ("Cada archivo que cambió va entero, con el formato de "
                 "siempre: una línea «MODULO: ruta», una raya y su "
                 "contenido, hasta la línea de signos «=».")
    borrados = ""
    if deleted:
        borrados = (" Los archivos borrados aparecen solo con la nota "
                    "«<BORRADO…>».")
    return _parrafos(
        f"Este archivo reúne solo lo que cambió en el proyecto "
        f"«{project_name}» desde {change_set.reference()}: "
        f"{change_set.describe()}, tal y como estaban el {_cuando(when)}. "
        f"Lo generó {_firma()}.",

        "El árbol de carpetas es el del proyecto completo, para situar "
        "cada cambio; debajo solo van los archivos que cambiaron.",

        forma + borrados,
        *_para_ia(options),
        "" if diferencias else _como_devolver(),
        "Al final del archivo hay un resumen con las cifras totales.")


def _num(n):
    """1.234.567 (separador de miles a la española)."""
    return f"{n:,}".replace(",", ".")


def _plural(n, singular, plural):
    return f"{_num(n)} {singular if n == 1 else plural}"


def _lista(partes):
    if len(partes) > 1:
        return ", ".join(partes[:-1]) + " y " + partes[-1]
    return partes[0]


def _duracion(segundos):
    if segundos < 1:
        return "en menos de un segundo"
    if segundos < 60:
        return f"en {segundos:.1f} segundos"
    return f"en {int(segundos // 60)} min {int(segundos % 60)} s"


def summary_text(stats):
    """Resumen final, también en prosa: qué hay, qué se mencionó sin
    volcar y por qué, cuánto ocupa y cuánto tardó."""
    archivos = stats.get("files", 0)
    frases = []
    if archivos:
        clases = []
        solo_texto = archivos == stats.get("text", 0)
        if stats.get("text") or (solo_texto and not stats.get("diffs")):
            clases.append(_plural(stats.get("text", 0),
                                  "de texto (volcado entero)",
                                  "de texto (volcados enteros)"))
        if stats.get("binaries"):
            clases.append(_plural(stats["binaries"],
                                  "binario (solo se menciona, con su "
                                  "tamaño)", "binarios (solo se mencionan, "
                                  "con su tamaño)"))
        if stats.get("links"):
            clases.append(_plural(stats["links"],
                                  "enlace simbólico (se indica su destino, "
                                  "sin seguirlo)", "enlaces simbólicos (se "
                                  "indica su destino, sin seguirlos)"))
        if stats.get("special"):
            clases.append(_plural(stats["special"],
                                  "especial (tubería o dispositivo: no se "
                                  "abre)", "especiales (tuberías o "
                                  "dispositivos: no se abren)"))
        if stats.get("diffs"):
            clases.append(_plural(stats["diffs"], "con sus diferencias",
                                  "con sus diferencias"))
        if stats.get("for_ai"):
            clases.append(_plural(stats["for_ai"],
                                  "omitido para la IA (de bloqueo, "
                                  "minificado, generado o de terceros)",
                                  "omitidos para la IA (de bloqueo, "
                                  "minificados, generados o de terceros)"))
        if stats.get("deleted"):
            clases.append(_plural(stats["deleted"], "borrado (solo se "
                                  "menciona)", "borrados (solo se "
                                  "mencionan)"))
        if stats.get("errors"):
            clases.append(_plural(stats["errors"], "que no se pudo leer",
                                  "que no se pudieron leer"))
        de_repos = ""
        if "repos" in stats:
            de_repos = " de " + _plural(stats["repos"], "repositorio",
                                        "repositorios")
        frases.append(f"Este archivo recorre "
                      f"{_plural(archivos, 'archivo', 'archivos')}"
                      f"{de_repos}: {_lista(clases)}." if clases else
                      f"Este archivo recorre "
                      f"{_plural(archivos, 'archivo', 'archivos')}"
                      f"{de_repos}.")
    elif "repos" in stats:
        frases.append("No hay archivos que volcar: "
                      + ("el repositorio está vacío o todo lo que tiene "
                         "está ignorado." if stats["repos"] == 1 else
                         f"los {_num(stats['repos'])} repositorios están "
                         "vacíos o todo lo que tienen está ignorado."))
    else:
        frases.append("No hay archivos que volcar: la carpeta está vacía o "
                      "todo lo que tiene está ignorado.")
    if stats.get("cut"):
        n = stats["cut"]
        frases.append(f"De los de texto, {_num(n)} "
                      + ("creció mientras se leía y está tal como era al "
                         "abrirlo." if n == 1 else
                         "crecieron mientras se leían y están tal como "
                         "eran al abrirlos."))
    if stats.get("redacted"):
        n = stats["redacted"]
        verbo = "Se tachó " if n == 1 else "Se tacharon "
        frases.append(
            verbo + _plural(n, "secreto", "secretos") + " en "
            + _plural(stats.get("redacted_files", 0) or 1, "archivo",
                      "archivos")
            + " («••••••••»): este archivo no "
            + ("lo contiene." if n == 1 else "los contiene."))
    if stats.get("ignored"):
        n = stats["ignored"]
        frases.append(_plural(n, "archivo ignorado", "archivos ignorados")
                      + " (por el .gitignore o el cuadro de ignorar) "
                      + ("no aparece." if n == 1 else "no aparecen."))
    frases.append(f"Ocupa {fmt_size(stats['bytes'])} y se generó "
                  f"{_duracion(stats['seconds'])}.")
    return ("#" * 80 + "\nRESUMEN\n" + "#" * 80 + "\n\n"
            + textwrap.fill(" ".join(frases), WIDTH) + "\n")
