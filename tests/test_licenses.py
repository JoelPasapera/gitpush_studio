# -*- coding: utf-8 -*-
"""Añadir LICENSE al publicar.

Lo importante aquí es que el texto legal salga exacto (no se inventa ni
se reformatea, solo se rellenan los huecos del propio texto) y que nunca
se pise una licencia que el proyecto ya tenga.
"""

import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.gitenv import HAVE_GIT, init_bare

from gitpush_studio.core import github, licenses
from gitpush_studio.core.publish import Callbacks, PublishJob, publish

# Suma de comprobación del texto oficial de Apache 2.0.
APACHE_MD5 = "86d3f3a95c324c9479bd8986968f4327"


def write(folder, rel, content="x\n"):
    p = Path(folder) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def remote_files(bare, branch="main"):
    p = subprocess.run(["git", "ls-tree", "-r", "--name-only", branch],
                       cwd=bare, capture_output=True, text=True)
    return sorted(x for x in p.stdout.splitlines() if x)


def remote_file(bare, rel, branch="main"):
    return subprocess.run(["git", "show", f"{branch}:{rel}"], cwd=bare,
                          capture_output=True, text=True).stdout


class CatalogTests(unittest.TestCase):

    def test_the_three_bundled_ones_work_without_network(self):
        with mock.patch.object(github, "api",
                               side_effect=AssertionError("sin red")):
            for key in licenses.BUNDLED:
                self.assertTrue(licenses.license_text(key), key)

    def test_apache_text_is_the_official_one_byte_for_byte(self):
        text = licenses.license_text("apache-2.0")
        self.assertEqual(hashlib.md5(text.encode("utf-8")).hexdigest(),
                         APACHE_MD5)
        self.assertIn("Copyright [yyyy] [name of copyright owner]", text)

    def test_every_catalog_entry_is_usable(self):
        self.assertEqual(licenses.CATALOG[0][0], licenses.DEFAULT)
        self.assertEqual(licenses.DEFAULT, "apache-2.0")
        for key, name, what in licenses.CATALOG:
            self.assertTrue(key and name and what)
            self.assertEqual(licenses.display_name(key), name)

    def test_normalize_key_accepts_key_name_or_nothing(self):
        self.assertEqual(licenses.normalize_key("mit"), "mit")
        self.assertEqual(licenses.normalize_key("Apache 2.0"), "apache-2.0")
        self.assertEqual(licenses.normalize_key("MIT"), "mit")
        self.assertEqual(licenses.normalize_key(""), "")
        self.assertEqual(licenses.normalize_key("ninguna"), "")
        self.assertIsNone(licenses.normalize_key("inventada"))

    def test_fill_replaces_only_the_holes(self):
        text = licenses.license_text("mit")
        filled = licenses.fill(text, "Ana Pérez", 2024)
        self.assertIn("Copyright (c) 2024 Ana Pérez", filled)
        self.assertNotIn("[year]", filled)
        self.assertNotIn("[fullname]", filled)
        # El resto del texto no se toca.
        self.assertEqual(len(filled.splitlines()), len(text.splitlines()))
        self.assertIn('THE SOFTWARE IS PROVIDED "AS IS"', filled)

    def test_gpl_holes_are_filled_too(self):
        filled = licenses.fill(licenses.license_text("gpl-3.0"), "Ana", 2024)
        self.assertIn("Copyright (C) 2024  Ana", filled)
        self.assertNotIn("<year>", filled)


class WriteTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_the_license_with_year_and_owner(self):
        name, msg = licenses.write_license(self.tmp, "mit", "Ana", 2024)
        self.assertEqual(name, "LICENSE")
        self.assertIn("MIT", msg)
        text = (self.tmp / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2024 Ana", text)
        self.assertTrue(text.endswith("\n"))

    def test_never_overwrites_an_existing_license(self):
        for existente in ("LICENSE", "LICENSE.md", "license.txt", "COPYING",
                          "LICENCE", "LICENSE-MIT", "LICENSE-APACHE",
                          "COPYING.LESSER", "COPYING.LIB", "UNLICENSE",
                          "LICENSE.markdown", "license.html", "COPYRIGHT"):
            carpeta = self.tmp / existente.lower().replace(".", "_")
            carpeta.mkdir()
            write(carpeta, existente, "MI LICENCIA PROPIA\n")
            name, msg = licenses.write_license(carpeta, "mit", "Ana")
            self.assertEqual(name, "", existente)
            self.assertIn("ya tiene", msg)
            self.assertEqual((carpeta / existente).read_text("utf-8"),
                             "MI LICENCIA PROPIA\n")
            self.assertEqual(sorted(p.name for p in carpeta.iterdir()),
                             [existente])

    def test_a_licenses_folder_counts_as_a_license(self):
        """Estándar REUSE: las licencias viven en LICENSES/."""
        write(self.tmp, "LICENSES/MIT.txt", "MIT\n")
        name, msg = licenses.write_license(self.tmp, "apache-2.0", "Ana")
        self.assertEqual(name, "")
        self.assertFalse((self.tmp / "LICENSE").exists())

    def test_a_license_appearing_meanwhile_is_not_overwritten(self):
        """Entre «¿hay licencia?» y escribirla, otro programa puede crearla:
        la creación es exclusiva, nunca encima."""
        write(self.tmp, "LICENSE", "LA DEL USUARIO\n")
        with mock.patch.object(licenses, "existing_license",
                               return_value=""):
            name, msg = licenses.write_license(self.tmp, "mit", "Ana")
        self.assertEqual(name, "")
        self.assertEqual((self.tmp / "LICENSE").read_text("utf-8"),
                         "LA DEL USUARIO\n")
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["LICENSE"])

    def test_a_readme_is_not_mistaken_for_a_license(self):
        write(self.tmp, "README.md")
        write(self.tmp, "licensing-notes.md")
        self.assertEqual(licenses.existing_license(self.tmp), "")
        name, _ = licenses.write_license(self.tmp, "mit", "Ana")
        self.assertEqual(name, "LICENSE")

    def test_without_owner_it_refuses_to_write_a_half_filled_license(self):
        name, msg = licenses.write_license(self.tmp, "mit", "  ")
        self.assertEqual(name, "")
        self.assertIn("a nombre de quién", msg)
        self.assertFalse((self.tmp / "LICENSE").exists())

    def test_no_license_writes_nothing(self):
        name, msg = licenses.write_license(self.tmp, "", "Ana")
        self.assertEqual(name, "")
        self.assertIn("Sin licencia", msg)
        self.assertEqual(list(self.tmp.iterdir()), [])


class FetchTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patch = mock.patch.object(licenses, "CACHE_DIR",
                                       self.tmp / "cache")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_non_bundled_license_is_downloaded_once_and_cached(self):
        llamadas = []

        def fake_api(method, path, token="", payload=None, **kw):
            llamadas.append(path)
            return 200, {"body": "TEXTO BSD [year] [fullname]\n"}
        with mock.patch.object(github, "api", fake_api):
            text = licenses.license_text("bsd-3-clause")
            self.assertIn("TEXTO BSD", text)
            self.assertEqual(llamadas, ["/licenses/bsd-3-clause"])
            # La segunda vez sale de la caché: no se vuelve a pedir.
            self.assertIn("TEXTO BSD", licenses.license_text("bsd-3-clause"))
            self.assertEqual(len(llamadas), 1)
        self.assertTrue((self.tmp / "cache" / "bsd-3-clause.txt").is_file())

    def test_without_network_it_says_so_instead_of_inventing_text(self):
        carpeta = self.tmp / "proj"
        carpeta.mkdir()
        with mock.patch.object(github, "api",
                               side_effect=OSError("sin red")):
            name, msg = licenses.write_license(carpeta, "mpl-2.0", "Ana")
        self.assertEqual(name, "")
        self.assertIn("conexión", msg)
        self.assertFalse((carpeta / "LICENSE").exists())

    def test_an_api_error_is_not_cached(self):
        with mock.patch.object(github, "api", lambda *a, **k: (404, {})):
            self.assertEqual(licenses.license_text("epl-2.0"), "")
        self.assertFalse((self.tmp / "cache" / "epl-2.0.txt").exists())


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class PublishWithLicenseTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bare = init_bare(self.tmp)
        self.proj = Path(self.tmp) / "proj"
        write(self.proj, "a.txt", "A\n")
        self.logs = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def job(self, **kw):
        base = dict(folder=str(self.proj), url=self.bare, user="Ana Pérez",
                    email="ana@x.y", preview=False, check=False)
        base.update(kw)
        return PublishJob(**base)

    def cb(self):
        return Callbacks(log=lambda t, tag="out": self.logs.append(t))

    def test_publishing_adds_the_license_to_github(self):
        r = publish(self.job(license="apache-2.0"), self.cb())
        self.assertEqual(r.status, "ok", r.message)
        self.assertIn("LICENSE", remote_files(self.bare))
        texto = remote_file(self.bare, "LICENSE")
        self.assertIn("Apache License", texto)
        self.assertIn(f"Copyright {__import__('datetime').date.today().year} "
                      "Ana Pérez", texto)
        self.assertTrue(any("Apache 2.0" in t for t in self.logs))

    def test_the_owner_falls_back_to_the_github_user(self):
        job = self.job(license="mit", user="", email="",
                       url="https://github.com/JoelPasapera/demo.git")
        # No se publica (esa URL no existe), pero el paso de licencia sí corre.
        publish(job, self.cb())
        texto = (self.proj / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("JoelPasapera", texto)

    def test_an_explicit_owner_wins(self):
        publish(self.job(license="mit", license_owner="Editorial Equis"),
                self.cb())
        self.assertIn("Editorial Equis",
                      (self.proj / "LICENSE").read_text(encoding="utf-8"))

    def test_republishing_does_not_touch_the_license(self):
        self.assertEqual(publish(self.job(license="mit"), self.cb()).status,
                         "ok")
        original = (self.proj / "LICENSE").read_text(encoding="utf-8")
        (self.proj / "LICENSE").write_text(original + "\nMi añadido\n",
                                           encoding="utf-8")
        mio = (self.proj / "LICENSE").read_text(encoding="utf-8")
        r = publish(self.job(license="apache-2.0"), self.cb())   # otra distinta
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual((self.proj / "LICENSE").read_text("utf-8"), mio)
        self.assertIn("Mi añadido", remote_file(self.bare, "LICENSE"))

    def test_without_license_nothing_is_created(self):
        r = publish(self.job(), self.cb())
        self.assertEqual(r.status, "ok", r.message)
        self.assertFalse((self.proj / "LICENSE").exists())
        self.assertEqual(remote_files(self.bare), ["a.txt"])

    def test_a_download_failure_does_not_stop_the_publication(self):
        with mock.patch.object(github, "api", side_effect=OSError("sin red")), \
                mock.patch.object(licenses, "CACHE_DIR",
                                  Path(self.tmp) / "cache"):
            r = publish(self.job(license="mpl-2.0"), self.cb())
        self.assertEqual(r.status, "ok", r.message)          # se publica igual
        self.assertFalse((self.proj / "LICENSE").exists())
        self.assertEqual(remote_files(self.bare), ["a.txt"])
        self.assertTrue(any("conexión" in t for t in self.logs))

    def test_from_state_reads_the_license(self):
        job = PublishJob.from_state({"folder": str(self.proj),
                                     "url": self.bare, "license": "MIT",
                                     "license_owner": " Ana "})
        self.assertEqual(job.license, "mit")
        self.assertEqual(job.license_owner, "Ana")
        vacio = PublishJob.from_state({"folder": "x", "url": "y"})
        self.assertEqual(vacio.license, "")


if __name__ == "__main__":
    unittest.main()
