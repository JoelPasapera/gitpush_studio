# -*- coding: utf-8 -*-
"""Copias de seguridad y operaciones que pisan archivos: traer cambios,
restaurar una versión, recuperar un respaldo.

Invariantes que se defienden aquí:

  A. La copia es EXACTA: cada byte de cada archivo, también con finales
     de línea mezclados, filtros de git activados, binarios y nombres
     que no son UTF-8.
  B. La copia nunca forma parte de lo que se publica: no mueve la rama,
     no toca el índice, no ejecuta hooks y no necesita firmar.
  C. Nada que una operación pise o borre se pierde: o estaba en un
     commit, o está en la copia (también los archivos ignorados).
  D. Antes de borrar se comprueba que lo que hay en el disco es lo que
     quedó guardado; si cambió, no se borra.
  E. Traer cambios en modo combinar NO hace commit de lo tuyo.
  F. Lo peligroso se rechaza antes de tocar nada: ramas que parecen
     opciones de git, operaciones a medias, conflictos sin resolver,
     HEAD suelto, y GitHub cambiado desde la comprobación.
"""

import hashlib
import os
import random
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.gitenv import HAVE_GIT, clone, git, init_bare, init_repo

from gitpush_studio.core import history, integrate, safekeeping, sync
from gitpush_studio.core.errors import StepError
from gitpush_studio.core.git import Git
from gitpush_studio.core.gitobj import blob_id, file_blob_id
from gitpush_studio.core.publish import PublishJob, publish
from gitpush_studio.core.refnames import branch_problem

POSIX = os.name == "posix"


def write(folder, rel, content=b"x\n"):
    p = Path(folder) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode("utf-8")
    p.write_bytes(content)
    return p


def snapshot(folder):
    """{ruta: sha256} de toda la carpeta salvo .git (sin seguir enlaces)."""
    root = os.fspath(folder)
    out = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            full = os.path.join(base, f)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if os.path.islink(full):
                out[rel] = "link:" + os.readlink(full)
            else:
                with open(full, "rb") as fh:
                    out[rel] = hashlib.sha256(fh.read()).hexdigest()
    return out


def show(folder, ref, rel):
    """Bytes de `rel` dentro de `ref`, o None si no está."""
    p = subprocess.run(["git", "cat-file", "blob", f"{ref}:{rel}"],
                       cwd=str(folder), capture_output=True)
    return p.stdout if p.returncode == 0 else None


def rev(folder, ref="HEAD"):
    return git(folder, "rev-parse", ref).strip()


def is_ancestor(folder, a, b):
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                          cwd=str(folder)).returncode == 0


def commit(folder, msg="c"):
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "--no-gpg-sign", "-m", msg)
    return rev(folder)


def firma_que_falla(folder):
    """Exige firmar cada commit con un programa que siempre falla (sea
    cual sea el formato de firma configurado en el equipo)."""
    git(folder, "config", "commit.gpgsign", "true")
    git(folder, "config", "gpg.format", "openpgp")
    git(folder, "config", "gpg.program", "false")
    git(folder, "config", "gpg.ssh.program", "false")


def backup_branches(folder):
    return [b for b in Git().local_branches(folder)
            if b.startswith(safekeeping.BACKUP_PREFIX)]


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class BackupIsExactAndInvisible(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = Path(init_repo(self.tmp / "r"))
        write(self.repo, "a.txt", "A\n")
        self.head = commit(self.repo, "primera")
        self.g = Git()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_every_byte_is_kept_even_with_line_ending_filters(self):
        git(self.repo, "config", "core.autocrlf", "true")
        write(self.repo, ".gitattributes", "* text=auto\n")
        contenidos = {
            "crlf.txt": b"uno\r\ndos\r\n",
            "mixto.txt": b"a\nb\r\nc\rd",
            "binario.dat": bytes(range(256)) * 3,
            "a.txt": b"A modificado\r\n",
            "vacio.txt": b"",
        }
        for rel, data in contenidos.items():
            write(self.repo, rel, data)
        rama = self.g.backup(self.repo)
        for rel, data in contenidos.items():
            self.assertEqual(show(self.repo, rama, rel), data,
                             f"«{rel}» no se guardó byte a byte")

    def test_saves_untracked_and_endangered_ignored_files_only(self):
        write(self.repo, ".gitignore", "*.env\nbuild/\n")
        write(self.repo, "nuevo.txt", "N\n")
        write(self.repo, "claves.env", "CLAVE=1\n")
        write(self.repo, "build/salida.bin", b"\x00\x01")
        rama = self.g.backup(self.repo, at_risk=["claves.env"])
        self.assertEqual(show(self.repo, rama, "nuevo.txt"), b"N\n")
        self.assertEqual(show(self.repo, rama, "claves.env"), b"CLAVE=1\n")
        # Lo ignorado que nadie va a tocar no entra en la copia.
        self.assertIsNone(show(self.repo, rama, "build/salida.bin"))

    def test_backup_moves_nothing_and_is_not_part_of_the_branch(self):
        write(self.repo, "a.txt", "cambio sin guardar\n")
        write(self.repo, "nuevo.txt", "N\n")
        git(self.repo, "add", "nuevo.txt")             # algo en el índice
        indice = (self.repo / ".git" / "index").read_bytes()
        estado = git(self.repo, "status", "--porcelain")
        foto = snapshot(self.repo)
        rama = self.g.backup(self.repo)
        self.assertEqual(rev(self.repo), self.head)
        self.assertEqual(self.g.current_branch(self.repo), "main")
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), indice)
        self.assertEqual(git(self.repo, "status", "--porcelain"), estado)
        self.assertEqual(snapshot(self.repo), foto)
        self.assertFalse(is_ancestor(self.repo, rama, "HEAD"),
                         "el respaldo quedó dentro de la rama publicada")

    def test_no_hooks_run_and_no_signature_is_needed(self):
        hooks = self.repo / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        marca = self.tmp / "marca"
        for nombre in ("pre-commit", "commit-msg", "post-commit",
                       "post-checkout", "prepare-commit-msg"):
            h = hooks / nombre
            h.write_text(f"#!/bin/sh\necho {nombre} >> '{marca}'\nexit 1\n")
            h.chmod(0o755)
        # Firmar siempre, con un programa de firma que falla.
        firma_que_falla(self.repo)
        write(self.repo, "a.txt", "cambio\n")
        rama = self.g.backup(self.repo)
        self.assertTrue(rama)
        self.assertEqual(show(self.repo, rama, "a.txt"), b"cambio\n")
        self.assertFalse(marca.exists(), "se ejecutó un hook al respaldar")

    def test_a_submodule_pointer_is_part_of_the_backup(self):
        """La copia es exacta también en lo que no son archivos: el
        commit al que apunta un submódulo (su contenido es de él)."""
        sub = Path(init_repo(self.repo / "lib"))
        write(sub, "x.txt", "X\n")
        interno = commit(sub, "interno")
        git(self.repo, "add", "lib")
        commit(self.repo, "con submódulo")
        write(self.repo, "a.txt", "cambiado\n")      # algo que guardar
        rama = self.g.backup(self.repo)
        linea = git(self.repo, "ls-tree", rama, "lib").split()
        self.assertEqual(linea[:3], ["160000", "commit", interno])

    def test_a_clean_folder_is_backed_up_by_pointing_at_head(self):
        rama = self.g.backup(self.repo)
        self.assertEqual(rev(self.repo, rama), self.head)

    def test_two_backups_in_the_same_minute_never_collide(self):
        primera = self.g.backup(self.repo)
        write(self.repo, "x.txt", "X\n")
        segunda = self.g.backup(self.repo)
        self.assertNotEqual(primera, segunda)
        self.assertEqual(len(backup_branches(self.repo)), 2)

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "nombres no UTF-8: solo en sistemas que los admiten")
    def test_three_backups_in_the_same_minute_never_collide(self):
        ramas = []
        with mock.patch.object(safekeeping.time, "strftime",
                               return_value="2026-01-01-0000"):
            for i in range(3):
                write(self.repo, "a.txt", f"cambio {i}\n")
                ramas.append(self.g.backup(self.repo))
        self.assertEqual(ramas, ["respaldo-2026-01-01-0000",
                                 "respaldo-2026-01-01-0000-2",
                                 "respaldo-2026-01-01-0000-3"])

    def test_names_that_are_not_utf8_are_saved(self):
        raiz = os.fsencode(self.repo)
        nombre = b"caf\xe9 \xff.txt"
        with open(os.path.join(raiz, nombre), "wb") as fh:
            fh.write(b"LATIN-1\n")
        rama = self.g.backup(self.repo)
        p = subprocess.run(["git", "ls-tree", "-r", "-z", "--name-only",
                            rama], cwd=str(self.repo), capture_output=True)
        self.assertIn(nombre, p.stdout.split(b"\0"))
        datos = subprocess.run([b"git", b"cat-file", b"blob",
                                rama.encode() + b":" + nombre],
                               cwd=str(self.repo), capture_output=True).stdout
        self.assertEqual(datos, b"LATIN-1\n")

    def test_a_file_git_cannot_read_stops_everything(self):
        """Lo mismo sin depender de permisos (como root, «chmod 0» no
        impide leer): git falla con UN archivo del lote, se repite uno a
        uno para saber cuál, y se dice."""
        write(self.repo, "otro.txt", "O\n")
        write(self.repo, "privado.txt", "secreto\n")
        real = Git.run

        def run(gself, args, *a, **kw):
            if args[:1] == ["hash-object"] and (
                    "--stdin-paths" in args or "privado.txt" in args):
                return subprocess.CompletedProcess(
                    args, 128, b"", b"fatal: could not open")
            return real(gself, args, *a, **kw)
        with mock.patch.object(Git, "run", run):
            with self.assertRaises(StepError) as cm:
                self.g.backup(self.repo)
        self.assertIn("privado.txt", str(cm.exception))
        self.assertEqual(backup_branches(self.repo), [])

    @unittest.skipUnless(POSIX and hasattr(os, "geteuid")
                         and os.geteuid() != 0,
                         "hace falta un usuario sin privilegios")
    def test_an_unreadable_file_stops_everything(self):
        p = write(self.repo, "privado.txt", "secreto\n")
        p.chmod(0)
        try:
            with self.assertRaises(StepError) as cm:
                self.g.backup(self.repo)
            self.assertIn("privado.txt", str(cm.exception))
            self.assertEqual(backup_branches(self.repo), [])
        finally:
            p.chmod(0o644)


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class OnlyWhatIsSavedIsDeleted(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = Path(init_repo(self.tmp / "r"))
        write(self.repo, "a.txt", "A\n")
        commit(self.repo)
        self.g = Git()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_file_that_changed_after_the_backup_is_not_deleted(self):
        write(self.repo, "igual.txt", "I\n")
        write(self.repo, "cambia.txt", "antes\n")
        rama = self.g.backup(self.repo)
        write(self.repo, "cambia.txt", "después\n")
        write(self.repo, "despues.txt", "nuevo\n")      # no está en la copia
        borradas, conservadas = safekeeping.remove_verified(
            self.g, self.repo, rama, ["igual.txt", "cambia.txt",
                                      "despues.txt"])
        self.assertEqual(borradas, ["igual.txt"])
        self.assertEqual(sorted(conservadas), ["cambia.txt", "despues.txt"])
        self.assertEqual((self.repo / "cambia.txt").read_text(), "después\n")

    def test_emptied_folders_go_away_but_never_the_project(self):
        write(self.repo, "d/e/f.txt", "F\n")
        rama = self.g.backup(self.repo)
        safekeeping.remove_verified(self.g, self.repo, rama, ["d/e/f.txt"])
        self.assertFalse((self.repo / "d").exists())
        self.assertTrue(self.repo.is_dir())
        self.assertTrue((self.repo / ".git").is_dir())

    def test_without_a_backup_nothing_is_deleted(self):
        """Ni siquiera lo que es idéntico a la última versión: sin copia
        explícita no hay nada contra lo que comprobar."""
        write(self.repo, "x.txt", "X\n")
        borradas, conservadas = safekeeping.remove_verified(
            self.g, self.repo, "", ["x.txt", "a.txt"])
        self.assertEqual((borradas, conservadas), ([], ["x.txt", "a.txt"]))
        self.assertTrue((self.repo / "x.txt").exists())
        self.assertTrue((self.repo / "a.txt").exists())


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class SyncKeepsEverything(unittest.TestCase):
    """«pc1» publica, «pc2» trae los cambios."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bare = init_bare(self.tmp, "remote")
        self.pc1 = self.tmp / "pc1"
        write(self.pc1, "a.txt", "".join(f"línea {i}\n" for i in range(1, 11)))
        write(self.pc1, "b.txt", "B\n")
        self.publish_pc1()
        self.pc2 = Path(clone(self.bare, self.tmp / "pc2"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def publish_pc1(self, **kw):
        r = publish(PublishJob(folder=str(self.pc1), url=self.bare, user="T",
                               email="t@x.y", preview=False, check=False,
                               **kw))
        self.assertEqual(r.status, "ok", r.message)

    def remote_head(self):
        return git(self.bare, "rev-parse", "main").strip()

    def test_combine_joins_lines_and_leaves_your_work_uncommitted(self):
        a = (self.pc1 / "a.txt").read_text(encoding="utf-8")
        write(self.pc1, "a.txt", a.replace("línea 9\n", "línea 9 GITHUB\n"))
        self.publish_pc1()
        local = (self.pc2 / "a.txt").read_text(encoding="utf-8")
        write(self.pc2, "a.txt", local.replace("línea 1\n", "línea 1 MÍA\n"))
        r = sync.apply(self.pc2, self.bare, mode="merge")
        self.assertEqual(r.status, "ok", r.message)
        texto = (self.pc2 / "a.txt").read_text(encoding="utf-8")
        self.assertIn("línea 1 MÍA\n", texto)
        self.assertIn("línea 9 GITHUB\n", texto)
        # La rama es exactamente GitHub; lo tuyo sigue sin commit.
        self.assertEqual(rev(self.pc2), self.remote_head())
        diff = git(self.pc2, "diff", "HEAD")
        self.assertIn("+línea 1 MÍA", diff)
        self.assertNotIn("GITHUB", diff)

    def test_diverged_histories_are_combined_without_touching_your_work(self):
        write(self.pc2, "local.txt", "commit local sin publicar\n")
        commit(self.pc2, "local")
        mio = self.pc2 / "borrador.md"
        mio.write_text("sin guardar\n", encoding="utf-8")
        write(self.pc1, "c.txt", "C de GitHub\n")
        self.publish_pc1()
        for metodo in ("merge-tree", "copia temporal"):
            with self.subTest(metodo=metodo):
                if metodo == "copia temporal":
                    git(self.pc2, "reset", "-q", "--keep", "HEAD^1")
                    self.assertEqual(len(git(self.pc2, "rev-list",
                                             "--parents", "-n1", "HEAD")
                                         .split()), 2)
                    parche = mock.patch.object(integrate, "merge_tree",
                                               return_value=None)
                else:
                    parche = mock.patch.object(integrate, "merge_tree",
                                               wraps=integrate.merge_tree)
                with parche:
                    r = sync.apply(self.pc2, self.bare, mode="merge",
                                   identity=("t", "t@x"))
                self.assertEqual(r.status, "ok", r.message)
                padres = git(self.pc2, "rev-list", "--parents", "-n1",
                             "HEAD").split()
                self.assertEqual(len(padres), 3, "no es una combinación")
                self.assertEqual((self.pc2 / "c.txt").read_text(),
                                 "C de GitHub\n")
                self.assertEqual((self.pc2 / "local.txt").read_text(),
                                 "commit local sin publicar\n")
                self.assertEqual(mio.read_text(encoding="utf-8"),
                                 "sin guardar\n")
                self.assertIn("?? borrador.md",
                              git(self.pc2, "status", "--porcelain"))
                self.assertFalse(is_ancestor(self.pc2, r.backup_branch,
                                             "HEAD"))

    def test_an_ignored_file_that_github_also_has_is_saved_exactly(self):
        (self.pc2 / ".git" / "info" / "exclude").write_text("config.env\n")
        write(self.pc2, "config.env", b"MIS=CLAVES\r\n")
        write(self.pc1, "config.env", "DE=GITHUB\n")
        self.publish_pc1()
        rep = sync.inspect(self.pc2, self.bare)
        self.assertIn("config.env", rep.overwritten)
        r = sync.apply(self.pc2, self.bare, mode="merge",
                       expected=rep.remote_sha)
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual((self.pc2 / "config.env").read_bytes(),
                         b"DE=GITHUB\n")
        self.assertEqual(show(self.pc2, r.backup_branch, "config.env"),
                         b"MIS=CLAVES\r\n")

    def test_mirror_keeps_ignored_files_nobody_needs(self):
        (self.pc2 / ".git" / "info" / "exclude").write_text("*.log\n")
        write(self.pc2, "notas.log", "mías\n")
        write(self.pc2, "extra.txt", "E\n")
        write(self.pc1, "a.txt", "nuevo\n")
        self.publish_pc1()
        r = sync.apply(self.pc2, self.bare, mode="mirror")
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual((self.pc2 / "notas.log").read_text(), "mías\n")
        self.assertFalse((self.pc2 / "extra.txt").exists())
        self.assertEqual(show(self.pc2, r.backup_branch, "extra.txt"),
                         b"E\n")
        self.assertEqual(rev(self.pc2), self.remote_head())

    def test_the_backup_never_reaches_github(self):
        write(self.pc2, "borrador.txt", "no publicar todavía\n")
        write(self.pc1, "a.txt", "nuevo\n")
        self.publish_pc1()
        r = sync.apply(self.pc2, self.bare, mode="merge")
        self.assertTrue(r.backup_branch)
        respaldo = rev(self.pc2, r.backup_branch)
        # Publicar después: ni la rama ni su commit pueden llegar.
        r2 = publish(PublishJob(folder=str(self.pc2), url=self.bare,
                                user="T", email="t@x.y", preview=False,
                                check=False, ignore=["borrador.txt"]))
        self.assertEqual(r2.status, "ok", r2.message)
        refs = git(self.bare, "for-each-ref", "--format=%(refname)")
        self.assertNotIn("respaldo", refs)
        tiene = subprocess.run(["git", "cat-file", "-e", respaldo],
                               cwd=self.bare).returncode == 0
        self.assertFalse(tiene, "el commit de respaldo llegó a GitHub")
        self.assertNotIn("borrador.txt", git(self.bare, "ls-tree", "-r",
                                             "--name-only", "main"))

    def test_github_changing_after_the_check_stops_everything(self):
        write(self.pc1, "a.txt", "v2\n")
        self.publish_pc1()
        rep = sync.inspect(self.pc2, self.bare)
        write(self.pc1, "a.txt", "v3\n")
        self.publish_pc1()                          # alguien publica más
        write(self.pc2, "mio.txt", "M\n")
        foto = snapshot(self.pc2)
        r = sync.apply(self.pc2, self.bare, mode="mirror",
                       expected=rep.remote_sha)
        self.assertEqual(r.status, "error")
        self.assertIn("Comprobar", r.message)
        self.assertEqual(snapshot(self.pc2), foto)
        self.assertEqual(backup_branches(self.pc2), [])

    def test_a_branch_that_looks_like_an_option_never_reaches_git(self):
        marca = self.tmp / "EJECUTADO"
        rama = f"--upload-pack=touch {marca}"
        with self.assertRaises(StepError):
            sync.inspect(self.pc2, self.bare, branch=rama)
        r = sync.apply(self.pc2, self.bare, branch=rama)
        self.assertEqual(r.status, "error")
        self.assertFalse(marca.exists())
        for mala in ("a:b", "x..y", "-f", "con espacio", "rama.lock", "@"):
            self.assertTrue(branch_problem(mala), mala)

    def test_half_finished_git_operations_are_left_alone(self):
        foto = snapshot(self.pc2)
        merge_head = self.pc2 / ".git" / "MERGE_HEAD"
        merge_head.write_text(rev(self.pc2) + "\n")
        for modo in ("merge", "mirror"):
            r = sync.apply(self.pc2, self.bare, mode=modo)
            self.assertEqual(r.status, "error")
            self.assertIn("a medias", r.message)
        r = history.restore_version(self.pc2, rev(self.pc2))
        self.assertEqual(r.status, "error")
        merge_head.unlink()
        self.assertEqual(snapshot(self.pc2), foto)

    def test_every_half_finished_operation_is_left_alone(self):
        foto = snapshot(self.pc2)
        gd = self.pc2 / ".git"
        # La lista va escrita aquí, no se lee del código: si alguien
        # quitara una de la comprobación, esta prueba tiene que fallar.
        for nombre, es_carpeta in (("MERGE_HEAD", False),
                                   ("CHERRY_PICK_HEAD", False),
                                   ("REVERT_HEAD", False),
                                   ("rebase-merge", True),
                                   ("rebase-apply", True),
                                   ("BISECT_LOG", False)):
            marca = gd / nombre
            if es_carpeta:
                marca.mkdir()
            else:
                marca.write_text(rev(self.pc2) + "\n")
            try:
                for modo in ("merge", "mirror"):
                    r = sync.apply(self.pc2, self.bare, mode=modo)
                    self.assertEqual(r.status, "error", nombre)
                    self.assertIn("a medias" if "BISECT" not in nombre
                                  else "bisect", r.message)
                r = history.restore_version(self.pc2, rev(self.pc2))
                self.assertEqual(r.status, "error", nombre)
            finally:
                if es_carpeta:
                    shutil.rmtree(marca)
                else:
                    marca.unlink()
        self.assertEqual(snapshot(self.pc2), foto)

    def test_deleting_what_github_changed_is_a_conflict(self):
        write(self.pc1, "b.txt", "B de GitHub\n")
        self.publish_pc1()
        (self.pc2 / "b.txt").unlink()
        foto = snapshot(self.pc2)
        rep = sync.inspect(self.pc2, self.bare)
        self.assertIn(("b.txt", "lo borraste tú y en GitHub cambió"),
                      rep.conflicts)
        r = sync.apply(self.pc2, self.bare, mode="merge")
        self.assertEqual(r.status, "error")
        self.assertEqual(snapshot(self.pc2), foto)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "sin tuberías con nombre")
    def test_something_that_is_not_a_file_is_never_replaced(self):
        write(self.pc1, "canal", "de GitHub\n")
        self.publish_pc1()
        os.mkfifo(str(self.pc2 / "canal"))
        rep = sync.inspect(self.pc2, self.bare)
        self.assertIn("canal", [ruta for ruta, _ in rep.conflicts])
        r = sync.apply(self.pc2, self.bare, mode="merge")
        self.assertEqual(r.status, "error")
        self.assertTrue(stat.S_ISFIFO(os.lstat(self.pc2 / "canal").st_mode))

    def test_a_file_where_github_needs_a_folder_is_a_conflict(self):
        write(self.pc1, "d/x.txt", "X\n")
        self.publish_pc1()
        write(self.pc2, "d", "mi archivo d\n")
        foto = snapshot(self.pc2)
        rep = sync.inspect(self.pc2, self.bare)
        self.assertIn("d/x.txt", [ruta for ruta, _ in rep.conflicts])
        r = sync.apply(self.pc2, self.bare, mode="merge")
        self.assertEqual(r.status, "error")
        self.assertEqual(snapshot(self.pc2), foto)

    def test_mirror_saves_an_ignored_file_it_overwrites(self):
        (self.pc2 / ".git" / "info" / "exclude").write_text("config.env\n")
        write(self.pc2, "config.env", b"MIS=CLAVES\r\n")
        write(self.pc1, "config.env", "DE=GITHUB\n")
        self.publish_pc1()
        r = sync.apply(self.pc2, self.bare, mode="mirror")
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual((self.pc2 / "config.env").read_bytes(),
                         b"DE=GITHUB\n")
        self.assertEqual(show(self.pc2, r.backup_branch, "config.env"),
                         b"MIS=CLAVES\r\n")

    def test_unresolved_conflicts_are_left_alone(self):
        sha = git(self.pc2, "rev-parse", "HEAD:b.txt").strip()
        info = "".join(f"100644 {sha} {n}\tb.txt\n" for n in (1, 2, 3))
        subprocess.run(["git", "update-index", "--index-info"],
                       cwd=str(self.pc2), input="0 " + "0" * 40 + "\tb.txt\n"
                       + info, text=True, check=True)
        r = sync.apply(self.pc2, self.bare, mode="mirror")
        self.assertEqual(r.status, "error")
        self.assertIn("conflictos", r.message)

    def test_a_detached_head_is_left_alone(self):
        git(self.pc2, "checkout", "-q", "--detach")
        r = sync.apply(self.pc2, self.bare, mode="mirror")
        self.assertEqual(r.status, "error")
        self.assertIn("HEAD suelto", r.message)


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class RestoringKeepsEverything(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = Path(init_repo(self.tmp / "r"))
        write(self.repo, "a.txt", "v1\n")
        self.v1 = commit(self.repo, "v1")
        write(self.repo, "a.txt", "v2\n")
        write(self.repo, "c.txt", "C\n")
        self.v2 = commit(self.repo, "v2")
        (self.repo / ".git" / "info" / "exclude").write_text("*.env\n")
        self.g = Git()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_recovering_a_backup_brings_files_back_without_committing(self):
        write(self.repo, "a.txt", b"mi version\r\n")
        write(self.repo, "nuevo.txt", "N\n")
        write(self.repo, "claves.env", "CLAVE=1\n")
        rama = self.g.backup(self.repo, at_risk=["claves.env"])
        # Después todo cambia…
        write(self.repo, "a.txt", "otra cosa\n")
        (self.repo / "nuevo.txt").unlink()
        write(self.repo, "claves.env", "CLAVE=2\n")
        write(self.repo, "posterior.txt", "P\n")
        r = history.restore(self.repo, rama)
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual((self.repo / "a.txt").read_bytes(), b"mi version\r\n")
        self.assertEqual((self.repo / "nuevo.txt").read_text(), "N\n")
        self.assertEqual((self.repo / "claves.env").read_text(), "CLAVE=1\n")
        self.assertFalse((self.repo / "posterior.txt").exists())
        # …sin commit: la rama sigue donde estaba, lo ignorado sigue
        # ignorado y nada queda preparado para publicarse a escondidas.
        self.assertEqual(rev(self.repo), self.v2)
        estado = git(self.repo, "status", "--porcelain")
        self.assertNotIn("claves.env", estado)
        self.assertEqual(git(self.repo, "diff", "--cached"), "")
        # Lo que había justo antes también está a salvo.
        self.assertEqual(show(self.repo, r.backup_branch, "posterior.txt"),
                         b"P\n")
        self.assertEqual(show(self.repo, r.backup_branch, "claves.env"),
                         b"CLAVE=2\n")

    def test_recovering_a_backup_clears_a_file_where_a_folder_goes(self):
        write(self.repo, "d/x.txt", "X\n")
        rama = self.g.backup(self.repo)
        shutil.rmtree(self.repo / "d")
        write(self.repo, "d", "ahora soy un archivo\n")
        # Ignorado: no sale entre lo «no rastreado»; solo estorba.
        with open(self.repo / ".git" / "info" / "exclude", "a") as fh:
            fh.write("/d\n")
        r = history.restore(self.repo, rama)
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual((self.repo / "d" / "x.txt").read_text(), "X\n")
        # Lo que estorbaba está en la copia de justo antes.
        self.assertEqual(show(self.repo, r.backup_branch, "d"),
                         b"ahora soy un archivo\n")

    def test_restore_prepares_its_commit_before_touching_anything(self):
        write(self.repo, "a.txt", "sin guardar\n")
        foto = snapshot(self.repo)
        firma_que_falla(self.repo)
        r = history.restore_version(self.repo, self.v1)
        self.assertEqual(r.status, "error")
        self.assertEqual(snapshot(self.repo), foto)
        self.assertEqual(rev(self.repo), self.v2)
        self.assertEqual(backup_branches(self.repo), [])

    def test_restore_saves_ignored_files_it_overwrites(self):
        # Una versión antigua que SÍ tenía el archivo que hoy ignoras.
        write(self.repo, "viejo.env", "PUBLICADO=1\n")
        git(self.repo, "add", "-f", "viejo.env")
        con_env = commit(self.repo, "con env")
        git(self.repo, "rm", "-q", "--cached", "viejo.env")
        commit(self.repo, "sin env")
        write(self.repo, "viejo.env", "MIO=SECRETO\n")
        r = history.restore_version(self.repo, con_env)
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(show(self.repo, r.backup_branch, "viejo.env"),
                         b"MIO=SECRETO\n")


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class ChangesDuringTheOperationAreNeverLost(unittest.TestCase):
    """Un editor que guarda justo DESPUÉS de la copia de seguridad: su
    cambio no estaría en ninguna parte y la operación lo pisaría. Se
    para sin tocar nada, y sin falsas alarmas por lo que no se toca."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bare = init_bare(self.tmp, "remote")
        self.pc1 = self.tmp / "pc1"
        for nombre in ("a.txt", "b.txt", "c.txt"):
            write(self.pc1, nombre, nombre + " v1\n")
        self.publish_pc1()
        self.pc2 = Path(clone(self.bare, self.tmp / "pc2"))
        write(self.pc1, "b.txt", "b.txt de GitHub\n")
        write(self.pc1, "nuevo.txt", "nuevo de GitHub\n")
        self.publish_pc1()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def publish_pc1(self):
        r = publish(PublishJob(folder=str(self.pc1), url=self.bare, user="T",
                               email="t@x.y", preview=False, check=False))
        self.assertEqual(r.status, "ok", r.message)

    def after(self, module, name, accion):
        """Ejecuta `accion` justo después de `module.name(...)`."""
        real = getattr(module, name)

        def y_luego(*a, **kw):
            resultado = real(*a, **kw)
            accion()
            return resultado
        return mock.patch.object(module, name, y_luego)

    def assert_stopped(self, r, antes):
        self.assertEqual(r.status, "error")
        self.assertIn("cambió en la carpeta", r.message)
        self.assertEqual(rev(self.pc2), antes)

    def test_mirror_stops_if_your_work_changes_after_the_backup(self):
        write(self.pc2, "c.txt", "mío\n")
        antes = rev(self.pc2)
        ruta = self.pc2 / "c.txt"

        def editor():
            # Mismo tamaño: solo la fecha delata el cambio.
            st = os.stat(ruta)
            ruta.write_text("MÍO\n", encoding="utf-8")
            os.utime(ruta, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
        with self.after(sync, "make_backup", editor):
            r = sync.apply(self.pc2, self.bare, mode="mirror")
        self.assert_stopped(r, antes)
        self.assertEqual(ruta.read_text(encoding="utf-8"), "MÍO\n")

    def test_mirror_stops_if_a_clean_file_starts_changing(self):
        antes = rev(self.pc2)
        with self.after(sync, "files_at_risk",
                        lambda: write(self.pc2, "a.txt", "editado\n")):
            r = sync.apply(self.pc2, self.bare, mode="mirror")
        self.assert_stopped(r, antes)
        self.assertEqual((self.pc2 / "a.txt").read_text(), "editado\n")

    def test_mirror_stops_if_a_file_only_you_have_changes(self):
        """Lo que solo tienes tú (no está en GitHub) también desaparece
        con el espejo: se vigila aunque GitHub no lo traiga."""
        write(self.pc2, "solo-mio.txt", "commit local\n")
        commit(self.pc2, "solo mío")
        antes = rev(self.pc2)
        with self.after(sync, "make_backup",
                        lambda: write(self.pc2, "solo-mio.txt", "editado\n")):
            r = sync.apply(self.pc2, self.bare, mode="mirror")
        self.assert_stopped(r, antes)
        self.assertEqual((self.pc2 / "solo-mio.txt").read_text(),
                         "editado\n")

    @unittest.skipUnless(POSIX, "la fecha de cambio (ctime) es de POSIX")
    def test_a_change_that_keeps_size_and_date_is_still_seen(self):
        """Mismo tamaño y misma fecha de modificación: lo delata git
        (compara el ctime del índice y, si no cuadra, el contenido)."""
        git(self.pc2, "config", "core.trustctime", "true")
        git(self.pc2, "config", "core.checkStat", "default")
        ruta = self.pc2 / "a.txt"
        antes = rev(self.pc2)

        def editor():
            st = os.stat(ruta)
            ruta.write_text("A.TXT V1\n", encoding="utf-8")
            os.utime(ruta, ns=(st.st_atime_ns, st.st_mtime_ns))
        with self.after(sync, "files_at_risk", editor):
            r = sync.apply(self.pc2, self.bare, mode="mirror")
        self.assert_stopped(r, antes)
        self.assertEqual(ruta.read_text(encoding="utf-8"), "A.TXT V1\n")

    def test_mirror_stops_if_a_file_appears_where_github_writes(self):
        write(self.pc2, "c.txt", "mío\n")
        antes = rev(self.pc2)
        with self.after(sync, "make_backup",
                        lambda: write(self.pc2, "nuevo.txt", "recién\n")):
            r = sync.apply(self.pc2, self.bare, mode="mirror")
        self.assert_stopped(r, antes)
        self.assertEqual((self.pc2 / "nuevo.txt").read_text(), "recién\n")

    def test_combining_stops_if_an_incoming_file_changes(self):
        write(self.pc2, "c.txt", "mío\n")           # obliga a hacer copia
        antes = rev(self.pc2)
        with self.after(sync, "make_backup",
                        lambda: write(self.pc2, "b.txt", "lo edito ya\n")):
            r = sync.apply(self.pc2, self.bare, mode="merge")
        self.assert_stopped(r, antes)
        self.assertEqual((self.pc2 / "b.txt").read_text(), "lo edito ya\n")
        self.assertEqual((self.pc2 / "c.txt").read_text(), "mío\n")

    def test_combining_ignores_edits_to_files_it_does_not_touch(self):
        write(self.pc2, "c.txt", "mío\n")
        with self.after(sync, "make_backup",
                        lambda: write(self.pc2, "c.txt", "sigo escribiendo\n")):
            r = sync.apply(self.pc2, self.bare, mode="merge")
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual((self.pc2 / "c.txt").read_text(),
                         "sigo escribiendo\n")
        self.assertEqual((self.pc2 / "b.txt").read_text(),
                         "b.txt de GitHub\n")

    def test_restoring_a_version_stops_if_a_file_changes(self):
        primera = git(self.pc2, "rev-list", "--max-parents=0",
                      "HEAD").split()[0]
        sync.apply(self.pc2, self.bare, mode="mirror")
        write(self.pc2, "c.txt", "mío\n")
        antes = rev(self.pc2)
        with self.after(history, "make_backup",
                        lambda: write(self.pc2, "c.txt", "mío y más\n")):
            r = history.restore_version(self.pc2, primera)
        self.assert_stopped(r, antes)
        self.assertEqual((self.pc2 / "c.txt").read_text(), "mío y más\n")

    def test_recovering_a_backup_stops_if_a_file_it_would_overwrite_changes(self):
        write(self.pc2, "a.txt", "versión del respaldo\n")
        rama = Git().backup(self.pc2)
        write(self.pc2, "a.txt", "versión de ahora\n")
        antes = rev(self.pc2)
        with self.after(history, "make_backup",
                        lambda: write(self.pc2, "a.txt", "y la de después\n")):
            r = history.restore(self.pc2, rama)
        self.assert_stopped(r, antes)
        self.assertEqual((self.pc2 / "a.txt").read_text(),
                         "y la de después\n")

    def test_combining_stops_if_your_branch_moves(self):
        write(self.pc2, "c.txt", "mío\n")
        foto = snapshot(self.pc2)

        def commit_ahora():
            git(self.pc2, "commit", "-q", "--allow-empty", "--no-gpg-sign",
                "-m", "otro commit")
        with self.after(sync, "make_backup", commit_ahora):
            r = sync.apply(self.pc2, self.bare, mode="merge")
        self.assertEqual(r.status, "error")
        self.assertEqual(snapshot(self.pc2), foto)

    def test_mirror_keeps_an_untracked_file_that_changed_after_the_backup(self):
        """Lo que no tiene nada que ver con GitHub no se vigila (no bloquea
        nada); al final solo se borra si sigue idéntico a la copia."""
        write(self.pc2, "u.txt", "nuevo mío\n")
        with self.after(sync, "make_backup",
                        lambda: write(self.pc2, "u.txt", "editado\n")):
            r = sync.apply(self.pc2, self.bare, mode="mirror")
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual((self.pc2 / "u.txt").read_text(), "editado\n")
        self.assertIn("no se borraron", r.message)

    def test_restoring_keeps_an_untracked_file_that_changed_after_the_backup(self):
        write(self.pc2, "u.txt", "nuevo mío\n")
        with self.after(history, "make_backup",
                        lambda: write(self.pc2, "u.txt", "editado\n")):
            r = history.restore_version(self.pc2, rev(self.pc2))
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual((self.pc2 / "u.txt").read_text(), "editado\n")
        self.assertIn("no se quitaron", r.message)

    def test_updating_a_download_rechecks_after_downloading(self):
        from gitpush_studio.core.git import update_clone

        class GitConEditor(Git):
            def run(gself, args, *a, **kw):
                p = Git.run(gself, args, *a, **kw)
                if args and args[0] == "fetch":
                    write(self.pc2, "a.txt", "editado mientras bajaba\n")
                return p
        antes = rev(self.pc2)
        estado, _, mensaje = update_clone(GitConEditor(), self.pc2)
        self.assertEqual(estado, "skip", mensaje)
        self.assertEqual(rev(self.pc2), antes)
        self.assertEqual((self.pc2 / "a.txt").read_text(),
                         "editado mientras bajaba\n")


def nested_repo(folder, rel):
    """Otro repositorio dentro del proyecto, con su historial y un
    borrador sin commit. Devuelve una comprobación de que sigue intacto."""
    sub = Path(init_repo(Path(folder) / rel))
    write(sub, "dato.txt", "SECRETO\n")
    commit(sub, "interno")
    write(sub, "borrador.txt", "SIN COMMIT\n")
    cabeza = rev(sub)

    def intacto(test):
        test.assertTrue((sub / ".git").is_dir(),
                        f"se borró el repositorio anidado «{rel}»")
        test.assertEqual(rev(sub), cabeza)
        test.assertEqual((sub / "dato.txt").read_text(), "SECRETO\n")
        test.assertEqual((sub / "borrador.txt").read_text(), "SIN COMMIT\n")
    return intacto


def tree_with(repo, entradas):
    """Árbol de git con {ruta: (modo, sha)}, sin tocar ni la carpeta ni
    el índice del repositorio."""
    indice = Path(repo) / ".git" / "indice-de-prueba"
    env = dict(os.environ, GIT_INDEX_FILE=str(indice))
    try:
        for ruta, (modo, sha) in entradas.items():
            subprocess.run(["git", "update-index", "--add", "--cacheinfo",
                            f"{modo},{sha},{ruta}"], cwd=str(repo), env=env,
                           check=True, capture_output=True)
        return subprocess.run(["git", "write-tree"], cwd=str(repo), env=env,
                              check=True, capture_output=True,
                              text=True).stdout.strip()
    finally:
        if indice.exists():
            indice.unlink()


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class NestedRepositoriesAreNeverWiped(unittest.TestCase):
    """Git borra una carpeta ENTERA, con su .git, si la otra versión pone
    un archivo en su sitio: `reset --hard`, `read-tree -u` y un `merge`
    con esa carpeta ignorada lo hacen sin preguntar. Un historial no
    cabe en un respaldo, así que se rechaza antes de tocar nada."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bare = init_bare(self.tmp, "remote")
        self.pc1 = self.tmp / "pc1"
        write(self.pc1, "a.txt", "A\n")
        self.publish(self.pc1)
        self.pc2 = Path(clone(self.bare, self.tmp / "pc2"))
        # En GitHub aparece un ARCHIVO «vendor»; en pc2, «vendor» es otro
        # repositorio (ignorado, como suele estarlo).
        write(self.pc1, "vendor", "archivo de GitHub\n")
        self.publish(self.pc1)
        (self.pc2 / ".git" / "info" / "exclude").write_text("vendor/\n")
        self.intacto = nested_repo(self.pc2, "vendor")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def publish(self, folder, **kw):
        r = publish(PublishJob(folder=str(folder), url=self.bare, user="T",
                               email="t@x.y", preview=False, check=False,
                               **kw))
        self.assertEqual(r.status, "ok", r.message)
        return r

    def test_a_nested_repo_is_not_a_change_of_yours(self):
        """No se publica ni se descarta: no puede aparecer como «tus
        cambios sin publicar» ni impedir que la carpeta esté al día."""
        pc3 = Path(clone(self.bare, self.tmp / "pc3"))
        intacto = nested_repo(pc3, "herramienta")
        rep = sync.inspect(pc3, self.bare)
        self.assertEqual(rep.local_changes, [])
        self.assertTrue(rep.identical)
        r = sync.apply(pc3, self.bare, mode="mirror")
        self.assertEqual(r.status, "ok", r.message)
        self.assertIn("ya estaba igual", r.message)
        intacto(self)
        self.assertEqual(backup_branches(pc3), [])

    def test_mirror_refuses_before_touching_anything(self):
        rep = sync.inspect(self.pc2, self.bare)
        self.assertIn("vendor", rep.mirror_blocked)
        self.assertEqual(rep.blocked("merge"), "")
        antes, foto = rev(self.pc2), snapshot(self.pc2)
        r = sync.apply(self.pc2, self.bare, mode="mirror",
                       expected=rep.remote_sha)
        self.assertEqual(r.status, "error")
        self.assertIn("vendor", r.message)
        self.intacto(self)
        self.assertEqual(rev(self.pc2), antes)
        self.assertEqual(snapshot(self.pc2), foto)
        self.assertEqual(backup_branches(self.pc2), [])

    def test_combining_reports_a_conflict_and_touches_nothing(self):
        rep = sync.inspect(self.pc2, self.bare)
        self.assertIn("vendor", [ruta for ruta, _ in rep.conflicts])
        antes, foto = rev(self.pc2), snapshot(self.pc2)
        r = sync.apply(self.pc2, self.bare, mode="merge")
        self.assertEqual(r.status, "error")
        self.intacto(self)
        self.assertEqual(rev(self.pc2), antes)
        self.assertEqual(snapshot(self.pc2), foto)

    def test_publishing_with_add_only_refuses_before_merging(self):
        write(self.pc2, "mio.txt", "M\n")
        remoto = git(self.bare, "rev-parse", "main").strip()
        r = publish(PublishJob(folder=str(self.pc2), url=self.bare, user="T",
                               email="t@x.y", preview=False, check=False,
                               mode="addedit"))
        self.assertEqual(r.status, "error", r.message)
        self.assertIn("vendor", r.message)
        self.intacto(self)
        self.assertEqual(git(self.bare, "rev-parse", "main").strip(), remoto)

    def test_updating_a_download_leaves_it_alone(self):
        from gitpush_studio.core.git import update_clone
        antes = rev(self.pc2)
        estado, _, mensaje = update_clone(Git(), self.pc2)
        self.assertEqual(estado, "skip", mensaje)
        self.assertIn("vendor", mensaje)
        self.intacto(self)
        self.assertEqual(rev(self.pc2), antes)

    def test_restoring_a_version_refuses_but_recovering_a_backup_works(self):
        repo = Path(init_repo(self.tmp / "r"))
        write(repo, "a.txt", "A\n")
        write(repo, "vendor", "versión antigua\n")
        con_archivo = commit(repo, "con vendor")
        rama = Git().backup(repo)                 # apunta a ese commit
        git(repo, "rm", "-q", "vendor")
        commit(repo, "sin vendor")
        (repo / ".git" / "info" / "exclude").write_text("vendor/\n")
        intacto = nested_repo(repo, "vendor")
        antes, foto = rev(repo), snapshot(repo)
        r = history.restore_version(repo, con_archivo)
        self.assertEqual(r.status, "error")
        self.assertIn("vendor", r.message)
        intacto(self)
        self.assertEqual((rev(repo), snapshot(repo)), (antes, foto))
        self.assertEqual(backup_branches(repo), [rama])
        # Recuperar un respaldo escribe archivo a archivo y nunca borra
        # una carpeta con contenido: sigue adelante sin tocar el otro
        # repositorio y dice qué no pudo poner.
        r = history.restore(repo, rama)
        self.assertEqual(r.status, "ok", r.message)
        self.assertIn("no se tocaron", r.message)
        intacto(self)
        self.assertEqual(rev(repo), antes)

    @unittest.skipUnless(POSIX, "enlaces simbólicos")
    def test_what_a_folder_that_would_be_wiped_puts_at_risk(self):
        g = Git()
        repo = Path(init_repo(self.tmp / "u"))
        write(repo, "t", "T\n")
        # «k»: carpeta rastreada que el destino convierte en archivo. Lo
        # rastreado ya está en un commit: no es un riesgo.
        write(repo, "k/v.txt", "V\n")
        base = commit(repo, "base")
        blob = git(repo, "hash-object", "-w", "t").strip()
        destino = tree_with(repo, {"d": ("100644", blob),
                                   "t": ("100644", blob),
                                   "k": ("100644", blob),
                                   "m": ("160000", base),
                                   "q/r": ("100644", blob)})
        # «d»: carpeta sin rastrear con un archivo, uno ignorado y un
        # enlace a una carpeta (git lo borraría igual: cuenta).
        write(repo, "d/a.txt", "A\n")
        write(repo, "d/hondo/b.env", "CLAVE=1\n")
        (repo / ".git" / "info" / "exclude").write_text("*.env\n")
        os.symlink(str(self.tmp), str(repo / "d" / "enlace"))
        # «t»: rastreado como archivo, hoy es una carpeta con algo dentro.
        (repo / "t").unlink()
        write(repo, "t/n.txt", "N\n")
        # «m»: submódulo en el destino; git deja su carpeta en paz.
        write(repo, "m/z.txt", "Z\n")
        # «q»: un archivo donde el destino necesita una carpeta.
        write(repo, "q", "Q\n")
        self.assertEqual(safekeeping.files_at_risk(g, repo, destino),
                         ["d/a.txt", "d/enlace", "d/hondo/b.env", "q",
                          "t/n.txt"])
        # Un repositorio (también un «.git» archivo, a cualquier
        # profundidad) dentro de lo que se borraría: no se empieza.
        write(repo, "d/hondo/.git", "gitdir: /otro/sitio\n")
        with self.assertRaises(safekeeping.RepoInTheWay) as cm:
            safekeeping.files_at_risk(g, repo, destino)
        self.assertEqual(cm.exception.paths, ["d/hondo"])
        self.assertIsInstance(cm.exception, StepError)
        # Quien nunca borra carpetas lo puede pedir sin bloqueo.
        self.assertEqual(safekeeping.files_at_risk(g, repo, destino,
                                                   strict=False),
                         ["d/a.txt", "d/enlace", "d/hondo/b.env", "q",
                          "t/n.txt"])


class RefNamesMatchGit(unittest.TestCase):
    """Las reglas propias tienen que coincidir con las de git."""

    @unittest.skipUnless(HAVE_GIT, "git no disponible")
    def test_differential_against_check_ref_format(self):
        rnd = random.Random(20261016)
        alfabeto = list("ab/.-_@{}~^:?*[\\ ñ") + ["\t", "\x7f", "lock"]
        nombres = ["main", "feature/x", "a.lock", ".a", "a/.b", "a..b",
                   "a//b", "/a", "a/", "a.", "@", "a@{b", "ñandú", "x y",
                   "HEAD", "-x", "a/b.lock/c"]
        for _ in range(1500):
            nombres.append("".join(rnd.choice(alfabeto)
                                   for _ in range(rnd.randint(1, 8))))
        for nombre in nombres:
            # Reglas propias de las RAMAS (git branch las rechaza aunque
            # check-ref-format las admita como referencia).
            if nombre.startswith("-") or nombre in ("HEAD", "@"):
                self.assertTrue(branch_problem(nombre), nombre)
                continue
            p = subprocess.run(["git", "check-ref-format",
                                "refs/heads/" + nombre], capture_output=True)
            with self.subTest(nombre=nombre):
                self.assertEqual(branch_problem(nombre) == "",
                                 p.returncode == 0)


class GitObjectIdsMatchGit(unittest.TestCase):

    @unittest.skipUnless(HAVE_GIT, "git no disponible")
    def test_blob_ids_are_the_same_as_git_s(self):
        rnd = random.Random(7)
        tmp = Path(tempfile.mkdtemp())
        try:
            for n in (0, 1, 100, 70000):
                datos = bytes(rnd.getrandbits(8) for _ in range(n))
                f = tmp / f"f{n}"
                f.write_bytes(datos)
                esperado = subprocess.run(
                    ["git", "hash-object", "--no-filters", str(f)],
                    capture_output=True, text=True).stdout.strip()
                self.assertEqual(blob_id(datos), esperado)
                self.assertEqual(file_blob_id(str(f)), esperado)
            if POSIX:
                enlace = tmp / "enlace"
                os.symlink("destino/de/prueba", enlace)
                self.assertEqual(file_blob_id(str(enlace)),
                                 blob_id(b"destino/de/prueba"))
                fifo = tmp / "tuberia"
                os.mkfifo(fifo)
                with self.assertRaises(ValueError):
                    file_blob_id(str(fifo))      # nunca se abre: bloquearía
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @unittest.skipUnless(hasattr(os, "mkfifo") and hasattr(os, "O_NOFOLLOW"),
                         "sin tuberías o sin O_NOFOLLOW")
    def test_a_swap_after_looking_never_hangs_or_follows(self):
        """Entre mirar (lstat) y abrir, alguien cambia el archivo por un
        enlace a una tubería: se falla, no se sigue ni se espera."""
        from gitpush_studio.core import gitobj
        tmp = Path(tempfile.mkdtemp())
        try:
            normal = tmp / "normal.txt"
            normal.write_bytes(b"x")
            fifo = tmp / "tuberia"
            os.mkfifo(fifo)
            cambiado = tmp / "cambiado.txt"
            os.symlink(str(fifo), str(cambiado))
            visto = os.lstat(normal)
            resultado = {}

            def medir():
                try:
                    resultado["id"] = gitobj.file_blob_id(str(cambiado))
                except (OSError, ValueError) as e:
                    resultado["error"] = e
            with mock.patch.object(gitobj.os, "lstat", lambda p: visto):
                hilo = threading.Thread(target=medir, daemon=True)
                hilo.start()
                hilo.join(10)
            self.assertFalse(hilo.is_alive(), "se quedó esperando a la tubería")
            self.assertIn("error", resultado)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
