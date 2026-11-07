# -*- coding: utf-8 -*-
"""Conexión con GitHub usando el token guardado.

Sirve para que git no consulte al gestor de credenciales, que es quien
abre la ventana «Select an account» en cada publicación. El token viaja
por variables de entorno (`GIT_CONFIG_*`), nunca en la línea de comandos
ni en `.git/config`, y si no es válido se vuelve a intentar sin él para
no dejar al usuario bloqueado.
"""

import base64
import subprocess
import unittest

from gitpush_studio.core.git import (auth_env, looks_like_auth_error,
                                     push_error_message)
from gitpush_studio.core.publish import (Callbacks, PublishContext,
                                         PublishJob, step_branch_and_remote,
                                         step_push)

TOKEN = "ghp_" + "a" * 36
URL = "https://github.com/ana/proyecto.git"


class FakeGit:
    """Registra cada llamada y devuelve resultados preparados."""

    def __init__(self, results=None):
        self.calls = []            # (args, env_extra)
        self.results = list(results or [])

    def run(self, args, cwd=None, check=True, timeout=None, env_extra=None,
            echo=True, register=None):
        self.calls.append((list(args), dict(env_extra or {})))
        if self.results:
            code, out = self.results.pop(0)
        else:
            code, out = 0, ""
        return subprocess.CompletedProcess(args, code, out, "")

    # lo que usan los pasos, sin tocar disco
    def ensure_remote(self, folder, url):
        self.calls.append((["remote", "set-url", "origin", url], {}))

    def current_branch(self, folder):
        return "main"

    def has_commits(self, folder):
        return True

    def local_branches(self, folder):
        return ["main"]


def context(token=TOKEN, results=None, **job_kw):
    job = PublishJob(folder="/tmp/x", url=URL, token=token, **job_kw)
    return PublishContext(job, Callbacks(), git=FakeGit(results))


def env_of(call):
    return call[1]


def slot(env):
    """Índice del hueco GIT_CONFIG_* que lleva la cabecera, o -1.

    No se puede dar por hecho que sea el 0: si el entorno ya trae
    `GIT_CONFIG_COUNT` (lo hacen algunos servidores de integración
    continua y contenedores), `auth_env` añade el suyo *después* para no
    pisar los que ya había. La prueba busca el hueco por su clave, igual
    que haría git.
    """
    for clave, valor in env.items():
        if (clave.startswith("GIT_CONFIG_KEY_")
                and valor.endswith(".extraheader")):
            return int(clave[len("GIT_CONFIG_KEY_"):])
    return -1


def header(env):
    n = slot(env)
    return "" if n < 0 else env.get("GIT_CONFIG_VALUE_%d" % n, "")


class AuthEnvTests(unittest.TestCase):

    def test_header_carries_the_token_without_showing_it(self):
        env = auth_env(URL, TOKEN, base_env={})
        self.assertEqual(env["GIT_CONFIG_KEY_0"],
                         "http.https://github.com/.extraheader")
        valor = env["GIT_CONFIG_VALUE_0"]
        self.assertNotIn(TOKEN, valor)
        usuario, _, clave = base64.b64decode(
            valor.split("basic ")[1]).decode().partition(":")
        self.assertEqual(usuario, "x-access-token")
        self.assertEqual(clave, TOKEN)

    def test_no_token_or_non_http_url_means_no_header(self):
        self.assertEqual(auth_env(URL, ""), {})
        self.assertEqual(auth_env("git@github.com:ana/p.git", TOKEN), {})
        self.assertEqual(auth_env("/ruta/local.git", TOKEN), {})

    def test_it_respects_an_existing_git_config_count(self):
        env = auth_env(URL, TOKEN, base_env={"GIT_CONFIG_COUNT": "3"})
        self.assertEqual(env["GIT_CONFIG_COUNT"], "4")
        self.assertIn("GIT_CONFIG_KEY_3", env)

    def test_it_never_overwrites_the_slots_already_in_use(self):
        """Si el entorno ya trae ajustes, la cabecera va en un hueco nuevo.

        Pisar el hueco 0 dejaría sin efecto un ajuste del usuario (un
        proxy, por ejemplo) y, peor, lo sustituiría por el token.
        """
        previo = {"GIT_CONFIG_COUNT": "2",
                  "GIT_CONFIG_KEY_0": "http.proxy",
                  "GIT_CONFIG_VALUE_0": "http://proxy:8080",
                  "GIT_CONFIG_KEY_1": "core.autocrlf",
                  "GIT_CONFIG_VALUE_1": "false"}
        env = auth_env(URL, TOKEN, base_env=previo)
        for clave in ("GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
                      "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1"):
            self.assertNotIn(clave, env)
        resultado = dict(previo)
        resultado.update(env)                  # como hace Git._env
        self.assertEqual(resultado["GIT_CONFIG_VALUE_0"], "http://proxy:8080")
        self.assertEqual(resultado["GIT_CONFIG_COUNT"], "3")
        self.assertEqual(slot(resultado), 2)
        self.assertIn("basic ", header(resultado))

    def test_auth_error_detection(self):
        for texto in ("fatal: authentication failed",
                      "could not read username for 'https://github.com'",
                      "remote: permission to ana/p.git denied",
                      "the requested url returned error: 403"):
            self.assertTrue(looks_like_auth_error(texto), texto)
        self.assertFalse(looks_like_auth_error("! [rejected] non-fast-forward"))


class PublishUsesTheTokenTests(unittest.TestCase):

    def test_fetch_and_push_carry_the_header(self):
        ctx = context()
        step_branch_and_remote(ctx)
        step_push(ctx)
        fetch = [c for c in ctx.git.calls if c[0][:1] == ["fetch"]]
        push = [c for c in ctx.git.calls if c[0][:1] == ["push"]]
        self.assertEqual(len(fetch), 1)
        self.assertEqual(len(push), 1)
        for call in fetch + push:
            self.assertIn("basic ", header(env_of(call)))

    def test_the_token_never_appears_in_the_command_line(self):
        ctx = context()
        step_branch_and_remote(ctx)
        step_push(ctx)
        for args, _ in ctx.git.calls:
            self.assertNotIn(TOKEN, " ".join(args))
            self.assertNotIn("extraheader", " ".join(args))
        # Tampoco se escribe en la configuración del repositorio.
        self.assertFalse([a for a, _ in ctx.git.calls
                          if a[:1] == ["config"] and "extraheader" in " ".join(a)])

    def test_without_token_nothing_changes(self):
        ctx = context(token="")
        step_branch_and_remote(ctx)
        step_push(ctx)
        for _, env in ctx.git.calls:
            self.assertEqual(env, {})

    def test_an_invalid_token_falls_back_to_the_credential_helper(self):
        # 1) fetch falla por credenciales, 2) fetch sin token va bien,
        # 3) push va bien.
        ctx = context(results=[(128, "fatal: Authentication failed"),
                               (0, ""), (0, "")])
        step_branch_and_remote(ctx)
        fetch = [c for c in ctx.git.calls if c[0][:1] == ["fetch"]]
        self.assertEqual(len(fetch), 2)
        self.assertIn("basic ", header(env_of(fetch[0])))
        self.assertEqual(env_of(fetch[1]), {})       # sin token: lo pide git
        self.assertTrue(ctx.remote_exists)
        self.assertEqual(ctx.auth, {})

    def test_push_retries_once_without_the_token(self):
        ctx = context(results=[(1, "remote: Permission denied"), (0, "")])
        ctx.remote_exists = True
        step_push(ctx)                               # no lanza
        push = [c for c in ctx.git.calls if c[0][:1] == ["push"]]
        self.assertEqual(len(push), 2)
        self.assertIn("basic ", header(env_of(push[0])))
        self.assertEqual(env_of(push[1]), {})

    def test_a_non_auth_failure_is_not_retried(self):
        ctx = context(results=[(1, "! [rejected] non-fast-forward")])
        ctx.remote_exists = True
        with self.assertRaises(Exception) as cm:
            step_push(ctx)
        self.assertIn("alguien más", str(cm.exception))
        self.assertEqual(len([c for c in ctx.git.calls
                              if c[0][:1] == ["push"]]), 1)

    def test_error_messages_never_leak_the_token(self):
        self.assertNotIn(TOKEN, push_error_message("authentication failed"))
        ctx = context(results=[(1, f"fatal: {TOKEN} rejected"), (1, "nope")])
        ctx.remote_exists = True
        with self.assertRaises(Exception) as cm:
            step_push(ctx)
        self.assertNotIn(TOKEN, str(cm.exception))


if __name__ == "__main__":
    unittest.main()


class MacKeychainNeverShowsTheToken(unittest.TestCase):
    """En macOS el token iba como ARGUMENTO de `security`: cualquier
    programa del equipo podía verlo en la lista de procesos mientras
    duraba la llamada. Ahora va por la entrada estándar."""

    def test_the_token_goes_through_stdin(self):
        from unittest import mock
        from gitpush_studio.core import secrets
        token = "ghp_" + "Ab1" * 12
        llamadas = []

        class Hecho:
            def __init__(self, out=""):
                self.returncode, self.stdout, self.stderr = 0, out, ""

        def falso_run(args, input=None, **kw):
            llamadas.append((list(args), input))
            if "find-generic-password" in args:
                return Hecho(token)
            return Hecho()

        with mock.patch.object(secrets.sys, "platform", "darwin"), \
                mock.patch.object(secrets.subprocess, "run", falso_run):
            entrada = secrets.protect_token(token)
        self.assertEqual(entrada, {"mode": "keychain"})
        for args, stdin in llamadas:
            self.assertFalse(any(token in a for a in args),
                             f"el token aparece en los argumentos: {args}")
        self.assertTrue(any(stdin and token in stdin for _, stdin in llamadas))
