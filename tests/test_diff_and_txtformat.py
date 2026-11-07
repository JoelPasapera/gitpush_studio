# -*- coding: utf-8 -*-
"""El algoritmo de diferencias y el lector del formato TXT.

Las diferencias se comprueban contra una subsecuencia común más larga
calculada por fuerza bruta (programación dinámica): en miles de casos
aleatorios el resultado tiene que ser válido (cada pareja es de líneas
iguales y en orden) y óptimo. El lector se prueba con el TXT exacto que
genera la app y con las formas en que suele devolverlo una IA.
"""

import difflib
import os
import random
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import List, Sequence

from gitpush_studio.core import diff
from gitpush_studio.core.pack import consolidate_many, consolidate_project
from gitpush_studio.core.txtformat import (EQUALS_LINE, parse_txt)


def lcs_length(a: Sequence[int], b: Sequence[int]) -> int:
    """Longitud de la subsecuencia común más larga, por fuerza bruta."""
    previous = [0] * (len(b) + 1)
    for x in a:
        current = [0] * (len(b) + 1)
        for j, y in enumerate(b):
            if x == y:
                current[j + 1] = previous[j] + 1
            else:
                current[j + 1] = max(previous[j + 1], current[j])
        previous = current
    return previous[len(b)]


def assert_valid_pairs(test: unittest.TestCase, a: Sequence, b: Sequence,
                       pairs: List) -> None:
    for i, j in pairs:
        test.assertEqual(a[i], b[j])
    for k in range(len(pairs) - 1):
        test.assertLess(pairs[k][0], pairs[k + 1][0])
        test.assertLess(pairs[k][1], pairs[k + 1][1])


class MyersTests(unittest.TestCase):

    def test_random_small_cases_are_valid_and_optimal(self):
        rnd = random.Random(20261106)
        for _ in range(4000):
            a = [rnd.randint(0, rnd.randint(1, 6))
                 for _ in range(rnd.randint(0, 18))]
            b = [rnd.randint(0, rnd.randint(1, 6))
                 for _ in range(rnd.randint(0, 18))]
            pairs = diff.matching_pairs(a, b)
            assert_valid_pairs(self, a, b, pairs)
            self.assertEqual(len(pairs), lcs_length(a, b), (a, b))
            codes = diff.opcodes(a, b)
            self.assertEqual(diff.apply_opcodes(a, b, codes), b)

    def test_opcodes_cover_both_sides_without_gaps(self):
        rnd = random.Random(5)
        for _ in range(500):
            a = [rnd.randint(0, 4) for _ in range(rnd.randint(0, 30))]
            b = [rnd.randint(0, 4) for _ in range(rnd.randint(0, 30))]
            codes = diff.opcodes(a, b)
            i = j = 0
            for code in codes:
                self.assertEqual((code.a_start, code.b_start), (i, j))
                i, j = code.a_end, code.b_end
                if code.tag == diff.EQUAL:
                    self.assertEqual(a[code.a_start:code.a_end],
                                     b[code.b_start:code.b_end])
            self.assertEqual((i, j), (len(a), len(b)))

    def test_identical_and_empty_inputs(self):
        self.assertEqual(diff.opcodes([], []), [])
        self.assertEqual(diff.opcodes(["x"], ["x"]),
                         [diff.Opcode(diff.EQUAL, 0, 1, 0, 1)])
        self.assertEqual(diff.opcodes([], ["x", "y"]),
                         [diff.Opcode(diff.INSERT, 0, 0, 0, 2)])
        self.assertEqual(diff.opcodes(["x"], []),
                         [diff.Opcode(diff.DELETE, 0, 1, 0, 0)])
        self.assertFalse(diff.has_changes(diff.opcodes(["a", "b"],
                                                       ["a", "b"])))

    def test_change_counts(self):
        codes = diff.opcodes(["a", "b", "c", "d"], ["a", "X", "c", "d", "e"])
        self.assertEqual(diff.change_counts(codes), (2, 1))

    def test_a_long_file_with_few_changes_is_fast(self):
        a = [f"línea {i}" for i in range(200_000)]
        b = list(a)
        for k in range(0, len(b), 1000):
            b[k] = "cambiada"
        t0 = time.perf_counter()
        codes = diff.opcodes(a, b)
        elapsed = time.perf_counter() - t0
        self.assertEqual(diff.change_counts(codes), (200, 200))
        self.assertLess(elapsed, 5.0)

    def test_two_unrelated_big_files_finish_and_stay_correct(self):
        rnd = random.Random(3)
        a = [str(rnd.randint(0, 40)) for _ in range(20_000)]
        b = [str(rnd.randint(0, 40)) for _ in range(20_000)]
        t0 = time.perf_counter()
        pairs = diff.matching_pairs(a, b)
        elapsed = time.perf_counter() - t0
        assert_valid_pairs(self, a, b, pairs)
        codes = diff.opcodes_from_pairs(pairs, len(a), len(b))
        self.assertEqual(diff.apply_opcodes(a, b, codes), b)
        self.assertLess(elapsed, 20.0)

    def test_a_tiny_budget_still_gives_a_correct_answer(self):
        rnd = random.Random(11)
        for _ in range(200):
            a = [rnd.randint(0, 3) for _ in range(rnd.randint(1, 60))]
            b = [rnd.randint(0, 3) for _ in range(rnd.randint(1, 60))]
            pairs = diff.matching_pairs(a, b, step_budget=1)
            assert_valid_pairs(self, a, b, pairs)
            codes = diff.opcodes_from_pairs(pairs, len(a), len(b))
            self.assertEqual(diff.apply_opcodes(a, b, codes), b)


class UnifiedDiffTests(unittest.TestCase):

    def reference(self, a: List[str], b: List[str]) -> str:
        lines = list(difflib.unified_diff(a, b, "a", "b", lineterm=""))
        return "\n".join(lines) + "\n" if lines else ""

    def test_same_output_as_difflib_on_simple_edits(self):
        base = [f"l{i}" for i in range(40)]
        cases = []
        cases.append((base, base[:10] + ["X"] + base[11:]))         # cambio
        cases.append((base, ["nueva"] + base))                       # al inicio
        cases.append((base, base + ["final"]))                       # al final
        cases.append((base, base[:-1]))                              # quitar
        cases.append((base, base[:5] + base[6:30] + ["Y"] + base[30:]))
        cases.append(([], ["a", "b"]))
        cases.append((["a", "b"], []))
        cases.append((base, base))
        for a, b in cases:
            self.assertEqual(diff.unified_diff(a, b), self.reference(a, b))

    def test_hunk_line_numbers(self):
        a = ["uno", "dos", "tres", "cuatro"]
        b = ["uno", "DOS", "tres", "cuatro", "cinco"]
        (hunk,) = diff.hunks(a, b, context=1)
        self.assertEqual(diff.hunk_header(hunk), "@@ -1,4 +1,5 @@")
        tags = [(line.tag, line.text, line.old_number, line.new_number)
                for line in hunk.lines]
        self.assertEqual(tags, [
            (" ", "uno", 1, 1), ("-", "dos", 2, None), ("+", "DOS", None, 2),
            (" ", "tres", 3, 3), (" ", "cuatro", 4, 4),
            ("+", "cinco", None, 5)])

    def test_distant_changes_make_separate_hunks(self):
        a = [str(i) for i in range(100)]
        b = list(a)
        b[5] = "cinco"
        b[90] = "noventa"
        self.assertEqual(len(diff.hunks(a, b)), 2)


def write(folder: Path, rel: str, data: bytes) -> None:
    path = folder / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class ParseExactTxtTests(unittest.TestCase):
    """El TXT tal cual lo genera la app se lee entero y sin errores."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "proyecto"
        self.project.mkdir()

    def pack(self) -> str:
        out = self.tmp / "salida.txt"
        consolidate_project(self.project, out, use_git=False)
        return out.read_text(encoding="utf-8")

    def test_every_file_comes_back_with_its_content(self):
        write(self.project, "src/main.py", b"def f():\n    return 1\n")
        write(self.project, "crlf.txt", b"uno\r\ndos\r\n")
        write(self.project, "bom.txt", b"\xef\xbb\xbfcon BOM\n")
        write(self.project, "vacio.txt", b"")
        write(self.project, "imagen.png", b"\x89PNG\r\n\x1a\n\x00\x00")
        write(self.project, "readme.md",
              b"Titulo\n" + b"=" * 30 + b"\n\nTexto con MODULO: falso\n")
        parsed = parse_txt(self.pack())
        self.assertEqual(parsed.kind, "proyecto")
        self.assertEqual(parsed.root, "proyecto")
        by_path = {m.path: m for m in parsed.modules}
        self.assertEqual(sorted(by_path), ["bom.txt", "crlf.txt",
                                           "imagen.png", "readme.md",
                                           "src/main.py", "vacio.txt"])
        self.assertEqual(by_path["src/main.py"].lines,
                         ("def f():", "    return 1"))
        self.assertEqual(by_path["crlf.txt"].lines, ("uno", "dos"))
        self.assertEqual(by_path["bom.txt"].lines, ("con BOM",))
        self.assertEqual(by_path["vacio.txt"].lines, ())
        self.assertEqual(by_path["imagen.png"].note, "binario")
        self.assertEqual(by_path["readme.md"].lines,
                         ("Titulo", "=" * 30, "", "Texto con MODULO: falso"))
        for module in parsed.modules:
            self.assertTrue(module.terminated)
            self.assertEqual(module.ignored_tail, "")
            self.assertFalse(module.incomplete)

    def test_a_file_that_ends_with_its_own_rule_keeps_it(self):
        write(self.project, "a.txt", b"texto\n" + b"=" * 80 + b"\n")
        write(self.project, "b.txt", b"otro\n")
        by_path = {m.path: m for m in parse_txt(self.pack()).modules}
        self.assertEqual(by_path["a.txt"].lines, ("texto", "=" * 80))
        self.assertEqual(by_path["b.txt"].lines, ("otro",))

    def test_a_file_explaining_the_format_does_not_split(self):
        # Un README que cita una cabecera dentro de un bloque ```.
        write(self.project, "guia.md",
              b"Ejemplo:\n\n```\nMODULO: src/x.py\n```\n\nFin.\n")
        write(self.project, "z.txt", b"z\n")
        by_path = {m.path: m for m in parse_txt(self.pack()).modules}
        self.assertEqual(sorted(by_path), ["guia.md", "z.txt"])
        self.assertIn("MODULO: src/x.py", by_path["guia.md"].lines)

    def test_a_profile_txt_keeps_each_repository(self):
        for name in ("uno", "dos"):
            write(self.tmp / "perfil" / name, "app.py",
                  f"print('{name}')\n".encode())
        out = self.tmp / "perfil.txt"
        consolidate_many("ana", [self.tmp / "perfil" / "uno",
                                 self.tmp / "perfil" / "dos"], out,
                         use_git=False)
        parsed = parse_txt(out.read_text(encoding="utf-8"))
        self.assertEqual(parsed.kind, "perfil")
        self.assertEqual(parsed.root, "ana")
        found = sorted((m.repo, m.path, m.lines) for m in parsed.modules)
        self.assertEqual(found, [("dos", "app.py", ("print('dos')",)),
                                 ("uno", "app.py", ("print('uno')",))])

    @unittest.skipIf(os.name == "nt", "enlaces simbólicos")
    def test_notes_are_recognised(self):
        write(self.project, "real.txt", b"hola\n")
        os.symlink("real.txt", self.project / "enlace.txt")
        by_path = {m.path: m for m in parse_txt(self.pack()).modules}
        self.assertEqual(by_path["enlace.txt"].note, "enlace")
        self.assertEqual(by_path["real.txt"].note, "")


class ParseAiAnswerTests(unittest.TestCase):
    """Lo que suele devolver una IA."""

    def only(self, text: str):
        modules = parse_txt(text).modules
        self.assertEqual(len(modules), 1, modules)
        return modules[0]

    def test_fenced_blocks_with_chat_around(self):
        text = ("Claro, aquí tienes los cambios:\n\n"
                "MODULO: src/a.py\n```python\nprint('adiós')\n```\n\n"
                "Y también este:\n\n"
                "**MODULO: `b.txt`**\n```\nuno\nnuevo\n```\n"
                "Espero que te sirva.\n")
        parsed = parse_txt(text)
        self.assertEqual(parsed.kind, "fragmento")
        a, b = parsed.modules
        self.assertEqual((a.path, a.lines), ("src/a.py", ("print('adiós')",)))
        self.assertEqual(a.fenced_lines,
                         ("```python", "print('adiós')", "```"))
        self.assertEqual(a.ignored_tail, "Y también este:")
        self.assertEqual((b.path, b.lines), ("b.txt", ("uno", "nuevo")))
        self.assertEqual(b.ignored_tail, "Espero que te sirva.")

    def test_header_decorations_are_removed(self):
        for header in ("### MODULO: src\\win\\x.py", "MODULO: ./src/win/x.py",
                       "MODULO:src/win/x.py", "MODULO: «src/win/x.py»",
                       "MODULO: 'src/win/x.py'  "):
            module = self.only(header + "\n" + "-" * 80 + "\nx = 1\n")
            self.assertEqual(module.path, "src/win/x.py", header)
            self.assertEqual(module.lines, ("x = 1",))

    def test_separators_dropped_but_dashes_kept(self):
        text = ("MODULO: a.py\n" + "-" * 80 + "\ncódigo a\n"
                "MODULO: b.py\n" + "-" * 80 + "\ncódigo b\n")
        a, b = parse_txt(text).modules
        self.assertEqual(a.lines, ("código a",))
        self.assertEqual(b.lines, ("código b",))
        self.assertFalse(b.terminated)

    def test_chat_after_the_last_separator_is_set_aside(self):
        text = ("MODULO: a.py\n" + "-" * 80 + "\nx = 2\n\n" + EQUALS_LINE
                + "\n\nAvísame si necesitas algo más.\n")
        module = self.only(text)
        self.assertEqual(module.lines, ("x = 2",))
        self.assertEqual(module.ignored_tail, "Avísame si necesitas algo más.")

    def test_a_header_inside_ordinary_text_is_not_a_module(self):
        text = ("MODULO: a.txt\n" + "-" * 80 + "\nprimera\n"
                "MODULO: esto es texto\nsegunda\n")
        module = self.only(text)
        self.assertEqual(module.lines,
                         ("primera", "MODULO: esto es texto", "segunda"))

    def test_the_whole_txt_wrapped_in_a_fence(self):
        text = ("```\nMODULO: a.py\n" + "-" * 80 + "\nx = 3\n\n"
                + EQUALS_LINE + "\n```\n")
        module = self.only(text)
        self.assertEqual(module.lines, ("x = 3",))

    def test_a_longer_fence_can_hold_a_shorter_one(self):
        text = ("MODULO: doc.md\n````markdown\n# Título\n```\ncódigo\n```\n"
                "````\n")
        module = self.only(text)
        self.assertEqual(module.lines, ("# Título", "```", "código", "```"))

    def test_an_unclosed_fence_is_kept_as_content(self):
        module = self.only("MODULO: a.py\n```python\nx = 1\n")
        self.assertEqual(module.lines, ("```python", "x = 1"))
        self.assertIsNone(module.fenced_lines)

    def test_windows_line_endings_in_the_pasted_text(self):
        text = ("MODULO: a.py\r\n" + "-" * 80 + "\r\nuno\r\ndos\r\n\r\n"
                + EQUALS_LINE + "\r\n")
        self.assertEqual(self.only(text).lines, ("uno", "dos"))

    def test_incomplete_contents_are_flagged(self):
        cut = self.only("MODULO: log.txt\n" + "-" * 80 + "\nlínea\n"
                        "<ARCHIVO CORTADO: creció mientras se leía; aquí "
                        "está tal como era al abrirlo (1 KB)>\n")
        self.assertTrue(cut.incomplete)
        broken = self.only("MODULO: x.txt\n" + "-" * 80 + "\nmitad\n"
                           "<ERROR AL LEER ARCHIVO a mitad: E/S. Falta el "
                           "resto>\n")
        self.assertTrue(broken.incomplete)
        whole = self.only("MODULO: y.txt\n" + "-" * 80 + "\n<html>\n")
        self.assertFalse(whole.incomplete)
        self.assertEqual(whole.note, "")

    def test_nothing_to_read(self):
        self.assertEqual(parse_txt("").modules, ())
        self.assertEqual(parse_txt("Solo charla, sin módulos.\n").modules, ())
        self.assertEqual(parse_txt("MODULO RAIZ: x\n").modules, ())


if __name__ == "__main__":
    unittest.main()
