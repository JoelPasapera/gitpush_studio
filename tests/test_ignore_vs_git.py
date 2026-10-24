# -*- coding: utf-8 -*-
"""El comparador de ignorados, contrastado contra GIT DE VERDAD.

`IgnoreMatcher` se usa cuando la carpeta todavía no es un repositorio y
para filtrar lo que entra en el TXT del proyecto. Si sus reglas no son
las de git, el usuario escribe un patrón en el cuadro de ignorar, ve que
se escribe en el .gitignore, da por hecho que ese archivo queda fuera…
y acaba publicado o dentro del TXT, que lleva todo el código junto.

Por eso aquí no se comprueban casos elegidos a mano: se monta un
repositorio con archivos reales y, para cada patrón, se compara archivo
por archivo lo que dice `git check-ignore` con lo que dice el
comparador. Cualquier regla de git que no entendamos sale aquí.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

from tests.gitenv import HAVE_GIT

from gitpush_studio.core.ignore import IgnoreMatcher, anchor_pattern

ARCHIVOS = [
    ".env", "sub/.env", "a/b/.env", "main.py", "src/app.js",
    "build/out.o", "a/build/out.o", "notas.log", "sub/notas.log",
    "datos[2024].csv", "datos2024.csv", "temp1", "temp/x", "x.tmp",
    "a/b/c.tmp", "secretos/clave.txt", "a/secretos/clave.txt",
    "Datos/tabla.csv", "datos/tabla.csv", "doc/guía.md", "con espacio.txt",
    "node_modules/p/i.js", "a/node_modules/p/i.js", "keep.txt",
    "informe final .txt", "back\\slash.txt",
    # Casos que distinguen una regla de otra; sin ellos, una traducción
    # equivocada pasaría la comparación por casualidad:
    "doc/sub/nota.md",          # ¿«doc/*.md» cruza la barra?
    "datos[2024]x.csv",         # ¿se mezclan comodines escapados y no?
    "a/build/dentro/hondo.o",   # ¿una carpeta excluida arrastra lo suyo?
    "bu/x.txt", "a/bu/y.txt",
]

# Lo que un usuario escribe de verdad en el cuadro de ignorar.
PATRONES = [
    "**/.env", ".env", "/.env", "*.log", "**/*.log", "build/", "**/build/",
    "secretos/**", "secretos/*", "**/secretos/*", "[Dd]atos/*.csv",
    "node_modules/", "*.tmp", "temp?", "a/b/*.tmp", "/main.py",
    "doc/*.md", "**/*.js", "datos\\[2024\\].csv", "sub/**", "a/**/.env",
    "*", "?.tmp", "con espacio.txt", "[!k]*.txt", "a/**/*.tmp",
    "**/temp?", "/sub/", "doc/", "*.o",
    # Y los patrones que distinguen esas reglas:
    "a/bu*",                    # carpeta por comodín: arrastra su contenido
    "a/bu*/",
    "datos\\[2024\\]*.csv",     # escapado Y comodín en el mismo tramo
    "doc/*/nota.md",
    "a/*/dentro",
    # «/**/x» es «x a cualquier profundidad»: la «/» inicial no ancla.
    "/**/.env", "/**/build/", "/**/notas.log", "/**/secretos/clave.txt",
]


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class MatcherAgreesWithGit(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", cls.tmp], check=True,
                       capture_output=True)
        cls.creados = []
        for rel in ARCHIVOS:
            ruta = os.path.join(cls.tmp, rel)
            try:
                os.makedirs(os.path.dirname(ruta) or cls.tmp, exist_ok=True)
                with open(ruta, "w", encoding="utf-8") as fh:
                    fh.write("x")
            except OSError:
                continue          # nombres que este sistema no admite
            cls.creados.append(rel)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def git_ignora(self, rel):
        return subprocess.run(["git", "check-ignore", "-q", "--", rel],
                              cwd=self.tmp,
                              capture_output=True).returncode == 0

    def escribir(self, *patrones):
        with open(os.path.join(self.tmp, ".gitignore"), "w",
                  encoding="utf-8") as fh:
            fh.write("\n".join(patrones) + "\n")

    def test_every_pattern_matches_exactly_what_git_matches(self):
        for pat in PATRONES:
            self.escribir(pat)
            matcher = IgnoreMatcher([pat])
            for rel in self.creados:
                with self.subTest(patron=pat, archivo=rel):
                    self.assertEqual(matcher.ignored_path(rel.lower()),
                                     self.git_ignora(rel))

    def test_several_patterns_at_once_also_agree(self):
        grupos = [["*.log", "build/", "**/.env"],
                  ["node_modules/", "*.tmp", "/main.py"],
                  ["secretos/**", "[Dd]atos/*.csv", "doc/*.md"]]
        for grupo in grupos:
            self.escribir(*grupo)
            matcher = IgnoreMatcher(grupo)
            for rel in self.creados:
                with self.subTest(patrones=grupo, archivo=rel):
                    self.assertEqual(matcher.ignored_path(rel.lower()),
                                     self.git_ignora(rel))

    def test_a_path_turned_into_a_pattern_excludes_only_that_path(self):
        """`anchor_pattern` convierte una ruta concreta en patrón. Git y
        el comparador tienen que excluir ESE archivo y ninguno más.

        La única diferencia admitida es por mayúsculas: el comparador no
        las distingue a propósito (en Windows y macOS el sistema de
        archivos tampoco), así que puede excluir «Datos/x» cuando se
        pidió «datos/x». Esa diferencia sobra por el lado seguro —deja
        FUERA del TXT algo que nadie pidió incluir—; la contraria sería
        la grave, y es la que esta prueba prohíbe.
        """
        for rel in self.creados:
            pat = anchor_pattern(rel)
            self.escribir(pat)
            matcher = IgnoreMatcher([pat])
            with self.subTest(archivo=rel):
                self.assertTrue(self.git_ignora(rel),
                                f"git no excluye «{rel}» con «{pat}»")
                self.assertTrue(matcher.ignored_path(rel.lower()),
                                f"el comparador no excluye «{rel}»")
            for otro in self.creados:
                if otro == rel:
                    continue
                mio = matcher.ignored_path(otro.lower())
                suyo = self.git_ignora(otro)
                with self.subTest(archivo=rel, otro=otro):
                    if mio == suyo:
                        continue
                    self.assertTrue(
                        mio and otro.lower() == rel.lower(),
                        f"con «{pat}»: git={suyo} comparador={mio} para "
                        f"«{otro}» (solo se admite diferir en mayúsculas)")

    def test_the_matcher_never_includes_what_git_excludes(self):
        """La dirección que importa: si git excluye un archivo, el
        comparador también. Al revés sobra por el lado seguro; así
        nunca entra en el TXT algo que el usuario mandó fuera."""
        for pat in PATRONES:
            self.escribir(pat)
            matcher = IgnoreMatcher([pat])
            for rel in self.creados:
                if not self.git_ignora(rel):
                    continue
                with self.subTest(patron=pat, archivo=rel):
                    self.assertTrue(
                        matcher.ignored_path(rel.lower()),
                        f"git excluye «{rel}» con «{pat}» y el comparador "
                        "lo dejaría pasar")


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class FolderWalkAgreesWithGit(unittest.TestCase):
    """El recorrido de carpetas SIN repositorio (el que decide qué entra
    en el TXT) contra lo que git dejaría fuera en esa misma carpeta:
    .gitignore anidados y .gitignore guardados con marca BOM."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def crear(self, rel, texto="x", bom=False):
        ruta = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(ruta) or self.tmp, exist_ok=True)
        with open(ruta, "wb") as fh:
            fh.write((b"\xef\xbb\xbf" if bom else b"")
                     + texto.encode("utf-8"))

    def comparar(self):
        from gitpush_studio.core.walk import project_entries
        mios, _ = project_entries(self.tmp, use_git=False)
        mios = sorted(r for r, _, _ in mios)
        subprocess.run(["git", "init", "-q", self.tmp], check=True,
                       capture_output=True)
        p = subprocess.run(["git", "ls-files", "-z", "--others",
                            "--exclude-standard"], cwd=self.tmp,
                           capture_output=True)
        de_git = sorted(x.decode("utf-8") for x in p.stdout.split(b"\0")
                        if x)
        # Lo grave sería incluir algo que git deja fuera.
        sobran = sorted(set(mios) - set(de_git))
        self.assertEqual(sobran, [], "entrarían en el TXT aunque git los "
                                     "excluye")
        self.assertEqual(mios, de_git)

    def test_nested_gitignore_files(self):
        self.crear("api/.gitignore", "secrets.yml\n/local/\n")
        self.crear("api/secrets.yml", "clave: 1")
        self.crear("api/local/datos.txt")
        self.crear("api/app.py")
        self.crear("web/secrets.yml")          # fuera del alcance de api/
        self.crear("web/local/datos.txt")
        self.crear("api/v2/.gitignore", "*.tmp\n")
        self.crear("api/v2/a.tmp")
        self.crear("api/v2/b.py")
        self.crear("x.tmp")                    # el *.tmp de v2 no llega aquí
        self.comparar()

    def test_a_gitignore_saved_with_bom(self):
        self.crear(".gitignore", ".env\n*.log\n", bom=True)
        self.crear(".env", "DB_PASSWORD=secreta")
        self.crear("app.log")
        self.crear("main.py")
        self.comparar()


if __name__ == "__main__":
    unittest.main()
