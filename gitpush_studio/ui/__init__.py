# -*- coding: utf-8 -*-
"""Interfaz customtkinter.

    theme.py     paleta y fuentes
    widgets.py   fábricas de widgets con el estilo de la app
    base.py      BaseDialog · DecisionDialog · WorkerDialog
    jobs.py      PublishController (uno o varios proyectos, decisiones en
                 serie) y QueuePanel (una fila por proyecto)
    app.py       ventana principal
    dialogs/     un diálogo por archivo

Regla única: los hilos solo encolan mensajes; los widgets se tocan en el
hilo de la interfaz. Toda la lógica vive en core/.
"""
