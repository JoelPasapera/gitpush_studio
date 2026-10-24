# -*- coding: utf-8 -*-
"""Pruebas con disco y con git de verdad (repositorios «bare» locales
hacen de GitHub: mismo protocolo, sin red)."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.gitenv import HAVE_GIT, init_bare, init_repo

from gitpush_studio.core import scan, walk
from gitpush_studio.core.git import Git
from gitpush_studio.core.pack import consolidate_many, consolidate_project
from gitpush_studio.core.publish import (TOTAL_STEPS, Callbacks,
                                         PreviewChoice, PublishJob, publish,
                                         publish_many)

def write(folder, rel, content="x\n"):
    p = Path(folder) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def make_bare(where, name):
    return init_bare(where, name)


def remote_files(bare, branch="main"):
    p = subprocess.run(["git", "ls-tree", "-r", "--name-only", branch],
                       cwd=bare, capture_output=True, text=True)
    return sorted(x for x in p.stdout.splitlines() if x)


def remote_file(bare, rel, branch="main"):
    p = subprocess.run(["git", "show", f"{branch}:{rel}"], cwd=bare,
                       capture_output=True, text=True)
    return p.stdout


def job_for(folder, bare, **kw):
    base = dict(folder=str(folder), url=bare, user="Test", email="t@x.y")
    base.update(kw)
    return PublishJob(**base)



def fake_token(prefix, n, seed=7):
    """Un token con aspecto de real (variado, sin secuencias), armado al
    ejecutar: así este archivo no lleva ninguno escrito."""
    import random
    rnd = random.Random(seed)
    alfabeto = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return prefix + "".join(rnd.choice(alfabeto) for _ in range(n))

class WalkScanPackTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.proj = Path(self.tmp) / "proj"
        write(self.proj, "src/main.py", "print('hola')\n")
        write(self.proj, "src/util.py", "def f(): pass\n")
        write(self.proj, "node_modules/lib/index.js", "x")
        write(self.proj, "logo.png", "\x89PNG binario")
        write(self.proj, ".gitignore", "node_modules/\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_collect_prunes_ignored_dirs_and_git(self):
        (self.proj / ".git").mkdir()
        write(self.proj, ".git/HEAD", "ref")
        entries, _ = walk.project_entries(self.proj, use_git=False)
        rels = [r for r, _, _ in entries]
        self.assertEqual(rels, [".gitignore", "logo.png", "src/main.py",
                               "src/util.py"])

    def test_render_tree(self):
        tree = walk.render_tree("proj", ["src/main.py", "README.md",
                                         "src/sub/x.txt"])
        self.assertEqual(tree.splitlines(), [
            "proj/", "├── src/", "│   ├── sub/", "│   │   └── x.txt",
            "│   └── main.py", "└── README.md"])

    def test_scan_entries_detects_sizes_names_and_content(self):
        # La clave se arma aquí para que ESTE archivo no contenga una
        # cadena que la propia revisión marcaría al publicar el proyecto.
        clave = "9f2b7c" + "41e8d0a63f"
        write(self.proj, "config.py", 'API_KEY = "' + clave + '"\n')
        write(self.proj, ".env", "SECRET=1\n")
        write(self.proj, ".env.example", "SECRET=\n")
        write(self.proj, "notes.txt", "token ghp_" + "a" * 36 + " listo\n")
        entries, _ = walk.project_entries(self.proj, use_git=False)
        entries.append(("video.mp4", "/no/existe", scan.LIMIT_HARD + 1))
        entries.append(("big.iso", "/no/existe", scan.LIMIT_WARN + 1))
        findings, stats = scan.scan_entries(entries)
        by_path = {f["path"]: f for f in findings}
        self.assertEqual(by_path["video.mp4"]["kind"], "huge")
        self.assertEqual(by_path["big.iso"]["kind"], "big")
        self.assertEqual(by_path[".env"]["kind"], "secret")
        self.assertIn("GitHub", by_path["notes.txt"]["reason"])
        self.assertIn("contraseña", by_path["config.py"]["reason"])
        self.assertNotIn(".env.example", by_path)
        self.assertNotIn("logo.png", by_path)
        self.assertEqual([f["kind"] for f in findings][:2], ["huge", "secret"])
        self.assertGreaterEqual(stats["content"], 3)

    def test_confidence_separates_real_secrets_from_examples(self):
        clave = "9f2b7c" + "41e8d0a63f"
        write(self.proj, "real.py", 'API_KEY = "' + clave + '"\n')
        write(self.proj, "docs.md", 'token = "' + "<tu-token-aqui>" + '"\n')
        write(self.proj, "demo.py", 'password = "' + "aaaaaaaaaaaa" + '"\n')
        write(self.proj, "seq.py", 'secret = "' + "abcdefghijklm" + '"\n')
        write(self.proj, "duro.txt", fake_token("ghp_", 36) + "\n")
        # El relleno de la documentación («ghp_xxxx…») no es un token.
        write(self.proj, "readme.md", "export T=ghp_" + "x" * 36 + "\n")
        write(self.proj, ".env", "X=1\n")
        entries, _ = walk.project_entries(self.proj, use_git=False)
        findings, _ = scan.scan_entries(entries)
        conf = {f["path"]: f["confidence"] for f in findings}
        self.assertEqual(conf["duro.txt"], "alta")     # formato de GitHub
        self.assertEqual(conf[".env"], "alta")         # nombre delator
        self.assertEqual(conf["real.py"], "media")     # heurística general
        for ejemplo in ("docs.md", "demo.py", "seq.py", "readme.md"):
            self.assertEqual(conf[ejemplo], "baja", ejemplo)
            self.assertIn("ejemplo", dict(
                (f["path"], f["reason"]) for f in findings)[ejemplo])
        # Lo más creíble se enseña primero.
        self.assertEqual([f["confidence"] for f in findings][:2],
                         ["alta", "alta"])

    def test_big_files_are_medium_confidence(self):
        entries = [("grande.iso", "/no/existe", scan.LIMIT_WARN + 1),
                   ("enorme.iso", "/no/existe", scan.LIMIT_HARD + 1)]
        findings, _ = scan.scan_entries(entries)
        conf = {f["path"]: f["confidence"] for f in findings}
        self.assertEqual(conf["enorme.iso"], "alta")
        self.assertEqual(conf["grande.iso"], "media")

    def test_consolidate_project_excludes_itself_and_binaries(self):
        out = self.proj / "proj.txt"
        st = consolidate_project(self.proj, out, ["*.png"])
        text = out.read_text(encoding="utf-8")
        self.assertIn("MODULO: src/main.py", text)
        self.assertIn("print('hola')", text)
        self.assertNotIn("proj.txt", text)
        self.assertNotIn("MODULO: node_modules", text)
        self.assertNotIn("MODULO: logo.png", text)
        self.assertEqual(st["files"], 3)          # .gitignore + 2 .py
        st2 = consolidate_project(self.proj, out, [])
        self.assertIn("MODULO: logo.png", out.read_text(encoding="utf-8"))
        self.assertEqual(st2["binaries"], 1)

    def test_consolidate_many_streams_profile(self):
        other = Path(self.tmp) / "otro"
        write(other, "a.txt", "A")
        out = Path(self.tmp) / "perfil.txt"
        st = consolidate_many("ana", [self.proj, other], out)
        text = out.read_text(encoding="utf-8")
        self.assertEqual(st["repos"], 2)
        self.assertIn("PERFIL DE GITHUB: ana", text)
        self.assertIn("REPOSITORIO 1/2: ana/otro", text)
        self.assertIn("REPOSITORIO 2/2: ana/proj", text)
        self.assertLess(text.index("ana/otro"), text.index("ana/proj"))


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class GitSourceOfTruthTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.proj = Path(self.tmp) / "proj"
        write(self.proj, "keep.txt")
        write(self.proj, "Build/out.bin")
        write(self.proj, "sub/.gitignore", "local.log\n")
        write(self.proj, "sub/local.log")
        write(self.proj, "sub/code.py")
        write(self.proj, ".gitignore", "Build/\n")
        init_repo(self.proj)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_files_honours_nested_gitignore(self):
        rels = [r for r, _, _ in Git().list_files(self.proj)]
        self.assertEqual(rels, [".gitignore", "keep.txt", "sub/.gitignore",
                               "sub/code.py"])

    def test_project_entries_prefers_git_and_applies_extra_patterns(self):
        entries, skipped = walk.project_entries(self.proj, ["*.py"])
        self.assertEqual([r for r, _, _ in entries],
                         [".gitignore", "keep.txt", "sub/.gitignore"])
        self.assertEqual(skipped, 1)

    def test_consolidate_inside_repo_excludes_itself(self):
        out = self.proj / "proj.txt"
        st = consolidate_project(self.proj, out)          # fuente: git
        text = out.read_text(encoding="utf-8")
        self.assertNotIn("proj.txt", text)
        self.assertIn("MODULO: sub/code.py", text)
        self.assertNotIn("MODULO: sub/local.log", text)  # .gitignore anidado
        self.assertEqual(st["files"], 4)
        st2 = consolidate_project(self.proj, out)         # ahora existe: sigue fuera
        self.assertEqual(st2["files"], 4)

    def test_is_dirty(self):
        git = Git()
        self.assertTrue(git.is_dirty(self.proj))
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@x",
                        "add", "-A"], cwd=self.proj, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@x",
                        "commit", "-q", "-m", "init"], cwd=self.proj,
                       check=True)
        self.assertFalse(git.is_dirty(self.proj))
        (self.proj / "keep.txt").unlink()
        self.assertTrue(git.is_dirty(self.proj))
        self.assertFalse(git.is_dirty(self.proj, ignore_deletions=True))


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class PublishPipelineTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.proj = Path(self.tmp) / "proj"
        write(self.proj, "a.txt", "A1\n")
        write(self.proj, "b.txt", "B1\n")
        self.bare = make_bare(self.tmp, "remote")
        self.logs = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def callbacks(self, review=("continue", []), preview=True):
        seen = {}

        def ask_review(findings):
            seen["findings"] = findings
            return review

        def ask_preview(added, modified, deleted, mode):
            seen["preview"] = (added, modified, deleted, mode)
            return preview
        cb = Callbacks(log=lambda t, tag="out": self.logs.append((tag, t)),
                       progress=lambda n, tot, lbl: seen.setdefault(
                           "progress", []).append((n, tot)),
                       ask_review=ask_review, ask_preview=ask_preview)
        return cb, seen

    def test_first_publish_mirror_then_update(self):
        cb, seen = self.callbacks()
        r = publish(job_for(self.proj, self.bare), cb)
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(remote_files(self.bare), ["a.txt", "b.txt"])
        self.assertEqual(seen["preview"][:3], (["a.txt", "b.txt"], [], []))
        # El número de pasos lo define STEPS, no esta prueba: lo que se
        # comprueba es que el progreso llega al final sin saltarse ninguno.
        self.assertEqual(seen["progress"][-1], (TOTAL_STEPS, TOTAL_STEPS))
        self.assertEqual([n for n, _ in seen["progress"]],
                         list(range(1, TOTAL_STEPS + 1)))

        # Segunda publicación: modifica a, borra b, añade c.
        write(self.proj, "a.txt", "A2\n")
        (self.proj / "b.txt").unlink()
        write(self.proj, "c.txt", "C\n")
        cb, seen = self.callbacks()
        r = publish(job_for(self.proj, self.bare), cb)
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(seen["preview"][:3], (["c.txt"], ["a.txt"], ["b.txt"]))
        self.assertEqual(remote_files(self.bare), ["a.txt", "c.txt"])
        self.assertEqual(remote_file(self.bare, "a.txt"), "A2\n")
        self.assertFalse(Git().is_dirty(self.proj))

    def test_the_ignore_box_works_on_an_existing_repository(self):
        """Un patrón de carpeta («app») tiene que frenar todo lo que hay
        dentro, aunque ya estuviera rastreado."""
        write(self.proj, "app/uno.md", "1\n")
        write(self.proj, "app/sub/dos.md", "2\n")
        write(self.proj, "bin/tres.exe", "3\n")
        write(self.proj, "notas.md", "4\n")
        self.assertEqual(publish(job_for(self.proj, self.bare)).status, "ok")
        self.assertIn("app/uno.md", remote_files(self.bare))
        cb, seen = self.callbacks()
        r = publish(job_for(self.proj, self.bare,
                            ignore=["app", "bin", "notas.md"]), cb)
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(remote_files(self.bare),
                         [".gitignore", "a.txt", "b.txt"])
        self.assertEqual(seen["preview"][2],           # se borran del remoto
                         ["app/sub/dos.md", "app/uno.md", "bin/tres.exe",
                          "notas.md"])
        for rel in ("app/uno.md", "app/sub/dos.md", "bin/tres.exe",
                    "notas.md"):
            self.assertTrue((self.proj / rel).exists(), rel)

    def test_ignore_patterns_keep_their_shape(self):
        """Las formas habituales: carpeta con y sin barra, comodín y ruta."""
        for patron, dentro, fuera in (
                ("build/", "build/x.js", "builder.js"),
                ("*.log", "logs/app.log", "app.txt"),
                ("src/generado", "src/generado/a.py", "src/a.py")):
            with self.subTest(patron=patron):
                proyecto = Path(self.tmp) / ("p-" + patron.strip("/*."))
                write(proyecto, dentro, "x\n")
                write(proyecto, fuera, "y\n")
                bare = make_bare(self.tmp, "r-" + patron.strip("/*."))
                self.assertEqual(publish(job_for(proyecto, bare)).status, "ok")
                self.assertEqual(publish(job_for(proyecto, bare,
                                                 ignore=[patron])).status,
                                 "ok")
                remotos = remote_files(bare)
                self.assertNotIn(dentro, remotos)
                self.assertIn(fuera, remotos)

    def test_review_ignore_writes_anchored_gitignore_and_untracks(self):
        write(self.proj, "secrets/.env", "TOKEN=1\n")
        cb, seen = self.callbacks(review=("ignore", ["secrets/.env"]))
        r = publish(job_for(self.proj, self.bare, ignore=["*.log"]), cb)
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual([f["path"] for f in seen["findings"]],
                         ["secrets/.env"])
        gi = (self.proj / ".gitignore").read_text(encoding="utf-8")
        self.assertEqual(gi.splitlines(), ["*.log", "/secrets/.env"])
        self.assertEqual(remote_files(self.bare), [".gitignore", "a.txt",
                                                   "b.txt"])

    def test_preview_keep_restores_files_to_folder(self):
        self.assertEqual(publish(job_for(self.proj, self.bare)).status, "ok")
        (self.proj / "b.txt").unlink()
        write(self.proj, "a.txt", "A2\n")
        cb, seen = self.callbacks(preview=(True, ["b.txt"], True))
        r = publish(job_for(self.proj, self.bare), cb)
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(seen["preview"][2], ["b.txt"])
        self.assertEqual(remote_files(self.bare), ["a.txt", "b.txt"])
        self.assertEqual(remote_file(self.bare, "a.txt"), "A2\n")
        self.assertEqual((self.proj / "b.txt").read_text("utf-8"), "B1\n")
        self.assertFalse(Git().is_dirty(self.proj))
        # La siguiente publicación ya no propone borrarlo.
        cb, seen = self.callbacks()
        publish(job_for(self.proj, self.bare), cb)
        self.assertNotIn("preview", seen)

    def test_preview_keep_index_only_asks_again_next_time(self):
        self.assertEqual(publish(job_for(self.proj, self.bare)).status, "ok")
        (self.proj / "b.txt").unlink()
        cb, _ = self.callbacks(preview=(True, ["b.txt", "no-existe"], False))
        r = publish(job_for(self.proj, self.bare), cb)
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(remote_files(self.bare), ["a.txt", "b.txt"])
        self.assertFalse((self.proj / "b.txt").exists())
        cb, seen = self.callbacks(preview=True)     # ahora sí se borra
        publish(job_for(self.proj, self.bare), cb)
        self.assertEqual(seen["preview"][2], ["b.txt"])
        self.assertEqual(remote_files(self.bare), ["a.txt"])

    def test_preview_keep_on_first_publish_from_fresh_folder(self):
        self.assertEqual(publish(job_for(self.proj, self.bare)).status, "ok")
        fresh = Path(self.tmp) / "fresh"            # sin .git, sin b.txt
        write(fresh, "a.txt", "otro\n")
        cb, seen = self.callbacks(preview=(True, ["b.txt"], True))
        r = publish(job_for(fresh, self.bare), cb)
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(remote_files(self.bare), ["a.txt", "b.txt"])
        self.assertEqual((fresh / "b.txt").read_text("utf-8"), "B1\n")

    def test_keeping_an_ignored_file_never_overwrites_the_local_copy(self):
        """El archivo está en GitHub y también en la carpeta, pero
        ignorado (así no se sube). Al mantenerlo, su contenido local debe
        quedar intacto: no está rastreado, así que machacarlo sería una
        pérdida irrecuperable."""
        write(self.proj, "secreto.env", "DE-GITHUB\n")
        self.assertEqual(publish(job_for(self.proj, self.bare)).status, "ok")
        otro = Path(self.tmp) / "otro-pc"          # carpeta sin .git
        write(otro, "a.txt", "A1\n")
        write(otro, "b.txt", "B1\n")
        write(otro, "secreto.env", "MIO-LOCAL\n")
        cb, seen = self.callbacks(preview=PreviewChoice(
            keep_deleted=["secreto.env"], restore_to_folder=True))
        r = publish(job_for(otro, self.bare, ignore=["secreto.env"]), cb)
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(seen["preview"][2], ["secreto.env"])
        self.assertEqual((otro / "secreto.env").read_text("utf-8"),
                         "MIO-LOCAL\n")                      # no se machaca
        self.assertEqual(remote_file(self.bare, "secreto.env"), "DE-GITHUB\n")

    def test_keeping_a_missing_file_does_restore_it(self):
        self.assertEqual(publish(job_for(self.proj, self.bare)).status, "ok")
        (self.proj / "b.txt").unlink()             # ya no está en la carpeta
        cb, _ = self.callbacks(preview=PreviewChoice(
            keep_deleted=["b.txt"], restore_to_folder=True))
        self.assertEqual(publish(job_for(self.proj, self.bare), cb).status,
                         "ok")
        self.assertEqual((self.proj / "b.txt").read_text("utf-8"), "B1\n")

    def test_preview_skip_added_with_gitignore(self):
        self.assertEqual(publish(job_for(self.proj, self.bare)).status, "ok")
        write(self.proj, "borrador.txt", "no subir\n")
        write(self.proj, "listo.txt", "sí\n")
        cb, seen = self.callbacks(preview=PreviewChoice(
            skip_added=["borrador.txt", "no-existe.txt"]))
        r = publish(job_for(self.proj, self.bare), cb)
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(seen["preview"][0], ["borrador.txt", "listo.txt"])
        self.assertEqual(remote_files(self.bare),
                         [".gitignore", "a.txt", "b.txt", "listo.txt"])
        self.assertTrue((self.proj / "borrador.txt").exists())
        self.assertEqual((self.proj / ".gitignore").read_text("utf-8"),
                         "/borrador.txt\n")
        self.assertFalse(Git().is_dirty(self.proj))
        # Ya no vuelve a proponerlo.
        write(self.proj, "otro.txt", "o\n")
        cb, seen = self.callbacks()
        publish(job_for(self.proj, self.bare), cb)
        self.assertEqual(seen["preview"][0], ["otro.txt"])

    def test_preview_skip_added_without_gitignore_asks_again(self):
        self.assertEqual(publish(job_for(self.proj, self.bare)).status, "ok")
        write(self.proj, "borrador.txt", "no subir\n")
        cb, _ = self.callbacks(preview=PreviewChoice(
            skip_added=["borrador.txt"], ignore_skipped=False))
        self.assertEqual(publish(job_for(self.proj, self.bare), cb).status,
                         "ok")
        self.assertEqual(remote_files(self.bare), ["a.txt", "b.txt"])
        self.assertFalse((self.proj / ".gitignore").exists())
        cb, seen = self.callbacks()
        publish(job_for(self.proj, self.bare), cb)
        self.assertEqual(seen["preview"][0], ["borrador.txt"])

    def test_preview_skip_modified_keeps_local_file(self):
        self.assertEqual(publish(job_for(self.proj, self.bare)).status, "ok")
        write(self.proj, "a.txt", "A-local\n")
        write(self.proj, "b.txt", "B-nuevo\n")
        cb, seen = self.callbacks(preview=PreviewChoice(
            skip_modified=["a.txt", "b.txt-inventado"]))
        r = publish(job_for(self.proj, self.bare), cb)
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(seen["preview"][1], ["a.txt", "b.txt"])
        self.assertEqual(remote_file(self.bare, "a.txt"), "A1\n")   # intacto
        self.assertEqual(remote_file(self.bare, "b.txt"), "B-nuevo\n")
        self.assertEqual((self.proj / "a.txt").read_text("utf-8"), "A-local\n")
        # Sigue siendo distinto: la próxima vez vuelve a aparecer.
        cb, seen = self.callbacks()
        publish(job_for(self.proj, self.bare), cb)
        self.assertEqual(seen["preview"][1], ["a.txt"])

    def test_preview_skip_everything_publishes_nothing_new(self):
        self.assertEqual(publish(job_for(self.proj, self.bare)).status, "ok")
        write(self.proj, "a.txt", "A2\n")
        write(self.proj, "nuevo.txt", "N\n")
        (self.proj / "b.txt").unlink()
        before = remote_files(self.bare)
        cb, _ = self.callbacks(preview=PreviewChoice(
            keep_deleted=["b.txt"], skip_added=["nuevo.txt"],
            skip_modified=["a.txt"]))
        r = publish(job_for(self.proj, self.bare), cb)
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(remote_files(self.bare), sorted(before + [".gitignore"]))
        self.assertEqual(remote_file(self.bare, "a.txt"), "A1\n")
        self.assertTrue((self.proj / "nuevo.txt").exists())
        self.assertEqual((self.proj / "b.txt").read_text("utf-8"), "B1\n")

    def test_addedit_keeps_remote_only_files(self):
        self.assertEqual(publish(job_for(self.proj, self.bare)).status, "ok")
        fresh = Path(self.tmp) / "fresh"        # otra carpeta, sin .git
        write(fresh, "a.txt", "A-nuevo\n")
        write(fresh, "c.txt", "C\n")
        cb, seen = self.callbacks()
        r = publish(job_for(fresh, self.bare, mode="addedit"), cb)
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(seen["preview"][2], [])            # nada «a borrar»
        self.assertEqual(remote_files(self.bare), ["a.txt", "b.txt", "c.txt"])
        self.assertEqual(remote_file(self.bare, "a.txt"), "A-nuevo\n")

    def test_mirror_over_unrelated_history_replaces_tree_keeps_history(self):
        self.assertEqual(publish(job_for(self.proj, self.bare)).status, "ok")
        fresh = Path(self.tmp) / "fresh"
        write(fresh, "solo.txt", "S\n")
        r = publish(job_for(fresh, self.bare))
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(remote_files(self.bare), ["solo.txt"])
        log = subprocess.run(["git", "log", "--oneline", "main"],
                             cwd=self.bare, capture_output=True, text=True)
        self.assertGreaterEqual(len(log.stdout.splitlines()), 3)

    def test_preview_rejected_cancels_without_pushing(self):
        cb, _ = self.callbacks(preview=False)
        r = publish(job_for(self.proj, self.bare), cb)
        self.assertEqual(r.status, "cancelled")
        self.assertEqual(remote_files(self.bare), [])

    def test_auto_mode_ignores_only_dubious_findings(self):
        write(self.proj, "ejemplo.py", 'api_key = "' + "<pon-el-tuyo>" + '"\n')
        cb, seen = self.callbacks()
        r = publish(job_for(self.proj, self.bare, auto=True), cb)
        self.assertEqual(r.status, "ok", r.message)
        self.assertIn("ejemplo.py", remote_files(self.bare))
        self.assertTrue(any("poco probables" in t for _, t in self.logs))
        # Pero un secreto creíble sí la detiene.
        write(self.proj, "de-verdad.txt", fake_token("ghp_", 36) + "\n")
        r = publish(job_for(self.proj, self.bare, auto=True), cb)
        self.assertEqual(r.status, "blocked")
        self.assertNotIn("de-verdad.txt", remote_files(self.bare))

    def test_auto_mode_is_blocked_by_findings(self):
        write(self.proj, ".env", "X=1\n")
        cb, seen = self.callbacks()
        r = publish(job_for(self.proj, self.bare, auto=True), cb)
        self.assertEqual(r.status, "blocked")
        self.assertNotIn("findings", seen)       # nunca abre la revisión
        self.assertEqual(remote_files(self.bare), [])

    def test_empty_project_explains_nothing_to_publish(self):
        empty = Path(self.tmp) / "vacio"
        empty.mkdir()
        r = publish(job_for(empty, self.bare, ignore=[".gitignore"]))
        self.assertEqual(r.status, "error")
        self.assertIn("nada que publicar", r.message)

    def test_validation_errors_do_not_touch_git(self):
        r = publish(PublishJob(folder=str(self.proj), url=""))
        self.assertEqual(r.status, "error")
        self.assertFalse((self.proj / ".git").exists())
        r = publish(PublishJob(folder=str(self.proj), url=self.bare,
                               user="solo-nombre"))
        self.assertIn("correo", r.message)

    def test_publish_many_runs_projects_concurrently(self):
        jobs, bares = [], []
        for i in range(3):
            p = Path(self.tmp) / f"p{i}"
            write(p, "f.txt", f"P{i}\n")
            b = make_bare(self.tmp, f"r{i}")
            jobs.append(job_for(p, b, name=f"p{i}"))
            bares.append(b)
        # Dos trabajos al MISMO remoto: deben ir en serie, sin pisarse.
        p3 = Path(self.tmp) / "p3"
        write(p3, "extra.txt", "E\n")
        jobs.append(job_for(p3, bares[0], name="p3", mode="addedit"))

        seen_labels = []
        results = publish_many(
            jobs, callbacks_for=lambda j: Callbacks(
                log=lambda t, tag="out", j=j: seen_labels.append(j.label)),
            workers=4)
        self.assertEqual([r.status for r in results], ["ok"] * 4,
                         [r.message for r in results])
        self.assertEqual([r.job.label for r in results], ["p0", "p1", "p2",
                                                          "p3"])
        self.assertEqual(remote_files(bares[1]), ["f.txt"])
        self.assertEqual(remote_files(bares[0]), ["extra.txt", "f.txt"])
        self.assertTrue(set(seen_labels) >= {"p0", "p1", "p2", "p3"})

    def test_from_state_matches_ui_dict(self):
        s = {"folder": str(self.proj), "url": self.bare, "branch": "",
             "ignore": "node_modules/\n\n*.log\n", "mode": "addedit",
             "message": "", "stamp": True}
        job = PublishJob.from_state(s, name="perfil")
        self.assertEqual(job.branch, "main")
        self.assertEqual(job.ignore, ["node_modules/", "*.log"])
        self.assertTrue(job.addedit)
        self.assertEqual(job.label, "perfil")
        self.assertIn("Actualizar proyecto completo —", job.commit_message())


if __name__ == "__main__":
    unittest.main()
