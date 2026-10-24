# -*- coding: utf-8 -*-
"""Fuzzing propio (semillas fijas, reproducible) de todo lo que lee datos
que no controlamos: salidas de git, nombres dentro de un .tar.gz,
configuraciones editadas a mano, cabeceras HTTP, textos hostiles.

No se comprueba «qué» devuelven (eso lo hacen las demás pruebas) sino
las INVARIANTES que nunca pueden romperse:

  · ninguna entrada lanza una excepción imprevista;
  · un nombre de un .tar.gz nunca sale del destino (ni absoluto, ni con
    «..», ni vacío);
  · una configuración, por rota que esté, se normaliza a los tipos
    esperados;
  · las búsquedas acaban en un tiempo acotado.
"""

import json
import random
import time
import unittest

from gitpush_studio.core import config, snapshot
from gitpush_studio.core.git import parse_name_status_z, parse_status_z
from gitpush_studio.core.gitstats import iter_records, parse_record
from gitpush_studio.core.integrate import Change
from gitpush_studio.core.net import retry_after
from gitpush_studio.core.refnames import branch_problem
from gitpush_studio.core.secret_rules import (find_secrets,
                                              looks_like_placeholder,
                                              name_rule)
from gitpush_studio.core.textenc import detect, to_utf8

RONDAS = 3000
_PIEZAS = ["a", "/", "\\", "..", ".", "\0", "\n", "\t", " ", ":", "*", "?",
           "~", "^", "@{", "-", "ñ", "é", "‮", "C:", "\\\\?\\", "%2e",
           "R100", "M", "A", "D", "??", "!!", "\x1e", "\x1f"]


def texto_raro(rnd, maximo=40):
    return "".join(rnd.choice(_PIEZAS) for _ in range(rnd.randint(0,
                                                                  maximo)))


def bytes_raros(rnd, maximo=200):
    return bytes(rnd.getrandbits(8) for _ in range(rnd.randint(0, maximo)))


class GitOutputParsers(unittest.TestCase):

    def test_name_status_and_status_never_crash(self):
        rnd = random.Random(1)
        for _ in range(RONDAS):
            entrada = texto_raro(rnd, 60)
            a, m, d = parse_name_status_z(entrada)
            self.assertTrue(all(isinstance(x, str) for x in a + m + d))
            self.assertIsInstance(parse_status_z(entrada), list)
            self.assertIsInstance(parse_status_z(entrada, True), list)

    def test_history_records_never_crash(self):
        rnd = random.Random(2)
        for _ in range(RONDAS):
            crudo = bytes_raros(rnd, 120)
            if rnd.random() < 0.5:
                crudo = b"\x1e" + crudo
            trozos = []
            i = 0
            while i < len(crudo):
                n = rnd.randint(1, 16)
                trozos.append(crudo[i:i + n])
                i += n
            for registro in iter_records(trozos):
                parse_record(registro)          # None o una tupla: nunca lanza

    def test_change_objects_accept_any_modes(self):
        rnd = random.Random(3)
        for _ in range(500):
            c = Change(texto_raro(rnd), rnd.choice(["000000", "100644",
                                                    "160000", "120000", "x"]),
                       "0" * 40, rnd.choice(["000000", "100755", "160000"]),
                       "f" * 40)
            self.assertIsInstance(c.added, bool)
            self.assertIsInstance(c.gitlink, bool)


class ArchiveNames(unittest.TestCase):

    def test_a_tar_member_name_never_escapes(self):
        rnd = random.Random(4)
        for _ in range(RONDAS * 2):
            nombre = texto_raro(rnd, 30)
            seguro = snapshot.safe_name(nombre)
            if not seguro:
                continue
            partes = seguro.split("/")
            self.assertFalse(seguro.startswith("/"), nombre)
            self.assertNotIn("..", partes, nombre)
            self.assertNotIn("", partes, nombre)
            self.assertNotIn(".", partes, nombre)
            self.assertFalse(len(seguro) > 1 and seguro[1] == ":"
                             and seguro[0].isalpha(), nombre)


class UntrustedConfiguration(unittest.TestCase):

    def valor(self, rnd, hondo=0):
        tipo = rnd.randint(0, 6 if hondo < 3 else 3)
        if tipo == 0:
            return None
        if tipo == 1:
            return rnd.choice([True, False, 0, -1, 10 ** 30, 2.5, "NaN"])
        if tipo == 2:
            return texto_raro(rnd, 10)
        if tipo == 3:
            return rnd.choice(["mirror", "addedit", "10", "30", "abc"])
        if tipo == 4:
            return [self.valor(rnd, hondo + 1) for _ in range(rnd.randint(0,
                                                                          3))]
        return {texto_raro(rnd, 6) or "k": self.valor(rnd, hondo + 1)
                for _ in range(rnd.randint(0, 4))}

    def test_any_json_shape_is_normalised(self):
        rnd = random.Random(5)
        claves = ["profiles", "last_profile", "last_state", "version",
                  "github_token", "otra"]
        for _ in range(RONDAS):
            datos = {c: self.valor(rnd) for c in claves if rnd.random() < 0.7}
            datos = json.loads(json.dumps(datos))     # solo lo que es JSON
            limpio, _ = config._migrate(datos)
            self.assertIsInstance(limpio["profiles"], dict)
            self.assertTrue(all(isinstance(k, str) and isinstance(v, dict)
                                for k, v in limpio["profiles"].items()))
            self.assertIsInstance(limpio["last_state"], dict)
            self.assertTrue(limpio["last_profile"] is None
                            or isinstance(limpio["last_profile"], str))
            estado = config.clean_state(self.valor(rnd))
            self.assertIn(estado.get("auto_interval", 10), config.INTERVALS)


class TextAndHeaders(unittest.TestCase):

    def test_encoding_detection_never_crashes(self):
        rnd = random.Random(6)
        for _ in range(RONDAS):
            crudo = bytes_raros(rnd, 300)
            codificacion, bom = detect(crudo)
            self.assertIn(codificacion, (None, "utf-8", "utf-16-le",
                                         "utf-16-be"))
            salida = to_utf8(crudo)
            if salida is not None:
                salida.decode("utf-8", "strict" if codificacion != "utf-8"
                              else "replace")

    def test_retry_after_is_always_bounded(self):
        rnd = random.Random(7)
        for _ in range(RONDAS):
            v = rnd.choice([texto_raro(rnd, 8), str(rnd.uniform(-1e12, 1e12)),
                            "inf", "-inf", "nan", "", "1e400"])
            s = retry_after({"Retry-After": v}, default=1.5)
            self.assertTrue(0 <= s <= 120, (v, s))

    def test_branch_names_and_placeholders_never_crash(self):
        rnd = random.Random(8)
        for _ in range(RONDAS):
            t = texto_raro(rnd, 20)
            self.assertIsInstance(branch_problem(t), str)
            self.assertIsInstance(looks_like_placeholder(t), bool)
            name_rule(t)

    def test_secret_search_is_bounded_on_random_text(self):
        rnd = random.Random(9)
        piezas = [b"password", b"=", b":", b"'", b'"', b"ghp_", b"sk-",
                  b"://", b"@", b"key", b"api_", b"token", b"AKIA", b"\n",
                  b"-----BEGIN ", b"PRIVATE KEY-----", b"x" * 40, b" "]
        t0 = time.time()
        for _ in range(400):
            texto = b"".join(rnd.choice(piezas)
                             for _ in range(rnd.randint(0, 400)))
            r = find_secrets(texto, rnd.choice(["a.py", ".env", "x.yml"]))
            self.assertTrue(r is None or r[1] in ("alta", "media", "baja"))
        self.assertLess(time.time() - t0, 30)


if __name__ == "__main__":
    unittest.main()
