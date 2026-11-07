# -*- coding: utf-8 -*-
"""Lanzador de GitPush Studio: doble clic para abrir la app.

Debe estar junto a la carpeta `gitpush_studio/`. La extensión .pyw
abre la interfaz sin ventana de consola en Windows.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gitpush_studio.__main__ import main  # noqa: E402

main()
