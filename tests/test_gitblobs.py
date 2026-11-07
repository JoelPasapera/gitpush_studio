# -*- coding: utf-8 -*-
"""Leer de git lo que no cambió: el TXT tiene que salir IDÉNTICO, byte a
byte, al de leer la carpeta. Cualquier duda (cambios, marcas que hacen
que git no mire el archivo, filtros, conversiones de finales de línea)
se resuelve leyendo del disco."""

import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from tests.gitenv import HAVE_GIT, commit_all, git, init_repo

from gitpush_studio.core import gitblobs, pack, preamble
from gitpush_studio.core.pack import consolidate_project


class FixedDate(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 6, 10, 30)


def write(folder: Path, rel: str, data) -> None:
    path = folder / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class GitBlobsTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        init_repo(self.repo)
        for i in range(30):
            write(self.repo, f"src/m{i:02d}.py",
                  "".join(f"valor_{i}_{k} = {k}\n" for k in range(20)))
        write(self.repo, "crlf.txt", b"uno\r\ndos\r\n")
        write(self.repo, "img.png", b"\x89PNG\r\n\x1a\n\x00\x01")
        commit_all(self.repo)
        for patch in (mock.patch.object(pack, "datetime", FixedDate),
                      mock.patch.object(preamble, "datetime", FixedDate),
                      mock.patch.object(gitblobs, "MIN_FILES", 1)):
            patch.start()
            self.addCleanup(patch.stop)

    def txt(self, enabled: bool, name: str) -> bytes:
        out = self.tmp / name
        with mock.patch.object(gitblobs, "ENABLED", enabled):
            self.stats = consolidate_project(self.repo, out)
        return out.read_bytes()

    def assert_same_as_disk(self):
        from_git = self.txt(True, "git.txt")
        served = self.stats["from_git"]
        from_disk = self.txt(False, "disco.txt")
        self.assertEqual(self.stats["from_git"], 0)
        self.assertEqual(from_git, from_disk)
        return served

    def test_same_bytes_as_reading_the_folder(self):
        write(self.repo, "src/m03.py", "cambiado en disco\n")
        # Mismo tamaño que lo guardado: solo git sabe que cambió.
        mismo = (self.repo / "src" / "m04.py").read_bytes()
        write(self.repo, "src/m04.py", mismo.replace(b"= 0", b"= 9"))
        write(self.repo, "nuevo.py", "sin rastrear\n")
        served = self.assert_same_as_disk()
        self.assertGreater(served, 25)

    def test_line_ending_conversions_never_leak_in(self):
        git(self.repo, "config", "core.autocrlf", "true")
        write(self.repo, "lf.txt", "uno\ndos\n")       # en disco con LF
        commit_all(self.repo, "lf")
        write(self.repo, ".gitattributes", "*.md text eol=crlf\n")
        write(self.repo, "doc.md", "a\nb\n")
        commit_all(self.repo, "md")
        self.assert_same_as_disk()

    def test_files_git_does_not_look_at_are_read_from_disk(self):
        git(self.repo, "update-index", "--assume-unchanged", "src/m01.py")
        write(self.repo, "src/m01.py", "editado sin que git mire\n")
        git(self.repo, "update-index", "--skip-worktree", "src/m02.py")
        write(self.repo, "src/m02.py", "tampoco lo mira\n")
        # Y uno editado sin cambiar de tamaño: lo guardado mediría igual.
        git(self.repo, "update-index", "--assume-unchanged", "src/m06.py")
        mismo = (self.repo / "src" / "m06.py").read_bytes()
        write(self.repo, "src/m06.py", mismo.replace(b"= 0", b"= 7"))
        text = self.txt(True, "git.txt").decode("utf-8")
        self.assertIn("editado sin que git mire", text)
        self.assertIn("tampoco lo mira", text)
        self.assertIn("valor_6_0 = 7", text)
        self.assert_same_as_disk()

    def test_filtered_files_are_never_asked_to_git(self):
        git(self.repo, "config", "filter.raro.clean", "cat")
        git(self.repo, "config", "filter.raro.smudge", "cat")
        write(self.repo, ".gitattributes", "src/m05.py filter=raro\n")
        commit_all(self.repo, "filtro")
        pedidos = []
        real = gitblobs._BlobStream

        def spy(git_, folder, requests):
            pedidos.extend(rel for rel, _sha in requests)
            return real(git_, folder, requests)
        with mock.patch.object(gitblobs, "_BlobStream", spy):
            self.txt(True, "git.txt")
        self.assertTrue(pedidos)
        self.assertNotIn("src/m05.py", pedidos)
        self.assertNotIn("img.png", pedidos)
        self.assert_same_as_disk()

    def test_if_git_fails_halfway_the_rest_comes_from_disk(self):
        real_next = gitblobs._BlobStream.next
        calls = {"n": 0}

        def flaky(stream):
            calls["n"] += 1
            if calls["n"] > 5:
                raise OSError("roto")
            return real_next(stream)
        with mock.patch.object(gitblobs._BlobStream, "next", flaky):
            from_git = self.txt(True, "git.txt")
        from_disk = self.txt(False, "disco.txt")
        self.assertEqual(from_git, from_disk)

    def test_windows_line_endings_are_served_exactly(self):
        git(self.repo, "config", "core.autocrlf", "true")
        # Lo que hace git en Windows al sacar los archivos: todo con CRLF.
        # (Sin la configuración fija de las pruebas, que apaga autocrlf.)
        shutil.rmtree(str(self.repo / "src"))
        subprocess.run(["git", "checkout", "--", "src"], cwd=str(self.repo),
                       capture_output=True, check=True)
        self.assertIn(b"\r\n", (self.repo / "src" / "m00.py").read_bytes())
        # Uno mezclado (lo dejó así un editor): nunca se adivina.
        write(self.repo, "src/m07.py", b"valor = 1\r\nsuelto\n")
        served = self.assert_same_as_disk()
        self.assertGreater(served, 25)

    def test_what_is_in_the_folder(self):
        self.assertEqual(gitblobs.as_in_folder(b"a\nb\n", 4), b"a\nb\n")
        self.assertEqual(gitblobs.as_in_folder(b"a\nb\n", 6),
                         b"a\r\nb\r\n")
        self.assertIsNone(gitblobs.as_in_folder(b"a\nb\n", 5))
        self.assertIsNone(gitblobs.as_in_folder(b"a\r\nb\n", 7))

    def test_small_folders_do_not_bother(self):
        with mock.patch.object(gitblobs, "MIN_FILES", 10_000):
            self.txt(True, "git.txt")
        self.assertEqual(self.stats["from_git"], 0)


if __name__ == "__main__":
    unittest.main()
