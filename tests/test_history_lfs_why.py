# -*- coding: utf-8 -*-
"""Secretos en el historial, archivos enormes por Git LFS y «¿por qué
no se sube?»."""

import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.gitenv import (HAVE_GIT, commit_all, git, init_bare,
                          init_repo)

from gitpush_studio.core import lfs, scan
from gitpush_studio.core.errors import Cancelled, StepError
from gitpush_studio.core.exclude import Private, write_private
from gitpush_studio.core.git import Git
from gitpush_studio.core.history_scan import report_lines, scan_history
from gitpush_studio.core.publish import Callbacks, PublishJob, publish
from gitpush_studio.core.why import NOT_PUBLISHED, PUBLISHED, UNKNOWN, explain

GITHUB_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
STRIPE_KEY = "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
HAVE_LFS = HAVE_GIT and subprocess.run(
    ["git", "lfs", "version"], capture_output=True).returncode == 0


def write(folder: Path, rel: str, data) -> Path:
    path = folder / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    return path


class RepoCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        init_repo(self.repo)


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class HistoryScanTests(RepoCase):

    def test_deleted_secrets_are_still_found(self):
        write(self.repo, "app.py", "print('hola')\n")
        write(self.repo, "config.py", f'TOKEN = "{GITHUB_TOKEN}"\n')
        write(self.repo, ".env", "DB_PASSWORD=algo\n")
        commit_all(self.repo, "Añadir configuración")
        (self.repo / "config.py").unlink()
        (self.repo / ".env").unlink()
        commit_all(self.repo, "Quitar secretos")
        write(self.repo, "pagos.py", f'KEY = "{STRIPE_KEY}"\n')
        commit_all(self.repo, "Pagos")
        report = scan_history(str(self.repo))
        by_path = {f.path: f for f in report.findings}
        self.assertIn("config.py", by_path)
        self.assertIn(".env", by_path)
        self.assertIn("pagos.py", by_path)
        token = by_path["config.py"]
        self.assertFalse(token.still_present)
        self.assertEqual(token.first_subject, "Añadir configuración")
        self.assertTrue(token.first_commit)
        self.assertEqual(token.line, 1)
        self.assertTrue(by_path["pagos.py"].still_present)
        self.assertEqual(report.commits, 3)
        texto = "\n".join(t for t, _tag in report_lines(report))
        self.assertIn("cambiarla (rotarla)", texto)
        self.assertIn("ya no está en la versión actual", texto)

    def test_local_backups_and_stash_are_not_publications(self):
        write(self.repo, "a.py", "x = 1\n")
        commit_all(self.repo)
        git(self.repo, "checkout", "-q", "-b", "respaldo-2026-01-01-1200")
        write(self.repo, "copia.py", f'TOKEN = "{GITHUB_TOKEN}"\n')
        commit_all(self.repo, "respaldo")
        git(self.repo, "checkout", "-q", "main")
        write(self.repo, "a.py", f'x = "{STRIPE_KEY}"\n')
        git(self.repo, "stash")
        report = scan_history(str(self.repo))
        self.assertEqual([f.path for f in report.findings], [])

    def test_tags_count(self):
        write(self.repo, "a.py", "x = 1\n")
        commit_all(self.repo)
        git(self.repo, "checkout", "-q", "-b", "temporal")
        write(self.repo, "b.py", f'TOKEN = "{GITHUB_TOKEN}"\n')
        commit_all(self.repo, "con token")
        git(self.repo, "tag", "v0.1")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "branch", "-D", "temporal")
        report = scan_history(str(self.repo))
        self.assertEqual([f.path for f in report.findings], ["b.py"])

    def test_examples_do_not_alarm(self):
        write(self.repo, "settings.py", 'PASSWORD = "changeme"\n')
        commit_all(self.repo)
        report = scan_history(str(self.repo))
        serious = [f for f in report.findings if f.confidence != "baja"]
        self.assertEqual(serious, [])

    def test_stop_and_wrong_folders(self):
        write(self.repo, "a.py", "x = 1\n")
        commit_all(self.repo)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            scan_history(str(self.repo), cancel=cancel)
        with self.assertRaises(StepError):
            scan_history(str(self.tmp))
        empty = self.tmp / "vacio"
        init_repo(empty)
        self.assertEqual(scan_history(str(empty)).findings, [])


@unittest.skipUnless(HAVE_LFS, "git-lfs no disponible")
class LfsTests(RepoCase):

    def test_new_big_files_go_by_lfs(self):
        write(self.repo, "a.txt", "a\n")
        commit_all(self.repo)
        write(self.repo, "datos/enorme.bin", os.urandom(4096))
        tracked, problems = lfs.track(str(self.repo), ["datos/enorme.bin"])
        self.assertEqual((tracked, problems), (["datos/enorme.bin"], []))
        attributes = (self.repo / ".gitattributes").read_text()
        self.assertIn("datos/enorme.bin filter=lfs", attributes)
        self.assertEqual(lfs.lfs_tracked(Git(), str(self.repo),
                                         ["datos/enorme.bin", "a.txt"]),
                         {"datos/enorme.bin"})

    def test_a_file_already_in_a_commit_changes_nothing(self):
        write(self.repo, "enorme.bin", os.urandom(4096))
        write(self.repo, "otro.bin", os.urandom(4096))
        commit_all(self.repo)
        config_before = (self.repo / ".git" / "config").read_text()
        tracked, problems = lfs.track(str(self.repo),
                                      ["enorme.bin", "nuevo.bin"])
        self.assertEqual(tracked, [])
        self.assertEqual([p for p, _why in problems], ["enorme.bin"])
        self.assertFalse((self.repo / ".gitattributes").exists())
        self.assertEqual((self.repo / ".git" / "config").read_text(),
                         config_before)

    def test_without_git_lfs_it_says_so(self):
        with mock.patch.object(lfs, "lfs_available", lambda git: False):
            with self.assertRaises(StepError) as caught:
                lfs.track(str(self.repo), ["x.bin"])
        self.assertIn("git-lfs.com", str(caught.exception))

    def test_publishing_with_lfs(self):
        bare = init_bare(self.tmp, "remoto")
        write(self.repo, "app.py", "print(1)\n")
        write(self.repo, "modelo.bin", os.urandom(3000))
        preguntas = []

        def ask_review(findings):
            preguntas.append([f["path"] for f in findings])
            return "lfs", [f["path"] for f in findings]
        job = PublishJob(folder=str(self.repo), url=bare, user="t",
                         email="t@x.y", preview=False, license="")
        with mock.patch.object(scan, "LIMIT_HARD", 2000), \
                mock.patch.object(scan, "LIMIT_WARN", 1000):
            result = publish(job, Callbacks(ask_review=ask_review))
            self.assertTrue(result.ok, result.message)
            self.assertEqual(preguntas, [["modelo.bin"]])
            pointer = git(Path(bare), "show", "main:modelo.bin")
            self.assertTrue(pointer.startswith(
                "version https://git-lfs.github.com/spec/v1"))
            # La siguiente revisión ya no pregunta por él: va por LFS.
            write(self.repo, "app.py", "print(2)\n")
            result = publish(job, Callbacks(ask_review=ask_review))
            self.assertTrue(result.ok, result.message)
            self.assertEqual(len(preguntas), 1)


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class WhyTests(RepoCase):

    def setUp(self):
        super().setUp()
        write(self.repo, ".gitignore", "# comentario\n*.log\n")
        write(self.repo, "sub/.gitignore", "privado/\n")
        write(self.repo, "app.py", "print(1)\n")
        write(self.repo, "registro.log", "x\n")
        write(self.repo, "sub/privado/notas.txt", "x\n")
        write(self.repo, "sub/normal.txt", "x\n")
        commit_all(self.repo)

    def test_root_and_nested_gitignore(self):
        answer = explain(str(self.repo), "registro.log")
        self.assertEqual(answer.verdict, NOT_PUBLISHED)
        self.assertIn("«.gitignore» (línea 2)", answer.reasons[0])
        self.assertIn("*.log", answer.reasons[0])
        answer = explain(str(self.repo),
                         str(self.repo / "sub" / "privado" / "notas.txt"))
        self.assertEqual(answer.verdict, NOT_PUBLISHED)
        self.assertIn("sub/.gitignore", answer.reasons[0])

    def test_private_list_and_box(self):
        write(self.repo, "PROYECTO.md", "x\n")
        write(self.repo, "borrador.md", "x\n")
        write_private(str(self.repo), Private(("/PROYECTO.md",), ()))
        answer = explain(str(self.repo), "PROYECTO.md")
        self.assertEqual(answer.verdict, NOT_PUBLISHED)
        self.assertIn("lista privada de la app", answer.reasons[0])
        answer = explain(str(self.repo), "borrador.md", ["/borrador.md"])
        self.assertEqual(answer.verdict, NOT_PUBLISHED)
        self.assertIn("cuadro", answer.reasons[0])

    def test_global_exclusions(self):
        global_file = self.tmp / "ignore-global"
        global_file.write_text("*.bak\n")
        git(self.repo, "config", "core.excludesFile", str(global_file))
        write(self.repo, "copia.bak", "x\n")
        answer = explain(str(self.repo), "copia.bak")
        self.assertEqual(answer.verdict, NOT_PUBLISHED)
        self.assertIn("lista global", answer.reasons[0])

    def test_nested_repository_and_empty_folder(self):
        init_repo(self.repo / "vendor" / "lib")
        write(self.repo, "vendor/lib/x.py", "x\n")
        answer = explain(str(self.repo), "vendor/lib/x.py")
        self.assertEqual(answer.verdict, NOT_PUBLISHED)
        self.assertIn("otro repositorio", answer.reasons[0])
        (self.repo / "vacia").mkdir()
        answer = explain(str(self.repo), "vacia")
        self.assertEqual(answer.verdict, NOT_PUBLISHED)
        self.assertIn("carpeta vacía", answer.reasons[0])

    def test_published_files_and_their_notes(self):
        answer = explain(str(self.repo), "app.py")
        self.assertEqual(answer.verdict, PUBLISHED)
        write(self.repo, "id_rsa", "x\n")
        answer = explain(str(self.repo), "id_rsa")
        self.assertEqual(answer.verdict, PUBLISHED)
        self.assertIn("revisión de seguridad", " ".join(answer.notes))

    def test_negations_and_tracked_files(self):
        write(self.repo, ".gitignore", "*.log\n!importante.log\n")
        write(self.repo, "importante.log", "x\n")
        answer = explain(str(self.repo), "importante.log")
        self.assertEqual(answer.verdict, PUBLISHED)
        self.assertIn("«!»", " ".join(answer.notes))
        answer = explain(str(self.repo), "app.py", ["/app.py"])
        self.assertEqual(answer.verdict, NOT_PUBLISHED)
        self.assertIn("ya estaba publicado", " ".join(answer.notes))

    def test_outside_or_missing(self):
        self.assertEqual(explain(str(self.repo), str(self.tmp)).verdict,
                         UNKNOWN)
        self.assertEqual(explain(str(self.repo), "no-existe.txt").verdict,
                         UNKNOWN)
        self.assertEqual(explain(str(self.repo), "../fuera.txt").verdict,
                         UNKNOWN)

    def test_a_folder_without_git(self):
        loose = self.tmp / "suelta"
        write(loose, ".gitignore", "*.tmp\n")
        write(loose, "a.tmp", "x\n")
        write(loose, "b.txt", "x\n")
        self.assertEqual(explain(str(loose), "a.tmp").verdict, NOT_PUBLISHED)
        self.assertEqual(explain(str(loose), "b.txt").verdict, PUBLISHED)


if __name__ == "__main__":
    unittest.main()
