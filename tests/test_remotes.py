# -*- coding: utf-8 -*-
"""Publicar en el repositorio equivocado.

Una carpeta conectada a un repositorio (su «origin») y un formulario que
dice OTRO: antes, publicar cambiaba el «origin» sin decir nada y subía el
proyecto allí (en modo espejo, ese repositorio quedaba igual que la
carpeta). Estas pruebas exigen que:

  · sin una decisión explícita no se publique NADA en ninguno de los dos
    y la carpeta no cambie ni un byte (ni la licencia, ni el TXT);
  · «el de la carpeta» publique allí sin tocar el otro, y «el del
    formulario» publique en el otro y mueva el «origin»;
  · la auto-publicación nunca publique en otro repositorio;
  · las formas distintas de escribir el MISMO repositorio no pregunten;
  · un token metido en la URL no aparezca nunca en lo que se enseña.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from tests.gitenv import HAVE_GIT, git, init_bare, init_repo
from tests.test_safety import snapshot

from gitpush_studio.core import release, remotes, sync, urls
from gitpush_studio.core.git import Git
from gitpush_studio.core.publish import Callbacks, PublishJob, publish


def write(folder, rel, content="x\n"):
    path = Path(folder) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def token_like(prefix="ghp_"):
    """Algo con forma de token, armado al ejecutar (este archivo no lleva
    ninguno escrito)."""
    return prefix + "".join(chr(ord("a") + (i * 7) % 26) for i in range(36))


class RepositoryKeyTests(unittest.TestCase):

    def test_the_same_repository_written_in_different_ways(self):
        formas = ["https://github.com/Ana/App.git",
                  "https://github.com/ana/app",
                  "https://github.com/ana/app/",
                  "git@github.com:ana/app.git",
                  "git@github.com:Ana/App",
                  "ssh://git@github.com/ana/app.git",
                  "ssh://git@github.com:22/ana/app/",
                  f"https://ana:{token_like()}@github.com/ana/app.git",
                  "  https://github.com/ana/app.git  "]
        for forma in formas:
            self.assertEqual(urls.repository_key(forma), "github.com/ana/app",
                             forma)
            self.assertTrue(urls.same_repository(forma, formas[0]), forma)

    def test_different_repositories(self):
        self.assertFalse(urls.same_repository("https://github.com/ana/app",
                                              "https://github.com/ana/otra"))
        self.assertFalse(urls.same_repository("https://github.com/ana/app",
                                              "https://github.com/luis/app"))
        self.assertFalse(urls.same_repository("https://github.com/ana/app",
                                              "https://gitlab.com/ana/app"))
        self.assertFalse(urls.same_repository("", ""))
        self.assertEqual(urls.repository_key(""), "")

    def test_local_repositories(self):
        with tempfile.TemporaryDirectory() as tmp:
            bare = Path(tmp) / "remoto.git"
            bare.mkdir()
            proyecto = Path(tmp) / "proyecto"
            proyecto.mkdir()
            self.assertTrue(urls.same_repository(str(bare),
                                                 "file://" + bare.as_posix()))
            # Una ruta relativa del «origin» se resuelve desde la carpeta.
            self.assertTrue(urls.same_repository("../remoto.git", str(bare),
                                                 base_a=str(proyecto)))
            self.assertFalse(urls.same_repository(str(bare),
                                                  str(Path(tmp) / "otro.git")))
            self.assertTrue(urls.repository_key(str(bare)).startswith(
                "local:"))

    def test_credentials_never_reach_what_is_shown(self):
        token = token_like()
        url = f"https://x-access-token:{token}@github.com/ana/app.git"
        self.assertEqual(urls.without_credentials(url),
                         "https://github.com/ana/app.git")
        self.assertEqual(urls.repo_web_url(url), "https://github.com/ana/app")
        self.assertEqual(urls.readable_url(url), "https://github.com/ana/app")
        conflicto = remotes.RemoteConflict("/x", url,
                                           "https://github.com/ana/otra")
        self.assertNotIn(token, conflicto.explanation())
        self.assertIn("github.com/ana/app", conflicto.explanation())
        self.assertIn("github.com/ana/otra", conflicto.explanation())


class ConfiguredOriginTests(unittest.TestCase):
    """Leer el «origin» de .git/config sin lanzar git (para proponer el
    nombre del repositorio en «Publicar varias carpetas»)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def config(self, text):
        write(self.tmp / ".git", "config", text)
        return remotes.configured_origin(str(self.tmp))

    def test_reads_only_the_origin(self):
        self.assertEqual(self.config(
            "[core]\n\tbare = false\n"
            "[remote \"upstream\"]\n\turl = https://github.com/otro/x.git\n"
            "[Remote \"origin\"]  ; comentario\n"
            "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
            "\tURL = \"https://github.com/ana/mi web.git\" # comentario\n"),
            "https://github.com/ana/mi web.git")
        self.assertEqual(self.config("[remote.origin]\nurl = git@h:a/b\n"),
                         "git@h:a/b")
        self.assertEqual(self.config("[remote \"Origin\"]\nurl = x\n"), "")
        self.assertEqual(self.config("[remote \"origin\"]\nfetch = y\n"), "")
        self.assertEqual(remotes.configured_origin(str(self.tmp / "no")), "")

    def test_the_batch_suggests_the_repository_it_already_publishes_to(self):
        from gitpush_studio.core.batch import suggest_for_folder
        carpeta = self.tmp / "web final"
        write(carpeta / ".git", "config",
              "[remote \"origin\"]\n\turl = https://github.com/ana/mi-web.git\n")
        self.assertEqual(suggest_for_folder(str(carpeta)), "mi-web")
        sin_remoto = self.tmp / "Cálculo Numérico"
        sin_remoto.mkdir()
        self.assertEqual(suggest_for_folder(str(sin_remoto)),
                         "Calculo-Numerico")
        otro_servidor = self.tmp / "otro"
        write(otro_servidor / ".git", "config",
              "[remote \"origin\"]\n\turl = /srv/repos/proyecto.git\n")
        self.assertEqual(suggest_for_folder(str(otro_servidor)), "otro")


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class GuardCase(unittest.TestCase):
    """Una carpeta ya publicada en A, y otro repositorio B vacío."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.proj = Path(self.tmp) / "proyecto"
        write(self.proj, "a.txt", "A\n")
        self.a = init_bare(self.tmp, "a")
        self.b = init_bare(self.tmp, "b")
        primera = publish(self.job(self.a, preview=False))
        self.assertEqual(primera.status, "ok", primera.message)
        write(self.proj, "a.txt", "A cambiado\n")
        self.logs = []
        self.asked = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def job(self, url, **kw):
        base = dict(folder=str(self.proj), url=url, user="Test",
                    email="t@x.y", check=False, preview=False)
        base.update(kw)
        return PublishJob(**base)

    def callbacks(self, answer=None):
        def ask_remote(conflict):
            self.asked.append(conflict)
            if answer is None:
                raise AssertionError("no debía preguntar")
            return answer
        return Callbacks(log=lambda text, tag="out": self.logs.append(text),
                         ask_remote=ask_remote)

    def origin(self):
        return Git().remote_url(str(self.proj))

    def tips(self):
        return (git(self.a, "rev-parse", "--verify", "-q", "main",
                    check=False).strip(),
                git(self.b, "rev-parse", "--verify", "-q", "main",
                    check=False).strip())


class FindConflictTests(GuardCase):

    def test_detects_only_another_repository(self):
        self.assertIsNone(remotes.find_conflict(str(self.proj), self.a))
        self.assertIsNone(remotes.find_conflict(
            str(self.proj), "file://" + Path(self.a).as_posix()))
        conflicto = remotes.find_conflict(str(self.proj), self.b)
        self.assertIsNotNone(conflicto)
        self.assertEqual(conflicto.current, self.a)
        self.assertEqual(conflicto.wanted, self.b)
        self.assertIsNone(remotes.find_conflict(str(self.proj), ""))

    def test_a_folder_without_remote_or_inside_another_repo(self):
        nueva = Path(self.tmp) / "nueva"
        write(nueva, "x.txt")
        self.assertIsNone(remotes.find_conflict(str(nueva), self.b))
        # Una subcarpeta del proyecto no es «la carpeta del repositorio»:
        # publicarla crea uno nuevo dentro, sin remoto.
        sub = self.proj / "sub"
        write(sub, "y.txt")
        self.assertIsNone(remotes.find_conflict(str(sub), self.b))


class PublishGuardTests(GuardCase):

    def test_without_a_decision_nothing_is_published_nor_written(self):
        antes = snapshot(self.proj)
        puntas = self.tips()
        # Con licencia y TXT pedidos: si la comprobación llegara tarde,
        # la carpeta ya tendría un LICENSE o un TXT nuevos.
        r = publish(self.job(self.b, license="mit", license_owner="Ana",
                             consolidate=True))
        self.assertEqual(r.status, "cancelled", r.message)
        self.assertEqual(snapshot(self.proj), antes)
        self.assertEqual(self.tips(), puntas)
        self.assertEqual(self.origin(), self.a)
        self.assertIn("ya publica en", r.message)

    def test_cancel_answer(self):
        puntas = self.tips()
        r = publish(self.job(self.b), self.callbacks(remotes.CANCEL))
        self.assertEqual(r.status, "cancelled")
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(self.tips(), puntas)
        self.assertEqual(self.origin(), self.a)

    def test_publish_in_the_folders_repository(self):
        antes_b = self.tips()[1]
        r = publish(self.job(self.b), self.callbacks(remotes.USE_FOLDER))
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(r.job.url, self.a)
        self.assertEqual(self.origin(), self.a)
        self.assertEqual(self.tips()[1], antes_b)          # B, intacto
        self.assertEqual(git(self.a, "show", "main:a.txt"), "A cambiado\n")

    def test_publish_in_the_forms_repository(self):
        antes_a = self.tips()[0]
        r = publish(self.job(self.b), self.callbacks(remotes.USE_FORM))
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(self.origin(), self.b)
        self.assertEqual(self.tips()[0], antes_a)          # A, intacto
        self.assertEqual(git(self.b, "show", "main:a.txt"), "A cambiado\n")
        # La siguiente ya no pregunta: la carpeta publica en B.
        write(self.proj, "a.txt", "otra vez\n")
        self.asked.clear()
        r = publish(self.job(self.b), self.callbacks(None))
        self.assertEqual(r.status, "ok", r.message)

    def test_auto_publish_never_goes_to_another_repository(self):
        puntas = self.tips()
        r = publish(self.job(self.b, auto=True),
                    self.callbacks(remotes.USE_FORM))
        self.assertEqual(r.status, "blocked", r.message)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.tips(), puntas)
        self.assertEqual(self.origin(), self.a)

    def test_the_same_repository_written_differently_does_not_ask(self):
        r = publish(self.job("file://" + Path(self.a).as_posix()),
                    self.callbacks(None))
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(self.asked, [])

    def test_a_token_in_the_origin_never_shows(self):
        token = token_like()
        con_token = f"https://x-access-token:{token}@github.com/ana/app.git"
        git(self.proj, "remote", "set-url", "origin", con_token)
        r = publish(self.job(self.b), self.callbacks(remotes.CANCEL))
        self.assertEqual(r.status, "cancelled")
        self.assertEqual(self.asked[0].current_web,
                         "https://github.com/ana/app")
        todo = "\n".join(self.logs) + r.message
        self.assertNotIn(token, todo)
        self.assertIn("github.com/ana/app", todo)
        self.assertEqual(self.origin(), con_token)          # sin tocar


class CommandLineTests(GuardCase):

    def cli(self, *extra):
        import contextlib
        import io
        from gitpush_studio import cli
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            rc = cli.main(["publish", "--folder", str(self.proj), "--url",
                           self.b, "--user", "T", "--email", "t@x.y",
                           "--yes", "--no-check", "--no-preview",
                           "--token", "", *extra])
        return rc, salida.getvalue()

    def test_yes_alone_never_switches_the_repository(self):
        puntas = self.tips()
        rc, salida = self.cli()
        self.assertNotEqual(rc, 0)
        self.assertIn("--switch-remote", salida)
        self.assertEqual(self.tips(), puntas)
        self.assertEqual(self.origin(), self.a)

    def test_switch_remote_publishes_in_the_new_one(self):
        rc, salida = self.cli("--switch-remote")
        self.assertEqual(rc, 0, salida)
        self.assertEqual(self.origin(), self.b)
        self.assertEqual(git(self.b, "show", "main:a.txt"), "A cambiado\n")


class EnsureRemoteTests(GuardCase):

    def test_the_form_without_credentials_keeps_the_origins_access(self):
        token = token_like()
        con_token = f"https://ana:{token}@github.com/ana/app.git"
        git(self.proj, "remote", "set-url", "origin", con_token)
        Git().ensure_remote(str(self.proj), "https://github.com/ana/app.git")
        self.assertEqual(self.origin(), con_token)
        # Otra URL (u otro token) sí se escribe.
        Git().ensure_remote(str(self.proj), "https://github.com/ana/otra.git")
        self.assertEqual(self.origin(), "https://github.com/ana/otra.git")


class ReleaseAndSyncTests(GuardCase):

    def setUp(self):
        super().setUp()
        git(self.proj, "checkout", "--", "a.txt")          # sin cambios

    def test_a_release_is_never_created_in_another_repository(self):
        r = release.create_release(str(self.proj), self.b, "ana", "b",
                                   token="x", tag="v1.0.0",
                                   attach_zip=False)
        self.assertEqual(r.status, "error")
        self.assertIn("ya publica en", r.message)
        self.assertEqual(self.origin(), self.a)
        self.assertEqual(git(self.proj, "tag", "--list").strip(), "")

    def test_bringing_changes_from_another_repository_warns(self):
        otro = Path(self.tmp) / "otro"
        init_repo(otro)
        write(otro, "b.txt", "B\n")
        git(otro, "add", "-A")
        git(otro, "commit", "-q", "-m", "b")
        git(otro, "push", "-q", self.b, "HEAD:refs/heads/main")
        rep = sync.inspect(str(self.proj), self.b)
        self.assertEqual(rep.other_remote, self.a)
        self.assertEqual(sync.inspect(str(self.proj), self.a).other_remote,
                         "")


if __name__ == "__main__":
    unittest.main()
