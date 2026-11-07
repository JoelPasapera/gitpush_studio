# -*- coding: utf-8 -*-
"""El cuadro «Archivos y carpetas a ignorar» deja las cosas fuera de
GitHub SIN escribirlas en el .gitignore.

Antes cada línea del cuadro acababa en el .gitignore, y el .gitignore se
publica: en GitHub se leían los nombres de lo que no querías enseñar
(«PROJECT_CONTEXT.md», «ARCHITECTURE.md», «notas/»…). Ahora van a la
lista privada de git (`.git/info/exclude`), que nunca se sube. Aquí se
exige que:

  · ni los archivos ni sus NOMBRES lleguen a GitHub, en ningún commit;
  · tu .gitignore no se toque (byte a byte), y lo que versiones
    anteriores escribieron en él del cuadro se quite de ahí;
  · lo que ya estaba fuera siga fuera aunque se publique con el cuadro
    vacío o con el de otro proyecto (la lista solo crece), y que volver a
    publicarlo sea una decisión explícita;
  · un «!» de tu .gitignore no pueda colar lo que el cuadro excluye;
  · git (estado, auto-publicación) y el TXT respeten la lista sin
    pasarles el cuadro;
  · la lista viva donde git la busca también en un worktree, y nunca se
    escriba a través de un enlace;
  · lo que hay en `.git/info/exclude` fuera del bloque quede intacto.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.gitenv import HAVE_GIT, git, init_bare, init_repo

from gitpush_studio.core import exclude
from gitpush_studio.core.exclude import (END, START, Private, app_txt_lines,
                                         exclude_path, merged, read_private,
                                         split_block, write_private)
from gitpush_studio.core.git import Git
from gitpush_studio.core.ignore import remove_from_gitignore
from gitpush_studio.core.pack import consolidate_project
from gitpush_studio.core.publish import Callbacks, PublishJob, publish

POSIX = os.name == "posix"
NOMBRES = ("PROJECT_CONTEXT.md", "ARCHITECTURE.md", "docs", "notas")


def write(folder, rel, content="x\n"):
    p = Path(folder) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode("utf-8")
    p.write_bytes(content)
    return p


def remote_files(bare, branch="main"):
    p = subprocess.run(["git", "ls-tree", "-r", "--name-only", branch],
                       cwd=bare, capture_output=True, text=True)
    return sorted(x for x in p.stdout.splitlines() if x)


def history_text(bare):
    """Todo lo que hay en GitHub, en TODOS los commits: rutas y
    contenido."""
    p = subprocess.run(["git", "log", "--all", "-p", "--name-status",
                        "--format=%H %s"], cwd=bare, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return p.stdout


def job_for(folder, bare, **kw):
    base = dict(folder=str(folder), url=bare, user="T", email="t@x.y",
                preview=False, check=False)
    base.update(kw)
    return PublishJob(**base)


class TheBlockInGitsPrivateList(unittest.TestCase):

    def test_round_trip_keeps_everything_outside_the_block(self):
        fuera = ("# git ls-files --others --exclude-from=.git/info/exclude\r\n"
                 "# Lines that start with '#' are comments.\r\n"
                 "mio.tmp\r\n")
        texto = (fuera + START + "\r\n" + exclude.USER + "\r\n"
                 + "docs\r\nnotas/\r\n" + exclude.AUTO + "\r\n"
                 + "/p.txt\r\n" + END + "\r\nfinal\r\n")
        antes, privada, despues = split_block(texto)
        self.assertEqual(antes, fuera)
        self.assertEqual(despues, "final\r\n")
        self.assertEqual(privada, Private(("docs", "notas/"), ("/p.txt",)))

    def test_a_cut_block_and_repeated_blocks_are_understood(self):
        cortado = START + "\na\nb\n"
        self.assertEqual(split_block(cortado)[1].user, ("a", "b"))
        doble = (START + "\na\n" + END + "\nmedio\n" + START + "\nb\na\n"
                 + END + "\n")
        antes, privada, despues = split_block(doble)
        self.assertEqual(privada.user, ("a", "b"))
        self.assertEqual(antes + despues, "medio\n")

    def test_merging_only_grows_unless_asked(self):
        viejo = Private(("docs", "bin"), ("/p.txt",))
        self.assertEqual(merged(viejo, user=()), viejo)          # cuadro vacío
        self.assertEqual(merged(viejo, user=["x"]).user, ("docs", "bin", "x"))
        self.assertEqual(merged(viejo, release=["docs"]).user, ("bin",))
        # Lo que vuelve a estar en el cuadro no se libera.
        self.assertEqual(merged(viejo, user=["docs"],
                                release=["docs"]).user, ("docs", "bin"))
        # Lo de la app nunca se libera desde el cuadro.
        self.assertEqual(merged(viejo, release=["/p.txt"]).auto, ("/p.txt",))

    def test_only_the_app_txt_pairs_are_recognised(self):
        lineas = ["*.log", "/proyecto.txt", "/.proyecto.txt.*.tmp",
                  "/sub/salida.txt", "/sub/.salida.txt.*.tmp", "/suelto.txt"]
        self.assertEqual(app_txt_lines(lineas),
                         ["/proyecto.txt", "/.proyecto.txt.*.tmp",
                          "/sub/salida.txt", "/sub/.salida.txt.*.tmp"])
        self.assertEqual(app_txt_lines(["/.huerfano.txt.*.tmp"]), [])


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class WritingTheList(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        init_repo(self.repo)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_it_lives_inside_dot_git_and_keeps_what_was_there(self):
        ruta = self.repo / ".git" / "info" / "exclude"
        ruta.parent.mkdir(parents=True, exist_ok=True)
        propio = b"# mis cosas\r\nlocal.cfg\r\n\xff\xfe-no-utf8\r\n"
        ruta.write_bytes(propio)
        self.assertTrue(write_private(self.repo, Private(("docs",), ())))
        self.assertTrue(ruta.read_bytes().startswith(propio))
        self.assertEqual(read_private(self.repo).user, ("docs",))
        # Reescribir lo mismo no toca el archivo.
        self.assertFalse(write_private(self.repo, Private(("docs",), ())))
        # Vaciar la lista quita el bloque y deja lo tuyo como estaba.
        write_private(self.repo, Private((), ()))
        self.assertEqual(ruta.read_bytes(), propio)

    def test_a_missing_info_folder_is_created(self):
        shutil.rmtree(self.repo / ".git" / "info", ignore_errors=True)
        write_private(self.repo, Private(("docs",), ()))
        self.assertEqual(read_private(self.repo).user, ("docs",))

    def test_git_itself_obeys_it(self):
        write(self.repo, "docs/a.md")
        write(self.repo, "ok.txt")
        write_private(self.repo, Private(("docs",), ()))
        p = subprocess.run(["git", "status", "--porcelain",
                            "--untracked-files=all"], cwd=self.repo,
                           capture_output=True, text=True)
        self.assertNotIn("docs", p.stdout)
        self.assertIn("ok.txt", p.stdout)

    @unittest.skipUnless(POSIX, "enlaces de POSIX")
    def test_it_is_never_written_through_a_link(self):
        ajeno = self.tmp / "ajeno.txt"
        ajeno.write_text("no me toques\n")
        ruta = self.repo / ".git" / "info" / "exclude"
        ruta.parent.mkdir(parents=True, exist_ok=True)
        if ruta.exists():
            ruta.unlink()
        os.symlink(str(ajeno), str(ruta))
        with self.assertRaises(Exception) as cm:
            write_private(self.repo, Private(("docs",), ()))
        self.assertIn("enlace", str(cm.exception))
        self.assertEqual(ajeno.read_text(), "no me toques\n")

    def test_in_a_worktree_it_is_the_shared_list(self):
        write(self.repo, "a.txt")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        wt = self.tmp / "wt"
        git(self.repo, "worktree", "add", "-q", str(wt))
        self.assertTrue((wt / ".git").is_file())
        ruta = exclude_path(wt, Git())
        self.assertEqual(os.path.realpath(str(ruta)), os.path.realpath(
            str(self.repo / ".git" / "info" / "exclude")))
        self.assertIsNone(exclude_path(wt))             # sin git, no adivina
        self.assertIsNone(exclude_path(self.tmp))       # no es repositorio


class RemovingLinesFromYourGitignore(unittest.TestCase):

    def test_only_exact_lines_go_and_the_rest_is_byte_identical(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        gi = tmp / ".gitignore"
        gi.write_bytes(b"\xef\xbb\xbf# mio\r\n*.log\r\ndocs\r\n  notas  \r\n"
                       b"docs/\r\n!keep.log\r\ndocs\r\nfinal")
        self.assertEqual(remove_from_gitignore(tmp, ["docs", "notas", "x"]),
                         ["docs", "notas"])
        self.assertEqual(gi.read_bytes(),
                         b"\xef\xbb\xbf# mio\r\n*.log\r\ndocs/\r\n"
                         b"!keep.log\r\nfinal")
        self.assertEqual(remove_from_gitignore(tmp, ["docs"]), [])

    def test_the_first_line_after_the_bom_is_a_line_like_any_other(self):
        """Los editores de Windows ponen una marca BOM delante: la primera
        línea tiene que reconocerse igual, y la marca, quedarse."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        gi = tmp / ".gitignore"
        gi.write_bytes(b"\xef\xbb\xbfPROJECT_CONTEXT.md\r\n*.log\r\n")
        self.assertEqual(remove_from_gitignore(tmp, ["PROJECT_CONTEXT.md"]),
                         ["PROJECT_CONTEXT.md"])
        self.assertEqual(gi.read_bytes(), b"\xef\xbb\xbf*.log\r\n")

    @unittest.skipUnless(POSIX, "enlaces de POSIX")
    def test_a_linked_gitignore_is_left_alone(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        ajeno = tmp / "ajeno"
        ajeno.write_text("docs\n")
        os.symlink(str(ajeno), str(tmp / ".gitignore"))
        self.assertEqual(remove_from_gitignore(tmp, ["docs"]), [])
        self.assertEqual(ajeno.read_text(), "docs\n")


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class NamesNeverReachGitHub(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.proj = self.tmp / "proyecto"
        write(self.proj, "main.py", "print('hola')\n")
        write(self.proj, "PROJECT_CONTEXT.md", "contexto privado\n")
        write(self.proj, "ARCHITECTURE.md", "arquitectura\n")
        write(self.proj, "docs/guia.md", "guía\n")
        write(self.proj, "notas/idea.txt", "idea\n")
        self.bare = init_bare(self.tmp, "remoto")
        self.logs = []
        self.cb = Callbacks(log=lambda t, tag="out": self.logs.append(t))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def publicar(self, **kw):
        r = publish(job_for(self.proj, self.bare, **kw), self.cb)
        self.assertEqual(r.status, "ok", r.message)
        return r

    def assert_hidden(self):
        self.assertEqual(remote_files(self.bare), ["main.py"])
        historia = history_text(self.bare)
        for nombre in ("PROJECT_CONTEXT", "ARCHITECTURE", "docs", "notas"):
            self.assertNotIn(nombre, historia,
                             f"«{nombre}» aparece en GitHub")

    def test_neither_the_files_nor_their_names_are_published(self):
        self.publicar(ignore=["PROJECT_CONTEXT.md", "ARCHITECTURE.md",
                              "docs", "notas/"])
        self.assert_hidden()
        self.assertFalse((self.proj / ".gitignore").exists())
        self.assertEqual(read_private(self.proj).user,
                         ("PROJECT_CONTEXT.md", "ARCHITECTURE.md", "docs",
                          "notas/"))
        # Siguen en tu carpeta, intactos.
        self.assertEqual((self.proj / "PROJECT_CONTEXT.md").read_text(
            encoding="utf-8"), "contexto privado\n")

    def test_what_the_old_version_wrote_in_gitignore_is_moved_out(self):
        """Proyecto publicado con una versión anterior: el .gitignore
        lleva tus reglas y, al final, las líneas del cuadro y las del TXT.
        Esas se van de ahí (y de GitHub en la siguiente publicación); las
        tuyas se quedan exactamente igual."""
        tuyas = b"# mis reglas\r\n*.log\r\n"
        de_la_app = (b"PROJECT_CONTEXT.md\r\nARCHITECTURE.md\r\ndocs\r\n"
                     b"notas/\r\n/proyecto.txt\r\n/.proyecto.txt.*.tmp\r\n")
        write(self.proj, ".gitignore", tuyas + de_la_app)
        r = publish(job_for(self.proj, self.bare), self.cb)    # versión vieja
        self.assertEqual(r.status, "ok", r.message)
        self.assertIn("PROJECT_CONTEXT",
                      subprocess.run(["git", "show", "main:.gitignore"],
                                     cwd=self.bare, capture_output=True,
                                     text=True).stdout)
        self.publicar(ignore=["PROJECT_CONTEXT.md", "ARCHITECTURE.md",
                              "docs", "notas/"])
        self.assertEqual((self.proj / ".gitignore").read_bytes(), tuyas)
        actual = subprocess.run(["git", "show", "main:.gitignore"],
                                cwd=self.bare, capture_output=True,
                                text=True).stdout
        self.assertEqual(actual.replace("\r", ""), "# mis reglas\n*.log\n")
        privada = read_private(self.proj)
        self.assertEqual(privada.auto, ("/proyecto.txt",
                                        "/.proyecto.txt.*.tmp"))
        self.assertEqual(remote_files(self.bare), [".gitignore", "main.py"])
        self.assertTrue(any("Quitado del .gitignore" in t
                            for t in self.logs), self.logs)

    def test_a_gitignore_with_only_box_lines_ends_up_empty(self):
        """Ni se borra (tu archivo sigue ahí) ni conserva un solo nombre."""
        write(self.proj, ".gitignore", "PROJECT_CONTEXT.md\nARCHITECTURE.md"
                                       "\ndocs\nnotas/\n")
        self.publicar(ignore=["PROJECT_CONTEXT.md", "ARCHITECTURE.md",
                              "docs", "notas/"])
        self.assertEqual((self.proj / ".gitignore").read_bytes(), b"")
        self.assertEqual(subprocess.run(
            ["git", "show", "main:.gitignore"], cwd=self.bare,
            capture_output=True, text=True).stdout, "")
        historia = history_text(self.bare)
        for nombre in ("PROJECT_CONTEXT", "ARCHITECTURE", "docs", "notas"):
            self.assertNotIn(nombre, historia)

    def test_publishing_with_an_empty_box_keeps_them_out(self):
        """Abrir la carpeta sin su perfil (cuadro vacío) o publicar con el
        cuadro de otro proyecto no puede volver a subir nada."""
        self.publicar(ignore=["PROJECT_CONTEXT.md", "ARCHITECTURE.md",
                              "docs", "notas/"])
        write(self.proj, "main.py", "print('adiós')\n")
        self.logs.clear()
        self.publicar(ignore=[])
        self.assert_hidden()
        self.assertTrue(any("Siguen fuera aunque no estén en el cuadro" in t
                            for t in self.logs), self.logs)

    def test_publishing_it_again_is_an_explicit_release(self):
        self.publicar(ignore=["docs", "PROJECT_CONTEXT.md",
                              "ARCHITECTURE.md", "notas/"])
        self.logs.clear()
        self.publicar(ignore=["PROJECT_CONTEXT.md", "ARCHITECTURE.md",
                              "notas/"], release=["docs"])
        self.assertEqual(remote_files(self.bare), ["docs/guia.md",
                                                   "main.py"])
        self.assertNotIn("docs", read_private(self.proj).user)
        self.assertTrue(any("Vuelve a publicarse" in t for t in self.logs))

    def test_a_negation_in_your_gitignore_cannot_sneak_it_in(self):
        """Antes las líneas del cuadro iban al final del .gitignore y
        ganaban a un «!» anterior; ahora la lista privada pesa menos que
        el .gitignore para git, así que se comprueba aparte."""
        write(self.proj, ".gitignore", "!importante.log\n")
        write(self.proj, "logs/.gitignore", "!keep.log\n")
        write(self.proj, "importante.log", "1\n")
        write(self.proj, "logs/keep.log", "2\n")
        write(self.proj, "logs/otro.log", "3\n")
        self.publicar(ignore=["*.log", "PROJECT_CONTEXT.md",
                              "ARCHITECTURE.md", "docs", "notas/"])
        subido = remote_files(self.bare)
        for log in ("importante.log", "logs/keep.log", "logs/otro.log"):
            self.assertNotIn(log, subido)
        self.assertTrue(any("vuelve a incluir" in t for t in self.logs),
                        self.logs)
        # Y siguen en la carpeta.
        self.assertTrue((self.proj / "logs" / "keep.log").is_file())

    def test_git_status_and_the_txt_obey_it_without_the_box(self):
        self.publicar(ignore=["PROJECT_CONTEXT.md", "ARCHITECTURE.md",
                              "docs", "notas/"])
        write(self.proj, "docs/nueva.md", "nueva\n")
        # La auto-publicación pregunta a git: no ve cambios que publicar.
        self.assertEqual(Git().pending_changes(self.proj), [])
        out = self.tmp / "proyecto.txt"
        consolidate_project(self.proj, out)
        texto = out.read_text(encoding="utf-8")
        self.assertIn("MODULO: main.py", texto)
        for nombre in NOMBRES:
            self.assertNotIn("MODULO: " + nombre, texto)

    @unittest.skipUnless(POSIX, "gancho de git ejecutable")
    def test_the_list_survives_a_failed_publication(self):
        """GitHub rechaza la subida al final: lo que se dejó fuera sigue
        apuntado, y la próxima vez tampoco se sube."""
        gancho = Path(self.bare) / "hooks" / "pre-receive"
        gancho.write_text("#!/bin/sh\necho rechazado >&2\nexit 1\n")
        gancho.chmod(0o755)
        r = publish(job_for(self.proj, self.bare, ignore=["docs"]), self.cb)
        self.assertNotEqual(r.status, "ok")
        self.assertIn("docs", read_private(self.proj).user)
        gancho.unlink()
        self.publicar(ignore=[])
        self.assertNotIn("docs/guia.md", remote_files(self.bare))

    def test_the_command_line_can_publish_it_again(self):
        import contextlib
        import io
        from gitpush_studio import cli
        base = ["publish", "--folder", str(self.proj), "--url", self.bare,
                "--user", "T", "--email", "t@x.y", "--yes", "--no-check",
                "--no-preview", "--token", ""]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(base + ["--ignore", "docs"]), 0)
            self.assertNotIn("docs/guia.md", remote_files(self.bare))
            self.assertEqual(cli.main(base), 0)                 # sigue fuera
            self.assertNotIn("docs/guia.md", remote_files(self.bare))
            self.assertEqual(cli.main(base + ["--unignore", "docs"]), 0)
        self.assertIn("docs/guia.md", remote_files(self.bare))
        self.assertFalse((self.proj / ".gitignore").exists())

    @unittest.skipUnless(POSIX, "enlaces de POSIX")
    def test_a_linked_private_list_stops_the_publication(self):
        init_repo(self.proj)
        ajeno = self.tmp / "ajeno"
        ajeno.write_text("de otro\n")
        ruta = self.proj / ".git" / "info" / "exclude"
        ruta.parent.mkdir(parents=True, exist_ok=True)
        if ruta.exists():
            ruta.unlink()
        os.symlink(str(ajeno), str(ruta))
        r = publish(job_for(self.proj, self.bare, ignore=["docs"]), self.cb)
        self.assertEqual(r.status, "error")
        self.assertIn("enlace", r.message)
        self.assertEqual(ajeno.read_text(), "de otro\n")
        self.assertEqual(remote_files(self.bare), [])


if __name__ == "__main__":
    unittest.main()
