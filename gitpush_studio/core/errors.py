# -*- coding: utf-8 -*-
"""Excepciones controladas del núcleo.

Todas llevan un mensaje pensado para mostrarse tal cual al usuario.
"""


class StepError(Exception):
    """Fallo esperado de una operación (git, red, permisos…)."""


class Cancelled(Exception):
    """El usuario detuvo la operación; no se ha publicado nada."""


class Blocked(Exception):
    """La publicación automática se detuvo por hallazgos de seguridad."""
