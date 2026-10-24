# -*- coding: utf-8 -*-
"""La revisión de secretos: lo que se le escapaba y lo que marcaba de más.

Cada caso viene de un fallo confirmado:

  · un valor de ejemplo ENCIMA de la contraseña de verdad la tapaba (se
    miraba solo la primera coincidencia) y se publicaba en automático;
  · UTF-16 (PowerShell) y archivos de más de 1 MB no se leían nunca;
  · faltaban formatos actuales (gho_, ASIA, la clave secreta de AWS,
    claves de Google que acaban en «-», xapp-, Stripe sin «api_key»,
    PGP, cadenas de conexión, KEY=valor sin comillas…);
  · 300 avisos por nombre dejaban el contenido sin revisar;
  · y en sentido contrario, avisos falsos que enseñan a ignorar avisos:
    el «cacert.pem» público, una presentación «charla.key», la etiqueta
    «password: Contraseña» de un archivo de traducciones.

Los tokens de prueba se arman al ejecutar (variados, sin secuencias):
este archivo no lleva ninguno escrito, así que la propia revisión no lo
marca al publicar el proyecto.
"""

import os
import random
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from gitpush_studio.core import scan
from gitpush_studio.core.scan import scan_entries
from gitpush_studio.core.secret_rules import (ALTA, BAJA, MEDIA,
                                              entropy, find_secrets)
from gitpush_studio.core.textenc import detect, to_utf8

_ALFA = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"


def tok(n, seed=1, alfabeto=_ALFA):
    rnd = random.Random(seed)
    return "".join(rnd.choice(alfabeto) for _ in range(n))


def write(folder, rel, content):
    p = Path(folder) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content if isinstance(content, bytes)
                  else content.encode("utf-8"))
    return p


class ScanCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self, **kw):
        entries = []
        for base, dirs, files in os.walk(self.tmp):
            for f in files:
                full = os.path.join(base, f)
                rel = os.path.relpath(full, self.tmp).replace(os.sep, "/")
                entries.append((rel, full, os.lstat(full).st_size))
        findings, stats = scan_entries(sorted(entries), **kw)
        return {f["path"]: f for f in findings}, stats


class RealSecretsAreFound(ScanCase):

    def test_the_real_value_below_an_example_is_not_hidden(self):
        real = "Kp9vR2mQ7zW4"
        write(self.tmp, "settings.py",
              '# ejemplo: password = "changeme123"\n'
              f'password = "{real}"\n')
        write(self.tmp, "config/database.yml",
              'development:\n  password: "changeme123"\n'
              f'production:\n  password: "{real}"\n')
        hallados, _ = self.scan()
        self.assertEqual(hallados["settings.py"]["confidence"], MEDIA)
        self.assertIn("línea 2", hallados["settings.py"]["reason"])
        self.assertEqual(hallados["config/database.yml"]["confidence"], MEDIA)
        self.assertEqual(hallados["config/database.yml"]["line"], 4)

    def test_utf16_with_and_without_bom(self):
        linea = f"token=ghp_{tok(36)}\r\n"
        write(self.tmp, "con_bom.txt", b"\xff\xfe" + linea.encode("utf-16-le"))
        write(self.tmp, "sin_bom.txt", linea.encode("utf-16-le"))
        write(self.tmp, "big_endian.txt",
              b"\xfe\xff" + linea.encode("utf-16-be"))
        hallados, _ = self.scan()
        for rel in ("con_bom.txt", "sin_bom.txt", "big_endian.txt"):
            self.assertEqual(hallados[rel]["confidence"], ALTA, rel)
            self.assertIn("GitHub", hallados[rel]["reason"])

    def test_files_over_one_megabyte_are_read(self):
        relleno = '{"cell_type": "code", "source": ["print(1)"]},\n' * 30000
        clave = "sk-proj-" + tok(48, seed=3)
        write(self.tmp, "analisis.ipynb",
              relleno + f'"openai.api_key = \'{clave}\'"\n')
        hallados, stats = self.scan()
        self.assertGreater((self.tmp / "analisis.ipynb").stat().st_size,
                           scan.SMALL_FILE)
        self.assertEqual(hallados["analisis.ipynb"]["confidence"], ALTA)
        self.assertEqual(hallados["analisis.ipynb"]["line"], 30001)

    def test_a_secret_cut_between_two_chunks_is_still_seen(self):
        token = "ghp_" + tok(36, seed=4)
        delante = "x" * (scan.CHUNK - 10) + "\n"
        write(self.tmp, "volcado.sql", delante + token + "\n" + "y" * 5000)
        hallados, _ = self.scan()
        self.assertEqual(hallados["volcado.sql"]["confidence"], ALTA)
        self.assertEqual(hallados["volcado.sql"]["line"], 2)

    def test_current_token_formats(self):
        casos = {
            "a.txt": f"gho_{tok(36, 5)}",
            "b.txt": f"ghs_{tok(36, 6)}",
            "c.txt": f"ASIA{tok(16, 7, 'ABCDEFGHJKLMNPQRSTUVWXYZ234567')}",
            "credentials": "[default]\naws_secret_access_key = "
                           + tok(40, 8, _ALFA + "/+"),
            "d.js": "const k = 'AIza" + tok(34, 9) + "-';",
            "e.txt": f"xapp-1-A{tok(10, 10)}-{tok(12, 11)}",
            "f.txt": f"xoxe-1-{tok(40, 12)}",
            "settings.py": f'STRIPE_KEY = "sk_live_{tok(24, 13)}"',
            "clave.asc": "-----BEGIN PGP PRIVATE KEY BLOCK-----\n\n"
                         + tok(64, 14, _ALFA + "+/") + "\n",
            "docker-compose.yml":
                f"DATABASE_URL: postgres://app:{tok(14, 15)}@db:5432/app",
            "g.env.yml": f"MONGO: mongodb+srv://u:{tok(12, 16)}@c.net/db",
            "application.properties": f"db.password={tok(14, 17)}",
            "prod.tfvars": f'api_token = "{tok(30, 18)}"',
            ".envrc": f"export SECRET_KEY={tok(30, 19)}",
            ".pypirc": "[pypi]\nusername = __token__\npassword = pypi-AgE"
                       + tok(60, 20) + "\n",
        }
        for rel, texto in casos.items():
            write(self.tmp, rel, texto + "\n")
        write(self.tmp, ".git-credentials", "https://u:x@github.com\n")
        write(self.tmp, "release.jks", b"\xfe\xed\xfe\xed\x00\x00")
        hallados, _ = self.scan()
        for rel in list(casos) + [".git-credentials", "release.jks"]:
            with self.subTest(archivo=rel):
                self.assertIn(rel, hallados)
                self.assertIn(hallados[rel]["confidence"], (ALTA, MEDIA))

    def test_many_name_warnings_never_skip_the_content(self):
        for i in range(350):
            write(self.tmp, f"claves/id_rsa_{i:03d}", "x")
        write(self.tmp, "app.py", f'TOKEN = "ghp_{tok(36, 21)}"\n')
        hallados, stats = self.scan()
        self.assertIn("app.py", hallados)
        self.assertEqual(hallados["app.py"]["confidence"], ALTA)
        self.assertTrue(stats["truncated"])

    def test_a_text_too_big_to_read_is_reported_not_silenced(self):
        write(self.tmp, "volcado.csv", "a,b\n" * 1000)
        with mock.patch.object(scan, "MAX_SCAN_FILE", 1000), \
                mock.patch.object(scan, "SMALL_FILE", 500):
            hallados, stats = self.scan()
        self.assertEqual(hallados["volcado.csv"]["confidence"], BAJA)
        self.assertIn("sin revisar", hallados["volcado.csv"]["reason"])
        self.assertEqual(stats["unscanned"], 1)


class RealPasswordsAreNotTakenForExamples(unittest.TestCase):
    """«Parece un valor de ejemplo» baja la confianza, y la publicación
    automática NO se detiene por eso. Antes bastaba un «$» dentro de la
    contraseña, o una palabra como «test» o «here» en medio
    («Contest2024!», «Here4You!»), para tomarla por un ejemplo."""

    REALES = ("Xk9#mQ2$vL7pR4wZ", "Contest2024!", "Here4You!2024",
              "Password123", "MyPassword2024!", "Admin2024!",
              "Mi$ecreto2024", "S3cure<Pass>99", "Tr0ub4dor&3")
    EJEMPLOS = ("changeme123", "your_password_here", "${DB_PASSWORD}",
                "<tu-contraseña>", "{{ db_password }}", "$DB_PASSWORD",
                "%DB_PASSWORD%", "xxxxxxxxxx", "insert-password-here",
                "tu_clave_aqui", "yourPasswordHere", "dummy-secret")

    def test_real_passwords_are_reported_as_credible(self):
        for valor in self.REALES:
            for rel, linea in ((".env", f'DB_PASSWORD="{valor}"\n'),
                               ("settings.py", f'password = "{valor}"\n')):
                with self.subTest(valor=valor, archivo=rel):
                    r = find_secrets(linea.encode("utf-8"), rel)
                    self.assertIsNotNone(r)
                    self.assertEqual(r[1], MEDIA)

    def test_examples_and_templates_stay_low(self):
        for valor in self.EJEMPLOS:
            with self.subTest(valor=valor):
                r = find_secrets(f'DB_PASSWORD="{valor}"\n'.encode("utf-8"),
                                 ".env")
                self.assertIsNotNone(r)
                self.assertEqual(r[1], BAJA)

    def test_a_discarded_match_does_not_hide_the_next_one(self):
        """La primera coincidencia de la regla se descarta (en código y
        sin comillas es una expresión); la de debajo es la contraseña."""
        r = find_secrets(b'password = get_password()\n'
                         b'password = "Xk9#mQ2$vL7pR4wZ"\n', "app.py")
        self.assertIsNotNone(r)
        self.assertEqual((r[1], r[2]), (MEDIA, 26))

    def test_a_shell_variable_inside_a_connection_url_is_low(self):
        for var in (b"$DB_PASS", b"$db_pass"):
            r = find_secrets(b"postgres://app:" + var + b"@db:5432/app\n",
                             "deploy.sh")
            self.assertEqual(r[1], BAJA, var)
        r = find_secrets(b"postgres://app:Kp9vR2mQ7zW4@db:5432/app\n",
                         "deploy.sh")
        self.assertIn(r[1], (ALTA, MEDIA))


class NoiseIsNotAFinding(ScanCase):

    def test_public_certificates_and_keynote_files(self):
        cert = ("-----BEGIN CERTIFICATE-----\n" + tok(64, 30, _ALFA + "+/")
                + "\n-----END CERTIFICATE-----\n") * 20
        write(self.tmp, "certifi/cacert.pem", cert)
        write(self.tmp, "charla.key", b"PK\x03\x04" + b"\x00" * 200)
        write(self.tmp, "web.css", ".sk-loader-spinner-container-wrapper "
                                   "{ color: red }\n")
        write(self.tmp, "config/locales/es.yml",
              "es:\n  password: \"Contraseña\"\n  token: \"Código de "
              "verificación\"\n")
        write(self.tmp, "app.py", 'password = os.environ["DB_PASSWORD"]\n'
                                  "token = request.headers.get('X')\n"
                                  "TOKEN_URL = 'https://example.org/token'\n"
                                  "token_expiry = 3600\n")
        hallados, _ = self.scan()
        for rel in ("certifi/cacert.pem", "charla.key", "web.css", "app.py"):
            self.assertNotIn(rel, hallados, rel)
        self.assertEqual(hallados["config/locales/es.yml"]["confidence"],
                         BAJA)

    def test_documentation_placeholders_are_low(self):
        write(self.tmp, "README.md",
              "export GITHUB_TOKEN=ghp_" + "x" * 36 + "\n"
              "AWS key: AKIAIOSFODNN7EXAMPLE\n"
              "postgres://user:password@localhost/app\n"
              "-----BEGIN OPENSSH PRIVATE KEY-----\n...\n")
        hallados, _ = self.scan()
        self.assertEqual(hallados["README.md"]["confidence"], BAJA)

    def test_a_binary_key_file_is_still_flagged(self):
        write(self.tmp, "servidor.key", b"\x30\x82\x04\xa4\x02\x01\x00" * 20)
        hallados, _ = self.scan()
        self.assertEqual(hallados["servidor.key"]["confidence"], MEDIA)


class ScanNeverHangsNorLeaves(ScanCase):

    @unittest.skipUnless(hasattr(os, "mkfifo"), "sin tuberías con nombre")
    def test_a_fifo_does_not_hang_the_review(self):
        os.mkfifo(str(self.tmp / "tuberia.txt"))
        entradas = [("tuberia.txt", str(self.tmp / "tuberia.txt"), 4096)]
        hecho = []
        hilo = threading.Thread(target=lambda: hecho.append(
            scan_entries(entradas)), daemon=True)
        hilo.start()
        hilo.join(20)
        self.assertFalse(hilo.is_alive(), "la revisión se quedó colgada")

    def test_links_are_not_followed_out_of_the_project(self):
        fuera = Path(tempfile.mkdtemp())
        try:
            write(fuera, "id_ed25519", "-----BEGIN OPENSSH PRIVATE KEY-----\n"
                  + tok(70, 40, _ALFA + "+/") + "\n")
            try:
                os.symlink(str(fuera / "id_ed25519"),
                           str(self.tmp / "notas.md"))
            except (OSError, NotImplementedError):
                self.skipTest("este sistema no permite crear enlaces")
            hallados, _ = self.scan()
            self.assertNotIn("notas.md", hallados)
        finally:
            shutil.rmtree(fuera, ignore_errors=True)


class Helpers(unittest.TestCase):

    def test_text_encoding_detection(self):
        self.assertEqual(detect(b"hola")[0], "utf-8")
        self.assertEqual(detect(b"\xef\xbb\xbfhola"), ("utf-8", 3))
        self.assertEqual(detect("hola mundo".encode("utf-16-le"))[0],
                         "utf-16-le")
        self.assertIsNone(detect(bytes(range(256)))[0])
        self.assertIsNone(detect(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")[0])
        self.assertEqual(to_utf8(b"\xff\xfe" + "ñandú".encode("utf-16-le")),
                         "ñandú".encode("utf-8"))

    def test_entropy(self):
        self.assertEqual(entropy(b""), 0.0)
        self.assertEqual(entropy(b"aaaa"), 0.0)
        self.assertAlmostEqual(entropy(b"abcd"), 2.0)

    def test_name_index_agrees_with_fnmatch(self):
        """El índice de patrones de nombre tiene que decir lo mismo que
        recorrer la lista con `fnmatch`, solo que más deprisa."""
        import fnmatch
        from gitpush_studio.core import secret_rules as R

        def a_mano(rel):
            low = rel.lower()
            name = low.rsplit("/", 1)[-1]
            if any(fnmatch.fnmatchcase(name, p) for p in R.NAME_EXCLUDE):
                return None
            for patron, motivo, conf in R.NAME_RULES:
                if fnmatch.fnmatchcase(name, patron):
                    return motivo, conf
            for sufijo, motivo, conf in R.PATH_RULES:
                if low == sufijo or low.endswith("/" + sufijo):
                    return motivo, conf
            return None

        rnd = random.Random(11)
        piezas = [".env", ".", "env", "id_rsa", ".pub", "x", ".pem", "key",
                  "service-account", ".json", "-credentials", ".tfstate",
                  ".backup", "secrets", ".yml", "a/", "kubeconfig",
                  ".example", "_netrc", ".p12", ".aws/credentials"]
        for _ in range(20000):
            nombre = "".join(rnd.choice(piezas)
                             for _ in range(rnd.randint(1, 5)))
            self.assertEqual(R.name_rule(nombre), a_mano(nombre), nombre)

    def test_rules_are_fast_on_big_noisy_text(self):
        """Ninguna regla puede volverse cuadrática con texto hostil."""
        import time
        hostil = (b"password" * 50 + b"=" + b"a" * 50 + b" ") * 2000
        t0 = time.time()
        find_secrets(hostil, "a.env")
        self.assertLess(time.time() - t0, 5.0)


if __name__ == "__main__":
    unittest.main()
