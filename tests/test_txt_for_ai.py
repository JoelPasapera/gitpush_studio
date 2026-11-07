# -*- coding: utf-8 -*-
"""El TXT para una IA: secretos tachados, ruido fuera, solo lo que cambió
y partes que caben en su contexto.

Lo que más importa aquí es lo que NO puede pasar: que un secreto llegue
al TXT con la opción de tacharlos puesta, que un trozo de un archivo
partido se aplique de vuelta como si fuera el archivo entero, que la
limpieza de partes viejas borre algo que no sea una parte nuestra, o que
aplicar de vuelta un archivo con «••••••••» escriba los puntos en lugar
de la clave real.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.gitenv import HAVE_GIT, commit_all, init_repo

from gitpush_studio.core import pack, reader
from gitpush_studio.core.apply_txt import (STATUS_MODIFIED, STATUS_REJECTED,
                                           STATUS_SAME, apply_plan,
                                           plan_apply)
from gitpush_studio.core.errors import StepError
from gitpush_studio.core.pack import PackOptions, consolidate_project
from gitpush_studio.core.redact import (MASK, Redactor, redact_bytes,
                                        unredact_lines)
from gitpush_studio.core.txtformat import parse_txt
from gitpush_studio.core.txtparts import (estimate_tokens, existing_parts,
                                          tokens_to_bytes)

GITHUB_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
AWS_KEY = "AKIA" + "Q7XK2M4N6P8R0T2V"
DB_PASSWORD = "Cl4veMuyS3creta"
KEY_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ" * 6
PRIVATE_KEY = ("-----BEGIN PRIVATE KEY-----\n" + KEY_BODY[:64] + "\n"
               + KEY_BODY[64:128] + "\n-----END PRIVATE KEY-----\n")
SECRETS = (GITHUB_TOKEN, AWS_KEY[4:], DB_PASSWORD, KEY_BODY[:40])


def write(folder: Path, rel: str, data) -> Path:
    path = folder / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    return path


class TmpCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "proyecto"
        self.project.mkdir()

    def pack(self, options: PackOptions, name: str = "salida.txt", **kw):
        out = self.tmp / name
        stats = consolidate_project(self.project, out, options=options, **kw)
        return out.read_bytes().decode("utf-8"), stats


class RedactionTests(TmpCase):

    def secret_project(self):
        write(self.project, "src/config.py",
              f'TOKEN = "{GITHUB_TOKEN}"\nDEBUG = True\n')
        write(self.project, "infra/aws.ini",
              f"[default]\naws_access_key_id = {AWS_KEY}\n")
        write(self.project, "db.yml",
              f"url: postgres://ana:{DB_PASSWORD}@db.local/app\npool: 5\n")
        write(self.project, "keys/server.pem", PRIVATE_KEY)
        write(self.project, ".env", "API_KEY=loquesea12345\nPORT=8080\n")
        write(self.project, "ejemplo.py",
              'password = "changeme"\nnombre = "ana"\n')

    def test_no_secret_reaches_the_txt(self):
        self.secret_project()
        text, stats = self.pack(PackOptions(redact=True), use_git=False)
        for secret in SECRETS + ("loquesea12345",):
            self.assertNotIn(secret, text)
        self.assertIn(MASK, text)
        self.assertGreaterEqual(stats["redacted"], 5)
        self.assertIn("-----BEGIN PRIVATE KEY-----", text)
        self.assertIn("-----END PRIVATE KEY-----", text)
        # Lo que solo parece un ejemplo y lo que no es secreto se quedan.
        self.assertIn('password = "changeme"', text)
        self.assertIn("DEBUG = True", text)
        self.assertIn("tach", text.split("RESUMEN")[1])

    def test_without_the_option_nothing_is_touched(self):
        self.secret_project()
        text, stats = self.pack(PackOptions(), use_git=False)
        self.assertIn(GITHUB_TOKEN, text)
        self.assertEqual(stats["redacted"], 0)

    def test_big_files_are_redacted_while_streaming(self):
        filler = "".join(f"línea normal {i}\n" for i in range(20000))
        write(self.project, "grande.log",
              f"TOKEN={GITHUB_TOKEN}\n" + filler + PRIVATE_KEY + filler
              + f"postgres://u:{DB_PASSWORD}@h/db\n")

        def small_threshold(entries):
            return reader.iter_contents(entries, small=4096)
        with mock.patch.object(pack, "iter_contents", small_threshold):
            text, stats = self.pack(PackOptions(redact=True), use_git=False)
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        self.assertIn("línea normal 19999", text)
        self.assertGreaterEqual(stats["redacted"], 3)

    def test_streaming_gives_the_same_result_at_any_batch_size(self):
        data = ("intro\n" + PRIVATE_KEY + f'x = "{GITHUB_TOKEN}"\n' * 3
                + "fin\n" * 40).encode()
        expected, _count = redact_bytes(data, "a.txt")
        for batch in (7, 64, 300, 4096):
            for step in (1, 13, 512):
                out = []
                redactor = Redactor(out.append, "a.txt", batch=batch)
                for i in range(0, len(data), step):
                    redactor.feed(data[i:i + step])
                redactor.close()
                self.assertEqual(b"".join(out), expected, (batch, step))

    def test_key_files_by_name_are_hidden_whole_when_unstructured(self):
        out, count = redact_bytes(b"machine github.com login ana password x\n",
                                  ".netrc")
        self.assertNotIn(b"password x", out)
        self.assertIn(b"SECRETO TACHADO", out)
        self.assertEqual(count, 1)


class UnredactTests(TmpCase):

    def test_an_ai_answer_with_masks_gets_the_real_values_back(self):
        original = (f'TOKEN = "{GITHUB_TOKEN}"\nDEBUG = True\n'
                    f'URL = "postgres://ana:{DB_PASSWORD}@db/app"\n')
        write(self.project, "config.py", original)
        text, _stats = self.pack(PackOptions(redact=True), use_git=False)
        self.assertNotIn(GITHUB_TOKEN, text)
        edited = text.replace("DEBUG = True", "DEBUG = False")
        plan = plan_apply(str(self.project), edited)
        change = [c for c in plan.changes if c.path == "config.py"][0]
        self.assertEqual(change.status, STATUS_MODIFIED, change.reason)
        self.assertEqual((change.added, change.removed), (1, 1))
        apply_plan(plan, ["config.py"], backup_root=self.tmp / "resp")
        self.assertEqual((self.project / "config.py").read_text(),
                         original.replace("DEBUG = True", "DEBUG = False"))

    def test_the_whole_redacted_txt_back_changes_nothing(self):
        write(self.project, "a.py", f'K = "{GITHUB_TOKEN}"\n')
        write(self.project, "k.pem", PRIVATE_KEY)
        write(self.project, ".env", "A=1\nB=secreto123\n")
        text, _stats = self.pack(PackOptions(redact=True), use_git=False)
        plan = plan_apply(str(self.project), text)
        for change in plan.changes:
            self.assertEqual(change.status, STATUS_SAME,
                             (change.path, change.reason))

    def test_a_new_masked_line_cannot_be_applied(self):
        write(self.project, "config.py", f'TOKEN = "{GITHUB_TOKEN}"\n')
        answer = ("MODULO: config.py\n```\n"
                  f'TOKEN = "{MASK}"\nOTRO_TOKEN = "{MASK}"\n```\n')
        plan = plan_apply(str(self.project), answer)
        change = plan.changes[0]
        self.assertEqual(change.status, STATUS_REJECTED)
        self.assertIn("tachado", change.reason)

    def test_a_masked_new_file_is_rejected(self):
        plan = plan_apply(str(self.project),
                          f"MODULO: nuevo.env\n```\nCLAVE={MASK}\n```\n")
        self.assertEqual(plan.changes[0].status, STATUS_REJECTED)

    def test_unredact_lines_maps_a_whole_key_block(self):
        original = PRIVATE_KEY.rstrip("\n").split("\n")
        redacted, _count = redact_bytes(PRIVATE_KEY.encode(), "k.pem")
        lines = redacted.decode().rstrip("\n").split("\n")
        self.assertEqual(len(lines), 3)
        restored, problem = unredact_lines(lines, original, "k.pem")
        self.assertEqual(problem, "")
        self.assertEqual(restored, original)


class ForAiTests(TmpCase):

    def test_noise_is_left_out_with_a_note(self):
        write(self.project, "src/app.py", "print('mío')\n")
        write(self.project, "package-lock.json", '{"lockfileVersion": 3}\n')
        write(self.project, "static/lib.min.js", "var a=1;" * 2000)
        write(self.project, "gen/api_pb2.py", "# generated\nx = 1\n")
        write(self.project, "codegen.ts",
              "// Code generated by tool. DO NOT EDIT.\nexport const x = 1;\n")
        write(self.project, "bundle.js", ("function f(){return 1};" * 400)
              + "\n")
        text, stats = self.pack(PackOptions(for_ai=True), use_git=False)
        modules = {m.path: m for m in parse_txt(text).modules}
        self.assertEqual(modules["src/app.py"].note, "")
        for noisy in ("package-lock.json", "static/lib.min.js",
                      "gen/api_pb2.py", "codegen.ts", "bundle.js"):
            self.assertEqual(modules[noisy].note, "omitido", noisy)
        self.assertEqual(stats["for_ai"], 5)
        self.assertIn("omitidos para la IA", " ".join(text.split()))

    def test_a_big_minified_file_is_recognised_by_its_start(self):
        write(self.project, "app.js", "a=1;" * 200_000)
        write(self.project, "main.py", "print(1)\n")

        def small_threshold(entries):
            return reader.iter_contents(entries, small=4096)
        with mock.patch.object(pack, "iter_contents", small_threshold):
            text, stats = self.pack(PackOptions(for_ai=True), use_git=False)
        modules = {m.path: m for m in parse_txt(text).modules}
        self.assertEqual(modules["app.js"].note, "omitido")
        self.assertEqual(modules["main.py"].lines, ("print(1)",))

    def test_token_estimate(self):
        write(self.project, "a.txt", "hola mundo\n" * 1000)
        _text, stats = self.pack(PackOptions(), use_git=False)
        self.assertEqual(stats["tokens"], estimate_tokens(stats["bytes"]))
        self.assertGreater(stats["tokens"], 1000)


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class ChangesTests(TmpCase):

    def setUp(self):
        super().setUp()
        init_repo(self.project)
        write(self.project, ".gitignore", "*.log\n")
        write(self.project, "igual.py", "x = 1\n")
        write(self.project, "cambia.py", "def f():\n    return 1\n")
        write(self.project, "borrame.txt", "adiós\n")
        write(self.project, "logo.png", b"\x89PNG\r\n\x1a\n\x00\x01")
        commit_all(self.project)
        write(self.project, "cambia.py", "def f():\n    return 2\n")
        write(self.project, "nuevo.py", "print('nuevo')\n")
        write(self.project, "ruido.log", "ignorado\n")
        write(self.project, "logo.png", b"\x89PNG\r\n\x1a\n\x00\x02\x03")
        os.unlink(str(self.project / "borrame.txt"))

    def test_only_changed_files_whole(self):
        text, stats = self.pack(PackOptions(changes="archivos"))
        modules = {m.path: m for m in parse_txt(text).modules}
        self.assertEqual(sorted(modules), ["borrame.txt", "cambia.py",
                                           "logo.png", "nuevo.py"])
        self.assertEqual(modules["cambia.py"].lines,
                         ("def f():", "    return 2"))
        self.assertEqual(modules["borrame.txt"].note, "borrado")
        self.assertNotIn("ruido.log", modules)
        self.assertIn("CAMBIOS DESDE: el commit", text)
        self.assertIn("igual.py", text.split("ESTRUCTURA")[1].split("====")[0])
        self.assertEqual(stats["deleted"], 1)

    def test_a_deleted_note_never_deletes(self):
        text, _stats = self.pack(PackOptions(changes="archivos"))
        plan = plan_apply(str(self.project), text)
        deleted = [c for c in plan.changes if c.path == "borrame.txt"][0]
        self.assertEqual(deleted.status, STATUS_REJECTED)
        self.assertFalse((self.project / "borrame.txt").exists())

    def test_only_the_differences(self):
        text, stats = self.pack(PackOptions(changes="diferencias"))
        self.assertEqual(parse_txt(text).modules, ())       # nada aplicable
        self.assertIn("DIFERENCIAS: cambia.py", text)
        self.assertIn("-    return 1\n+    return 2\n", text)
        self.assertIn("DIFERENCIAS: nuevo.py", text)
        self.assertIn("+print('nuevo')", text)
        self.assertIn("ARCHIVO BINARIO OMITIDO: cambió", text)
        self.assertIn("DIFERENCIAS: borrame.txt", text)
        self.assertNotIn("igual.py\n---", text)
        self.assertEqual(stats["diffs"], 2)

    def test_differences_never_show_secrets(self):
        write(self.project, "cambia.py",
              f'def f():\n    return 2\nTOKEN = "{GITHUB_TOKEN}"\n')
        text, _stats = self.pack(PackOptions(changes="diferencias",
                                             redact=True))
        self.assertNotIn(GITHUB_TOKEN, text)
        self.assertIn(MASK, text)

    def test_a_folder_that_is_not_a_repository(self):
        other = self.tmp / "suelta"
        other.mkdir()
        write(other, "a.txt", "a\n")
        with self.assertRaises(StepError):
            consolidate_project(other, self.tmp / "x.txt",
                                options=PackOptions(changes="archivos"))


class SplitTests(TmpCase):

    def build(self):
        for i in range(40):
            write(self.project, f"src/f{i:02d}.py",
                  "".join(f"linea_{i}_{k} = {k}\n" for k in range(60)))
        write(self.project, "enorme.txt",
              "".join(f"linea grande {k}\n" for k in range(5000)))

    def test_parts_respect_the_size_and_keep_files_whole(self):
        self.build()
        text, stats = self.pack(PackOptions(split_tokens=4000),
                                use_git=False)
        full = {m.path: m.lines for m in parse_txt(text).modules}
        parts = [Path(p) for p in stats["parts"]]
        self.assertGreater(len(parts), 3)
        budget = tokens_to_bytes(4000)
        seen = {}
        pieces = []
        for number, path in enumerate(parts, 1):
            self.assertRegex(path.name, r"^parte-\d+-de-\d+\.txt$")
            data = path.read_bytes()
            self.assertLessEqual(len(data), budget + 2048)
            parsed = parse_txt(data.decode("utf-8"))
            self.assertEqual(parsed.kind, "proyecto")
            self.assertEqual(parsed.root, "proyecto")
            for module in parsed.modules:
                if module.path == "enorme.txt":
                    self.assertTrue(module.incomplete)
                    pieces.extend(line for line in module.lines
                                  if not line.startswith("<"))
                    continue
                self.assertFalse(module.incomplete)
                self.assertNotIn(module.path, seen)
                seen[module.path] = module.lines
                self.assertEqual(module.lines, full[module.path])
        self.assertEqual(sorted(seen), sorted(p for p in full
                                              if p != "enorme.txt"))
        self.assertEqual(tuple(pieces), full["enorme.txt"])
        self.assertIn("ESTRUCTURA DEL PROYECTO",
                      parts[0].read_text(encoding="utf-8"))
        self.assertIn("RESUMEN", parts[-1].read_text(encoding="utf-8"))
        self.assertEqual(existing_parts(self.tmp / "salida.txt"), parts)

    def test_a_piece_of_a_split_file_is_never_applied(self):
        self.build()
        _text, stats = self.pack(PackOptions(split_tokens=4000),
                                 use_git=False)
        before = (self.project / "enorme.txt").read_bytes()
        for path in stats["parts"]:
            plan = plan_apply(str(self.project),
                              Path(path).read_text(encoding="utf-8"))
            for change in plan.changes:
                if change.path == "enorme.txt":
                    self.assertEqual(change.status, STATUS_REJECTED)
                else:
                    self.assertEqual(change.status, STATUS_SAME)
        self.assertEqual((self.project / "enorme.txt").read_bytes(), before)

    def test_stale_parts_are_removed_but_nothing_else(self):
        self.build()
        _text, first = self.pack(PackOptions(split_tokens=4000),
                                 use_git=False)
        folder = Path(first["parts"][0]).parent
        _text, second = self.pack(PackOptions(split_tokens=8000),
                                  use_git=False)
        self.assertLess(len(second["parts"]), len(first["parts"]))
        self.assertEqual(Path(second["parts"][0]).parent, folder)
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         sorted(Path(p).name for p in second["parts"]))
        # Si ahora cabe entero, las partes viejas sobran.
        _text, whole = self.pack(PackOptions(split_tokens=1_000_000),
                                 use_git=False)
        self.assertEqual(whole["parts"], [])
        self.assertFalse(folder.exists())
        # Sin pedir partes, las que hubiera no se tocan.
        _text, second = self.pack(PackOptions(split_tokens=8000),
                                  use_git=False)
        _text, plain = self.pack(PackOptions(), use_git=False)
        self.assertEqual(len(list(folder.iterdir())), len(second["parts"]))
        # Una carpeta con algo nuestro… y algo tuyo: no se toca, se usa otra.
        write(folder, "mis-notas.txt", "no me borres\n")
        _text, third = self.pack(PackOptions(split_tokens=4000),
                                 use_git=False)
        self.assertNotEqual(Path(third["parts"][0]).parent, folder)
        self.assertEqual((folder / "mis-notas.txt").read_text(),
                         "no me borres\n")
        for name in (Path(p).name for p in second["parts"]):
            self.assertTrue((folder / name).exists())

    def test_parts_inside_the_project_never_enter_the_next_txt(self):
        self.build()
        out = self.project / "proyecto.txt"
        first = consolidate_project(self.project, out, use_git=False,
                                    respect_ignores=False,
                                    options=PackOptions(split_tokens=4000))
        self.assertTrue(first["parts"])
        second = consolidate_project(self.project, out, use_git=False,
                                     respect_ignores=False)
        text = out.read_text(encoding="utf-8")
        self.assertNotIn("MODULO: proyecto-partes/", text)
        self.assertEqual(second["files"], 41)

    def test_a_small_txt_is_not_split(self):
        write(self.project, "a.txt", "a\n")
        _text, stats = self.pack(PackOptions(split_tokens=4000),
                                 use_git=False)
        self.assertEqual(stats["parts"], [])


if __name__ == "__main__":
    unittest.main()
