# -*- coding: utf-8 -*-
"""git más rápido: solo donde es seguro y nunca contra tu configuración."""

import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.gitenv import HAVE_GIT, commit_all, git, init_repo

from gitpush_studio.core import gitspeed
from gitpush_studio.core.git import Git


class FilesystemTests(unittest.TestCase):

    def test_mount_points_are_matched_by_the_longest_prefix(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        mounts = tmp / "mounts"
        mounts.write_text(
            "rootfs / ext4 rw 0 0\n"
            f"srv {tmp} xfs rw 0 0\n"
            f"red {tmp}/con\\040espacio nfs rw 0 0\n", encoding="utf-8")
        (tmp / "con espacio").mkdir()
        self.assertEqual(gitspeed._linux_filesystem(str(tmp / "a"),
                                                    str(mounts)), "xfs")
        self.assertEqual(gitspeed._linux_filesystem(
            str(tmp / "con espacio" / "x"), str(mounts)), "nfs")
        self.assertEqual(gitspeed._linux_filesystem("/usr", str(mounts)),
                         "ext4")

    def test_index_header_count(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / ".git").mkdir()
        (tmp / ".git" / "index").write_bytes(b"DIRC" + struct.pack(">II", 2,
                                                                   12345))
        self.assertEqual(gitspeed.tracked_count(str(tmp)), 12345)
        self.assertEqual(gitspeed.tracked_count(str(tmp / "nada")), 0)


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class TuneTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        init_repo(self.repo)
        (self.repo / "a.txt").write_text("a\n")
        commit_all(self.repo)
        gitspeed.forget()
        self.addCleanup(gitspeed.forget)

    def config(self, key):
        return git(self.repo, "config", "--local", "--get", key,
                   check=False).strip()

    def test_untracked_cache_on_a_safe_disk(self):
        with mock.patch.object(gitspeed, "filesystem_type",
                               lambda path: ("ext4", False)):
            applied = gitspeed.tune(str(self.repo), Git())
        self.assertEqual(applied, ["core.untrackedCache"])
        self.assertEqual(self.config("core.untrackedCache"), "true")
        # Una vez por sesión: la segunda llamada no hace nada.
        self.assertEqual(gitspeed.tune(str(self.repo), Git()), [])

    def test_never_on_network_or_unknown_disks(self):
        for answer in (("nfs", False), ("ntfs", True), ("", False)):
            gitspeed.forget()
            with mock.patch.object(gitspeed, "filesystem_type",
                                   lambda path, a=answer: a):
                self.assertEqual(gitspeed.tune(str(self.repo), Git()), [])
        self.assertEqual(self.config("core.untrackedCache"), "")

    def test_your_own_setting_always_wins(self):
        git(self.repo, "config", "core.untrackedCache", "false")
        with mock.patch.object(gitspeed, "filesystem_type",
                               lambda path: ("ext4", False)):
            self.assertEqual(gitspeed.tune(str(self.repo), Git()), [])
        self.assertEqual(self.config("core.untrackedCache"), "false")

    def test_fsmonitor_only_where_git_has_it_and_the_repo_is_big(self):
        with mock.patch.object(gitspeed, "filesystem_type",
                               lambda path: ("ntfs", False)), \
                mock.patch.object(gitspeed, "fsmonitor_platform",
                                  lambda: True), \
                mock.patch.object(gitspeed, "tracked_count",
                                  lambda folder: 50000), \
                mock.patch.object(gitspeed, "has_fsmonitor_daemon",
                                  lambda g: True):
            applied = gitspeed.tune(str(self.repo), Git())
        self.assertIn("core.fsmonitor", applied)
        gitspeed.forget()
        git(self.repo, "config", "--unset", "core.fsmonitor")
        with mock.patch.object(gitspeed, "filesystem_type",
                               lambda path: ("ntfs", False)), \
                mock.patch.object(gitspeed, "fsmonitor_platform",
                                  lambda: True), \
                mock.patch.object(gitspeed, "tracked_count",
                                  lambda folder: 10), \
                mock.patch.object(gitspeed, "has_fsmonitor_daemon",
                                  lambda g: True):
            self.assertNotIn("core.fsmonitor",
                             gitspeed.tune(str(self.repo), Git()))

    def test_a_folder_that_is_not_a_repository(self):
        other = self.tmp / "suelta"
        other.mkdir()
        self.assertEqual(gitspeed.tune(str(other), Git()), [])
        self.assertFalse((other / ".git").exists())


if __name__ == "__main__":
    unittest.main()
