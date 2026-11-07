# -*- coding: utf-8 -*-
"""El informe de analíticas como una sola página HTML autocontenida.

La plantilla (`gitpush_studio/informe/plantilla.html`) lleva el diseño y
los gráficos; aquí solo se le inyectan los datos como JSON. La página no
puede cargar nada de fuera (lo impide su propia política de seguridad) y,
por si se comparte, no lleva la ruta completa de la carpeta ni los
correos de los autores: eso se queda en el JSON, que es para uno mismo.
"""

import json
from pathlib import Path

from .. import APP_VERSION
from .atomic import write_bytes, write_text

PLANTILLA = Path(__file__).resolve().parent.parent / "informe" / \
    "plantilla.html"
_MARCA_DATOS = "__DATOS__"
_MARCA_TITULO = "__TITULO__"


def html_data(rep):
    """Lo que viaja a la página: el informe sin datos personales."""
    d = rep.to_dict(include_files=False)
    d.pop("root", None)
    d["version"] = APP_VERSION
    d["generated_files"] = len(rep.generated_files())
    d["unreadable"] = len(rep.unreadable())
    git = d.get("git")
    if git:
        for a in git.get("authors", []):
            a.pop("email", None)
    return d


def safe_json(data):
    """JSON que no puede cerrar el <script> que lo contiene.

    Un archivo llamado «</script><script>…» (el nombre lo elige quien
    escribe el proyecto) acabaría ejecutándose al abrir el informe. `<`,
    `>` y `&` se escriben como escapes Unicode, válidos dentro de una
    cadena JSON e inofensivos para el HTML.
    """
    return (json.dumps(data, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _escape_attr(texto):
    return (texto.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render(rep, plantilla=None):
    """La página completa, como texto."""
    base = (plantilla if plantilla is not None
            else PLANTILLA.read_text(encoding="utf-8"))
    titulo = "Análisis de " + (rep.name or "proyecto")
    # Primero el título, que va en la plantilla; los datos al final y con
    # un solo corte: así ni un nombre de archivo que contenga
    # «__TITULO__» ni un título que contenga «__DATOS__» se tocan.
    base = base.replace(_MARCA_TITULO, _escape_attr(titulo))
    antes, sep, despues = base.partition(_MARCA_DATOS)
    if not sep:
        raise ValueError("La plantilla del informe no tiene el hueco de "
                         "los datos.")
    return antes + safe_json(html_data(rep)) + despues


def write_html(rep, path):
    """Escribe la página sin dejarla a medias si algo falla."""
    write_text(path, render(rep))
    return str(path)


def write_json(rep, path, include_files=True):
    """El informe completo en JSON (con rutas, correos y archivo a
    archivo): para analizarlo con otras herramientas."""
    texto = json.dumps(rep.to_dict(include_files=include_files),
                       ensure_ascii=False, indent=1)
    write_bytes(path, texto.encode("utf-8"))
    return str(path)
