# -*- coding: utf-8 -*-
"""El TXT incluye cualquier archivo de texto ENTERO, mida lo que mida, y
el propio TXT no tiene tope de tamaño.

  · un archivo de 40 MB entra completo… sin cargarse nunca entero en
    memoria (se copia por bloques);
  · un TXT de más de 100 MB (el tope de antes) sale completo, sin la
    marca de «salida truncada»;
  · procesar por bloques da EXACTAMENTE lo mismo, byte a byte, que
    procesar el archivo de una vez, también con bloques de 1 byte que
    parten los caracteres UTF-8 por la mitad;
  · lo que es texto legible entra aunque no sea UTF-8: UTF-16 (con o sin
    BOM) y la codificación clásica de Windows (ANSI, cp1252) se
    convierten; un byte suelto roto en un UTF-8 es «�»;
  · un binario sin extensión conocida, por grande que sea, solo cuesta
    leer su principio;
  · un archivo que crece mientras se lee (un registro en uso) se copia
    tal como era al abrirlo y se marca: la copia no se alarga sin fin;
  · un fallo al leer a mitad deja una nota sin la ruta completa (el TXT
    se comparte); un fallo al ESCRIBIR (disco lleno) para todo;
  · «Detener», el disco a punto de llenarse o el disco lleno dejan el
    TXT anterior intacto y ningún temporal;
  · la lectura por delante tiene la memoria acotada y, si quien consume
    se detiene, ningún hilo sigue leyendo.
"""

import errno
import io
import os
import random
import shutil
import tempfile
import threading
import time
import tracemalloc
import unittest
from collections import deque, namedtuple
from pathlib import Path
from unittest import mock

from gitpush_studio.core import pack, publish as publish_mod, reader, txtbody
from gitpush_studio.core.errors import Cancelled, StepError
from gitpush_studio.core.pack import (consolidate_many, consolidate_project,
                                      write_modules)
from gitpush_studio.core.reader import (LARGE, Cut, Link, Special,
                                        iter_contents, iter_payloads,
                                        read_one, read_upto, read_whole)
from gitpush_studio.core.textenc import detect
from gitpush_studio.core.txtbody import (TextBody, small_body, stream_body)

POSIX = os.name == "posix"


def write(folder, rel, content):
    p = Path(folder) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content if isinstance(content, bytes)
                  else content.encode("utf-8"))
    return p


def by_chunks(raw, size):
    """El cuerpo de `raw` pasándolo a TextBody en trozos de `size`."""
    codificacion, bom = detect(raw, strict=True)
    if codificacion is None:
        return None
    partes = []
    cuerpo = TextBody(partes.append, codificacion, bom)
    for i in range(0, len(raw), size):
        cuerpo.feed(raw[i:i + size])
    cuerpo.close()
    return b"".join(bytes(p) for p in partes)


def stream(path, chunk=txtbody.CHUNK, tick=None):
    """(estado, cuerpo, escrito) de copiar `path` con stream_body."""
    fd, size = reader.open_regular(str(path))
    out = io.BytesIO()
    try:
        estado, escrito, error = stream_body(fd, size, out.write, tick,
                                             chunk=chunk)
    finally:
        os.close(fd)
    return estado, out.getvalue(), escrito, error


# Piezas con las que se arman contenidos difíciles.
_PIEZAS = (
    b"hola", b" ", b"\n", b"\r\n", b"\t", b"  \n\n", "ñ".encode(),
    "€".encode(), "𝄞".encode(), "año".encode(), b"\xff", b"\xc3",
    b"\xe2\x82", b"\x80", b"\xf1", b"x" * 70, b" " * 70, b"\x0b\x0c",
)


class BodiesAreTheSameWhateverTheChunking(unittest.TestCase):
    """La propiedad que hace fiable la copia por bloques."""

    def test_random_contents_split_anywhere(self):
        rnd = random.Random(20261026)
        casos = [b"", b"\n", b"   ", "año\n".encode("cp1252"),
                 b"\xef\xbb\xbfhola\n", b"\xef\xbb\xbf", b"\xef\xbb",
                 "texto".encode("utf-16"), "texto\n".encode("utf-16-be"),
                 # UTF-16 con pares sustitutos que los bloques parten, y
                 # con un byte suelto al final.
                 ("\ufeff" + "abc 𝄞 déf\r\n" * 9).encode("utf-16-le"),
                 ("\ufeff" + "abc 𝄞 déf\r\n" * 9).encode("utf-16-be"),
                 ("\ufeff" + "x𝄞" * 20).encode("utf-16-le") + b"A",
                 ("línea de texto normal\n" * 9).encode("utf-16-le"),
                 b"abc\xc3", b"abc\xc3x", "ñ".encode() + b"\xff"]
        for _ in range(400):
            casos.append(b"".join(rnd.choice(_PIEZAS)
                                  for _ in range(rnd.randint(0, 40))))
        for raw in casos:
            entero = small_body(raw)
            for trozo in (1, 2, 3, 5, 7, 64, 4096):
                with self.subTest(raw=raw[:60], trozo=trozo):
                    self.assertEqual(by_chunks(raw, trozo), entero)

    def test_the_result_is_always_valid_utf8_ending_in_one_newline(self):
        rnd = random.Random(7)
        for _ in range(300):
            raw = b"".join(rnd.choice(_PIEZAS)
                           for _ in range(rnd.randint(0, 30)))
            cuerpo = small_body(raw)
            if cuerpo is None:
                continue
            cuerpo.decode("utf-8")                   # no lanza
            self.assertTrue(cuerpo.endswith(b"\n"))
            self.assertFalse(cuerpo[:-1].endswith((b" ", b"\n", b"\t",
                                                   b"\r")), cuerpo)

    def test_the_common_case_is_not_copied(self):
        raw = b"print('hola')\n" * 1000
        self.assertIs(small_body(raw), raw)

    def test_streaming_a_real_file_matches_reading_it_whole(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        rnd = random.Random(3)
        raw = b"".join(rnd.choice(_PIEZAS[:10]) for _ in range(20000))
        archivo = write(tmp, "mezcla.txt", raw)
        for trozo in (1, 3, 4096, 65536, txtbody.CHUNK):
            estado, cuerpo, escrito, _ = stream(archivo, chunk=trozo)
            self.assertEqual(estado, "text")
            self.assertEqual(cuerpo, small_body(raw), trozo)
            self.assertEqual(escrito, len(cuerpo))


class ReadableTextIsIncluded(unittest.TestCase):

    def test_ansi_windows_text_is_converted_not_mangled(self):
        """Un CSV de Excel en español (cp1252): antes «a�o»."""
        raw = "año;niño;canción;pingüino;€\r\n".encode("cp1252") * 3
        cuerpo = small_body(raw).decode("utf-8")
        self.assertIn("año;niño;canción;pingüino;€", cuerpo)
        self.assertNotIn("�", cuerpo)

    def test_ansi_is_the_one_of_this_windows(self):
        """En un Windows ruso, el ANSI es cp1251: «Привет», no «Ïðèâåò»."""
        from gitpush_studio.core import textenc
        raw = "Привет, мир\r\n".encode("cp1251")
        with mock.patch.object(textenc, "_ANSI", ["cp1251"]):
            self.assertEqual(small_body(raw).decode("utf-8"), "Привет, мир\n")
        with mock.patch.object(textenc.os, "name", "nt"), \
                mock.patch.object(textenc.locale, "getencoding",
                                  lambda: "cp1251", create=True):
            self.assertEqual(textenc._ansi_del_sistema(), "cp1251")
        with mock.patch.object(textenc.os, "name", "nt"), \
                mock.patch.object(textenc.locale, "getencoding",
                                  lambda: "utf-8", create=True):
            self.assertEqual(textenc._ansi_del_sistema(), "cp1252")
        with mock.patch.object(textenc.os, "name", "posix"):
            self.assertEqual(textenc._ansi_del_sistema(), "cp1252")

    def test_a_stray_byte_in_utf8_is_a_replacement_char(self):
        raw = "año\n".encode() + b"caf\xe9\n"
        cuerpo = small_body(raw).decode("utf-8")
        self.assertIn("año", cuerpo)                 # no se reinterpreta
        self.assertIn("caf�", cuerpo)

    def test_ansi_after_a_long_ascii_start_is_still_converted(self):
        """La decisión se toma en el primer byte inválido, aunque llegue
        tras megas de ASCII (un CSV con cabecera y muchas filas)."""
        raw = b"id;nombre\n" * 300000 + "1;Muñoz\n".encode("cp1252")
        cuerpo = small_body(raw)
        self.assertTrue(cuerpo.endswith("1;Muñoz\n".encode()))
        self.assertEqual(by_chunks(raw, 65536), cuerpo)

    def test_utf16_with_and_without_bom_is_text(self):
        for raw in ("Clave=Valor\r\nOtra=Ñandú\r\n".encode("utf-16"),
                    "Clave=Valor\r\nOtra=Ñandú\r\n".encode("utf-16-le"),
                    "Clave=Valor\r\nOtra=Ñandú\r\n".encode("utf-16-be")):
            cuerpo = small_body(raw)
            self.assertIsNotNone(cuerpo, raw[:10])
            self.assertEqual(cuerpo.decode("utf-8"),
                             "Clave=Valor\r\nOtra=Ñandú\n")

    def test_the_utf8_bom_is_dropped(self):
        self.assertEqual(small_body(b"\xef\xbb\xbfhola\n"), b"hola\n")

    def test_binaries_are_still_binaries(self):
        self.assertIsNone(small_body(b"\x89PNG\r\n\x1a\n\x00\x00\x00"))
        self.assertIsNone(small_body(b"texto" * 100 + b"\x00" + b"x"))


class BigFilesAreCopiedInBlocks(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_huge_binary_without_extension_costs_one_small_read(self):
        archivo = write(self.tmp, "pesos.npy",
                        b"\x93NUMPY\x01\x00" + b"\x00\x07" * (6 << 20))
        leido = []
        real = txtbody._fill

        def cuenta(fh, mv, quiero):
            n = real(fh, mv, quiero)
            leido.append(n)
            return n
        with mock.patch.object(txtbody, "_fill", cuenta):
            estado, cuerpo, _, _ = stream(archivo)
        self.assertEqual(estado, "binary")
        self.assertEqual(cuerpo, b"")                # no se escribió nada
        self.assertLessEqual(sum(leido), txtbody.CHUNK)

    def test_a_file_that_grows_while_being_read_is_cut_where_it_was(self):
        archivo = write(self.tmp, "registro.log", b"linea\n" * 50000)
        medida = archivo.stat().st_size

        def crece(_n):
            with open(archivo, "ab") as fh:
                fh.write(b"NUEVO\n" * 1000)
        estado, cuerpo, _, _ = stream(archivo, chunk=65536, tick=crece)
        self.assertEqual(estado, "cut")
        self.assertNotIn(b"NUEVO", cuerpo)
        self.assertEqual(cuerpo, (b"linea\n" * 50000))
        self.assertGreater(archivo.stat().st_size, medida)

    def test_a_file_that_shrinks_while_being_read_ends_where_it_ends(self):
        archivo = write(self.tmp, "menguante.log", b"a" * 200000 + b"\n")

        def encoge(_n):
            os.truncate(str(archivo), 70000)
        estado, cuerpo, _, _ = stream(archivo, chunk=65536, tick=encoge)
        self.assertEqual(estado, "text")
        self.assertEqual(cuerpo, b"a" * 70000 + b"\n")

    def test_a_read_failure_halfway_keeps_what_was_read(self):
        archivo = write(self.tmp, "disco-roto.txt", b"x" * 300000)
        llamadas = []
        real = txtbody._fill

        def falla_al_tercero(fh, mv, quiero):
            llamadas.append(1)
            if len(llamadas) == 3:
                raise OSError(errno.EIO, "Input/output error")
            return real(fh, mv, quiero)
        with mock.patch.object(txtbody, "_fill", falla_al_tercero):
            estado, cuerpo, escrito, error = stream(archivo, chunk=65536)
        self.assertEqual(estado, "error")
        self.assertEqual(error.errno, errno.EIO)
        self.assertEqual(cuerpo, b"x" * 131072 + b"\n")
        self.assertEqual(escrito, len(cuerpo))

    def test_a_write_failure_is_not_taken_for_a_read_failure(self):
        archivo = write(self.tmp, "a.txt", b"x" * 300000)
        fd, size = reader.open_regular(str(archivo))
        self.addCleanup(os.close, fd)

        def disco_lleno(_b):
            raise OSError(errno.ENOSPC, "No space left on device")
        with self.assertRaises(OSError) as cm:
            stream_body(fd, size, disco_lleno, chunk=65536)
        self.assertEqual(cm.exception.errno, errno.ENOSPC)


class ReadingIsExactAndSafe(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_unchanged_file_is_read_with_one_exact_read(self):
        """Antes se pedía «hasta el límite» y se reservaban 2 MB por
        archivo, aunque midiera 200 bytes."""
        archivo = write(self.tmp, "a.py", b"x = 1\n" * 30)
        pedidos = []
        real = os.read

        def anota(fd, n):
            pedidos.append(n)
            return real(fd, n)
        with mock.patch.object(reader.os, "read", anota):
            self.assertEqual(read_whole(str(archivo)), b"x = 1\n" * 30)
            self.assertEqual(read_one(str(archivo), 8 << 20),
                             b"x = 1\n" * 30)
        self.assertEqual(pedidos, [181, 181])

    def test_short_reads_are_completed(self):
        """Un sistema de archivos (de red, por ejemplo) puede devolver
        menos de lo pedido sin haber llegado al final: se sigue leyendo."""
        archivo = write(self.tmp, "red.txt", b"0123456789" * 50)
        real = os.read
        with mock.patch.object(reader.os, "read",
                               lambda fd, n: real(fd, min(n, 7))):
            self.assertEqual(read_whole(str(archivo)), b"0123456789" * 50)
            self.assertEqual(read_one(str(archivo), 1000),
                             b"0123456789" * 50)

    def test_growth_after_listing_is_handled(self):
        archivo = write(self.tmp, "crece.txt", b"y" * 100)
        fd = os.open(str(archivo), os.O_RDONLY)
        self.addCleanup(os.close, fd)
        with open(archivo, "ab") as fh:
            fh.write(b"z" * 50)
        self.assertEqual(read_upto(fd, 100), b"y" * 100 + b"z")
        # Para el TXT: lo que medía al abrirlo, marcado.
        with mock.patch.object(reader, "open_regular",
                               lambda p, flags=0: (os.open(p, os.O_RDONLY),
                                                   100)):
            cortado = read_whole(str(archivo))
        self.assertIsInstance(cortado, Cut)
        self.assertEqual(cortado, b"y" * 100)
        # Para la revisión y las analíticas: todo hasta su límite.
        self.assertEqual(read_one(str(archivo), 1000), b"y" * 100 + b"z" * 50)
        self.assertEqual(read_one(str(archivo), 120), Cut(b"y" * 100 + b"z" * 20))
        self.assertIsInstance(read_one(str(archivo), 120), Cut)

    def test_a_small_file_that_became_large_is_left_for_streaming(self):
        archivo = write(self.tmp, "a.txt", b"q" * 5000)
        self.assertIs(read_whole(str(archivo), small=4096), LARGE)

    @unittest.skipUnless(POSIX, "enlaces y tuberías de POSIX")
    def test_links_and_pipes_are_never_opened(self):
        os.symlink("/etc/passwd", str(self.tmp / "enlace"))
        os.mkfifo(str(self.tmp / "tuberia"))
        self.assertEqual(read_whole(str(self.tmp / "enlace")),
                         Link("/etc/passwd"))
        self.assertIsInstance(read_whole(str(self.tmp / "tuberia")), Special)
        self.assertIsInstance(read_one(str(self.tmp / "tuberia"), 100),
                              Special)


Entrada = namedtuple("Entrada", "rel path size")


class ReadingAheadIsBounded(unittest.TestCase):

    def test_order_is_kept_with_small_large_and_binary_files(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        entradas = []
        for i in range(300):
            if i % 50 == 7:
                rel, datos = f"g{i:03}.txt", b"G" * 9000
            elif i % 30 == 3:
                rel, datos = f"b{i:03}.png", b"\x89PNG"
            else:
                rel, datos = f"p{i:03}.txt", f"{i}\n".encode()
            p = write(tmp, rel, datos)
            entradas.append((rel, str(p), len(datos)))
        vistos = list(iter_contents(entradas, small=8192, workers=4,
                                    batch_files=3))
        self.assertEqual([v[0] for v in vistos], [e[0] for e in entradas])
        for rel, _ruta, size, raw in vistos:
            if rel.startswith("g"):
                self.assertIs(raw, LARGE)
            elif rel.endswith(".png"):
                self.assertIsNone(raw)
            else:
                self.assertEqual(raw, f"{int(rel[1:4])}\n".encode())

    def test_a_long_run_of_unread_files_does_not_pile_up(self):
        """Veinte mil binarios seguidos (que no se leen) no se acumulan
        por delante sin tope: van en lotes, como todo."""
        maximo = [0]

        class Cola(deque):
            def append(self, x):
                super().append(x)
                maximo[0] = max(maximo[0], len(self))
        entradas = [Entrada(f"{i}", "", 0) for i in range(20000)]
        with mock.patch.object(reader, "deque", Cola):
            vistos = list(reader._prefetch(
                entradas, lambda e: e.rel, lambda e: None, workers=2,
                ahead=2, batch_files=64, batch_bytes=1, window_bytes=10))
        self.assertEqual(vistos, [e.rel for e in entradas])
        self.assertLessEqual(maximo[0], 2)

    def test_a_binary_between_texts_does_not_split_the_batch(self):
        tamanos = []
        real = reader._run_batch

        def anota(run_one, lote):
            tamanos.append(len(lote))
            return real(run_one, lote)
        entradas = [Entrada(f"{i}", "", 1) for i in range(640)]
        with mock.patch.object(reader, "_run_batch", anota):
            vistos = list(reader._prefetch(
                entradas, lambda e: "bin" if int(e.rel) % 3 == 0 else None,
                lambda e: e.rel, workers=2, ahead=2, batch_files=64,
                batch_bytes=1 << 20, window_bytes=1 << 20))
        self.assertEqual(vistos, ["bin" if i % 3 == 0 else str(i)
                                  for i in range(640)])
        self.assertEqual(tamanos, [64] * 10)

    def test_bytes_in_flight_are_bounded(self):
        hechos, consumidos = [0], [0]
        candado = threading.Lock()

        def lote(item):
            with candado:
                hechos[0] += 1
            return item
        entradas = [Entrada(f"{i}", "", 4) for i in range(200)]
        for item in reader._prefetch(entradas, lambda e: None, lote,
                                     workers=4, ahead=100, batch_files=1,
                                     batch_bytes=1, window_bytes=10):
            consumidos[0] += 1
            time.sleep(0.0005)
            with candado:
                # 4 + 4 + 4 bytes: con 10 de ventana, tres lotes a la vez.
                self.assertLessEqual(hechos[0] - consumidos[0], 3)
        self.assertEqual(consumidos[0], 200)

    def test_stopping_early_leaves_no_thread_reading(self):
        hechos = [0]

        def lote(item):
            time.sleep(0.002)
            hechos[0] += 1
            return item
        entradas = [Entrada(f"{i}", "", 1) for i in range(5000)]
        gen = reader._prefetch(entradas, lambda e: None, lote, workers=4,
                               ahead=8, batch_files=1, batch_bytes=1,
                               window_bytes=1 << 30)
        for i, _ in enumerate(gen):
            if i == 20:
                break
        gen.close()
        quietos = hechos[0]
        time.sleep(0.1)
        self.assertEqual(hechos[0], quietos)
        self.assertLess(quietos, 40)

    def test_payloads_keep_their_old_meaning(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        a = write(tmp, "a.txt", b"A" * 10)
        b = write(tmp, "b.txt", b"B" * 500)
        c = write(tmp, "c.png", b"\x89PNG")
        vistos = list(iter_payloads([("a.txt", str(a), 10),
                                     ("b.txt", str(b), 500),
                                     ("c.png", str(c), 4)], 100))
        self.assertEqual(vistos, [("a.txt", 10, b"A" * 10),
                                  ("b.txt", 500, None),
                                  ("c.png", 4, None)])


class PaceMeasuresBeforeGoingParallel(unittest.TestCase):
    """Cuántos lotes a la vez: uno si abrir es instantáneo (en caché),
    muchos si cada apertura es una espera (el antivirus de Windows)."""

    def setUp(self):
        self.ahora = [100.0]
        self.ritmo = reader.Pace(16, clock=lambda: self.ahora[0])

    def test_it_starts_alone_to_measure(self):
        self.assertEqual(self.ritmo.limit, 1)
        self.assertTrue(self.ritmo.alone(0))
        self.assertFalse(self.ritmo.alone(1))

    def test_fast_opens_stay_with_one_reader(self):
        self.ritmo.measured(64 * 6e-6, 64, True)        # 6 µs por archivo
        self.assertEqual(self.ritmo.limit, 1)

    def test_slow_opens_go_parallel(self):
        self.ritmo.measured(64 * 2e-3, 64, True)        # 2 ms: antivirus
        self.assertEqual(self.ritmo.limit, 16)
        otro = reader.Pace(16)
        otro.measured(64 * 60e-6, 64, True)             # 60 µs
        self.assertEqual(otro.limit, 3)

    def test_a_tiny_batch_is_not_enough_to_decide(self):
        """Un solo archivo frío no puede decidir: se sigue midiendo."""
        self.ritmo.measured(0.0005, 1, True)            # 1 archivo, 500 µs
        self.assertEqual(self.ritmo.limit, 1)
        self.assertTrue(self.ritmo.alone(0))
        self.ritmo.measured(63 * 5e-6, 63, True)        # el resto, en caché
        self.assertEqual(self.ritmo.limit, 1)           # media: ~12 µs
        self.assertFalse(self.ritmo.alone(0))

    def test_only_a_batch_that_ran_alone_counts(self):
        self.ritmo.measured(64 * 2e-3, 64, False)
        self.assertEqual(self.ritmo.limit, 1)
        self.assertTrue(self.ritmo.alone(0))

    def test_it_measures_again_every_few_seconds(self):
        self.ritmo.measured(64 * 2e-3, 64, True)
        self.ahora[0] += reader.Pace.EVERY - 0.1
        self.ritmo.tick()
        self.assertEqual(self.ritmo.limit, 16)
        self.ahora[0] += 0.2
        self.ritmo.tick()
        self.assertEqual(self.ritmo.limit, 1)           # vuelve a medir
        self.assertTrue(self.ritmo.alone(0))
        self.ritmo.measured(64 * 5e-6, 64, True)        # ya en caché
        self.assertEqual(self.ritmo.limit, 1)
        self.assertFalse(self.ritmo.alone(0))

    def test_slow_opens_really_are_read_in_parallel(self):
        """Con cada apertura esperando 2 ms (sin el intérprete, como el
        antivirus), 400 archivos tardan bastante menos que en serie."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        entradas = []
        for i in range(400):
            p = write(tmp, f"f{i:03}.txt", f"{i}\n")
            entradas.append((p.name, str(p), len(f"{i}\n")))
        real = os.open

        def lento(path, flags, *a):
            time.sleep(0.002)
            return real(path, flags, *a)
        with mock.patch.object(reader.os, "open", lento):
            t0 = time.perf_counter()
            vistos = list(iter_contents(entradas, batch_files=16))
            dt = time.perf_counter() - t0
        self.assertEqual([v[3] for v in vistos],
                         [f"{i}\n".encode() for i in range(400)])
        self.assertLess(dt, 400 * 0.002 / 3, "no se leyó en paralelo")


class TheTxtHasNoSizeLimit(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.proj = self.tmp / "proyecto"
        write(self.proj, "main.py", "print('hola')\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_40_mb_text_file_goes_in_whole_without_loading_it(self):
        linea = b"2026-10-26 12:00:00 INFO todo bien por aqui\n"
        n = (40 << 20) // len(linea)
        write(self.proj, "registro.log.txt", linea * n + b"FINAL\n")
        out = self.tmp / "salida.txt"
        tracemalloc.start()
        try:
            st = consolidate_project(self.proj, out)
            _, pico = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(pico, 16 << 20, "se cargó el archivo entero")
        datos = out.read_bytes()
        inicio = datos.index(b"MODULO: registro.log.txt")
        self.assertEqual(datos.count(linea, inicio), n)
        self.assertIn(b"FINAL\n", datos)
        self.assertNotIn(b"OMITIDO POR TAMA", datos)
        self.assertEqual((st["text"], st["binaries"]), (2, 0))
        self.assertNotIn("big", st)
        self.assertNotIn("truncated", st)

    def test_a_txt_over_the_old_100_mb_cap_is_complete(self):
        bloque = (b"0123456789" * 7 + b"\n") * 15000       # ~1 MB
        for i in range(105):
            write(self.proj, f"datos/parte{i:03}.csv", bloque)
        out = self.tmp / "salida.txt"
        st = consolidate_project(self.proj, out)
        self.assertGreater(st["bytes"], 100 << 20)
        self.assertEqual(st["text"], 106)
        with open(out, "rb") as fh:
            fh.seek(-4096, os.SEEK_END)
            cola = fh.read().decode("utf-8")
        self.assertIn("MODULO: datos/parte104.csv", self._modulos(out))
        self.assertNotIn("TRUNCADA", cola)
        self.assertNotIn("límite", cola)
        self.assertIn("106 de texto (volcados enteros)", " ".join(cola.split()))

    def _modulos(self, out):
        with open(out, "rb") as fh:
            return [l.decode("utf-8").strip() for l in fh
                    if l.startswith(b"MODULO: ")]

    def test_progress_is_reported_and_ends_at_the_real_size(self):
        for i in range(30):
            write(self.proj, f"m{i}.py", f"x = {i}\n" * 1000)
        avisos = []
        out = self.tmp / "salida.txt"
        with mock.patch.object(pack, "PROGRESS_EVERY", 0):
            st = consolidate_project(self.proj, out,
                                     on_progress=lambda *a: avisos.append(a))
        self.assertGreater(len(avisos), 10)
        escritos = [a[2] for a in avisos]
        self.assertEqual(escritos, sorted(escritos))
        self.assertEqual(avisos[-1], (31, 31, st["bytes"]))
        self.assertEqual(st["bytes"], out.stat().st_size)

    def test_stop_leaves_the_previous_txt_whole(self):
        out = self.tmp / "salida.txt"
        consolidate_project(self.proj, out)
        antes = out.read_bytes()
        write(self.proj, "enorme.txt", b"z" * (30 << 20))
        parar = threading.Event()

        def progreso(hechos, total, escritos):
            if escritos > (5 << 20):
                parar.set()
        with mock.patch.object(pack, "PROGRESS_EVERY", 0):
            with self.assertRaises(Cancelled):
                consolidate_project(self.proj, out, on_progress=progreso,
                                    cancel=parar)
        self.assertEqual(out.read_bytes(), antes)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()),
                         ["proyecto", "salida.txt"], "quedó un temporal")

    def test_it_stops_before_filling_the_disk(self):
        out = self.tmp / "salida.txt"
        consolidate_project(self.proj, out)
        antes = out.read_bytes()
        write(self.proj, "grande.txt", b"w" * (3 << 20))
        Uso = namedtuple("Uso", "total used free")
        with mock.patch.object(pack, "ROOM_EVERY", 1 << 20), \
                mock.patch.object(pack.shutil, "disk_usage",
                                  lambda _p: Uso(100 << 30, 0, 100 << 20)):
            with self.assertRaises(StepError) as cm:
                consolidate_project(self.proj, out)
        self.assertIn("para no llenar el disco", str(cm.exception))
        self.assertEqual(out.read_bytes(), antes)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()),
                         ["proyecto", "salida.txt"])

    def test_a_full_disk_is_explained_and_the_old_txt_survives(self):
        out = self.tmp / "salida.txt"
        consolidate_project(self.proj, out)
        antes = out.read_bytes()
        reales = pack.write_modules

        def disco_lleno(*a, **kw):
            raise OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(pack, "write_modules", disco_lleno):
            with self.assertRaises(StepError) as cm:
                consolidate_project(self.proj, out)
        self.assertIn("El disco se llenó", str(cm.exception))
        self.assertEqual(out.read_bytes(), antes)
        self.assertIs(pack.write_modules, reales)

    def test_a_read_error_note_does_not_reveal_the_full_path(self):
        secreto = write(self.proj, "privado.txt", "x\n")
        real = reader.os.open

        def niega(path, flags, *a):
            if str(path) == str(secreto):
                raise PermissionError(errno.EACCES, "Permission denied",
                                      str(path))
            return real(path, flags, *a)
        out = self.tmp / "salida.txt"
        with mock.patch.object(reader.os, "open", niega):
            st = consolidate_project(self.proj, out)
        texto = out.read_text(encoding="utf-8")
        self.assertIn("MODULO: privado.txt", texto)
        self.assertIn("ERROR AL LEER ARCHIVO: Permission denied", texto)
        self.assertNotIn(str(self.tmp), texto)
        self.assertEqual(st["errors"], 1)

    def test_every_kind_of_file_is_counted_once(self):
        write(self.proj, "logo.png", b"\x89PNG\x00")
        write(self.proj, "datos.bin2", b"\x00\x01\x02")
        write(self.proj, "notas.txt", "año\n".encode("cp1252"))
        write(self.proj, "reg.reg", "a=b\r\n".encode("utf-16"))
        write(self.proj, "grande.md", b"# t\n" * (3 << 20))
        out = self.tmp / "salida.txt"
        st = consolidate_project(self.proj, out)
        self.assertEqual(st["files"], 6)
        self.assertEqual(st["text"] + st["binaries"] + st["links"]
                         + st["special"] + st["errors"], st["files"])
        self.assertEqual((st["text"], st["binaries"]), (4, 2))
        texto = out.read_text(encoding="utf-8")
        self.assertIn("año", texto)
        self.assertIn("a=b", texto)
        self.assertEqual(texto.count("# t\n"), 3 << 20)

    def test_the_profile_txt_has_no_cap_either(self):
        otro = self.tmp / "otro"
        write(otro, "grande.txt", b"k" * (12 << 20) + b"\n")
        out = self.tmp / "perfil.txt"
        st = consolidate_many("ana", [self.proj, otro], out)
        self.assertEqual(st["text"], 2)
        with open(out, "rb") as fh:
            datos = fh.read()
        self.assertIn(b"k" * (12 << 20) + b"\n", datos)


class StoppingWorksEverywhere(unittest.TestCase):
    """«Detener» también vale para el TXT de cada repositorio, el del
    perfil y el que se genera al publicar."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repos = []
        for nombre in ("uno", "dos"):
            d = self.tmp / nombre
            write(d, "a.txt", nombre + "\n")
            self.repos.append(d)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_each_repo_txt_stops_and_saves_nothing(self):
        parar = threading.Event()
        parar.set()
        with self.assertRaises(Cancelled):
            pack.consolidate_each(self.repos, cancel=parar)
        for d in self.repos:
            self.assertEqual(sorted(p.name for p in d.iterdir()), ["a.txt"])

    def test_profile_txt_stops_and_keeps_the_previous_one(self):
        out = self.tmp / "perfil.txt"
        consolidate_many("ana", self.repos, out)
        antes = out.read_bytes()
        parar = threading.Event()
        parar.set()
        with self.assertRaises(Cancelled):
            consolidate_many("ana", self.repos, out, cancel=parar)
        self.assertEqual(out.read_bytes(), antes)
        self.assertEqual([p.name for p in self.tmp.iterdir()
                          if p.name.endswith(".tmp")], [])

    def test_stopping_during_the_publish_txt_stops_the_publish(self):
        from tests.gitenv import HAVE_GIT, init_bare
        if not HAVE_GIT:
            self.skipTest("git no disponible")
        bare = init_bare(self.tmp, "remoto")
        job = publish_mod.PublishJob(folder=str(self.repos[0]), url=bare,
                                     user="T", email="t@x.y", preview=False,
                                     check=False, consolidate=True)
        with mock.patch.object(publish_mod, "consolidate_project",
                               side_effect=Cancelled(pack.CANCELLED)):
            r = publish_mod.publish(job)
        self.assertEqual(r.status, "cancelled")
        self.assertIn("No se ha subido nada", r.message)
        import subprocess
        ramas = subprocess.run(["git", "-C", bare, "branch", "-a"],
                               capture_output=True, text=True).stdout
        self.assertEqual(ramas.strip(), "")

    def test_the_publish_log_says_how_much_is_written(self):
        from types import SimpleNamespace
        lineas = []
        ctx = SimpleNamespace(log=lambda t, tag="dim": lineas.append(t))
        avisa = publish_mod._txt_progress(ctx, cada=0)
        avisa(1, 10, 5 << 20)
        avisa(10, 10, 6 << 20)              # el final no se repite
        self.assertEqual(len(lineas), 1)
        self.assertIn("5 MB escritos (1 de 10 archivos)", lineas[0])


class WriteModulesStillWorksOnItsOwn(unittest.TestCase):

    def test_into_any_binary_file_object(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        a = write(tmp, "a.txt", b"uno\n")
        b = write(tmp, "b.txt", b"dos")
        out = io.BytesIO()
        contadores = {}
        n = write_modules(out, [("a.txt", str(a), 4), ("b.txt", str(b), 3)],
                          contadores)
        self.assertEqual(n, len(out.getvalue()))
        self.assertEqual(out.getvalue().count(b"MODULO: "), 2)
        self.assertIn(b"uno\n", out.getvalue())
        self.assertIn(b"dos\n", out.getvalue())
        self.assertEqual(contadores["text"], 2)


if __name__ == "__main__":
    unittest.main()
