# -*- coding: utf-8 -*-
"""Descarga por instantánea (.tar.gz), la alternativa a clonar.

No hay red aquí, así que la apertura de URLs se inyecta: se comprueba
qué URL se pide, en qué orden y con qué cabeceras, y sobre todo que la
extracción no se deje engañar por un .tar preparado con malas
intenciones (rutas absolutas, «..», enlaces fuera del destino).
"""

import io
import shutil
import tarfile
import tempfile
import unittest
from unittest import mock
import urllib.error
from pathlib import Path

from gitpush_studio.core import snapshot
from gitpush_studio.core.git import MODE_SNAPSHOT, BulkCloner

PREFIJO = "ana-proj-9f2b7c4"


def hacer_tar(destino, miembros, prefijo=PREFIJO):
    """Crea un .tar.gz con la forma que tiene el de GitHub.

    Un nombre que empiece por «/» se guarda tal cual (ruta absoluta);
    el resto cuelga del prefijo, incluidos los «..», que es como vendría
    un .tar preparado para escapar de la carpeta.
    """
    with tarfile.open(destino, "w:gz") as tar:
        for nombre, contenido in miembros:
            ruta = f"{prefijo}/{nombre}" if prefijo and not \
                nombre.startswith("/") else nombre
            datos = contenido.encode("utf-8")
            info = tarfile.TarInfo(ruta)
            info.size = len(datos)
            tar.addfile(info, io.BytesIO(datos))
    return destino


class RespuestaFalsa(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class UrlTests(unittest.TestCase):

    def test_the_direct_door_goes_first_and_costs_no_api_calls(self):
        urls = snapshot.snapshot_urls("ana", "proj", "main", "ghp_x")
        self.assertEqual(len(urls), 2)
        primera, cabeceras = urls[0]
        self.assertEqual(
            primera,
            "https://codeload.github.com/ana/proj/tar.gz/refs/heads/main")
        self.assertNotIn("Authorization", cabeceras)   # no hace falta
        segunda, cabeceras2 = urls[1]
        self.assertEqual(segunda,
                         "https://api.github.com/repos/ana/proj/tarball/main")
        self.assertEqual(cabeceras2["Authorization"], "Bearer ghp_x")

    def test_without_branch_only_the_api_door_is_possible(self):
        urls = snapshot.snapshot_urls("ana", "proj")
        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0][0].endswith("/repos/ana/proj/tarball"))


class SafeExtractionTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_wrapper_folder_is_not_counted_as_skipped(self):
        """Un .tar de GitHub trae entradas de carpeta, incluida la de más
        arriba: quitarla no es «omitir un archivo»."""
        ruta = self.tmp / "real.tar.gz"
        with tarfile.open(ruta, "w:gz") as tar:
            for d in (f"{PREFIJO}/", f"{PREFIJO}/src/"):
                info = tarfile.TarInfo(d)
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            for nombre, texto in ((f"{PREFIJO}/README.md", "hola\n"),
                                  (f"{PREFIJO}/src/app.py", "x = 1\n")):
                datos = texto.encode("utf-8")
                info = tarfile.TarInfo(nombre)
                info.size = len(datos)
                tar.addfile(info, io.BytesIO(datos))
        archivos, omitidos = snapshot.extract(ruta, self.tmp / "salida")
        self.assertEqual((archivos, omitidos), (2, 0))

    def test_an_impossible_filename_does_not_abort_the_rest(self):
        """En Windows hay nombres que no se pueden crear; uno solo no
        puede tirar la descarga entera."""
        tar = hacer_tar(self.tmp / "s.tar.gz",
                        [("bueno.txt", "ok\n"), ("raro.txt", "x\n"),
                         ("otro.txt", "y\n")])
        destino = self.tmp / "salida"
        abierto_real = open

        def abrir_falso(ruta, modo="r", *a, **kw):
            if str(ruta).endswith("raro.txt") and "w" in str(modo):
                raise OSError(22, "nombre no válido")
            return abierto_real(ruta, modo, *a, **kw)
        import builtins
        builtins.open = abrir_falso
        try:
            archivos, omitidos = snapshot.extract(tar, destino)
        finally:
            builtins.open = abierto_real
        self.assertEqual((archivos, omitidos), (2, 1))
        self.assertTrue((destino / "bueno.txt").exists())
        self.assertTrue((destino / "otro.txt").exists())
        self.assertFalse((destino / "raro.txt").exists())

    def test_the_top_folder_is_stripped(self):
        tar = hacer_tar(self.tmp / "s.tar.gz",
                        [("README.md", "hola\n"), ("src/app.py", "x = 1\n")])
        destino = self.tmp / "salida"
        archivos, omitidos = snapshot.extract(tar, destino)
        self.assertEqual((archivos, omitidos), (2, 0))
        self.assertEqual(sorted(p.name for p in destino.rglob("*")
                                if p.is_file()), ["README.md", "app.py"])
        self.assertEqual((destino / "src" / "app.py").read_text("utf-8"),
                         "x = 1\n")
        self.assertFalse((destino / PREFIJO).exists())

    def test_a_malicious_tar_cannot_write_outside_the_folder(self):
        fuera = self.tmp / "VICTIMA.txt"
        fuera.write_text("intacto\n", encoding="utf-8")
        tar = hacer_tar(self.tmp / "malo.tar.gz", [
            ("bueno.txt", "ok\n"),
            ("../VICTIMA.txt", "PISADO\n"),          # sube un nivel
            ("../../VICTIMA.txt", "PISADO\n"),       # sube dos
            ("sub/../../../VICTIMA.txt", "PISADO\n"),  # disimulado
            ("/etc/passwd", "PISADO\n"),             # ruta absoluta
        ])
        destino = self.tmp / "salida"
        archivos, omitidos = snapshot.extract(tar, destino)
        self.assertEqual((archivos, omitidos), (1, 4))
        self.assertEqual(fuera.read_text("utf-8"), "intacto\n")
        self.assertEqual([p.name for p in destino.rglob("*") if p.is_file()],
                         ["bueno.txt"])

    def test_either_barrier_alone_is_enough(self):
        """Hay dos defensas independientes: `safe_name` rechaza el nombre
        y `_inside` comprueba el destino final. Cada una basta por sí
        sola; esta prueba fija esa redundancia para que nadie retire una
        creyendo que la otra ya no hace falta."""
        nombres = [f"{PREFIJO}/../VICTIMA.txt",
                   f"{PREFIJO}/../../VICTIMA.txt",
                   f"{PREFIJO}/sub/../../../VICTIMA.txt",
                   "/etc/passwd"]
        destino = self.tmp / "salida" / "sub"
        for nombre in nombres:
            # Primera barrera: el nombre ni siquiera se acepta.
            self.assertEqual(snapshot.safe_name(nombre), "", nombre)
            # Segunda: aunque el nombre se aceptara tal cual, el destino
            # final cae fuera de la carpeta.
            crudo = nombre if nombre.startswith("/") else \
                "/".join(nombre.split("/")[1:])
            self.assertFalse(snapshot._inside(destino, destino / crudo),
                             nombre)

    def test_the_second_barrier_also_holds(self):
        """`safe_name` es la primera defensa; `_inside` es la de después,
        y tiene que valer por sí sola."""
        base = self.tmp / "destino"
        self.assertTrue(snapshot._inside(base, base / "a" / "b.txt"))
        self.assertTrue(snapshot._inside(base, base))
        self.assertFalse(snapshot._inside(base, base.parent / "otro.txt"))
        self.assertFalse(snapshot._inside(base, base / ".." / "fuera.txt"))
        self.assertFalse(snapshot._inside(base, Path("/etc/passwd")))
        # Un vecino con el mismo prefijo tampoco cuela: destino-bis ≠ destino
        self.assertFalse(snapshot._inside(base,
                                          Path(str(base) + "-bis") / "x"))

    def test_links_and_devices_are_skipped(self):
        ruta = self.tmp / "enlaces.tar.gz"
        with tarfile.open(ruta, "w:gz") as tar:
            datos = b"real\n"
            info = tarfile.TarInfo(f"{PREFIJO}/real.txt")
            info.size = len(datos)
            tar.addfile(info, io.BytesIO(datos))
            enlace = tarfile.TarInfo(f"{PREFIJO}/atajo")
            enlace.type = tarfile.SYMTYPE
            enlace.linkname = "/etc/passwd"
            tar.addfile(enlace)
        destino = self.tmp / "salida"
        archivos, omitidos = snapshot.extract(ruta, destino)
        self.assertEqual((archivos, omitidos), (1, 1))
        self.assertFalse((destino / "atajo").exists())


class FetchTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tar = hacer_tar(self.tmp / "origen.tar.gz",
                             [("a.txt", "A\n"), ("dir/b.txt", "B\n")])
        self.datos = self.tar.read_bytes()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def abridor(self, respuestas):
        """respuestas: lista de bytes o de excepciones, en orden."""
        pedidas = []

        def abrir(url, headers, timeout):
            pedidas.append((url, headers))
            r = respuestas.pop(0)
            if isinstance(r, Exception):
                raise r
            return RespuestaFalsa(r)
        return abrir, pedidas

    def test_a_snapshot_lands_as_plain_files_without_git(self):
        abrir, pedidas = self.abridor([self.datos])
        destino = self.tmp / "proj"
        estado, _, error = snapshot.fetch_snapshot(
            "ana", "proj", destino, branch="main", opener=abrir)
        self.assertEqual(estado, "ok", error)
        self.assertEqual(len(pedidas), 1)
        self.assertIn("codeload", pedidas[0][0])
        self.assertEqual((destino / "a.txt").read_text("utf-8"), "A\n")
        self.assertEqual((destino / "dir" / "b.txt").read_text("utf-8"), "B\n")
        self.assertFalse((destino / ".git").exists())   # no es un repositorio

    def test_if_the_direct_door_fails_it_tries_the_api(self):
        error404 = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        abrir, pedidas = self.abridor([error404, self.datos])
        estado, _, err = snapshot.fetch_snapshot(
            "ana", "proj", self.tmp / "proj", branch="main", token="ghp_x",
            opener=abrir)
        self.assertEqual(estado, "ok", err)
        self.assertEqual(len(pedidas), 2)
        self.assertIn("api.github.com", pedidas[1][0])
        self.assertEqual(pedidas[1][1]["Authorization"], "Bearer ghp_x")

    def test_both_doors_closed_explains_why_and_leaves_no_leftovers(self):
        e403 = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        abrir, _ = self.abridor([e403, e403])
        destino = self.tmp / "proj"
        estado, _, error = snapshot.fetch_snapshot(
            "ana", "proj", destino, branch="main", opener=abrir)
        self.assertEqual(estado, "fail")
        self.assertIn("privado", error)
        self.assertFalse(destino.exists())

    def test_an_existing_folder_is_never_touched(self):
        destino = self.tmp / "proj"
        destino.mkdir()
        (destino / "mio.txt").write_text("MI TRABAJO\n", encoding="utf-8")
        abrir, pedidas = self.abridor([self.datos])
        estado, _, motivo = snapshot.fetch_snapshot(
            "ana", "proj", destino, branch="main", opener=abrir)
        self.assertEqual(estado, "skip")
        self.assertEqual(pedidas, [])            # ni siquiera descarga
        self.assertEqual((destino / "mio.txt").read_text("utf-8"),
                         "MI TRABAJO\n")

    def test_stopping_mid_download_cleans_up(self):
        abrir, _ = self.abridor([self.datos])
        destino = self.tmp / "proj"
        estado, _, error = snapshot.fetch_snapshot(
            "ana", "proj", destino, branch="main", opener=abrir,
            should_stop=lambda: True)
        self.assertEqual(estado, "fail")
        self.assertIn("detenida", error)
        self.assertFalse(destino.exists())

    def test_stopping_mid_extraction_leaves_no_half_folder(self):
        """Extraer un repositorio grande tarda: «Detener» también corta
        ahí, y lo ya escrito no puede quedarse tirado."""
        grande = hacer_tar(self.tmp / "grande.tar.gz",
                           [(f"f{i}.txt", "x\n") for i in range(20)])
        abrir, _ = self.abridor([grande.read_bytes()])
        destino = self.tmp / "proj"
        cuenta = {"n": 0}

        def parar_a_mitad():
            cuenta["n"] += 1
            return cuenta["n"] > 12       # ya se escribieron algunos
        estado, _, error = snapshot.fetch_snapshot(
            "ana", "proj", destino, branch="main", opener=abrir,
            should_stop=parar_a_mitad)
        self.assertEqual(estado, "fail")
        self.assertIn("detenida", error)
        self.assertFalse(destino.exists(), "quedó media instantánea")
        self.assertEqual([p.name for p in self.tmp.iterdir()
                          if ".descargando-" in p.name], [],
                         "quedó la carpeta temporal de la extracción")

    def test_a_corrupt_archive_is_retried_and_then_reported(self):
        # Una descarga truncada suele arreglarse repitiéndola, así que se
        # reintenta; si sigue corrupta, se informa en vez de extraer basura.
        abrir, pedidas = self.abridor([b"esto no es un tar.gz"] * 6)
        esperas = []
        estado, _, error = snapshot.fetch_snapshot(
            "ana", "proj", self.tmp / "proj", branch="main", opener=abrir,
            sleep=esperas.append)
        self.assertEqual(estado, "fail")
        self.assertTrue(error)
        self.assertEqual(esperas, [3, 6])
        self.assertEqual(len(pedidas), 6)      # 2 puertas × 3 intentos

    def test_a_rate_limit_is_retried_and_reported_to_the_queue(self):
        e429 = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
        abrir, _ = self.abridor([e429, e429, self.datos])
        avisos, esperas = [], []
        estado, _, error = snapshot.fetch_snapshot(
            "ana", "proj", self.tmp / "proj", branch="main", opener=abrir,
            on_retry=lambda e, lim, err: avisos.append((e, lim)),
            sleep=esperas.append)
        self.assertEqual(estado, "ok", error)
        self.assertEqual(avisos, [(3, True)])   # True: es un 429, frena la cola
        self.assertEqual(esperas, [3])

    def test_a_missing_repository_is_not_retried(self):
        e404 = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        abrir, pedidas = self.abridor([e404] * 6)
        avisos = []
        estado, _, error = snapshot.fetch_snapshot(
            "ana", "proj", self.tmp / "proj", branch="main", opener=abrir,
            on_retry=lambda *a: avisos.append(a), sleep=lambda s: None)
        self.assertEqual(estado, "fail")
        self.assertEqual(len(pedidas), 2)       # las dos puertas, una vez
        self.assertEqual(avisos, [])
        self.assertIn("No encontré", error)


class NothingYouPutThereIsEverDeleted(unittest.TestCase):
    """Se extrae en una carpeta temporal al lado y se coloca al final con
    un renombrado. Antes se extraía directamente en el destino y, si algo
    fallaba, se «limpiaba» borrando TODO lo que hubiera dentro: también
    lo que el usuario hubiera dejado ahí mientras se descargaba."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.datos = hacer_tar(self.tmp / "o.tar.gz",
                               [("a.txt", "A\n")]).read_bytes()
        self.destino = self.tmp / "proj"
        self.destino.mkdir()                      # vacía: se puede usar

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sobras(self):
        return [p.name for p in self.tmp.iterdir()
                if ".descargando-" in p.name]

    def abridor_que_deja_algo(self, respuesta):
        def abrir(url, headers, timeout):
            # Mientras se descarga, el usuario guarda algo en la carpeta.
            (self.destino / "mio.txt").write_text("MÍO\n", encoding="utf-8")
            if isinstance(respuesta, Exception):
                raise respuesta
            return RespuestaFalsa(respuesta)
        return abrir

    def test_a_good_download_does_not_land_on_your_file(self):
        estado, _, error = snapshot.fetch_snapshot(
            "ana", "proj", self.destino, branch="main",
            opener=self.abridor_que_deja_algo(self.datos))
        self.assertEqual(estado, "fail")
        self.assertIn("dejó de estar vacía", error)
        self.assertEqual(sorted(p.name for p in self.destino.iterdir()),
                         ["mio.txt"])
        self.assertEqual((self.destino / "mio.txt").read_text("utf-8"),
                         "MÍO\n")
        self.assertEqual(self.sobras(), [])

    def test_a_broken_download_does_not_wipe_your_file(self):
        estado, _, _ = snapshot.fetch_snapshot(
            "ana", "proj", self.destino, branch="main",
            opener=self.abridor_que_deja_algo(b"no es un tar.gz"),
            retries=0)
        self.assertEqual(estado, "fail")
        self.assertEqual((self.destino / "mio.txt").read_text("utf-8"),
                         "MÍO\n")
        self.assertEqual(self.sobras(), [])

    def test_an_empty_folder_is_used_and_nothing_is_left_behind(self):
        abrir = lambda url, headers, timeout: RespuestaFalsa(self.datos)
        estado, _, error = snapshot.fetch_snapshot(
            "ana", "proj", self.destino, branch="main", opener=abrir)
        self.assertEqual(estado, "ok", error)
        self.assertEqual((self.destino / "a.txt").read_text("utf-8"), "A\n")
        self.assertEqual(self.sobras(), [])

    def test_a_failed_update_never_deletes_what_appeared_meanwhile(self):
        abrir = lambda url, headers, timeout: RespuestaFalsa(self.datos)
        self.destino.rmdir()
        self.assertEqual(snapshot.fetch_snapshot(
            "ana", "proj", self.destino, branch="main", opener=abrir,
            sha="SHA1")[0], "ok")

        def abrir_y_fallar(url, headers, timeout):
            # La versión anterior ya está apartada; el usuario crea la
            # carpeta de nuevo y guarda algo en ella.
            self.destino.mkdir(exist_ok=True)
            (self.destino / "nuevo-mio.txt").write_text("N\n",
                                                        encoding="utf-8")
            raise OSError("sin red")
        estado, _, mensaje = snapshot.update_snapshot(
            "ana", "proj", self.destino, branch="main", sha_remoto="SHA2",
            opener=abrir_y_fallar)
        self.assertEqual(estado, "fail")
        self.assertEqual((self.destino / "nuevo-mio.txt").read_text("utf-8"),
                         "N\n")
        anteriores = [p for p in self.tmp.iterdir()
                      if p.name.startswith("proj.anterior-")]
        self.assertEqual(len(anteriores), 1)
        self.assertIn(anteriores[0].name, mensaje)
        self.assertEqual((anteriores[0] / "a.txt").read_text("utf-8"), "A\n")


class RenameRetries(unittest.TestCase):
    """En Windows el antivirus abre un instante lo recién escrito y
    renombrar su carpeta falla con «acceso denegado»: se reintenta."""

    def test_windows_gets_a_few_retries(self):
        llamadas, esperas = [], []

        def renombrar(a, b):
            llamadas.append((a, b))
            if len(llamadas) < 3:
                raise PermissionError(13, "Acceso denegado")
        with mock.patch.object(snapshot.os, "rename", renombrar), \
                mock.patch.object(snapshot.os, "name", "nt"):
            snapshot._rename("a", "b", sleep=esperas.append)
        self.assertEqual(len(llamadas), 3)
        self.assertEqual(esperas, [0.2, 0.4])

    def test_elsewhere_a_denied_rename_fails_at_once(self):
        def renombrar(a, b):
            raise PermissionError(13, "denegado")
        with mock.patch.object(snapshot.os, "rename", renombrar), \
                mock.patch.object(snapshot.os, "name", "posix"):
            with self.assertRaises(PermissionError):
                snapshot._rename("a", "b",
                                 sleep=lambda s: self.fail("no debe esperar"))


class BulkSnapshotTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.datos = hacer_tar(self.tmp / "o.tar.gz",
                               [("f.txt", "contenido\n")]).read_bytes()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_bulk_downloader_can_use_snapshots(self):
        repos = [{"name": f"repo{i}", "clone_url": "https://x/y.git",
                  "private": False, "owner": "ana", "default_branch": "main"}
                 for i in range(3)]
        pedidas = []

        def abrir(url, headers, timeout):
            pedidas.append(url)
            return RespuestaFalsa(self.datos)

        original = snapshot._open
        snapshot._open = abrir
        try:
            bc = BulkCloner(workers=3)
            s = bc.run(repos, self.tmp / "salida", True, "",
                       lambda k, p: None, mode=MODE_SNAPSHOT)
        finally:
            snapshot._open = original
        self.assertEqual((s["ok"], s["fail"]), (3, 0))
        self.assertEqual(len(pedidas), 3)
        self.assertTrue(all("codeload" in u for u in pedidas))
        for i in range(3):
            carpeta = self.tmp / "salida" / f"repo{i}"
            self.assertEqual((carpeta / "f.txt").read_text("utf-8"),
                             "contenido\n")
            self.assertFalse((carpeta / ".git").exists())


if __name__ == "__main__":
    unittest.main()
