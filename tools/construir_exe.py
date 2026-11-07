# -*- coding: utf-8 -*-
"""Construye la versión instalable de Windows: «GitPush Studio.exe», que
funciona sin tener Python instalado.

    pip install customtkinter pyinstaller
    python tools/construir_exe.py              # carpeta con el .exe
    python tools/construir_exe.py --un-archivo # un único .exe

El resultado queda en «dist/GitPush Studio/» (o «dist/GitPush
Studio.exe»). La carpeta arranca más rápido; el archivo único es más
cómodo de mover, pero cada vez que se abre se descomprime en una carpeta
temporal (y algunos antivirus lo miran con lupa).

Git sigue siendo necesario en el equipo (https://git-scm.com): la app lo
usa para todo lo que tiene que ver con repositorios.

El flujo `.github/workflows/windows-exe.yml` hace esto mismo en GitHub
cada vez que publicas una etiqueta «v…» y adjunta el .exe a la release.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

RAIZ = Path(__file__).resolve().parent.parent
PAQUETE = RAIZ / "gitpush_studio"
NOMBRE = "GitPush Studio"
# Carpetas de datos del paquete que el .exe necesita (se buscan junto al
# código, con `Path(__file__)`).
DATOS = ("licencias", "informe", "assets")


def orden(un_archivo: bool = False) -> List[str]:
    """La orden de PyInstaller."""
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--windowed", "--name", NOMBRE,
            "--icon", str(PAQUETE / "assets" / "gitpush.ico"),
            "--collect-data", "customtkinter"]
    for carpeta in DATOS:
        origen = PAQUETE / carpeta
        if origen.is_dir():
            args += ["--add-data",
                     f"{origen}{os.pathsep}gitpush_studio/{carpeta}"]
    if un_archivo:
        args.append("--onefile")
    args.append(str(RAIZ / "GitPushStudio.pyw"))
    return args


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if os.name != "nt":
        print("Aviso: el .exe solo se puede construir en Windows (PyInstaller "
              "no hace ejecutables de otro sistema). Aquí saldría uno para "
              "este sistema.")
    args = orden("--un-archivo" in argv)
    print("$ " + " ".join(f'"{a}"' if " " in a else a for a in args))
    return subprocess.run(args, cwd=str(RAIZ)).returncode


if __name__ == "__main__":
    sys.exit(main())
