# -*- coding: utf-8 -*-
"""Código, comentarios y líneas en blanco (core.linecount).

El clasificador rápido no recorre byte a byte: enmascara los comentarios
y cuenta líneas en blanco con expresiones regulares. Es un truco, y los
trucos se prueban contra algo que no lo sea. Aquí hay una implementación
de REFERENCIA independiente —una máquina de estados carácter a carácter,
lenta pero evidente— y miles de documentos aleatorios hechos con los
tokens de cada lenguaje. Los dos tienen que dar exactamente lo mismo en
todos. Además, casos escritos a mano con su recuento comprobado.
"""

import random
import re
import time
import unittest

from gitpush_studio.core import languages as langs
from gitpush_studio.core.languages import LANGS, get
from gitpush_studio.core.linecount import (LineCounts, average_line_length,
                                           count_lines, normalize_text)

_WS = b" \t\r\f\v"
_DOC_AT = re.compile(rb"[rRbBuUfF]{0,2}(\"\"\"|''')")


# ------------------------------------------------------------------ #
#  Referencia: carácter a carácter, sin trucos                        #
# ------------------------------------------------------------------ #
def _string_end(text, i, opener, body):
    """Fin (exclusivo) de la cadena que abre `opener` en la posición i,
    siguiendo las reglas a mano, sin usar la regex de la tabla."""
    n = len(text)
    j = i + len(opener)
    if body is langs._RAW_BT[1]:                         # Go: sin escapes
        k = text.find(b"`", j)
        return n if k < 0 else k + 1
    if opener in (b'"""', b"'''", b"`"):                  # multilínea
        while j < n:
            if text.startswith(opener, j):
                return j + len(opener)
            if text[j:j + 1] == b"\\":
                j += 2
                continue
            j += 1
        return n
    # Una línea: se corta en el salto, o ANTES de una barra invertida
    # que no tenga nada escapable detrás (fin de texto o salto).
    while j < n:
        c = text[j:j + 1]
        if c == opener:
            return j + 1
        if c == b"\n":
            return j
        if c == b"\\":
            if j + 1 < n and text[j + 1:j + 2] != b"\n":
                j += 2
                continue
            return j
        j += 1
    return n


def _block_end(text, i, start, end, nested):
    n = len(text)
    j = i + len(start)
    depth = 1
    while j < n:
        if nested and text.startswith(start, j):
            depth += 1
            j += len(start)
            continue
        if text.startswith(end, j):
            depth -= 1
            j += len(end)
            if depth == 0:
                return j
            continue
        j += 1
    return n


def reference(raw, lang):
    text = normalize_text(raw)
    if not text:
        return (0, 0, 0)
    body = text[:-1] if text.endswith(b"\n") else text
    n = len(body)
    code = [False]
    comment = [False]
    linea = 0

    def marca(desde, hasta, cual):
        nonlocal linea
        for k in range(desde, hasta):
            c = body[k:k + 1]
            if c == b"\n":
                linea += 1
                code.append(False)
                comment.append(False)
            elif cual == "comment":
                comment[linea] = True
            elif c not in (b" ", b"\t", b"\r", b"\f", b"\v"):
                code[linea] = True

    bloques = sorted(lang.block, key=lambda t: -len(t[0]))
    cadenas = sorted(lang.strings, key=lambda t: -len(t[0]))
    lineas = [re.compile(f, re.MULTILINE) for f in lang.line]
    por_apertura = dict(lang.strings)
    i = 0
    while i < n:
        hecho = False
        if lang.docstrings:
            # Docstring: mirando HACIA DELANTE desde aquí (prefijo + comillas)
            # y con solo espacios entre el principio de la línea y aquí.
            ini = body.rfind(b"\n", 0, i) + 1
            dm = _DOC_AT.match(body, i)
            if (dm and body[ini:i].strip(b" \t") == b""
                    and dm.group(1) in lang.docstrings):
                q = dm.start(1)
                e = max(_string_end(body, q, dm.group(1),
                                    por_apertura[dm.group(1)]), q + 3)
                marca(i, e, "comment")
                i = e
                continue
        for start, end in bloques:
            if body.startswith(start, i):
                e = _block_end(body, i, start, end, lang.nested)
                marca(i, e, "comment")
                i, hecho = e, True
                break
        if hecho:
            continue
        for opener, rx in cadenas:
            if body.startswith(opener, i):
                e = max(_string_end(body, i, opener, rx), i + len(opener))
                marca(i, e, "code")
                i, hecho = e, True
                break
        if hecho:
            continue
        for rx in lineas:
            m = rx.match(body, i)
            if m:
                e = body.find(b"\n", m.end())
                e = n if e < 0 else e
                marca(i, e, "comment")
                i, hecho = e, True
                break
        if hecho:
            continue
        marca(i, i + 1, "code")
        i += 1

    trozos = body.split(b"\n")
    c = m = b = 0
    for k, trozo in enumerate(trozos):
        if trozo.strip(_WS) == b"":
            b += 1
        elif code[k]:
            c += 1
        elif comment[k]:
            m += 1
        else:                       # imposible: o es código o comentario
            raise AssertionError((lang.name, trozo))
    return (c, m, b)


def fast(raw, lang):
    r = count_lines(normalize_text(raw), lang)
    return (r.code, r.comment, r.blank)


# ------------------------------------------------------------------ #
#  Generador de documentos hostiles                                   #
# ------------------------------------------------------------------ #
_COMUNES = [b" ", b"  ", b"\t", b"\n", b"\n", b"\n", b"\r\n", b"x",
            b"abc", b"1", b"=", b";", b"(", b")", b"\\", b"\\\n",
            b"\\\\", b"$", b"&", b"|", b"r", b"b", b"f", b"rb", b"bar",
            b"\n    "]


def _alfabeto(lang):
    piezas = list(_COMUNES)
    for start, end in lang.block:
        piezas += [start, end, start, end]
    for opener, _ in lang.strings:
        piezas += [opener, opener, opener[:1]]
    for frag in (b"//", b"#", b"--", b"%", b"'", b'"', b"`", b"/*", b"*/",
                 b"<!--", b"-->", b"{-", b"-}", b"--[[", b"]]", b"<#",
                 b"#>", b"rem ", b"REM ", b"::", b"#[", b";", b'"""',
                 b"'''"):
        piezas.append(frag)
    return piezas


def documento(rng, piezas, largo):
    return b"".join(rng.choice(piezas) for _ in range(largo))


class DifferentialTests(unittest.TestCase):
    """Rápido contra referencia, en miles de documentos aleatorios."""

    CASOS_POR_LENGUAJE = 250

    def test_fast_equals_reference_in_every_language(self):
        rng = random.Random(20261016)
        for nombre, lang in sorted(LANGS.items()):
            piezas = _alfabeto(lang)
            for k in range(self.CASOS_POR_LENGUAJE):
                doc = documento(rng, piezas, rng.randint(0, 60))
                with self.subTest(lenguaje=nombre, caso=k, doc=doc):
                    self.assertEqual(fast(doc, lang), reference(doc, lang))

    def test_long_documents_too(self):
        rng = random.Random(7)
        for nombre in ("Python", "JavaScript", "Rust", "Lua", "Shell",
                       "HTML", "Haskell", "PowerShell", "SQL", "Go"):
            lang = get(nombre)
            piezas = _alfabeto(lang)
            for k in range(20):
                doc = documento(rng, piezas, rng.randint(200, 800))
                with self.subTest(lenguaje=nombre, caso=k):
                    self.assertEqual(fast(doc, lang), reference(doc, lang))


class KnownCountsTests(unittest.TestCase):
    """Casos escritos a mano, con su recuento comprobado línea a línea."""

    def check(self, nombre, texto, esperado):
        r = count_lines(normalize_text(texto), get(nombre))
        self.assertEqual((r.code, r.comment, r.blank), esperado,
                         f"{nombre}: {texto!r}")

    def test_python(self):
        # El """…""" que abre la línea es un docstring: documentación.
        self.check("Python",
                   b'# comentario\nx = 1  # cola\n\n"""doc\n'
                   b'# no es comentario\n"""\ny = "#no"\n', (2, 4, 1))

    def test_python_docstrings_only_when_they_start_the_line(self):
        self.check("Python",
                   b'def f():\n    """Documenta.\n\n    Sigue.\n    """\n'
                   b'    x = """esto es un valor\n    de dos lineas"""\n'
                   b"    r'''crudo'''\n    return x\n",
                   (4, 4, 1))

    def test_javascript_strings_hide_comment_markers(self):
        self.check("JavaScript",
                   b'// a\nconst u = "http://x";\n/* b\n  c */ f();\n'
                   b'/* d */\n\n`tpl // no\n`;\n', (4, 3, 1))

    def test_nested_blocks(self):
        self.check("Rust", b"/* a /* b */ c */\nfn main(){}\n", (1, 1, 0))
        # En C no se anidan: el primer «*/» cierra.
        self.check("C", b"/* a /* b */ c */\nint x;\n", (2, 0, 0))

    def test_lua_block_beats_line(self):
        self.check("Lua", b"--[[ bloque\nsigue ]]\n-- linea\nprint(1)\n",
                   (1, 3, 0))

    def test_shell_hash_only_at_word_start(self):
        self.check("Shell", b'echo a#b\n# c\nx=1 # d\necho "#e"\n${#v}\n',
                   (4, 1, 0))

    def test_batch_rem_and_double_colon(self):
        self.check("Batch", b"REM hola\n:: tambien\necho rem no\n  rem si\n",
                   (1, 3, 0))

    def test_latex_escaped_percent(self):
        self.check("LaTeX", b"% c\n50\\% de algo\ntexto % fin\n", (2, 1, 0))

    def test_html(self):
        self.check("HTML", b"<p>a</p>\n<!-- c\n  d -->\n<!-- e --> <b>\n",
                   (2, 2, 0))

    def test_php_attribute_is_not_a_comment(self):
        self.check("PHP", b"<?php\n#[Attr]\n# c\n// d\n", (2, 2, 0))

    def test_yaml_hash_inside_value(self):
        self.check("YAML", b"url: http://a#b\n# c\nk: v # d\n", (2, 1, 0))

    def test_unterminated_things_do_not_crash(self):
        self.check("C", b"/* sin cerrar\nint x;\n", (0, 2, 0))
        self.check("Python", b'x = """sin cerrar\n# dentro\n', (2, 0, 0))
        self.check("Python", b'x = """acaba en barra\\', (1, 0, 0))
        self.check("JavaScript", b"`plantilla\\", (1, 0, 0))

    def test_blank_lines_inside_a_block_comment_are_blank(self):
        self.check("C", b"/*\n\n   \n*/\n", (0, 2, 2))

    def test_crlf(self):
        self.check("Python", b"# a\r\nx = 1\r\n\r\n", (1, 1, 1))

    def test_line_endings_and_empty(self):
        self.check("Python", b"", (0, 0, 0))
        self.check("Python", b"\n", (0, 0, 1))
        self.check("Python", b"x", (1, 0, 0))
        self.check("Python", b"x\n", (1, 0, 0))
        self.check("Python", b"x\n\n", (1, 0, 1))
        self.check("Python", b"\n\nx", (1, 0, 2))

    def test_languages_without_comments_count_lines_as_code(self):
        self.check("JSON", b'{\n  "a": "// no"\n}\n\n', (3, 0, 1))


class NormalizeTests(unittest.TestCase):

    def test_utf8_bom_is_ignored(self):
        self.assertEqual(fast(b"\xef\xbb\xbf# c\nx\n", get("Python")),
                         (1, 1, 0))

    def test_utf16_is_text_not_binary(self):
        raw = "# comentario\nx = 1\n".encode("utf-16")
        self.assertIn(b"\x00", raw)                  # parecería binario
        self.assertEqual(fast(raw, get("Python")), (1, 1, 0))

    def test_average_line_length_spots_minified_files(self):
        self.assertLess(average_line_length(b"a\nb\nc\n"), 3)
        self.assertGreater(average_line_length(b"x" * 5000), 1000)
        self.assertEqual(average_line_length(b""), 0.0)


class InvariantTests(unittest.TestCase):

    def test_total_is_the_number_of_lines(self):
        rng = random.Random(99)
        for nombre, lang in LANGS.items():
            piezas = _alfabeto(lang)
            for _ in range(40):
                doc = documento(rng, piezas, rng.randint(0, 80))
                texto = normalize_text(doc)
                cuerpo = texto[:-1] if texto.endswith(b"\n") else texto
                lineas = (cuerpo.count(b"\n") + 1) if texto else 0
                r = count_lines(texto, lang)
                with self.subTest(lenguaje=nombre, doc=doc):
                    self.assertEqual(r.total, lineas)
                    self.assertGreaterEqual(min(r.code, r.comment, r.blank),
                                            0)

    def test_line_counts_add_up(self):
        a = LineCounts(3, 2, 1)
        a.add(LineCounts(1, 1, 1))
        self.assertEqual((a.code, a.comment, a.blank, a.total), (4, 3, 2, 9))


class SpeedTests(unittest.TestCase):

    def test_a_megabyte_of_code_takes_well_under_a_second(self):
        """No es una prueba de rendimiento fina (esas van aparte): solo
        caza una regresión catastrófica, como volver a ir byte a byte."""
        bloque = (b'def f(x):\n    # comentario\n    s = "http://x#y"\n'
                  b'    return x  # cola\n\n') * 12000
        t0 = time.perf_counter()
        r = count_lines(bloque, get("Python"))
        dt = time.perf_counter() - t0
        self.assertEqual((r.code, r.comment, r.blank),
                         (3 * 12000, 12000, 12000))
        self.assertLess(dt, 1.0, f"{len(bloque)} bytes en {dt:.2f} s")


if __name__ == "__main__":
    unittest.main()
