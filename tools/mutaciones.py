# -*- coding: utf-8 -*-
"""Pruebas de mutación: ¿las pruebas detectan de verdad los fallos?

    python tools/mutaciones.py                    # el lote de seguridad
    python tools/mutaciones.py otro-lote.json
    python tools/mutaciones.py --comprobar        # solo mira que cada
                                                  # mutación siga aplicando

Una prueba que pasa no demuestra nada si también pasaría con el código
roto. Aquí se rompe el código A PROPÓSITO, un fragmento cada vez (por
ejemplo: quitar la comprobación que impide borrar un archivo que cambió
después de la copia), y se ejecutan las pruebas indicadas:

  · si fallan, la mutación está «detectada»: esa protección tiene una
    prueba que la defiende;
  · si siguen pasando, «sobrevive»: esa parte del código podría romperse
    sin que nadie se enterara. Cada superviviente es una prueba que falta.

El lote es una lista JSON de objetos {"file", "old", "new", "desc",
"tests"}: `old` tiene que aparecer EXACTAMENTE una vez en `file` (si no,
se informa como «no aplica»: el código cambió y la mutación hay que
rehacerla). `tests` son los argumentos para `python -m unittest`
(empezar por "-f" para parar en el primer fallo: mucho más rápido).

El archivo mutado se restaura SIEMPRE: al terminar cada mutación, si el
proceso recibe Ctrl-C o una señal de terminación, y al salir. Se borran
las cachés de bytecode antes y después: con el mismo tamaño y la misma
fecha, Python reutilizaría el .pyc de la versión mutada.

Sale con código 1 si alguna mutación sobrevive o no aplica.
"""

import argparse
import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
LOTE = Path(__file__).resolve().parent / "mutaciones_seguridad.json"
PRUEBAS = ["discover", "-s", "tests", "-t", "."]

# Archivo mutado → su contenido original, para devolverlo pase lo que pase.
_PENDIENTE = {}


def _restaurar(*_a):
    for ruta, texto in list(_PENDIENTE.items()):
        Path(ruta).write_bytes(texto)
        _PENDIENTE.pop(ruta, None)


def _purgar_cache():
    for d in RAIZ.rglob("__pycache__"):
        shutil.rmtree(d, ignore_errors=True)


def _instalar_restauracion():
    atexit.register(_restaurar)
    for nombre in ("SIGTERM", "SIGINT", "SIGHUP"):
        sen = getattr(signal, nombre, None)
        if sen is not None:
            signal.signal(sen, lambda *a: (_restaurar(), sys.exit(130)))


def cargar(lote):
    datos = json.loads(Path(lote).read_text(encoding="utf-8"))
    if not isinstance(datos, list):
        raise SystemExit(f"{lote}: se esperaba una lista de mutaciones")
    return datos


def estado(m):
    """«ok» si la mutación se puede aplicar; si no, por qué."""
    ruta = RAIZ / m["file"]
    if not ruta.is_file():
        return "no existe el archivo"
    veces = ruta.read_text(encoding="utf-8").count(m["old"])
    if veces == 0:
        return "no aplica (el código cambió)"
    if veces > 1:
        return f"ambigua ({veces} apariciones)"
    return "ok"


def ejecutar(m, timeout):
    """True si las pruebas DETECTAN la mutación (fallan con ella)."""
    ruta = RAIZ / m["file"]
    original = ruta.read_bytes()
    texto = original.decode("utf-8")
    _PENDIENTE[str(ruta)] = original
    try:
        ruta.write_bytes(texto.replace(m["old"], m["new"]).encode("utf-8"))
        _purgar_cache()
        orden = [sys.executable, "-m", "unittest"] + (m.get("tests")
                                                      or PRUEBAS)
        entorno = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        try:
            p = subprocess.run(orden, cwd=str(RAIZ), capture_output=True,
                               timeout=timeout, env=entorno)
        except subprocess.TimeoutExpired:
            return True          # colgarse también es que algo se rompió
        return p.returncode != 0
    finally:
        ruta.write_bytes(original)
        _PENDIENTE.pop(str(ruta), None)
        _purgar_cache()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("lote", nargs="?", default=str(LOTE))
    ap.add_argument("--comprobar", action="store_true",
                    help="solo comprobar que cada mutación aplica")
    ap.add_argument("--timeout", type=int, default=900,
                    help="segundos máximos por mutación")
    a = ap.parse_args(argv)
    lote = cargar(a.lote)
    _instalar_restauracion()
    malas, vivas = [], []
    t0 = time.time()
    for i, m in enumerate(lote, 1):
        cabeza = f"[{i}/{len(lote)}]"
        motivo = estado(m)
        if motivo != "ok":
            print(f"{cabeza} ⚠ {motivo}: {m['desc']}", flush=True)
            malas.append(m["desc"])
            continue
        if a.comprobar:
            print(f"{cabeza} aplica: {m['desc']}", flush=True)
            continue
        if ejecutar(m, a.timeout):
            print(f"{cabeza} ✔ detectada: {m['desc']}", flush=True)
        else:
            print(f"{cabeza} ✖ SOBREVIVE: {m['desc']}", flush=True)
            vivas.append(m["desc"])
    if a.comprobar:
        print(f"\n{len(lote) - len(malas)}/{len(lote)} aplican")
        return 1 if malas else 0
    detectadas = len(lote) - len(malas) - len(vivas)
    print(f"\n{detectadas}/{len(lote)} detectadas en "
          f"{(time.time() - t0) / 60:.1f} min")
    for d in vivas:
        print(f"  sobrevive: {d}")
    for d in malas:
        print(f"  no aplica: {d}")
    return 1 if (vivas or malas) else 0


if __name__ == "__main__":
    sys.exit(main())
