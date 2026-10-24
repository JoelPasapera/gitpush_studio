# -*- coding: utf-8 -*-
"""Banco de rendimiento: genera un proyecto sintético grande y mide las
operaciones que recorren TODO el proyecto.

    python tools/benchmark.py [--files 20000] [--repeat 3] [--keep]

Qué mide (la mejor de `--repeat` vueltas, en caché caliente):

  · recorrido de carpetas (sin git) con el .gitignore aplicado;
  · listado con git (`git ls-files`, lo que se publicaría);
  · revisión de secretos sobre todo lo que se publicaría;
  · TXT del proyecto entero;
  · analíticas (lenguajes, líneas, duplicados; sin historial);
  · copia de seguridad exacta (`safekeeping.snapshot_tree`) y la
    búsqueda de archivos en riesgo (`files_at_risk`).

El proyecto se genera con una semilla fija: dos ejecuciones miden lo
mismo. No forma parte de las pruebas (tarda y depende de la máquina);
sirve para comparar versiones y para encontrar cuellos de botella.
"""

import argparse
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from gitpush_studio.core import safekeeping                  # noqa: E402
from gitpush_studio.core.analytics import analyze_project    # noqa: E402
from gitpush_studio.core.git import Git                      # noqa: E402
from gitpush_studio.core.ignore import IgnoreMatcher         # noqa: E402
from gitpush_studio.core.pack import consolidate_project     # noqa: E402
from gitpush_studio.core.scan import scan_entries            # noqa: E402
from gitpush_studio.core.walk import collect_included        # noqa: E402

# Vocabulario de código corriente; las palabras que disparan las reglas
# de secretos («token», «key»…) aparecen con la frecuencia de un proyecto
# real, no en cada línea.
_PALABRAS = ("def", "return", "self", "value", "items", "for", "in", "if",
             "else", "import", "class", "data", "result", "config", "path",
             "user", "cache", "name", "index", "list", "dict", "open",
             "write", "read", "line", "text", "file", "parse", "build") * 8 \
    + ("token", "keys", "secret_name", "password_hint")


def _codigo(rnd, lineas):
    out = []
    for i in range(lineas):
        sangria = "    " * rnd.randint(0, 3)
        if rnd.random() < 0.15:
            out.append(f"{sangria}# {' '.join(rnd.choices(_PALABRAS, k=6))}")
        else:
            out.append(sangria + " ".join(rnd.choices(_PALABRAS, k=8)))
    return "\n".join(out) + "\n"


def generar(destino, archivos, semilla=20261016):
    """Árbol sintético: código, datos, binarios, ignorados y algún secreto."""
    rnd = random.Random(semilla)
    destino = Path(destino)
    (destino / ".gitignore").write_text("node_modules/\n*.log\nbuild/\n",
                                        encoding="utf-8")
    carpetas = [Path(*[f"m{rnd.randint(0, 40)}" for _ in range(
        rnd.randint(1, 4))]) for _ in range(max(1, archivos // 25))]
    exts = (".py",) * 6 + (".js",) * 4 + (".md", ".json", ".css", ".html",
                                         ".png", ".log")
    total = 0
    for i in range(archivos):
        carpeta = destino / rnd.choice(carpetas)
        carpeta.mkdir(parents=True, exist_ok=True)
        ext = rnd.choice(exts)
        ruta = carpeta / f"f{i}{ext}"
        if ext == ".png":
            datos = bytes(rnd.getrandbits(8) for _ in range(rnd.randint(
                200, 20000)))
        else:
            texto = _codigo(rnd, int(rnd.lognormvariate(3.5, 1.0)) + 1)
            if rnd.random() < 0.002:
                texto += 'API_TOKEN = "ghp_%s"\n' % "".join(rnd.choices(
                    "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
                    "23456789", k=36))
            datos = texto.encode("utf-8")
        ruta.write_bytes(datos)
        total += len(datos)
    for i in range(archivos // 10):                    # lo que se ignora
        nm = destino / "node_modules" / f"p{i % 50}" / f"i{i}.js"
        nm.parent.mkdir(parents=True, exist_ok=True)
        nm.write_text("module.exports = 1;\n", encoding="utf-8")
    return total


def medir(nombre, fn, repeticiones, resultados):
    mejor, valor = None, None
    for _ in range(repeticiones):
        t0 = time.perf_counter()
        valor = fn()
        dt = time.perf_counter() - t0
        mejor = dt if mejor is None else min(mejor, dt)
    resultados.append((nombre, mejor))
    print(f"  {nombre:<44} {mejor:8.3f} s")
    return valor


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--files", type=int, default=20000)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--keep", action="store_true",
                    help="no borrar el proyecto generado")
    a = ap.parse_args(argv)
    tmp = Path(tempfile.mkdtemp(prefix="gitpush-bench-"))
    proyecto = tmp / "proyecto"
    proyecto.mkdir()
    try:
        t0 = time.perf_counter()
        total = generar(proyecto, a.files)
        print(f"Proyecto sintético: {a.files} archivos, "
              f"{total / 1e6:.1f} MB ({time.perf_counter() - t0:.1f} s en "
              f"generarlo)\n")
        resultados = []
        entradas, _ = medir("recorrido sin git (.gitignore aplicado)",
                            lambda: collect_included(
                                proyecto, IgnoreMatcher(["node_modules/",
                                                         "*.log", "build/"]),
                                nested_gitignores=True),
                            a.repeat, resultados)
        hallazgos, _ = medir("revisión de secretos",
                             lambda: scan_entries(entradas), a.repeat,
                             resultados)
        print(f"    ({len(hallazgos)} hallazgos)")
        medir("TXT del proyecto entero",
              lambda: consolidate_project(proyecto, tmp / "salida.txt",
                                          use_git=False), a.repeat,
              resultados)
        medir("analíticas (sin historial)",
              lambda: analyze_project(proyecto, use_git=False,
                                      history=False), a.repeat, resultados)
        g = Git()
        subprocess.run(["git", "init", "-q", str(proyecto)], check=True)
        subprocess.run(["git", "-C", str(proyecto), "add", "-A"],
                       check=True)
        subprocess.run(["git", "-C", str(proyecto), "-c", "user.name=b",
                        "-c", "user.email=b@b", "-c", "commit.gpgsign=false",
                        "commit", "-qm", "base"], check=True)
        medir("listado con git (lo que se publicaría)",
              lambda: g.list_files(proyecto), a.repeat, resultados)
        medir("copia de seguridad exacta (todo el árbol)",
              lambda: safekeeping.snapshot_tree(g, proyecto), a.repeat,
              resultados)
        medir("archivos en riesgo frente a HEAD",
              lambda: safekeeping.files_at_risk(g, proyecto, "HEAD"),
              a.repeat, resultados)
        print()
        return 0
    finally:
        if a.keep:
            print(f"Proyecto conservado en {proyecto}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
