# -*- coding: utf-8 -*-
"""`python -m gitpush_studio` abre la interfaz gráfica; la CLI vive en
`python -m gitpush_studio.cli`."""

import sys


def _missing_customtkinter():
    msg = ("Falta la librería 'customtkinter'.\n\n"
           "Instálala con este comando y vuelve a abrir la app:\n\n"
           "    pip install customtkinter")
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("GitPush Studio", msg)
    except Exception:                                       # noqa: BLE001
        print(msg, file=sys.stderr)
    sys.exit(1)


def main():
    try:
        import customtkinter                                # noqa: F401
    except ImportError:
        _missing_customtkinter()
    from .ui.app import main as ui_main
    ui_main()


if __name__ == "__main__":
    main()
