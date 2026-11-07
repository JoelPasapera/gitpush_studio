# -*- coding: utf-8 -*-
"""El vigilante de la carpeta (auto-publicación en tiempo real)."""

import os
import shutil
import struct
import tempfile
import time
import unittest
from pathlib import Path

from gitpush_studio.core import watch


def wait_for(condition, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


class PureTests(unittest.TestCase):

    def test_windows_notification_buffer(self):
        def record(name, last=False):
            raw = name.encode("utf-16-le")
            size = 12 + len(raw)
            size += (-size) % 4                     # alineado a 4 bytes
            head = struct.pack("<III", 0 if last else size, 3, len(raw))
            return (head + raw).ljust(size, b"\0")
        data = record("src\\app.py") + record("ñandú.txt", last=True)
        self.assertEqual(watch.parse_notify_buffer(data),
                         ["src\\app.py", "ñandú.txt"])

    def test_what_counts(self):
        self.assertTrue(watch._relevant("src/app.py"))
        self.assertTrue(watch._relevant(None))
        for ignored in (".git/index", "web/node_modules/x/y.js",
                        "pkg\\__pycache__\\m.pyc", ".venv/lib/a.py"):
            self.assertFalse(watch._relevant(ignored), ignored)

    def test_a_missing_folder_has_no_watcher(self):
        self.assertIsNone(watch.start_watcher("/no/existe/esto", print))


@unittest.skipUnless(hasattr(os, "uname") and os.uname().sysname == "Linux",
                     "inotify")
class LinuxWatcherTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / ".git").mkdir()
        (self.tmp / "node_modules" / "pkg").mkdir(parents=True)
        (self.tmp / "src").mkdir()
        self.seen = []
        self.watcher = watch.start_watcher(str(self.tmp), self.seen.append)
        self.assertIsNotNone(self.watcher)
        self.addCleanup(self.watcher.stop)

    def test_changes_are_reported(self):
        (self.tmp / "a.txt").write_text("1")
        (self.tmp / "src" / "b.py").write_text("2")
        self.assertTrue(wait_for(lambda: {"a.txt", "src/b.py"}
                                 <= set(self.seen)))
        os.rename(str(self.tmp / "a.txt"), str(self.tmp / "c.txt"))
        (self.tmp / "src" / "b.py").unlink()
        self.assertTrue(wait_for(lambda: "c.txt" in self.seen))

    def test_new_folders_are_watched_too(self):
        (self.tmp / "nueva").mkdir()
        self.assertTrue(wait_for(lambda: "nueva" in self.seen))
        time.sleep(0.2)
        (self.tmp / "nueva" / "dentro.txt").write_text("x")
        self.assertTrue(wait_for(lambda: "nueva/dentro.txt" in self.seen))

    def test_git_internals_and_heavy_folders_do_not_count(self):
        (self.tmp / ".git" / "index").write_text("x")
        (self.tmp / "node_modules" / "pkg" / "i.js").write_text("x")
        (self.tmp / "testigo.txt").write_text("x")
        self.assertTrue(wait_for(lambda: "testigo.txt" in self.seen))
        for rel in self.seen:
            self.assertFalse(rel and (rel.startswith(".git")
                                      or "node_modules" in rel), rel)

    def test_stopping_is_immediate(self):
        t0 = time.time()
        self.watcher.stop()
        self.assertLess(time.time() - t0, 2.0)
        self.assertFalse(self.watcher.running)


if __name__ == "__main__":
    unittest.main()
