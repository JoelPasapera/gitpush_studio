# -*- coding: utf-8 -*-
"""Pruebas de las partes puras del núcleo (no necesitan git)."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from gitpush_studio.core import config, git, ignore, urls
from gitpush_studio.core.fs import fmt_size, normalize_path


class IgnoreMatcherTests(unittest.TestCase):

    def matcher(self, *patterns):
        return ignore.IgnoreMatcher(patterns)

    def test_literal_name_matches_files_and_dirs(self):
        m = self.matcher("node_modules")
        self.assertTrue(m.ignored("node_modules", "node_modules", True))
        self.assertTrue(m.ignored("a/node_modules", "node_modules", False))
        self.assertFalse(m.ignored("a/node_modules2", "node_modules2", True))

    def test_dir_only_pattern_does_not_match_a_file(self):
        m = self.matcher("build/")
        self.assertTrue(m.ignored("build", "build", True))
        self.assertFalse(m.ignored("build", "build", False))

    def test_glob_on_name(self):
        m = self.matcher("*.log")
        self.assertTrue(m.ignored("logs/app.log", "app.log", False))
        self.assertFalse(m.ignored("logs/app.txt", "app.txt", False))

    def test_path_pattern_is_anchored_and_covers_children(self):
        m = self.matcher("src/generated")
        self.assertTrue(m.ignored("src/generated", "generated", True))
        self.assertTrue(m.ignored("src/generated/x.py", "x.py", False))
        self.assertFalse(m.ignored("other/src/generated", "generated", True))

    def test_leading_slash_anchors_to_root(self):
        """Antes «/.env» nunca coincidía con nada; ahora solo con la raíz."""
        m = self.matcher("/.env")
        self.assertTrue(m.ignored(".env", ".env", False))
        self.assertFalse(m.ignored("sub/.env", ".env", False))

    def test_comments_and_negations_are_skipped(self):
        m = self.matcher("# comentario", "!keep.txt", "   ")
        self.assertTrue(m.empty)

    def test_ignored_path_checks_ancestors(self):
        m = self.matcher("node_modules/", "*.tmp")
        self.assertTrue(m.ignored_path("web/node_modules/x/y.js"))
        self.assertTrue(m.ignored_path("cache/a.tmp"))
        self.assertFalse(m.ignored_path("web/src/app.js"))

    def test_anchor_pattern(self):
        self.assertEqual(ignore.anchor_pattern("sub/.env"), "/sub/.env")
        self.assertEqual(ignore.anchor_pattern("/x"), "/x")
        self.assertEqual(ignore.anchor_pattern("*.pem"), "*.pem")

    def test_append_gitignore_only_adds_missing(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".gitignore").write_text("a/\n# c\nb\n",
                                                encoding="utf-8")
            new = ignore.append_gitignore(d, ["b", "c", ""])
            self.assertEqual(new, ["c"])
            self.assertEqual(ignore.read_gitignore(d), ["a/", "# c", "b", "c"])
            self.assertEqual(ignore.append_gitignore(d, ["c"]), [])


class UrlTests(unittest.TestCase):

    def test_owner_repo(self):
        for u in ("https://github.com/ana/proj.git", "https://github.com/ana/proj",
                  "git@github.com:ana/proj.git", "https://github.com/ana/proj/"):
            self.assertEqual(urls.parse_owner_repo(u), ("ana", "proj"), u)
        self.assertEqual(urls.parse_owner_repo("https://gitlab.com/a/b"),
                         (None, None))

    def test_repo_name_from_url(self):
        self.assertEqual(urls.repo_name_from_url("https://github.com/a/b.git"),
                         "b")
        self.assertEqual(urls.repo_name_from_url("/tmp/repos/x.git"), "x")

    def test_username(self):
        self.assertEqual(urls.parse_username("https://github.com/Ana-1/"),
                         "Ana-1")
        self.assertEqual(urls.parse_username("ana"), "ana")
        self.assertEqual(urls.parse_username("-mal"), "")
        self.assertEqual(urls.parse_username("a" * 40), "")

    def test_web_url(self):
        self.assertEqual(urls.repo_web_url("git@github.com:a/b.git"),
                         "https://github.com/a/b")
        self.assertEqual(urls.repo_web_url("https://github.com/a/b.git/"),
                         "https://github.com/a/b")

    def test_remote_url_accepts_local_bare_repo(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(urls.is_remote_url(d))
        self.assertTrue(urls.is_remote_url("git@github.com:a/b.git"))
        self.assertFalse(urls.is_remote_url("ftp://x"))
        self.assertFalse(urls.is_remote_url(""))

    def test_http_origin(self):
        self.assertEqual(urls.http_origin("https://github.com/a/b.git"),
                         "https://github.com/")
        self.assertEqual(urls.http_origin("git@github.com:a/b"), "")


class GitParserTests(unittest.TestCase):

    def test_name_status_with_rename(self):
        out = "A\0new.txt\0M\0mod.txt\0R100\0old.txt\0renamed.txt\0D\0gone\0"
        added, modified, deleted = git.parse_name_status_z(out)
        self.assertEqual(added, ["new.txt"])
        self.assertEqual(modified, ["mod.txt", "renamed.txt"])
        self.assertEqual(deleted, ["gone"])

    def test_status_z_ignore_deletions(self):
        out = " M a.txt\0 D b.txt\0?? c.txt\0R  new\0old\0"
        self.assertEqual(git.parse_status_z(out),
                         ["a.txt", "b.txt", "c.txt", "new"])
        self.assertEqual(git.parse_status_z(out, ignore_deletions=True),
                         ["a.txt", "c.txt", "new"])
        self.assertEqual(git.parse_status_z(""), [])

    def test_auth_env_uses_config_env_not_argv(self):
        env = git.auth_env("https://github.com/a/b.git", "tok",
                           base_env={"GIT_CONFIG_COUNT": "2"})
        self.assertEqual(env["GIT_CONFIG_COUNT"], "3")
        self.assertEqual(env["GIT_CONFIG_KEY_2"],
                         "http.https://github.com/.extraheader")
        self.assertIn("basic", env["GIT_CONFIG_VALUE_2"])
        self.assertNotIn("tok", env["GIT_CONFIG_VALUE_2"])
        self.assertEqual(git.auth_env("git@github.com:a/b", "tok"), {})
        self.assertEqual(git.auth_env("https://github.com/a/b", ""), {})

    def test_push_error_classification(self):
        self.assertIn("credenciales",
                      git.push_error_message("fatal: authentication failed"))
        self.assertIn("no existe",
                      git.push_error_message("remote: repository not found"))
        self.assertIn("alguien más",
                      git.push_error_message("! [rejected] non-fast-forward"))
        self.assertIn("nada que publicar",
                      git.push_error_message("error: src refspec main does "
                                             "not match any"))


class ConfigTests(unittest.TestCase):

    def test_missing_file_gives_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            data, warn = config.load_config(Path(d) / "profiles.json")
            self.assertIsNone(warn)
            self.assertEqual(data["profiles"], {})

    def test_corrupt_file_is_backed_up_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "profiles.json"
            path.write_text("{ esto no es json", encoding="utf-8")
            data, warn = config.load_config(path)
            self.assertIsNotNone(warn)
            self.assertFalse(path.exists())
            backups = list(Path(d).glob("profiles.json.bak-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"),
                             "{ esto no es json")
            config.save_config(data, path)          # no toca la copia
            self.assertEqual(len(list(Path(d).glob("profiles.json.bak-*"))),
                             1)

    def test_save_is_atomic_and_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "profiles.json"
            data = config.defaults()
            data["profiles"]["p"] = {"folder": "x", "url": "y"}
            config.save_config(data, path)
            self.assertEqual(sorted(os.listdir(d)), ["profiles.json"])
            self.assertEqual(json.loads(path.read_text("utf-8"))["profiles"],
                             {"p": {"folder": "x", "url": "y"}})
            data2, warn = config.load_config(path)
            self.assertIsNone(warn)
            self.assertEqual(data2["version"], config.SCHEMA_VERSION)


class FsTests(unittest.TestCase):

    def test_fmt_size(self):
        self.assertEqual(fmt_size(512), "512 B")
        self.assertEqual(fmt_size(2048), "2 KB")
        self.assertEqual(fmt_size(3 * 1024 ** 2), "3 MB")
        self.assertEqual(fmt_size(int(1.5 * 1024 ** 3)), "1.5 GB")

    def test_normalize_path_strips_quotes(self):
        self.assertEqual(normalize_path('  "/tmp/x"  '), str(Path("/tmp/x")))
        self.assertEqual(normalize_path(""), "")


if __name__ == "__main__":
    unittest.main()
