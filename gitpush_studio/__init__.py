# -*- coding: utf-8 -*-
"""
GitPush Studio
==============
Publica, clona y empaqueta tus proyectos de GitHub sin tocar la terminal.

Estructura
----------
    gitpush_studio/
        core/   lógica pura (sin tkinter): ignorado, recorrido, revisión,
                empaquetado, git, API de GitHub, secretos, configuración
                y el pipeline de publicación.
        ui/     interfaz customtkinter: theme, widgets, base (diálogos),
                jobs (controlador de publicación y cola), app, dialogs/.
        cli.py  uso sin interfaz: publicar, analizar y empaquetar.

Requisitos: Python 3.9+, Git en el PATH. La interfaz gráfica requiere
además `pip install customtkinter`; el núcleo y la CLI no.
"""

APP_NAME = "GitPush Studio"
APP_VERSION = "3.2.0"
