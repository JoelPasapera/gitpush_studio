# -*- coding: utf-8 -*-
"""Flujo local ↔ GitHub: traer cambios, versiones, ramas, mensajes
automáticos y releases. Todo contra git real y repositorios bare."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.gitenv import HAVE_GIT, clone, git, init_bare, init_repo

from gitpush_studio.core import github, history, release, sync
from gitpush_studio.core.git import Git
from gitpush_studio.core.publish import PublishJob, publish


def write(folder, rel, content="x\n"):
    p = Path(folder) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def read(folder, rel):
    return (Path(folder) / rel).read_text(encoding="utf-8")


def make_bare(where, name):
    return init_bare(where, name)


def remote_files(bare, ref="main"):
    p = subprocess.run(["git", "ls-tree", "-r", "--name-only", ref],
                       cwd=bare, capture_output=True, text=True)
    return sorted(x for x in p.stdout.splitlines() if x)


def local_files(folder):
    return sorted(str(p.relative_to(folder)) for p in Path(folder).rglob("*")
                  if p.is_file() and ".git" not in p.parts)


def job_for(folder, bare, **kw):
    base = dict(folder=str(folder), url=bare, user="Test", email="t@x.y",
                preview=False, check=False)
    base.update(kw)
    return PublishJob(**base)


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class SyncTests(unittest.TestCase):
    """Dos clones del mismo remoto: «pc1» publica, «pc2» trae."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bare = make_bare(self.tmp, "remote")
        self.pc1 = Path(self.tmp) / "pc1"
        write(self.pc1, "a.txt", "A1\n")
        write(self.pc1, "b.txt", "B1\n")
        self.assertEqual(publish(job_for(self.pc1, self.bare)).status, "ok")
        self.pc2 = Path(self.tmp) / "pc2"
        clone(self.bare, self.pc2)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def publish_pc1(self):
        self.assertEqual(publish(job_for(self.pc1, self.bare)).status, "ok")

    def test_inspect_reports_incoming_and_local(self):
        write(self.pc1, "a.txt", "A2\n")
        write(self.pc1, "c.txt", "C\n")
        (self.pc1 / "b.txt").unlink()
        self.publish_pc1()
        write(self.pc2, "local.txt", "L\n")
        rep = sync.inspect(self.pc2, self.bare)
        self.assertTrue(rep.remote_exists)
        self.assertFalse(rep.up_to_date)
        self.assertEqual((rep.added, rep.modified, rep.deleted),
                         (["c.txt"], ["a.txt"], ["b.txt"]))
        self.assertEqual(rep.local_changes, ["local.txt"])
        self.assertTrue(rep.needs_backup)
        self.assertFalse(rep.local_ahead)
        self.assertEqual(local_files(self.pc2), ["a.txt", "b.txt",
                                                 "local.txt"])   # intacto

    def test_up_to_date(self):
        rep = sync.inspect(self.pc2, self.bare)
        self.assertTrue(rep.up_to_date)
        r = sync.apply(self.pc2, self.bare)
        self.assertEqual(r.status, "ok")
        self.assertIn("al día", r.message)
        self.assertEqual(r.backup_branch, "")

    def test_merge_keeps_local_only_files_and_prefers_github(self):
        write(self.pc1, "a.txt", "A-github\n")
        self.publish_pc1()
        write(self.pc2, "a.txt", "A-local\n")          # choca
        write(self.pc2, "solo-local.txt", "S\n")        # no choca
        r = sync.apply(self.pc2, self.bare, mode="merge")
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(read(self.pc2, "a.txt"), "A-github\n")
        self.assertEqual(local_files(self.pc2), ["a.txt", "b.txt",
                                                 "solo-local.txt"])
        self.assertTrue(r.backup_branch.startswith("respaldo-"))
        self.assertEqual(git(self.pc2, "show", f"{r.backup_branch}:a.txt"),
                         "A-local\n")
        # Traer cambios NO hace commit de lo tuyo: la rama queda justo en
        # lo que hay en GitHub y tu archivo nuevo sigue sin publicar (al
        # publicar pasará por la vista previa y la revisión de secretos).
        self.assertEqual(git(self.pc2, "rev-parse", "HEAD").strip(),
                         git(self.bare, "rev-parse", "main").strip())
        self.assertEqual(Git().status_changes(self.pc2), ["solo-local.txt"])
        self.assertNotIn("solo-local.txt",
                         git(self.pc2, "ls-tree", "-r", "--name-only", "HEAD"))

    def test_mirror_makes_local_identical_and_backs_up(self):
        write(self.pc1, "a.txt", "A2\n")
        self.publish_pc1()
        write(self.pc2, "extra.txt", "E\n")
        r = sync.apply(self.pc2, self.bare, mode="mirror")
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(local_files(self.pc2), ["a.txt", "b.txt"])
        self.assertEqual(read(self.pc2, "a.txt"), "A2\n")
        self.assertIn("extra.txt", git(self.pc2, "ls-tree", "-r",
                                       "--name-only", r.backup_branch))

    def test_fresh_init_with_files_merges_unrelated_histories(self):
        fresh = Path(self.tmp) / "fresh"
        write(fresh, "mio.txt", "M\n")
        init_repo(fresh)
        r = sync.apply(fresh, self.bare)
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(local_files(fresh), ["a.txt", "b.txt", "mio.txt"])

    def test_empty_repo_takes_remote(self):
        empty = init_repo(Path(self.tmp) / "empty")
        r = sync.apply(empty, self.bare)
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(local_files(empty), ["a.txt", "b.txt"])
        self.assertEqual(r.backup_branch, "")

    def test_not_a_repo_and_missing_branch(self):
        plain = Path(self.tmp) / "plain"
        plain.mkdir()
        with self.assertRaises(Exception):
            sync.inspect(plain, self.bare)
        self.assertIn("Clonar", sync.apply(plain, self.bare).message)
        rep = sync.inspect(self.pc2, self.bare, branch="no-existe")
        self.assertFalse(rep.remote_exists)
        self.assertIn("no existe", sync.apply(self.pc2, self.bare,
                                              branch="no-existe").message)

    def test_conflict_merge_fails_safely_and_mirror_recovers(self):
        (self.pc1 / "b.txt").unlink()               # borrado en GitHub…
        self.publish_pc1()
        write(self.pc2, "b.txt", "editado\n")       # …editado en local
        r = sync.apply(self.pc2, self.bare, mode="merge")
        self.assertEqual(r.status, "error")
        self.assertIn("Espejo", r.message)
        self.assertEqual(read(self.pc2, "b.txt"), "editado\n")   # intacto
        r2 = sync.apply(self.pc2, self.bare, mode="mirror")
        self.assertEqual(r2.status, "ok", r2.message)
        self.assertEqual(local_files(self.pc2), ["a.txt"])


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class HistoryTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bare = make_bare(self.tmp, "remote")
        self.proj = Path(self.tmp) / "proj"
        write(self.proj, "a.txt", "v1\n")
        self.assertEqual(publish(job_for(self.proj, self.bare,
                                         message="primera")).status, "ok")
        write(self.proj, "a.txt", "v2\n")
        write(self.proj, "c.txt", "C\n")
        self.assertEqual(publish(job_for(self.proj, self.bare,
                                         message="segunda")).status, "ok")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_versions(self):
        vs = history.list_versions(self.proj)
        self.assertEqual([v.subject for v in vs], ["segunda", "primera"])
        self.assertEqual(vs[0].files, 2)
        self.assertEqual((vs[0].insertions, vs[0].deletions), (2, 1))
        self.assertEqual(vs[0].author, "Test")
        self.assertRegex(vs[0].date, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")
        self.assertFalse(vs[0].is_backup)
        self.assertEqual(history.list_versions(Path(self.tmp) / "nada"), [])

    def test_restore_creates_new_commit_and_backup(self):
        first = history.list_versions(self.proj)[1]
        write(self.proj, "d.txt", "sin publicar\n")
        r = history.restore_version(self.proj, first.sha)
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(local_files(self.proj), ["a.txt"])
        self.assertEqual(read(self.proj, "a.txt"), "v1\n")
        vs = history.list_versions(self.proj)
        self.assertTrue(vs[0].subject.startswith("Restaurar versión"))
        # primera, segunda y la restauración: el respaldo NO entra en la
        # rama (antes era un commit más y se publicaba con ella).
        self.assertEqual(len([v for v in vs if not v.is_backup]), 3)
        backups = [v for v in vs if v.is_backup]
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].branch, r.backup_branch)
        self.assertIn("d.txt", git(self.proj, "ls-tree", "-r", "--name-only",
                                   r.backup_branch))
        fuera = subprocess.run(["git", "merge-base", "--is-ancestor",
                                r.backup_branch, "HEAD"], cwd=self.proj)
        self.assertNotEqual(fuera.returncode, 0,
                            "el respaldo forma parte de la rama publicada")
        # Publicar deja GitHub igual que la versión restaurada, sin forzar.
        self.assertEqual(publish(job_for(self.proj, self.bare)).status, "ok")
        self.assertEqual(remote_files(self.bare), ["a.txt"])

    def test_merge_commits_get_a_readable_subject(self):
        other = Path(self.tmp) / "other"
        write(other, "z.txt", "Z\n")
        self.assertEqual(publish(job_for(other, self.bare,
                                         mode="addedit")).status, "ok")
        vs = history.list_versions(other)
        self.assertTrue(vs[0].is_merge)
        self.assertEqual(vs[0].subject, history.MERGE_SUBJECT)
        self.assertFalse(vs[1].is_merge)

    def test_restore_same_version_and_bad_sha(self):
        head = history.list_versions(self.proj)[0]
        r = history.restore_version(self.proj, head.sha)
        self.assertEqual(r.status, "ok")
        self.assertIn("ya estaba", r.message)
        self.assertEqual(history.restore_version(self.proj, "deadbeef").status,
                         "error")

    def test_describe_changes(self):
        s, b = history.describe_changes(["img/a.png", "img/b.png"],
                                        ["src/app.js"], [])
        self.assertEqual(s, "Añade 2 imágenes y actualiza 1 script en img/ "
                            "y src/")
        self.assertEqual(b, "+ img/a.png\n+ img/b.png\n~ src/app.js")
        s, _ = history.describe_changes([], [], ["x.dat"])
        self.assertEqual(s, "Elimina 1 archivo")
        s, b = history.describe_changes([f"f{i}.py" for i in range(25)], [], [])
        self.assertEqual(s, "Añade 25 archivos Python")
        self.assertTrue(b.endswith("… y 5 más"))
        self.assertEqual(history.describe_changes([], [], []), ("", ""))

    def test_auto_message_in_publish(self):
        write(self.proj, "css/site.css", "b{}\n")
        write(self.proj, "css/x.css", "i{}\n")
        r = publish(job_for(self.proj, self.bare, auto_message=True))
        self.assertEqual(r.status, "ok", r.message)
        log = git(self.proj, "log", "-1", "--format=%s%n%b")
        self.assertEqual(log.splitlines()[0], "Añade 2 estilos en css/")
        self.assertIn("+ css/site.css", log)


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class BranchTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bare = make_bare(self.tmp, "remote")
        self.proj = Path(self.tmp) / "proj"
        write(self.proj, "a.txt")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_folder_is_truth_branch_is_destination(self):
        self.assertEqual(publish(job_for(self.proj, self.bare)).status, "ok")
        write(self.proj, "feature.txt")
        r = publish(job_for(self.proj, self.bare, branch="feature"))
        self.assertEqual(r.status, "ok", r.message)
        g = Git()
        self.assertEqual(g.current_branch(self.proj), "feature")
        self.assertEqual(g.local_branches(self.proj), ["feature"])
        self.assertEqual(remote_files(self.bare, "feature"),
                         ["a.txt", "feature.txt"])
        self.assertEqual(remote_files(self.bare, "main"), ["a.txt"])
        self.assertEqual(g.remote_branches(self.bare), ["feature", "main"])
        # De vuelta a main: la carpeta (con feature.txt) es lo que se
        # publica, combinado con el historial de main; nada desaparece
        # de la carpeta del usuario.
        r = publish(job_for(self.proj, self.bare, branch="main"))
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(g.current_branch(self.proj), "main")
        self.assertEqual(local_files(self.proj), ["a.txt", "feature.txt"])
        self.assertEqual(remote_files(self.bare, "main"),
                         ["a.txt", "feature.txt"])
        self.assertEqual(remote_files(self.bare, "feature"),
                         ["a.txt", "feature.txt"])

    def test_fresh_repo_renames_default_branch(self):
        r = publish(job_for(self.proj, self.bare, branch="principal"))
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(Git().local_branches(self.proj), ["principal"])

    def test_remote_branches_bad_url(self):
        with self.assertRaises(Exception):
            Git().remote_branches(str(Path(self.tmp) / "no.git"))


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class ReleaseTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bare = make_bare(self.tmp, "remote")
        self.proj = Path(self.tmp) / "proj"
        write(self.proj, "a.txt", "A\n")
        self.assertEqual(publish(job_for(self.proj, self.bare)).status, "ok")
        self.calls = []

        def fake_api(method, path, token="", payload=None, **kw):
            self.calls.append((method, path, payload))
            return 201, {"id": 77, "html_url": "https://github.com/ana/proj/"
                                                "releases/tag/" + payload["tag_name"]}

        def fake_upload(owner, repo, rid, token, name, data, **kw):
            self.calls.append(("upload", rid, name, len(data)))
            return 201, {}
        self.patches = [mock.patch.object(github, "api", fake_api),
                        mock.patch.object(github, "upload_asset", fake_upload)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_suggest_tag(self):
        self.assertEqual(release.suggest_tag(self.proj), "v1.0.0")
        git(self.proj, "tag", "v1.2.9")
        git(self.proj, "tag", "v1.10.0")
        self.assertEqual(release.suggest_tag(self.proj), "v1.10.1")
        self.assertTrue(release.valid_tag("v1.0.0-beta"))
        self.assertFalse(release.valid_tag("mal nombre"))

    def test_create_release_with_zip(self):
        r = release.create_release(self.proj, self.bare, "ana", "proj", "tok",
                                   "v1.0.0", "Primera", "Notas", True)
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(r.tag, "v1.0.0")
        self.assertEqual(r.asset, "proj-v1.0.0.zip")
        self.assertTrue(r.html_url.endswith("/releases/tag/v1.0.0"))
        self.assertIn("v1.0.0", subprocess.run(["git", "tag"], cwd=self.bare,
                                               capture_output=True,
                                               text=True).stdout)
        self.assertEqual(self.calls[0][:2], ("POST", "/repos/ana/proj/releases"))
        self.assertEqual(self.calls[0][2]["name"], "Primera")
        self.assertEqual(self.calls[1][:3], ("upload", 77, "proj-v1.0.0.zip"))
        self.assertGreater(self.calls[1][3], 100)
        r2 = release.create_release(self.proj, self.bare, "ana", "proj", "tok",
                                    "v1.0.0")
        self.assertEqual(r2.status, "error")
        self.assertIn("ya existe", r2.message)

    def test_release_refuses_unpublished_head(self):
        first = history.list_versions(self.proj)[0]
        write(self.proj, "b.txt", "B\n")
        self.assertEqual(publish(job_for(self.proj, self.bare)).status, "ok")
        r = history.restore_version(self.proj, first.sha)   # commit local
        self.assertEqual(r.status, "ok", r.message)
        r = release.create_release(self.proj, self.bare, "ana", "proj", "tok",
                                   "v3.0.0")
        self.assertEqual(r.status, "error")
        self.assertIn("Publica primero", r.message)
        self.assertEqual(self.calls, [])
        self.assertNotIn("v3.0.0", release.list_tags(self.proj))

    def test_a_tag_that_already_exists_on_github_is_refused_early(self):
        """Creada desde la web o desde otro equipo: no está en tu carpeta,
        pero subirla fallaría con un mensaje confuso."""
        otro = Path(self.tmp) / "otro"
        clone(self.bare, otro)
        git(otro, "tag", "v9.0.0")
        git(otro, "push", "-q", "origin", "refs/tags/v9.0.0")
        r = release.create_release(self.proj, self.bare, "ana", "proj", "tok",
                                   "v9.0.0")
        self.assertEqual(r.status, "error")
        self.assertIn("ya existe en GitHub", r.message)
        self.assertEqual(self.calls, [])

    def test_a_tag_named_like_a_branch_is_refused(self):
        git(self.proj, "branch", "v2.0.0")
        r = release.create_release(self.proj, self.bare, "ana", "proj", "tok",
                                   "v2.0.0")
        self.assertEqual(r.status, "error")
        self.assertIn("RAMA", r.message)
        for mala in ("v1..2", "v1.lock", "v1.", "-v1"):
            self.assertFalse(release.valid_tag(mala), mala)

    def test_a_nested_repository_does_not_block_a_release(self):
        """Un repositorio dentro del proyecto nunca se publica: no puede
        contar para siempre como «cambios sin publicar»."""
        sub = self.proj / "vendor"
        sub.mkdir()
        git(sub, "init", "-q")
        write(sub, "lib.py", "x = 1\n")
        r = release.create_release(self.proj, self.bare, "ana", "proj", "tok",
                                   "v1.0.0", attach_zip=False)
        self.assertEqual(r.status, "ok", r.message)

    def test_release_refuses_dirty_or_bad_tag(self):
        r = release.create_release(self.proj, self.bare, "ana", "proj", "tok",
                                   "v 1")
        self.assertIn("no válido", r.message)
        write(self.proj, "b.txt")
        r = release.create_release(self.proj, self.bare, "ana", "proj", "tok",
                                   "v2.0.0")
        self.assertIn("sin publicar", r.message)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
