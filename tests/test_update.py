# -*- coding: utf-8 -*-
"""Actualizar lo ya descargado en vez de volver a bajarlo.

La regla que se defiende aquí: poner al día trae solo lo que cambió,
nunca pisa trabajo tuyo, y cuando no puede garantizar eso se aparta y
dice por qué. Para las instantáneas, además, si nada cambió no se
descarga ni un byte.
"""

import io
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from tests.gitenv import HAVE_GIT, clone, commit_all, git, init_bare, init_repo

from gitpush_studio.core import snapshot
from gitpush_studio.core.git import (MODE_CLONE, ON_EXISTS_SKIP,
                                     ON_EXISTS_UPDATE, BulkCloner, Git,
                                     update_clone)


def escribir(carpeta, nombre, texto):
    p = Path(carpeta) / nombre
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(texto, encoding="utf-8")
    return p


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class UpdateCloneTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bare = init_bare(self.tmp)
        self.origen = self.tmp / "origen"
        init_repo(self.origen)
        escribir(self.origen, "a.txt", "v1\n")
        commit_all(self.origen, "primero")
        git(self.origen, "push", "-q", self.bare, "main")
        self.copia = Path(clone(self.bare, self.tmp / "copia"))
        self.git = Git()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def publicar(self, texto, nombre="a.txt", mensaje="cambio"):
        escribir(self.origen, nombre, texto)
        commit_all(self.origen, mensaje)
        git(self.origen, "push", "-q", self.bare, "main")

    def test_nothing_changed_means_nothing_downloaded(self):
        estado, _, mensaje = update_clone(self.git, self.copia, "main")
        self.assertEqual(estado, "uptodate")
        self.assertIn("al día", mensaje)

    def test_it_brings_only_what_changed(self):
        self.publicar("v2\n")
        self.publicar("nuevo\n", "b.txt", "segundo")
        estado, _, mensaje = update_clone(self.git, self.copia, "main")
        self.assertEqual(estado, "ok", mensaje)
        self.assertEqual((self.copia / "a.txt").read_text("utf-8"), "v2\n")
        self.assertEqual((self.copia / "b.txt").read_text("utf-8"), "nuevo\n")
        # Y al repetir ya no hay nada que hacer.
        self.assertEqual(update_clone(self.git, self.copia, "main")[0],
                         "uptodate")

    def test_uncommitted_work_is_never_overwritten(self):
        self.publicar("v2\n")
        escribir(self.copia, "a.txt", "LO MÍO SIN GUARDAR\n")
        escribir(self.copia, "borrador.txt", "trabajo nuevo\n")
        estado, _, mensaje = update_clone(self.git, self.copia, "main")
        self.assertEqual(estado, "skip")
        self.assertIn("cambios sin guardar", mensaje)
        self.assertEqual((self.copia / "a.txt").read_text("utf-8"),
                         "LO MÍO SIN GUARDAR\n")
        self.assertTrue((self.copia / "borrador.txt").exists())

    def test_local_commits_are_never_lost(self):
        self.publicar("v2\n")
        escribir(self.copia, "mio.txt", "mi trabajo\n")
        commit_all(self.copia, "mi commit")
        antes = self.git.sha(self.copia)
        estado, _, mensaje = update_clone(self.git, self.copia, "main")
        self.assertEqual(estado, "skip")
        self.assertIn("commits propios", mensaje)
        self.assertEqual(self.git.sha(self.copia), antes)
        self.assertTrue((self.copia / "mio.txt").exists())

    def test_a_shallow_copy_stays_shallow_and_brings_only_the_tip(self):
        """Actualizar una copia ligera no puede acabar trayendo el
        historial entero: sin `--depth 1`, un fetch arrastra los commits
        intermedios y la copia deja de ser ligera en la práctica."""
        ligera = self.tmp / "ligera"
        # Con una ruta local, git ignora --depth: hace falta file://
        subprocess.run(["git", "clone", "-q", "--depth", "1",
                        "file://" + str(Path(self.bare).resolve()),
                        str(ligera)], check=True)
        git(ligera, "config", "user.name", "t")
        git(ligera, "config", "user.email", "t@x")
        self.assertTrue(self.git.is_shallow(ligera))
        self.assertEqual(self._commits(ligera), 1)
        for i in range(2, 6):                 # cuatro versiones nuevas
            self.publicar(f"v{i}\n", mensaje=f"c{i}")
        estado, _, mensaje = update_clone(self.git, ligera, "main")
        self.assertEqual(estado, "ok", mensaje)
        self.assertEqual((ligera / "a.txt").read_text("utf-8"), "v5\n")
        self.assertTrue(self.git.is_shallow(ligera), "dejó de ser ligera")
        self.assertEqual(self._commits(ligera), 1,
                         "se trajo historial de más")

    def _commits(self, carpeta):
        return int(git(carpeta, "rev-list", "--count", "HEAD").strip())

    def test_a_folder_that_is_not_a_repository_says_so(self):
        suelta = self.tmp / "suelta"
        suelta.mkdir()
        escribir(suelta, "x.txt", "x\n")
        estado, _, mensaje = update_clone(self.git, suelta, "main")
        self.assertEqual(estado, "fail")
        self.assertIn("no es un repositorio", mensaje)
        self.assertTrue((suelta / "x.txt").exists())

    def test_a_missing_branch_is_reported(self):
        git(self.copia, "checkout", "-q", "-b", "solo-local")
        estado, _, mensaje = update_clone(self.git, self.copia, "solo-local")
        self.assertEqual(estado, "fail")
        self.assertTrue(mensaje)

    def test_another_branch_checked_out_is_left_alone(self):
        """Pedir «main» con la copia en otra rama movería ESA rama al
        commit de main: se aparta y lo dice."""
        git(self.copia, "checkout", "-q", "-b", "trabajo")
        self.publicar("v2\n")
        estado, _, mensaje = update_clone(self.git, self.copia, "main")
        self.assertEqual(estado, "skip")
        self.assertIn("trabajo", mensaje)
        self.assertEqual((self.copia / "a.txt").read_text("utf-8"), "v1\n")

    def test_an_ignored_file_the_new_version_brings_is_never_overwritten(self):
        (self.copia / ".git" / "info" / "exclude").write_text("config.env\n")
        escribir(self.copia, "config.env", "MIS-CLAVES\n")
        self.publicar("DE-GITHUB\n", nombre="config.env")
        estado, _, mensaje = update_clone(self.git, self.copia, "main")
        self.assertEqual(estado, "skip")
        self.assertIn("config.env", mensaje)
        self.assertEqual((self.copia / "config.env").read_text("utf-8"),
                         "MIS-CLAVES\n")

    def test_a_half_finished_merge_is_left_alone(self):
        (self.copia / ".git" / "MERGE_HEAD").write_text(
            self.git.sha(self.copia) + "\n")
        self.publicar("v2\n")
        estado, _, _ = update_clone(self.git, self.copia, "main")
        self.assertEqual(estado, "skip")
        self.assertEqual((self.copia / "a.txt").read_text("utf-8"), "v1\n")

    def test_remote_sha_without_cloning(self):
        sha = self.git.remote_sha(self.bare, "main")
        self.assertEqual(sha, self.git.sha(self.origen))
        self.assertEqual(self.git.remote_sha(self.bare, "inventada"), "")


class SnapshotUpdateTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.datos = self._tar([("f.txt", "v1\n")])
        self.datos2 = self._tar([("f.txt", "v2\n"), ("nuevo.txt", "n\n")])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tar(self, miembros):
        ruta = self.tmp / f"t{len(list(self.tmp.iterdir()))}.tar.gz"
        with tarfile.open(ruta, "w:gz") as tar:
            for nombre, texto in miembros:
                datos = texto.encode("utf-8")
                info = tarfile.TarInfo(f"ana-proj-sha/{nombre}")
                info.size = len(datos)
                tar.addfile(info, io.BytesIO(datos))
        return ruta.read_bytes()

    def abridor(self, contenido):
        pedidas = []

        def abrir(url, headers, timeout):
            pedidas.append(url)

            class R(io.BytesIO):
                def __enter__(self_):
                    return self_

                def __exit__(self_, *a):
                    self_.close()
            return R(contenido)
        return abrir, pedidas

    def bajar(self, destino, contenido, sha):
        abrir, _ = self.abridor(contenido)
        return snapshot.fetch_snapshot("ana", "proj", destino, branch="main",
                                       opener=abrir, sha=sha)

    def test_the_marker_records_what_was_downloaded(self):
        destino = self.tmp / "proj"
        self.assertEqual(self.bajar(destino, self.datos, "SHA1")[0], "ok")
        marca = snapshot.read_marker(destino)
        self.assertEqual(marca["sha"], "SHA1")
        self.assertEqual((marca["owner"], marca["repo"], marca["branch"]),
                         ("ana", "proj", "main"))
        self.assertIn("fecha", marca)

    def test_the_same_sha_downloads_nothing_at_all(self):
        destino = self.tmp / "proj"
        self.bajar(destino, self.datos, "SHA1")
        abrir, pedidas = self.abridor(self.datos2)
        estado, _, mensaje = snapshot.update_snapshot(
            "ana", "proj", destino, branch="main", sha_remoto="SHA1",
            opener=abrir)
        self.assertEqual(estado, "uptodate")
        self.assertEqual(pedidas, [], "descargó pese a no haber cambios")
        self.assertEqual((destino / "f.txt").read_text("utf-8"), "v1\n")

    def test_a_new_sha_downloads_and_keeps_the_previous_one_aside(self):
        destino = self.tmp / "proj"
        self.bajar(destino, self.datos, "SHA1")
        escribir(destino, "mis-notas.txt", "algo mío\n")
        abrir, pedidas = self.abridor(self.datos2)
        estado, _, mensaje = snapshot.update_snapshot(
            "ana", "proj", destino, branch="main", sha_remoto="SHA2",
            opener=abrir)
        self.assertEqual(estado, "ok", mensaje)
        self.assertEqual(len(pedidas), 1)
        self.assertEqual((destino / "f.txt").read_text("utf-8"), "v2\n")
        self.assertTrue((destino / "nuevo.txt").exists())
        self.assertEqual(snapshot.read_marker(destino)["sha"], "SHA2")
        # Lo anterior, incluido lo que el usuario había puesto, se conserva.
        anteriores = [p for p in self.tmp.iterdir()
                      if p.name.startswith("proj.anterior-")]
        self.assertEqual(len(anteriores), 1)
        self.assertEqual((anteriores[0] / "mis-notas.txt").read_text("utf-8"),
                         "algo mío\n")
        self.assertIn("anterior", mensaje)

    def test_a_failed_update_puts_everything_back(self):
        destino = self.tmp / "proj"
        self.bajar(destino, self.datos, "SHA1")
        escribir(destino, "mis-notas.txt", "algo mío\n")

        def abrir_roto(url, headers, timeout):
            raise OSError("sin red")
        estado, _, mensaje = snapshot.update_snapshot(
            "ana", "proj", destino, branch="main", sha_remoto="SHA2",
            opener=abrir_roto)
        self.assertEqual(estado, "fail")
        self.assertEqual((destino / "f.txt").read_text("utf-8"), "v1\n")
        self.assertEqual((destino / "mis-notas.txt").read_text("utf-8"),
                         "algo mío\n")
        self.assertEqual(snapshot.read_marker(destino)["sha"], "SHA1")
        self.assertEqual([p.name for p in self.tmp.iterdir()
                          if "anterior" in p.name], [])

    def test_a_folder_without_a_marker_is_left_alone(self):
        destino = self.tmp / "proj"
        destino.mkdir()
        escribir(destino, "cosas.txt", "de quién sabe dónde\n")
        estado, _, mensaje = snapshot.update_snapshot(
            "ana", "proj", destino, branch="main", sha_remoto="SHA2")
        self.assertEqual(estado, "skip")
        self.assertIn("no sé de qué versión", mensaje)
        self.assertTrue((destino / "cosas.txt").exists())


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class BulkUpdateTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bares, self.origenes, self.repos = [], [], []
        for i in range(2):
            bare = init_bare(self.tmp, f"r{i}")
            origen = self.tmp / f"o{i}"
            init_repo(origen)
            escribir(origen, "f.txt", f"v1-{i}\n")
            commit_all(origen, "primero")
            git(origen, "push", "-q", bare, "main")
            self.bares.append(bare)
            self.origenes.append(origen)
            self.repos.append({"name": f"repo{i}", "clone_url": bare,
                               "private": False, "owner": "ana",
                               "default_branch": "main"})
        self.salida = self.tmp / "salida"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def descargar(self, on_exists=ON_EXISTS_SKIP, mode=MODE_CLONE):
        return BulkCloner(workers=2).run(self.repos, self.salida, False, "",
                                         lambda k, p: None, mode=mode,
                                         on_exists=on_exists)

    def test_by_default_what_exists_is_skipped(self):
        self.assertEqual(self.descargar()["ok"], 2)
        s = self.descargar()
        self.assertEqual((s["ok"], s["skip"]), (0, 2))

    def test_updating_brings_the_new_commits(self):
        self.descargar()
        escribir(self.origenes[0], "f.txt", "v2\n")
        commit_all(self.origenes[0], "segundo")
        git(self.origenes[0], "push", "-q", self.bares[0], "main")
        s = self.descargar(on_exists=ON_EXISTS_UPDATE)
        self.assertEqual(s["update"], 1)        # uno cambió
        self.assertEqual(s["uptodate"], 1)      # el otro no
        self.assertEqual(s["ok"], 0)
        self.assertEqual((self.salida / "repo0" / "f.txt").read_text("utf-8"),
                         "v2\n")

    def test_updating_never_touches_a_folder_with_local_work(self):
        self.descargar()
        escribir(self.salida / "repo0", "f.txt", "MI VERSIÓN\n")
        escribir(self.origenes[0], "f.txt", "v2\n")
        commit_all(self.origenes[0], "segundo")
        git(self.origenes[0], "push", "-q", self.bares[0], "main")
        s = self.descargar(on_exists=ON_EXISTS_UPDATE)
        self.assertEqual(s["skip"], 1)
        self.assertEqual((self.salida / "repo0" / "f.txt").read_text("utf-8"),
                         "MI VERSIÓN\n")


if __name__ == "__main__":
    unittest.main()
