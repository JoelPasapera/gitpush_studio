# -*- coding: utf-8 -*-
"""Aplicar un TXT de vuelta al proyecto: lo que NUNCA puede pasar.

Trabajas con información sensible, así que aquí se defiende sobre todo
lo que no debe ocurrir: escribir fuera de la carpeta, tocar archivos que
nadie pidió cambiar, cambiar bytes de líneas que no cambiaron, borrar o
vaciar algo, aplicar a ciegas lo que una IA resumió con «…» o escribir
sin una copia exacta previa. Muchas pruebas comparan la huella de TODA
la carpeta (y de lo que hay alrededor) antes y después.
"""

import hashlib
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Dict

from tests.gitenv import HAVE_GIT, commit_all, git, init_repo

from gitpush_studio.core.apply_txt import (STATUS_MODIFIED, STATUS_NEW,
                                           STATUS_REJECTED, STATUS_SAME,
                                           apply_plan, plan_apply,
                                           split_safe_path)
from gitpush_studio.core.errors import StepError
from gitpush_studio.core.pack import consolidate_project
from gitpush_studio.core.txtformat import DASH_LINE, EQUALS_LINE


def module(path: str, body: str) -> str:
    """Un módulo en el formato exacto de la app."""
    return f"MODULO: {path}\n{DASH_LINE}\n{body}\n\n{EQUALS_LINE}\n\n"


def fenced(path: str, body: str, info: str = "") -> str:
    """Un módulo como lo suele devolver una IA."""
    return f"MODULO: {path}\n```{info}\n{body}\n```\n\n"


def fingerprint(root: Path) -> Dict[str, str]:
    """Huella de todo lo que hay bajo `root` (contenido, tipo y permisos)."""
    out: Dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(str(root)):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            info = os.lstat(str(path))
            if stat.S_ISLNK(info.st_mode):
                out[rel] = "enlace→" + os.readlink(str(path))
            elif stat.S_ISDIR(info.st_mode):
                out[rel] = "carpeta"
            else:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                out[rel] = f"{digest}:{stat.S_IMODE(info.st_mode):o}"
    return out


class ApplyTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "proyecto"
        self.project.mkdir()
        self.backups = self.tmp / "respaldos"
        # Algo junto al proyecto: nada de lo que se haga dentro puede
        # tocarlo.
        self.neighbour = self.tmp / "vecino.txt"
        self.neighbour.write_bytes(b"no me toques\n")

    def put(self, rel: str, data: bytes) -> Path:
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def plan(self, text: str):
        return plan_apply(str(self.project), text)

    def apply(self, plan, paths=None):
        selected = plan.default_selection() if paths is None else paths
        return apply_plan(plan, selected, backup_root=self.backups)

    def change(self, plan, path: str):
        for item in plan.changes:
            if item.path == path:
                return item
        self.fail(f"no está {path}: {[c.path for c in plan.changes]}")


class PathSafetyTests(ApplyTestCase):

    def assert_nothing_written(self, text: str):
        before_project = fingerprint(self.project)
        before_around = fingerprint(self.tmp)
        plan = self.plan(text)
        for item in plan.changes:
            self.assertEqual(item.status, STATUS_REJECTED, item)
        self.apply(plan, [c.path for c in plan.changes])
        self.assertEqual(fingerprint(self.project), before_project)
        self.assertEqual(fingerprint(self.tmp), before_around)
        return plan

    def test_absolute_paths_are_rejected(self):
        plan = self.assert_nothing_written(
            module(str(self.neighbour), "pisado")
            + module("C:\\Windows\\x.txt", "x")
            + module("\\\\servidor\\compartido\\x.txt", "x")
            + module("~/x.txt", "x"))
        self.assertIn("absoluta", plan.changes[0].reason)

    def test_going_up_or_into_git_is_rejected(self):
        self.put(".git/config", b"[core]\n")
        plan = self.assert_nothing_written(
            module("../vecino.txt", "pisado")
            + module("src/../../vecino.txt", "pisado")
            + module(".git/config", "[core]\nhooksPath=/tmp\n")
            + module("sub/.GIT/hooks/pre-commit", "rm -rf /"))
        reasons = " ".join(c.reason for c in plan.changes)
        self.assertIn("..", reasons)
        self.assertIn(".git", reasons)

    @unittest.skipIf(os.name == "nt", "enlaces simbólicos")
    def test_links_on_the_way_are_never_followed(self):
        outside = self.tmp / "fuera"
        outside.mkdir()
        (outside / "secreto.env").write_bytes(b"CLAVE=1\n")
        os.symlink(str(outside), str(self.project / "atajo"))
        self.put("real/dentro.txt", b"dentro\n")
        os.symlink("real", str(self.project / "alias"))
        os.symlink("real/dentro.txt", str(self.project / "enlace.txt"))
        plan = self.assert_nothing_written(
            module("atajo/secreto.env", "CLAVE=robada")
            + module("atajo/nuevo.txt", "x")
            + module("alias/dentro.txt", "cambiado")
            + module("enlace.txt", "cambiado"))
        for item in plan.changes:
            self.assertIn("enlace", item.reason)
        self.assertEqual((outside / "secreto.env").read_bytes(), b"CLAVE=1\n")

    def test_a_folder_or_a_file_in_the_way_is_rejected(self):
        self.put("carpeta/a.txt", b"a\n")
        self.put("archivo.txt", b"x\n")
        plan = self.assert_nothing_written(
            module("carpeta", "no soy carpeta")
            + module("archivo.txt/dentro.txt", "x"))
        self.assertIn("carpeta", plan.changes[0].reason)
        self.assertIn("no una carpeta", plan.changes[1].reason)

    def test_names_that_only_differ_in_case_are_not_duplicated(self):
        self.put("Readme.md", b"hola\n")
        plan = self.assert_nothing_written(module("README.md", "otro"))
        self.assertIn("mayúsculas", plan.changes[0].reason)

    def test_names_windows_cannot_create_are_rejected(self):
        plan = self.assert_nothing_written(
            module("con.txt", "x") + module("nota:secreta.txt", "x")
            + module("acaba en punto.", "x") + module("aux", "x"))
        self.assertEqual(len(plan.changes), 4)

    def test_split_safe_path(self):
        self.assertEqual(split_safe_path("./src//a.py"), (["src", "a.py"], ""))
        self.assertEqual(split_safe_path("src\\win\\a.py")[0],
                         ["src", "win", "a.py"])
        for bad in ("", "/", "..", "a/../b", "/etc/x", "D:/x", ".git",
                    "a/.git/b", "a\0b", "a\nb"):
            parts, reason = split_safe_path(bad)
            self.assertEqual(parts, [], bad)
            self.assertTrue(reason, bad)


class PreservationTests(ApplyTestCase):
    """Las líneas que no cambian conservan sus bytes exactos."""

    def apply_one(self, rel: str, original: bytes, txt_body: str) -> bytes:
        self.put(rel, original)
        plan = self.plan(fenced(rel, txt_body))
        item = self.change(plan, rel)
        self.assertEqual(item.status, STATUS_MODIFIED, item.reason)
        result = self.apply(plan)
        self.assertEqual(result.written, [rel])
        return (self.project / rel).read_bytes()

    def test_crlf_trailing_spaces_and_blank_tail_survive(self):
        original = (b"def f():  \r\n    return 1\r\n\r\n"
                    b"def g():\r\n    return 2\r\n\r\n\r\n")
        result = self.apply_one("a.py", original,
                                "def f():\n    return 1\n\ndef g():\n"
                                "    return 3")
        self.assertEqual(result, original.replace(b"return 2", b"return 3"))

    def test_lf_files_stay_lf(self):
        original = b"uno\ndos\ntres\n"
        result = self.apply_one("a.txt", original, "uno\nDOS\ntres\ncuatro")
        self.assertEqual(result, b"uno\nDOS\ntres\ncuatro\n")

    def test_utf8_bom_is_kept(self):
        original = b"\xef\xbb\xbfclave=1\notra=2\n"
        result = self.apply_one("conf.ini", original, "clave=1\notra=3")
        self.assertEqual(result, b"\xef\xbb\xbfclave=1\notra=3\n")

    def test_utf16_stays_utf16(self):
        original = "\ufeffuno\r\ndos\r\n".encode("utf-16-le")
        result = self.apply_one("ps.txt", original, "uno\ndos cambiado")
        self.assertEqual(result,
                         "\ufeffuno\r\ndos cambiado\r\n".encode("utf-16-le"))

    def test_ansi_stays_ansi(self):
        original = "café\naño\n".encode("cp1252")
        result = self.apply_one("viejo.txt", original, "café\naño\nmañana")
        self.assertEqual(result, "café\naño\nmañana\n".encode("cp1252"))

    def test_text_that_does_not_fit_in_ansi_is_rejected(self):
        self.put("viejo.txt", "café\n".encode("cp1252"))
        plan = self.plan(fenced("viejo.txt", "café 🚀"))
        item = self.change(plan, "viejo.txt")
        self.assertEqual(item.status, STATUS_REJECTED)
        self.assertIn("codificación", item.reason)

    def test_broken_bytes_on_untouched_lines_are_kept(self):
        original = b"ni\xc3\xb1o\nroto \xff\xfe aqu\xc3\xad\nfin\n"
        result = self.apply_one("mixto.txt", original,
                                "niño\nroto \ufffd\ufffd aquí\nfinal")
        self.assertEqual(result, b"ni\xc3\xb1o\nroto \xff\xfe aqu\xc3\xad\n"
                                 b"final\n")

    def test_a_file_without_final_newline_keeps_that(self):
        result = self.apply_one("a.txt", b"uno\ndos", "uno\ndos\ntres")
        self.assertEqual(result, b"uno\ndos\ntres")
        result = self.apply_one("b.txt", b"solo", "cambiado")
        self.assertEqual(result, b"cambiado")

    def test_executable_bit_is_kept(self):
        if os.name == "nt":
            self.skipTest("permisos POSIX")
        path = self.put("run.sh", b"#!/bin/sh\necho 1\n")
        os.chmod(str(path), 0o755)
        self.apply_one("run.sh", b"#!/bin/sh\necho 1\n",
                       "#!/bin/sh\necho 2")
        self.assertEqual(stat.S_IMODE(os.stat(str(path)).st_mode), 0o755)

    def test_whitespace_only_differences_change_nothing(self):
        self.put("a.py", b"x = 1   \r\ny = 2\t\r\n")
        before = fingerprint(self.project)
        plan = self.plan(fenced("a.py", "x = 1\ny = 2"))
        self.assertEqual(self.change(plan, "a.py").status, STATUS_SAME)
        self.apply(plan, ["a.py"])
        self.assertEqual(fingerprint(self.project), before)

    def test_a_markdown_file_that_really_starts_with_a_fence(self):
        original = b"```\ncodigo\n```\n"
        self.put("snippet.md", original)
        plan = self.plan(fenced("snippet.md", "codigo"))
        self.assertEqual(self.change(plan, "snippet.md").status, STATUS_SAME)

    def test_only_the_intended_files_change(self):
        self.put("a.txt", b"a\n")
        self.put("b.txt", b"b\n")
        self.put("c/d.txt", b"d\n")
        before = fingerprint(self.project)
        plan = self.plan(fenced("a.txt", "A") + fenced("b.txt", "B"))
        self.apply(plan, ["a.txt"])
        after = fingerprint(self.project)
        changed = sorted(k for k in after if after.get(k) != before.get(k))
        self.assertEqual(changed, ["a.txt"])
        self.assertEqual(self.neighbour.read_bytes(), b"no me toques\n")


class RejectionTests(ApplyTestCase):

    def test_notes_are_never_applied(self):
        self.put("logo.png", b"\x89PNG\r\n\x1a\n\x00")
        self.put("viejo.txt", b"sigo aqui\n")
        before = fingerprint(self.project)
        plan = self.plan(module("logo.png", "<ARCHIVO BINARIO OMITIDO (9 B)>")
                         + module("viejo.txt", "<BORRADO desde la última "
                                               "publicación>"))
        for item in plan.changes:
            self.assertEqual(item.status, STATUS_REJECTED)
        self.apply(plan, [c.path for c in plan.changes])
        self.assertEqual(fingerprint(self.project), before)

    def test_incomplete_contents_are_rejected(self):
        self.put("log.txt", b"uno\ndos\ntres\n")
        plan = self.plan(module("log.txt", "uno\n<ARCHIVO CORTADO: creció "
                                           "mientras se leía; aquí está tal "
                                           "como era al abrirlo (8 B)>"))
        item = self.change(plan, "log.txt")
        self.assertEqual(item.status, STATUS_REJECTED)
        self.assertIn("entero", item.reason)

    def test_an_empty_body_never_empties_a_file(self):
        self.put("a.txt", b"contenido valioso\n")
        plan = self.plan(module("a.txt", ""))
        self.assertEqual(self.change(plan, "a.txt").status, STATUS_REJECTED)

    def test_an_empty_new_file_can_be_created(self):
        plan = self.plan(module("pkg/__init__.py", ""))
        self.assertEqual(self.change(plan, "pkg/__init__.py").status,
                         STATUS_NEW)
        self.apply(plan)
        self.assertEqual((self.project / "pkg/__init__.py").read_bytes(), b"")

    def test_the_same_path_twice_is_rejected(self):
        self.put("a.txt", b"a\n")
        plan = self.plan(fenced("a.txt", "uno") + fenced("./a.txt", "dos"))
        for item in plan.changes:
            self.assertEqual(item.status, STATUS_REJECTED)
            self.assertIn("veces", item.reason)

    def test_binary_originals_are_rejected(self):
        self.put("datos.bin", b"\x00\x01\x02\x03" * 100)
        plan = self.plan(fenced("datos.bin", "texto"))
        self.assertIn("binario", self.change(plan, "datos.bin").reason)

    @unittest.skipIf(os.name == "nt", "enlaces duros")
    def test_hard_links_are_rejected(self):
        path = self.put("a.txt", b"a\n")
        os.link(str(path), str(self.tmp / "otro-nombre.txt"))
        plan = self.plan(fenced("a.txt", "b"))
        self.assertIn("enlaces duros", self.change(plan, "a.txt").reason)

    def test_the_app_txt_is_not_a_target(self):
        self.put("x.py", b"x = 1\n")
        consolidate_project(self.project, self.project / "proyecto.txt",
                            use_git=False)
        plan = self.plan(fenced("proyecto.txt", "pisado"))
        self.assertEqual(self.change(plan, "proyecto.txt").status,
                         STATUS_REJECTED)


class SuspicionTests(ApplyTestCase):

    def test_a_summarised_block_is_not_selected_by_default(self):
        body = "\n".join(f"    paso_{i}()" for i in range(20))
        self.put("a.py", f"def main():\n{body}\n    return 0\n".encode())
        plan = self.plan(fenced("a.py", "def main():\n    # ... resto del "
                                        "código igual ...\n    return 1"))
        item = self.change(plan, "a.py")
        self.assertEqual(item.status, STATUS_MODIFIED)
        self.assertTrue(item.suspicious)
        self.assertNotIn("a.py", plan.default_selection())

    def test_a_cut_answer_is_suspicious(self):
        lines = [f"linea {i}" for i in range(40)]
        self.put("a.txt", ("\n".join(lines) + "\n").encode())
        plan = self.plan(fenced("a.txt", "\n".join(lines[:15])))
        item = self.change(plan, "a.txt")
        self.assertTrue(item.suspicious)
        self.assertIn("cortada", item.warnings[0])

    def test_a_python_stub_is_not_suspicious(self):
        self.put("tipos.py", b"class A:\n    pass\n")
        plan = self.plan(fenced("tipos.py", "class A:\n    pass\n\n"
                                            "class B(Protocol):\n"
                                            "    def f(self): ...\n    ..."))
        self.assertFalse(self.change(plan, "tipos.py").suspicious)


class ApplyMechanicsTests(ApplyTestCase):

    def test_a_copy_is_made_before_writing_outside_git(self):
        self.put("a.txt", b"original exacto\r\n")
        plan = self.plan(fenced("a.txt", "cambiado"))
        result = self.apply(plan)
        self.assertEqual(result.backup_kind, "carpeta")
        copy = Path(result.backup) / "a.txt"
        self.assertEqual(copy.read_bytes(), b"original exacto\r\n")
        self.assertTrue((Path(result.backup) / "LEEME.txt").is_file())
        self.assertFalse(str(result.backup).startswith(str(self.project)))

    @unittest.skipUnless(HAVE_GIT, "git no disponible")
    def test_in_a_repository_the_copy_is_a_backup_branch(self):
        init_repo(self.project)
        self.put(".gitignore", b".env\n")
        self.put("app.py", b"print(1)\n")
        commit_all(self.project)
        self.put(".env", b"CLAVE=secreta\n")
        plan = self.plan(fenced("app.py", "print(2)")
                         + fenced(".env", "CLAVE=secreta\nOTRA=1"))
        result = self.apply(plan)
        self.assertEqual(result.backup_kind, "rama")
        self.assertTrue(result.backup.startswith("respaldo-"))
        self.assertEqual(git(self.project, "show",
                             f"{result.backup}:app.py"), "print(1)\n")
        self.assertEqual(git(self.project, "show", f"{result.backup}:.env"),
                         "CLAVE=secreta\n")
        self.assertEqual(git(self.project, "status", "--porcelain"),
                         " M app.py\n")

    def test_a_file_edited_after_reviewing_stops_everything(self):
        self.put("a.txt", b"uno\n")
        self.put("b.txt", b"dos\n")
        plan = self.plan(fenced("a.txt", "UNO") + fenced("b.txt", "DOS"))
        self.put("b.txt", b"dos, editado a mano\n")
        before = fingerprint(self.project)
        with self.assertRaises(StepError):
            self.apply(plan)
        self.assertEqual(fingerprint(self.project), before)
        self.assertFalse(self.backups.exists())

    def test_a_file_that_appears_where_a_new_one_goes(self):
        plan = self.plan(fenced("nuevo.txt", "mío"))
        self.put("nuevo.txt", b"lo puso otro programa\n")
        with self.assertRaises(StepError):
            self.apply(plan)
        self.assertEqual((self.project / "nuevo.txt").read_bytes(),
                         b"lo puso otro programa\n")

    @unittest.skipIf(os.name == "nt", "enlaces simbólicos")
    def test_a_link_planted_after_reviewing_stops_everything(self):
        self.put("sub/a.txt", b"a\n")
        plan = self.plan(fenced("sub/a.txt", "A") + fenced("sub/b.txt", "B"))
        outside = self.tmp / "fuera"
        outside.mkdir()
        (outside / "a.txt").write_bytes(b"a\n")
        shutil.rmtree(str(self.project / "sub"))
        os.symlink(str(outside), str(self.project / "sub"))
        with self.assertRaises(StepError):
            self.apply(plan)
        self.assertEqual((outside / "a.txt").read_bytes(), b"a\n")
        self.assertFalse((outside / "b.txt").exists())

    def test_new_files_and_folders_are_created(self):
        plan = self.plan(fenced("nuevo/dir/x.py", "print('x')"))
        result = self.apply(plan)
        self.assertEqual(result.created, ["nuevo/dir/x.py"])
        self.assertEqual((self.project / "nuevo/dir/x.py").read_bytes(),
                         b"print('x')\n")

    def test_nothing_selected_writes_nothing(self):
        self.put("a.txt", b"a\n")
        before = fingerprint(self.project)
        plan = self.plan(fenced("a.txt", "b"))
        result = self.apply(plan, [])
        self.assertEqual(result.written, [])
        self.assertEqual(fingerprint(self.project), before)
        self.assertFalse(self.backups.exists())


class RoundTripTests(ApplyTestCase):
    """El TXT de la app, devuelto tal cual o con cambios."""

    def build_project(self):
        self.put("src/main.py", b"import os\n\ndef main():\n    return 0\n")
        self.put("win.txt", b"linea\r\notra\r\n")
        self.put("bom.txt", b"\xef\xbb\xbfcon bom\n")
        self.put("ansi.txt", "año\n".encode("cp1252"))
        self.put("utf16.txt", "\ufeffhola\r\n".encode("utf-16-le"))
        self.put("vacio.txt", b"")
        self.put("blancos.txt", b"x  \n\n\n")
        self.put("img.png", b"\x89PNG\r\n\x1a\n\x00\x00")

    def txt(self) -> str:
        """El TXT tal cual, con sus «\r» (sin la conversión de saltos de
        línea de `read_text`)."""
        out = self.tmp / "salida.txt"
        consolidate_project(self.project, out, use_git=False)
        return out.read_bytes().decode("utf-8")

    def test_the_unchanged_txt_changes_nothing(self):
        self.build_project()
        before = fingerprint(self.project)
        plan = self.plan(self.txt())
        for item in plan.changes:
            self.assertIn(item.status, (STATUS_SAME, STATUS_REJECTED),
                          (item.path, item.status, item.reason))
        self.assertEqual(self.change(plan, "img.png").status, STATUS_REJECTED)
        self.apply(plan, [c.path for c in plan.changes])
        self.assertEqual(fingerprint(self.project), before)

    def test_an_edited_txt_changes_only_what_was_edited(self):
        self.build_project()
        before = fingerprint(self.project)
        text = self.txt().replace("    return 0", "    return 42")
        self.assertIn("linea\r\notra\n", text)
        text = text.replace("linea\r\notra\n", "linea\r\notra cambiada\n")
        plan = self.plan(text)
        modified = sorted(c.path for c in plan.changes
                          if c.status == STATUS_MODIFIED)
        self.assertEqual(modified, ["src/main.py", "win.txt"])
        self.apply(plan)
        after = fingerprint(self.project)
        changed = sorted(k for k in after if after[k] != before.get(k))
        self.assertEqual(changed, ["src/main.py", "win.txt"])
        self.assertEqual((self.project / "win.txt").read_bytes(),
                         b"linea\r\notra cambiada\r\n")


class ProfileTests(ApplyTestCase):

    def test_each_module_goes_to_its_repository(self):
        from gitpush_studio.core.pack import consolidate_many
        for name in ("uno", "dos"):
            self.put(f"{name}/app.py", f"print('{name}')\n".encode())
        out = self.tmp / "perfil.txt"
        consolidate_many("ana", [self.project / "uno", self.project / "dos"],
                         out, use_git=False)
        text = out.read_text(encoding="utf-8").replace("print('dos')",
                                                       "print('DOS')")
        plan = self.plan(text)
        self.assertEqual(plan.kind, "perfil")
        self.assertEqual([c.path for c in plan.changes
                          if c.status == STATUS_MODIFIED], ["dos/app.py"])
        self.apply(plan)
        self.assertEqual((self.project / "dos/app.py").read_bytes(),
                         b"print('DOS')\n")
        self.assertEqual((self.project / "uno/app.py").read_bytes(),
                         b"print('uno')\n")


if __name__ == "__main__":
    unittest.main()
