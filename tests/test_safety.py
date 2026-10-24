# -*- coding: utf-8 -*-
"""Seguridad de los datos: nada se borra ni se modifica sin permiso.

Estas pruebas no comprueban «pasó lo que esperaba en el archivo X»; hacen
una FOTO completa de la carpeta (ruta + hash de cada archivo) y del
repositorio remoto antes y después, y exigen que el conjunto de cambios
sea EXACTAMENTE el autorizado. Cualquier archivo de más que aparezca,
desaparezca o cambie de contenido rompe la prueba, aunque nadie hubiera
pensado en él al escribirla.

Invariantes que se defienden aquí:

  1. Publicar NUNCA borra ni modifica archivos de tu carpeta (en modo
     agregar/editar puede añadir los que solo estaban en GitHub).
  2. En GitHub solo cambia lo que la vista previa anunció, menos lo que
     el usuario dejó fuera.
  3. Cancelar (en la revisión, en la vista previa o por error) no deja
     ni un byte distinto en ningún lado.
  4. Las operaciones que SÍ son destructivas por diseño (traer cambios
     en modo espejo, restaurar una versión) dejan siempre lo anterior
     recuperable byte a byte en una rama de respaldo.
"""

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.gitenv import HAVE_GIT, clone, git, init_bare

from gitpush_studio.core import history, sync
from gitpush_studio.core.git import Git
from gitpush_studio.core.publish import (Callbacks, PreviewChoice, PublishJob,
                                         publish, publish_many)


# ------------------------------------------------------------------ #
#  Fotos y comparaciones                                              #
# ------------------------------------------------------------------ #
def snapshot(folder):
    """{ruta relativa: hash del contenido} de toda la carpeta, salvo .git."""
    root = Path(folder)
    out = {}
    for p in root.rglob("*"):
        if p.is_file() and ".git" not in p.relative_to(root).parts:
            rel = p.relative_to(root).as_posix()
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def remote_snapshot(bare, branch="main"):
    """{ruta: hash del blob} de la rama publicada.

    Con -z git entrega las rutas tal cual (sin escapar los acentos), que
    es justo lo que se quiere comparar.
    """
    p = subprocess.run(["git", "ls-tree", "-r", "-z", branch], cwd=bare,
                       capture_output=True, text=True, encoding="utf-8")
    out = {}
    for entry in p.stdout.split("\0"):
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and path:
            out[path] = parts[2]
    return out


def delta(before, after):
    """(añadidos, modificados, borrados) entre dos fotos."""
    added = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    modified = sorted(k for k in set(before) & set(after)
                      if before[k] != after[k])
    return added, modified, deleted


def write(folder, rel, content=b"x\n"):
    p = Path(folder) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode("utf-8")
    p.write_bytes(content)
    return p


def make_bare(where, name="remote"):
    return init_bare(where, name)


class SafetyCase(unittest.TestCase):
    """Base con las aserciones de invariante."""

    def assert_delta(self, before, after, added=(), modified=(), deleted=(),
                     what="la carpeta"):
        a, m, d = delta(before, after)
        self.assertEqual(a, sorted(added), f"archivos INESPERADOS en {what}: "
                                           f"{sorted(set(a) - set(added))}")
        self.assertEqual(m, sorted(modified),
                         f"archivos MODIFICADOS sin permiso en {what}: "
                         f"{sorted(set(m) - set(modified))}")
        self.assertEqual(d, sorted(deleted),
                         f"archivos BORRADOS sin permiso en {what}: "
                         f"{sorted(set(d) - set(deleted))}")

    def assert_untouched(self, before, after, what="la carpeta"):
        self.assert_delta(before, after, what=what)

    def assert_recoverable(self, folder, branch, paths, originals):
        """Cada ruta perdida debe poder recuperarse, byte a byte, desde la
        rama de respaldo."""
        self.assertTrue(branch, "no se creó ninguna rama de respaldo")
        for rel in paths:
            got = subprocess.run(["git", "show", f"{branch}:{rel}"],
                                 cwd=folder, capture_output=True)
            self.assertEqual(got.returncode, 0,
                             f"«{rel}» no está en la rama de respaldo")
            self.assertEqual(hashlib.sha256(got.stdout).hexdigest(),
                             originals[rel],
                             f"«{rel}» se recupera con otro contenido")


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class PublishNeverTouchesYourFolder(SafetyCase):
    """Invariante 1: publicar no borra ni cambia archivos tuyos."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bare = make_bare(self.tmp)
        self.proj = Path(self.tmp) / "proj"
        write(self.proj, "a.txt", "A\n")
        write(self.proj, "src/app.js", "console.log(1)\n")
        write(self.proj, "img/logo.png", b"\x89PNG\x00\x01\x02\r\n")
        write(self.proj, "docs/guía de uso.md", "# Guía\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def job(self, **kw):
        base = dict(folder=str(self.proj), url=self.bare, user="T",
                    email="t@x.y", preview=False, check=False)
        base.update(kw)
        return PublishJob(**base)

    def test_first_publish_changes_nothing_on_disk(self):
        before = snapshot(self.proj)
        self.assertEqual(publish(self.job()).status, "ok")
        self.assert_untouched(before, snapshot(self.proj))
        self.assertEqual(sorted(remote_snapshot(self.bare)), sorted(before))

    def test_binary_and_unicode_survive_the_round_trip(self):
        self.assertEqual(publish(self.job()).status, "ok")
        copia = clone(self.bare, Path(self.tmp) / "clon")
        self.assertEqual(snapshot(self.proj), snapshot(copia))

    def test_republishing_twice_is_idempotent_on_disk(self):
        self.assertEqual(publish(self.job()).status, "ok")
        before = snapshot(self.proj)
        remote_before = remote_snapshot(self.bare)
        self.assertEqual(publish(self.job()).status, "ok")
        self.assert_untouched(before, snapshot(self.proj))
        self.assert_untouched(remote_before, remote_snapshot(self.bare),
                              "GitHub")

    def test_mirror_mode_never_deletes_local_files(self):
        """GitHub tiene archivos que tú no: en espejo desaparecen de
        GitHub, pero tu carpeta no pierde nada."""
        otro = Path(self.tmp) / "otro"
        write(otro, "solo-en-github.txt", "S\n")
        write(otro, "a.txt", "A\n")
        self.assertEqual(publish(PublishJob(
            folder=str(otro), url=self.bare, user="T", email="t@x.y",
            preview=False, check=False)).status, "ok")
        before = snapshot(self.proj)
        self.assertEqual(publish(self.job()).status, "ok")
        self.assert_untouched(before, snapshot(self.proj))
        self.assertNotIn("solo-en-github.txt", remote_snapshot(self.bare))

    def test_addedit_only_adds_remote_files_never_overwrites(self):
        otro = Path(self.tmp) / "otro"
        write(otro, "solo-en-github.txt", "S\n")
        write(otro, "a.txt", "VERSION-DE-GITHUB\n")
        self.assertEqual(publish(PublishJob(
            folder=str(otro), url=self.bare, user="T", email="t@x.y",
            preview=False, check=False)).status, "ok")
        before = snapshot(self.proj)
        self.assertEqual(publish(self.job(mode="addedit")).status, "ok")
        # Solo puede APARECER lo que estaba únicamente en GitHub.
        self.assert_delta(before, snapshot(self.proj),
                          added=["solo-en-github.txt"])
        self.assertEqual((self.proj / "a.txt").read_text("utf-8"), "A\n")

    def _github_has(self, files):
        otro = Path(self.tmp) / "otro-pc"
        for rel, content in files.items():
            write(otro, rel, content)
        self.assertEqual(publish(PublishJob(
            folder=str(otro), url=self.bare, user="T", email="t@x.y",
            preview=False, check=False)).status, "ok")
        return otro

    def test_addedit_never_overwrites_a_private_file_github_also_has(self):
        """El fallo que se escapaba: «-X ours» trae lo que solo está en
        GitHub y git pisa SIN AVISAR los archivos ignorados que estén en
        esas rutas. Tu .env (que no publicas) se perdía para siempre: no
        estaba en ningún commit."""
        self._github_has({"config.env": "DE-GITHUB\n", "a.txt": "A\n"})
        write(self.proj, "config.env", "MIS-CLAVES-REALES\n")
        before = snapshot(self.proj)
        r = publish(self.job(mode="addedit", ignore=["config.env"]))
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual((self.proj / "config.env").read_text("utf-8"),
                         "MIS-CLAVES-REALES\n")
        added, modified, deleted = delta(before, snapshot(self.proj))
        self.assertEqual((modified, deleted), ([], []))
        # GitHub conserva el suyo (agregar/editar nunca borra allí).
        self.assertEqual(subprocess.run(
            ["git", "show", "main:config.env"], cwd=self.bare,
            capture_output=True, text=True).stdout, "DE-GITHUB\n")
        # Y hay copia exacta del tuyo por si algo hubiera fallado a medias.
        ramas = [b for b in Git().local_branches(self.proj)
                 if b.startswith("respaldo-")]
        self.assertEqual(len(ramas), 1)
        self.assertEqual(subprocess.run(
            ["git", "show", f"{ramas[0]}:config.env"], cwd=self.proj,
            capture_output=True, text=True).stdout, "MIS-CLAVES-REALES\n")

    def test_addedit_failed_merge_still_keeps_your_private_files(self):
        """Si la combinación falla (lo borraste y GitHub lo cambió), git
        deja la versión de GitHub en el disco antes de abortar."""
        self.assertEqual(publish(self.job()).status, "ok")
        otro = clone(self.bare, Path(self.tmp) / "otro-pc")
        write(otro, "src/app.js", "versión nueva en GitHub\n")
        self.assertEqual(publish(PublishJob(
            folder=otro, url=self.bare, user="T", email="t@x.y",
            preview=False, check=False)).status, "ok")
        # Aquí: se deja de publicar src/app.js (cuadro de ignorar), y el
        # archivo local cambia. GitHub lo cambió también → no combina.
        write(self.proj, "src/app.js", "MI VERSIÓN PRIVADA\n")
        r = publish(self.job(mode="addedit", ignore=["src/app.js"]))
        self.assertEqual((self.proj / "src/app.js").read_text("utf-8"),
                         "MI VERSIÓN PRIVADA\n", r.message)

    def test_excluding_a_published_file_is_announced_even_in_addedit(self):
        """En agregar/editar la vista previa ocultaba TODOS los borrados,
        también los que provoca excluir un archivo que ya estaba en
        GitHub: desaparecía de allí sin que nadie lo anunciara."""
        self.assertEqual(publish(self.job()).status, "ok")
        vistos = []

        def mirar(added, modified, deleted, mode):
            vistos.append(list(deleted))
            return PreviewChoice(keep_deleted=list(deleted),
                                 restore_to_folder=False)

        r = publish(self.job(mode="addedit", preview=True,
                             ignore=["docs/"]),
                    Callbacks(ask_preview=mirar))
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(vistos, [["docs/guía de uso.md"]])
        # El usuario pidió mantenerlo: sigue en GitHub.
        self.assertIn("docs/guía de uso.md", remote_snapshot(self.bare))

    def test_a_tag_named_like_the_branch_is_never_merged(self):
        """Con «git fetch origin main» a secas, una ETIQUETA llamada
        «main» en GitHub gana a la rama: se combinaba con otra historia
        (y en modo espejo quedaba de padre en lo publicado)."""
        self.assertEqual(publish(self.job()).status, "ok")
        otro = Path(self.tmp) / "otro"
        subprocess.run(["git", "init", "-q", str(otro)], check=True)
        write(otro, "ajeno.txt", "historia ajena\n")
        git(otro, "add", "-A")
        git(otro, "-c", "user.name=t", "-c", "user.email=t@x", "commit",
            "-qm", "ajeno")
        git(otro, "tag", "main")
        git(otro, "push", "-q", self.bare, "refs/tags/main")
        ajeno = git(otro, "rev-parse", "HEAD").strip()
        write(self.proj, "nuevo.txt", "N\n")
        for modo in ("addedit", "mirror"):
            r = publish(self.job(mode=modo))
            self.assertEqual(r.status, "ok", r.message)
            historia = git(self.bare, "rev-list", "refs/heads/main")
            self.assertNotIn(ajeno, historia,
                             f"la etiqueta entró en la rama ({modo})")
        # (Ojo: también aquí «main» a secas sería la etiqueta.)
        self.assertNotIn("ajeno.txt",
                         remote_snapshot(self.bare, "refs/heads/main"))
        self.assertIn("nuevo.txt",
                      remote_snapshot(self.bare, "refs/heads/main"))
        self.assertFalse((self.proj / "ajeno.txt").exists())

    def test_keeping_a_deletion_never_removes_a_file_in_the_way(self):
        """Mantener «docs/guía de uso.md» y devolverlo a la carpeta exige
        una carpeta «docs»; si ahí hay un ARCHIVO tuyo (ignorado), git
        checkout lo borraba para poder crearla."""
        self.assertEqual(publish(self.job()).status, "ok")
        shutil.rmtree(self.proj / "docs")
        write(self.proj, "docs", "MIS NOTAS (no se publican)\n")
        (self.proj / ".git" / "info" / "exclude").write_text("docs\n")
        cb = Callbacks(ask_preview=lambda a, m, d, mode: PreviewChoice(
            keep_deleted=list(d), restore_to_folder=True))
        r = publish(self.job(preview=True), cb)
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual((self.proj / "docs").read_text("utf-8"),
                         "MIS NOTAS (no se publican)\n")
        self.assertIn("docs/guía de uso.md", remote_snapshot(self.bare))

    def test_addedit_never_deletes_from_github_even_with_history(self):
        """Con historial local, borrar archivos en la carpeta no puede
        borrarlos de GitHub en modo agregar/editar."""
        self.assertEqual(publish(self.job(mode="addedit")).status, "ok")
        remote_before = remote_snapshot(self.bare)
        (self.proj / "a.txt").unlink()
        shutil.rmtree(self.proj / "src")
        shutil.rmtree(self.proj / "docs")
        write(self.proj, "nuevo.txt", "N\n")
        r = publish(self.job(mode="addedit"))
        self.assertEqual(r.status, "ok", r.message)
        self.assert_delta(remote_before, remote_snapshot(self.bare),
                          added=["nuevo.txt"], what="GitHub")

    def test_addedit_repeated_deletions_still_never_remove_anything(self):
        self.assertEqual(publish(self.job(mode="addedit")).status, "ok")
        remote_before = remote_snapshot(self.bare)
        for name in ("a.txt", "src/app.js", "img/logo.png"):
            (self.proj / name).unlink()
        for _ in range(2):
            self.assertEqual(publish(self.job(mode="addedit")).status, "ok")
        self.assert_untouched(remote_before, remote_snapshot(self.bare),
                              "GitHub")

    def test_adding_a_license_only_adds_that_one_file(self):
        before = snapshot(self.proj)
        r = publish(self.job(license="apache-2.0", license_owner="Ana"))
        self.assertEqual(r.status, "ok", r.message)
        self.assert_delta(before, snapshot(self.proj), added=["LICENSE"])
        self.assertIn("Apache License",
                      (self.proj / "LICENSE").read_text("utf-8"))

    def test_an_existing_license_is_never_replaced(self):
        write(self.proj, "LICENSE", "LICENCIA PROPIA DE LA EMPRESA\n")
        before = snapshot(self.proj)
        r = publish(self.job(license="gpl-3.0", license_owner="Ana"))
        self.assertEqual(r.status, "ok", r.message)
        self.assert_untouched(before, snapshot(self.proj))
        self.assertEqual((self.proj / "LICENSE").read_bytes(),
                         b"LICENCIA PROPIA DE LA EMPRESA\n")

    def test_failed_push_leaves_the_folder_intact(self):
        before = snapshot(self.proj)
        r = publish(self.job(url=str(Path(self.tmp) / "no-existe.git")))
        self.assertEqual(r.status, "error")
        self.assert_untouched(before, snapshot(self.proj))

    def test_the_ignore_box_also_stops_what_was_already_being_published(self):
        """El caso que se escapaba: el proyecto YA era un repositorio y
        esos archivos ya estaban rastreados, así que el .gitignore por sí
        solo no los frenaba."""
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True)
        git(self.proj, "config", "user.name", "t")
        git(self.proj, "config", "user.email", "t@x")
        write(self.proj, "app/ACCESS.md", "interno\n")
        write(self.proj, "bin/programa.exe", "binario\n")
        git(self.proj, "add", "-A")
        git(self.proj, "commit", "-qm", "trabajo previo")
        antes = snapshot(self.proj)
        r = publish(self.job(ignore=["app", "bin"]))
        self.assertEqual(r.status, "ok", r.message)
        remoto = remote_snapshot(self.bare)
        self.assertNotIn("app/ACCESS.md", remoto)
        self.assertNotIn("bin/programa.exe", remoto)
        self.assertIn("a.txt", remoto)
        # Y siguen en la carpeta, intactos: solo dejan de subirse.
        self.assert_delta(antes, snapshot(self.proj), added=[".gitignore"])

    def test_a_file_added_on_purpose_despite_gitignore_is_respected(self):
        """Si el usuario forzó un archivo que su propio .gitignore excluye,
        no somos nadie para quitarlo: solo actúan los patrones del cuadro."""
        write(self.proj, ".gitignore", "*.clave\n")
        write(self.proj, "importante.clave", "lo quiero dentro\n")
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True)
        git(self.proj, "config", "user.name", "t")
        git(self.proj, "config", "user.email", "t@x")
        git(self.proj, "add", "-A", "-f")
        git(self.proj, "commit", "-qm", "a propósito")
        r = publish(self.job(ignore=["node_modules/"]))
        self.assertEqual(r.status, "ok", r.message)
        self.assertIn("importante.clave", remote_snapshot(self.bare))

    def test_ignored_files_are_never_staged_nor_touched(self):
        write(self.proj, "secretos/.env", "CLAVE=123\n")
        write(self.proj, "node_modules/lib.js", "x\n")
        before = snapshot(self.proj)
        self.assertEqual(publish(self.job(
            ignore=["secretos/", "node_modules/"])).status, "ok")
        remote = remote_snapshot(self.bare)
        self.assertNotIn("secretos/.env", remote)
        self.assertNotIn("node_modules/lib.js", remote)
        # El .gitignore es el único archivo nuevo permitido en la carpeta.
        self.assert_delta(before, snapshot(self.proj), added=[".gitignore"])


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class EmbeddedRepositoriesAreNotPublishedAsBrokenLinks(SafetyCase):
    """Una carpeta con su propio .git dentro del proyecto: `git add` la
    registra como un enlace a un commit que GitHub no tiene (una carpeta
    que no se abre, sin un solo archivo) y la publicación decía «hecho».
    Ahora no se sube, se explica por qué, y la carpeta queda intacta."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bare = make_bare(self.tmp)
        self.proj = Path(self.tmp) / "proj"
        write(self.proj, "a.txt", "A\n")
        self.log = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def nested(self, rel):
        sub = self.proj / rel
        sub.mkdir(parents=True)
        git(sub, "init", "-q")
        write(sub, "lib.py", "x = 1\n")
        git(sub, "add", "-A")
        git(sub, "commit", "-q", "--no-gpg-sign", "-m", "interno")
        return git(sub, "rev-parse", "HEAD").strip()

    def publish(self, **kw):
        cb = Callbacks(log=lambda texto, tag="out": self.log.append(texto))
        r = publish(PublishJob(folder=str(self.proj), url=self.bare,
                               user="T", email="t@x.y", preview=False,
                               check=False, **kw), cb)
        self.assertEqual(r.status, "ok", r.message)
        return r

    def test_a_nested_repo_is_left_out_and_explained(self):
        cabeza = self.nested("vendor")
        antes = snapshot(self.proj)
        self.publish()
        self.assertEqual(sorted(remote_snapshot(self.bare)), ["a.txt"])
        self.assertTrue(any("«vendor»" in linea and "NO se sube" in linea
                            for linea in self.log), self.log)
        self.assert_untouched(antes, snapshot(self.proj))
        self.assertEqual(git(self.proj / "vendor", "rev-parse",
                             "HEAD").strip(), cabeza)
        # Las siguientes publicaciones tampoco la suben.
        write(self.proj, "b.txt", "B\n")
        self.publish(mode="addedit")
        self.assertEqual(sorted(remote_snapshot(self.bare)),
                         ["a.txt", "b.txt"])

    def test_auto_publish_does_not_see_a_nested_repo_as_a_change(self):
        """Git lo enseña siempre como «?? vendor/»: si contara, la
        auto-publicación publicaría para nada en cada vuelta."""
        self.nested("vendor")
        self.publish()
        g = Git()
        self.assertEqual(g.status_changes(self.proj), ["vendor/"])
        self.assertEqual(g.pending_changes(self.proj), [])
        write(self.proj, "nuevo.txt", "N\n")
        self.assertEqual(g.pending_changes(self.proj), ["nuevo.txt"])

    def test_a_nested_repo_without_commits_does_not_break_publishing(self):
        """`git add` fallaba entero («does not have a commit checked out»)
        y no se podía publicar nada mientras existiera esa carpeta."""
        for modo in ("mirror", "addedit"):
            sub = self.proj / f"nuevo-{modo}"
            sub.mkdir()
            git(sub, "init", "-q")
            write(sub, "x.py", "x = 1\n")
            write(self.proj, f"{modo}.txt", "M\n")
            self.publish(mode=modo)
            self.assertNotIn(f"nuevo-{modo}", remote_snapshot(self.bare))
            self.assertIn(f"{modo}.txt", remote_snapshot(self.bare))
            self.assertTrue(any(f"«nuevo-{modo}»" in linea
                                and "NO se sube" in linea
                                for linea in self.log), self.log)

    def test_a_registered_submodule_is_still_published(self):
        cabeza = self.nested("lib")
        write(self.proj, ".gitmodules", '[submodule "lib"]\n\tpath = lib\n'
                                        '\turl = https://example.com/l.git\n')
        self.publish()
        self.assertEqual(remote_snapshot(self.bare).get("lib"), cabeza)
        self.assertFalse(any("NO se sube" in linea for linea in self.log))

    @unittest.skipUnless(os.name == "posix", "«*» no vale en Windows")
    def test_a_name_with_wildcards_only_leaves_out_itself(self):
        """Las rutas van a git como literales: «lib*» no puede dejar
        fuera «lib-extra.txt»."""
        self.nested("lib*")
        write(self.proj, "lib-extra.txt", "E\n")
        self.publish()
        self.assertEqual(sorted(remote_snapshot(self.bare)),
                         ["a.txt", "lib-extra.txt"])

    def test_a_link_that_was_already_published_is_left_as_git_keeps_it(self):
        """Solo se frenan los enlaces NUEVOS: uno que ya estaba en GitHub
        (de antes) sigue el comportamiento de siempre de git."""
        self.nested("vendor")
        git(self.proj, "init", "-q")
        git(self.proj, "add", "-A")
        git(self.proj, "commit", "-q", "--no-gpg-sign", "-m", "con enlace")
        write(self.proj / "vendor", "otro.py", "y = 2\n")
        git(self.proj / "vendor", "add", "-A")
        git(self.proj / "vendor", "commit", "-q", "--no-gpg-sign", "-m", "2")
        nueva = git(self.proj / "vendor", "rev-parse", "HEAD").strip()
        self.publish()
        self.assertEqual(remote_snapshot(self.bare).get("vendor"), nueva)
        self.assertFalse(any("NO se sube" in linea for linea in self.log))

    def test_a_file_that_became_a_nested_repo_is_not_removed_from_github(self):
        write(self.proj, "vendor", "archivo publicado\n")
        self.publish()
        antes = remote_snapshot(self.bare)
        (self.proj / "vendor").unlink()
        self.nested("vendor")
        for modo in ("addedit", "mirror"):
            self.publish(mode=modo)
            self.assertEqual(remote_snapshot(self.bare), antes, modo)


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class OnlyApprovedChangesReachGitHub(SafetyCase):
    """Invariante 2: en GitHub solo cambia lo aprobado en la vista previa."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bare = make_bare(self.tmp)
        self.proj = Path(self.tmp) / "proj"
        for name in ("a.txt", "b.txt", "c.txt"):
            write(self.proj, name, f"{name}-v1\n")
        write(self.proj, "sub/d.txt", "d-v1\n")
        self.assertEqual(publish(self.job(preview=False)).status, "ok")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def job(self, **kw):
        base = dict(folder=str(self.proj), url=self.bare, user="T",
                    email="t@x.y", check=False)
        base.update(kw)
        return PublishJob(**base)

    def run_with(self, answer, **job_kw):
        """Publica capturando lo que la vista previa anunció."""
        seen = {}

        def ask_preview(added, modified, deleted, mode):
            seen["announced"] = (list(added), list(modified), list(deleted))
            return answer(added, modified, deleted) if callable(answer) \
                else answer
        cb = Callbacks(ask_preview=ask_preview)
        local_before = snapshot(self.proj)
        remote_before = remote_snapshot(self.bare)
        result = publish(self.job(**job_kw), cb)
        return (result, seen.get("announced"), local_before, remote_before)

    def test_remote_delta_equals_what_the_preview_announced(self):
        write(self.proj, "a.txt", "a-v2\n")
        write(self.proj, "nuevo.txt", "n\n")
        (self.proj / "b.txt").unlink()
        r, announced, _, remote_before = self.run_with(True)
        self.assertEqual(r.status, "ok", r.message)
        added, modified, deleted = announced
        self.assert_delta(remote_before, remote_snapshot(self.bare),
                          added=added, modified=modified, deleted=deleted,
                          what="GitHub")

    def remote_head(self):
        return subprocess.run(["git", "rev-parse", "main"], cwd=self.bare,
                              capture_output=True, text=True).stdout.strip()

    def assert_history_preserved(self, old_head):
        """La versión que había en GitHub debe seguir siendo antecesora de
        la nueva: publicar añade historia, nunca la reescribe."""
        r = subprocess.run(["git", "merge-base", "--is-ancestor", old_head,
                            "main"], cwd=self.bare, capture_output=True)
        self.assertEqual(r.returncode, 0,
                         "¡se reescribió el historial de GitHub! la versión "
                         f"{old_head[:7]} ya no es alcanzable")

    def test_publishing_never_rewrites_remote_history(self):
        old_head = self.remote_head()
        write(self.proj, "a.txt", "a-v2\n")
        (self.proj / "b.txt").unlink()
        self.assertEqual(publish(self.job(preview=False)).status, "ok")
        self.assert_history_preserved(old_head)

    def test_publishing_from_an_unrelated_folder_keeps_remote_history(self):
        old_head = self.remote_head()
        otro = Path(self.tmp) / "sin-relacion"
        write(otro, "z.txt", "Z\n")
        self.assertEqual(publish(PublishJob(
            folder=str(otro), url=self.bare, user="T", email="t@x.y",
            preview=False, check=False)).status, "ok")
        self.assert_history_preserved(old_head)
        # Y lo anterior sigue siendo recuperable desde el propio GitHub.
        got = subprocess.run(["git", "show", f"{old_head}:a.txt"],
                             cwd=self.bare, capture_output=True, text=True)
        self.assertEqual(got.stdout, "a.txt-v1\n")

    def test_a_concurrent_push_is_never_overwritten(self):
        """Si alguien publica mientras tú decides, tu publicación debe
        fallar y su trabajo seguir ahí (un push forzado lo borraría)."""
        otro = clone(self.bare, Path(self.tmp) / "companero")
        otro = Path(otro)
        write(otro, "de-mi-companero.txt", "SU TRABAJO\n")
        git(otro, "add", "-A")
        git(otro, "commit", "-qm", "trabajo del compañero")

        def interponerse(added, modified, deleted, mode):
            # Ocurre DESPUÉS de que nuestra publicación consultara GitHub.
            git(otro, "push", "-q", "origin", "HEAD:main")
            return True

        write(self.proj, "a.txt", "a-v2\n")
        r = publish(self.job(), Callbacks(ask_preview=interponerse))
        self.assertEqual(r.status, "error")
        self.assertIn("alguien más", r.message)
        remote = remote_snapshot(self.bare)
        self.assertIn("de-mi-companero.txt", remote)
        self.assertEqual(subprocess.run(
            ["git", "show", "main:de-mi-companero.txt"], cwd=self.bare,
            capture_output=True, text=True).stdout, "SU TRABAJO\n")

    def test_cancelling_the_preview_changes_absolutely_nothing(self):
        write(self.proj, "a.txt", "a-v2\n")
        write(self.proj, "nuevo.txt", "n\n")
        (self.proj / "b.txt").unlink()
        r, _, local_before, remote_before = self.run_with(False)
        self.assertEqual(r.status, "cancelled")
        self.assert_untouched(local_before, snapshot(self.proj))
        self.assert_untouched(remote_before, remote_snapshot(self.bare),
                              "GitHub")

    def test_keeping_everything_leaves_github_untouched(self):
        for name in ("a.txt", "b.txt", "c.txt"):
            (self.proj / name).unlink()
        (self.proj / "sub" / "d.txt").unlink()
        r, announced, _, remote_before = self.run_with(
            lambda a, m, d: PreviewChoice(keep_deleted=d,
                                          restore_to_folder=False))
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(len(announced[2]), 4)
        self.assert_untouched(remote_before, remote_snapshot(self.bare),
                              "GitHub")
        self.assertEqual(sorted(snapshot(self.proj)), [])   # nada reaparece

    def test_skipping_everything_leaves_github_untouched(self):
        write(self.proj, "a.txt", "a-v2\n")
        write(self.proj, "sub/d.txt", "d-v2\n")
        write(self.proj, "nuevo.txt", "n\n")
        write(self.proj, "sub/nuevo2.txt", "n2\n")
        r, _, local_before, remote_before = self.run_with(
            lambda a, m, d: PreviewChoice(skip_added=a, skip_modified=m,
                                          ignore_skipped=False))
        self.assertEqual(r.status, "ok", r.message)
        self.assert_untouched(remote_before, remote_snapshot(self.bare),
                              "GitHub")
        self.assert_untouched(local_before, snapshot(self.proj))

    def test_one_decision_never_leaks_to_its_neighbours(self):
        """Marcar UN archivo de cada grupo no puede afectar a los demás."""
        for name in ("a.txt", "b.txt", "c.txt", "sub/d.txt"):
            write(self.proj, name, f"{name}-v2\n")
        write(self.proj, "nuevo1.txt", "n1\n")
        write(self.proj, "nuevo2.txt", "n2\n")
        r, announced, _, remote_before = self.run_with(
            lambda a, m, d: PreviewChoice(skip_modified=["a.txt"],
                                          skip_added=["nuevo1.txt"],
                                          ignore_skipped=False))
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(announced[1], ["a.txt", "b.txt", "c.txt",
                                        "sub/d.txt"])
        self.assertEqual(announced[0], ["nuevo1.txt", "nuevo2.txt"])
        self.assert_delta(remote_before, remote_snapshot(self.bare),
                          added=["nuevo2.txt"],
                          modified=["b.txt", "c.txt", "sub/d.txt"],
                          what="GitHub")

    def test_keeping_one_deletion_does_not_keep_the_others(self):
        for name in ("a.txt", "b.txt", "c.txt"):
            (self.proj / name).unlink()
        r, _, _, remote_before = self.run_with(
            lambda a, m, d: PreviewChoice(keep_deleted=["b.txt"],
                                          restore_to_folder=False))
        self.assertEqual(r.status, "ok", r.message)
        self.assert_delta(remote_before, remote_snapshot(self.bare),
                          deleted=["a.txt", "c.txt"], what="GitHub")

    def test_tricky_names_are_matched_exactly(self):
        """Espacios, tildes, guiones iniciales y prefijos comunes."""
        names = ["con espacio.txt", "acentuado ñá.txt", "--raro.txt",
                 "a.txt.bak", "sub/a.txt"]
        for name in names:
            write(self.proj, name, "v1\n")
        self.assertEqual(publish(self.job(preview=False)).status, "ok")
        for name in names:
            write(self.proj, name, "v2\n")
        write(self.proj, "a.txt", "a-v2\n")
        r, _, _, remote_before = self.run_with(
            lambda a, m, d: PreviewChoice(
                skip_modified=["con espacio.txt", "--raro.txt", "a.txt"]))
        self.assertEqual(r.status, "ok", r.message)
        self.assert_delta(remote_before, remote_snapshot(self.bare),
                          modified=["acentuado ñá.txt", "a.txt.bak",
                                    "sub/a.txt"], what="GitHub")

    @unittest.skipUnless(os.name == "posix", "«:» no vale en Windows")
    def test_names_that_look_like_git_magic_are_taken_literally(self):
        """Para git, «:!a.txt» como ruta significa «todo menos a.txt» y
        «[bc].txt» es un patrón. Antes, mantener esos borrados ejecutaba
        `git checkout FETCH_HEAD -- ':!a.txt'` y PISABA cada archivo
        modificado de la carpeta con la versión de GitHub."""
        raros = [":!a.txt", "[bc].txt"]
        for nombre in raros:
            write(self.proj, nombre, "raro v1\n")
        self.assertEqual(publish(self.job(preview=False)).status, "ok")
        write(self.proj, "b.txt", "b MÍO sin publicar\n")
        for nombre in raros:
            (self.proj / nombre).unlink()
        r, announced, local_before, remote_before = self.run_with(
            lambda a, m, d: PreviewChoice(keep_deleted=list(raros)))
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(sorted(announced[2]), sorted(raros))
        self.assertEqual((self.proj / "b.txt").read_text(encoding="utf-8"),
                         "b MÍO sin publicar\n")
        self.assert_delta(local_before, snapshot(self.proj), added=raros)
        self.assert_delta(remote_before, remote_snapshot(self.bare),
                          modified=["b.txt"], what="GitHub")

    @unittest.skipUnless(os.name == "posix", "«:» no vale en Windows")
    def test_skipping_a_magic_looking_name_skips_only_that_file(self):
        write(self.proj, ":!a.txt", "raro v1\n")
        self.assertEqual(publish(self.job(preview=False)).status, "ok")
        write(self.proj, ":!a.txt", "raro v2\n")
        write(self.proj, "b.txt", "b v2\n")
        write(self.proj, ":!nuevo.txt", "n\n")
        write(self.proj, "nuevo.txt", "n\n")
        r, _, _, remote_before = self.run_with(
            lambda a, m, d: PreviewChoice(skip_modified=[":!a.txt"],
                                          skip_added=[":!nuevo.txt"],
                                          ignore_skipped=False))
        self.assertEqual(r.status, "ok", r.message)
        self.assert_delta(remote_before, remote_snapshot(self.bare),
                          added=["nuevo.txt"], modified=["b.txt"],
                          what="GitHub")

    @unittest.skipUnless(os.name == "posix", "«:» no vale en Windows")
    def test_excluding_a_magic_looking_name_unpublishes_only_that_file(self):
        """`git rm -r --cached -- ':!a.txt'` dejaba de publicar TODO el
        proyecto salvo a.txt: el modo espejo lo borraba de GitHub."""
        write(self.proj, ":!a.txt", "raro\n")
        # Uno publicado a propósito aunque el .gitignore lo excluye: si se
        # dejara de rastrear por error, `git add` ya no lo devolvería.
        write(self.proj, ".gitignore", "build/\n")
        write(self.proj, "build/keep.txt", "a propósito\n")
        git(self.proj, "add", "-f", "build/keep.txt")
        self.assertEqual(publish(self.job(preview=False)).status, "ok")
        antes = remote_snapshot(self.bare)
        self.assertIn("build/keep.txt", antes)
        r = publish(self.job(preview=False, ignore=["/:!a.txt"]))
        self.assertEqual(r.status, "ok", r.message)
        despues = remote_snapshot(self.bare)
        self.assertNotIn(":!a.txt", despues)
        for nombre in ("a.txt", "b.txt", "c.txt", "sub/d.txt",
                       "build/keep.txt"):
            self.assertEqual(despues.get(nombre), antes[nombre], nombre)
        self.assertTrue((self.proj / ":!a.txt").exists())

    def test_a_literal_name_typed_as_a_pattern_is_called_out(self):
        """Escribir «datos[2024].csv» en el cuadro de ignorar no excluye
        ese archivo: para git «[2024]» es una clase de caracteres. El
        archivo está, el patrón está, y no pasa nada. Ese silencio es lo
        peligroso, así que hay que decirlo y enseñar cómo escribirlo.
        """
        write(self.proj, "datos[2024].csv", "FILAS\n")
        dicho = []
        r = publish(self.job(preview=False, ignore=["datos[2024].csv"]),
                    Callbacks(log=lambda t, tag="out": dicho.append((t, tag))))
        self.assertEqual(r.status, "ok", r.message)
        avisos = [t for t, tag in dicho if tag == "warn"]
        self.assertTrue(any("tiene comodines" in t and r"\[2024\]" in t
                            for t in avisos), avisos)

    def test_a_normal_pattern_is_not_called_out(self):
        """El aviso no puede saltar con patrones corrientes, o se
        convierte en ruido que nadie lee."""
        write(self.proj, "salida.log", "x\n")
        dicho = []
        publish(self.job(preview=False, ignore=["*.log", "node_modules/"]),
                Callbacks(log=lambda t, tag="out": dicho.append((t, tag))))
        self.assertFalse([t for t, tag in dicho
                          if tag == "warn" and "comodines" in t], dicho)

    def test_names_with_wildcards_are_excluded_exactly(self):
        """Un archivo llamado «datos[2024].csv» no es un patrón.

        Si su nombre se escribe en el .gitignore sin escapar, git lee
        «[2024]» como «uno de estos caracteres», el archivo NO queda
        excluido y se sube: justo lo contrario de lo que se pidió. Aquí
        se exige que lo excluido sea lo señalado y nada más.
        """
        trampas = ["datos[2024].csv", "informe*.pem", "x?.log"]
        vecinos = ["datos2024.csv", "informeXX.pem", "xy.log"]
        for name in trampas + vecinos:
            write(self.proj, name, "v1\n")
        remote_before = remote_snapshot(self.bare)
        r = publish(self.job(preview=True), Callbacks(
            ask_preview=lambda a, m, d, mode: PreviewChoice(
                skip_added=trampas)))
        self.assertEqual(r.status, "ok", r.message)
        subido = remote_snapshot(self.bare)
        for name in trampas:
            self.assertNotIn(name, subido, f"«{name}» se subió aunque se "
                                           "pidió dejarlo fuera")
        for name in vecinos:
            self.assertIn(name, subido, f"«{name}» no se subió y nadie lo "
                                        "excluyó")
        # Y siguen todos en la carpeta: excluir no es borrar.
        for name in trampas:
            self.assertTrue((self.proj / name).is_file(), name)
        self.assertEqual(sorted(set(subido) - set(remote_before)),
                         sorted(vecinos + [".gitignore"]))

    def test_a_double_star_pattern_also_unpublishes_the_secret(self):
        """El cuadro de ignorar admite patrones de git, y «**/» es de los
        más usados. Si la app los interpreta con sus propias reglas en vez
        de preguntar a git, un `.env` YA publicado no se desrastrea: se
        escribe en el .gitignore, el usuario cree que lo quitó, y sigue
        en GitHub en cada publicación.
        """
        write(self.proj, ".env", "TOKEN=secreto\n")
        write(self.proj, "sub/.env", "OTRO=secreto\n")
        self.assertEqual(publish(self.job(preview=False)).status, "ok")
        self.assertIn(".env", remote_snapshot(self.bare))
        r = publish(self.job(preview=False, ignore=["**/.env"]))
        self.assertEqual(r.status, "ok", r.message)
        subido = remote_snapshot(self.bare)
        self.assertNotIn(".env", subido)
        self.assertNotIn("sub/.env", subido)
        # Excluir no es borrar: los dos siguen en la carpeta.
        self.assertTrue((self.proj / ".env").is_file())
        self.assertTrue((self.proj / "sub" / ".env").is_file())

    def test_other_git_only_patterns_also_unpublish(self):
        for patron, ruta in (("secretos/**", "secretos/clave.txt"),
                             ("[Dd]atos/*.csv", "Datos/tabla.csv")):
            with self.subTest(patron=patron):
                write(self.proj, ruta, "x\n")
                self.assertEqual(publish(self.job(preview=False)).status,
                                 "ok")
                self.assertIn(ruta, remote_snapshot(self.bare))
                r = publish(self.job(preview=False, ignore=[patron]))
                self.assertEqual(r.status, "ok", r.message)
                self.assertNotIn(ruta, remote_snapshot(self.bare))
                self.assertTrue((self.proj / ruta).is_file())

    def test_many_paths_are_all_applied_in_batches(self):
        """Más de 50 rutas: el troceado no puede dejarse ninguna."""
        names = [f"lote/f{i:03d}.txt" for i in range(120)]
        for name in names:
            write(self.proj, name, "v1\n")
        self.assertEqual(publish(self.job(preview=False)).status, "ok")
        for name in names:
            write(self.proj, name, "v2\n")
        keep = names[:75]
        r, _, _, remote_before = self.run_with(
            lambda a, m, d: PreviewChoice(skip_modified=keep))
        self.assertEqual(r.status, "ok", r.message)
        self.assert_delta(remote_before, remote_snapshot(self.bare),
                          modified=names[75:], what="GitHub")

    def test_writing_gitignore_never_loses_your_existing_rules(self):
        original = ("# mis reglas\n*.log\nnode_modules/\n"
                    "!importante.log\n")
        write(self.proj, ".gitignore", original)
        self.assertEqual(publish(self.job(preview=False)).status, "ok")
        write(self.proj, "claves/.env", "CLAVE=1\n")
        cb = Callbacks(ask_review=lambda f: ("ignore", [x["path"]
                                                        for x in f]),
                       ask_preview=lambda a, m, d, mode: True)
        r = publish(self.job(check=True, ignore=["dist/"]), cb)
        self.assertEqual(r.status, "ok", r.message)
        lines = (self.proj / ".gitignore").read_text("utf-8").splitlines()
        for kept in original.splitlines():
            self.assertIn(kept, lines, "se perdió una regla tuya")
        self.assertIn("dist/", lines)
        self.assertIn("/claves/.env", lines)

    def test_security_review_cancel_changes_nothing(self):
        write(self.proj, ".env", "CLAVE=1\n")
        local_before = snapshot(self.proj)
        remote_before = remote_snapshot(self.bare)
        cb = Callbacks(ask_review=lambda findings: ("cancel", []))
        r = publish(self.job(check=True), cb)
        self.assertEqual(r.status, "cancelled")
        self.assert_untouched(local_before, snapshot(self.proj))
        self.assert_untouched(remote_before, remote_snapshot(self.bare),
                              "GitHub")

    def test_excluding_a_secret_never_deletes_it_from_disk(self):
        write(self.proj, "claves/.env", "CLAVE=SECRETA\n")
        local_before = snapshot(self.proj)
        remote_before = remote_snapshot(self.bare)
        cb = Callbacks(ask_review=lambda f: ("ignore", [x["path"]
                                                        for x in f]),
                       ask_preview=lambda a, m, d, mode: True)
        r = publish(self.job(check=True), cb)
        self.assertEqual(r.status, "ok", r.message)
        self.assert_delta(local_before, snapshot(self.proj),
                          added=[".gitignore"])
        self.assertEqual((self.proj / "claves" / ".env").read_text("utf-8"),
                         "CLAVE=SECRETA\n")
        self.assert_delta(remote_before, remote_snapshot(self.bare),
                          added=[".gitignore"], what="GitHub")

    def test_excluding_an_already_published_secret_keeps_it_on_disk(self):
        """El caso grave: el secreto ya se subió sin querer. Al excluirlo
        debe salir de GitHub y SEGUIR en el disco, intacto."""
        write(self.proj, "claves/.env", "CLAVE=SECRETA\n")
        self.assertEqual(publish(self.job(preview=False)).status, "ok")
        self.assertIn("claves/.env", remote_snapshot(self.bare))
        local_before = snapshot(self.proj)
        remote_before = remote_snapshot(self.bare)
        cb = Callbacks(ask_review=lambda f: ("ignore", [x["path"]
                                                        for x in f]),
                       ask_preview=lambda a, m, d, mode: True)
        r = publish(self.job(check=True), cb)
        self.assertEqual(r.status, "ok", r.message)
        # En el disco: solo aparece el .gitignore; el secreto sigue igual.
        self.assert_delta(local_before, snapshot(self.proj),
                          added=[".gitignore"])
        self.assertEqual((self.proj / "claves" / ".env").read_bytes(),
                         b"CLAVE=SECRETA\n")
        # En GitHub: solo desaparece el secreto.
        self.assert_delta(remote_before, remote_snapshot(self.bare),
                          added=[".gitignore"], deleted=["claves/.env"],
                          what="GitHub")

    def test_auto_mode_blocked_by_findings_changes_nothing(self):
        write(self.proj, ".env", "CLAVE=1\n")
        write(self.proj, "nuevo.txt", "n\n")
        local_before = snapshot(self.proj)
        remote_before = remote_snapshot(self.bare)
        r = publish(self.job(check=True, auto=True))
        self.assertEqual(r.status, "blocked")
        self.assert_untouched(local_before, snapshot(self.proj))
        self.assert_untouched(remote_before, remote_snapshot(self.bare),
                              "GitHub")

    def test_kept_file_present_on_disk_is_never_overwritten(self):
        write(self.proj, "secreto.env", "DE-GITHUB\n")
        self.assertEqual(publish(self.job(preview=False)).status, "ok")
        otro = Path(self.tmp) / "otro-pc"
        for name in ("a.txt", "b.txt", "c.txt"):
            write(otro, name, f"{name}-v1\n")
        write(otro, "sub/d.txt", "d-v1\n")
        write(otro, "secreto.env", "MIO-LOCAL\n")
        local_before = snapshot(otro)
        remote_before = remote_snapshot(self.bare)
        cb = Callbacks(ask_preview=lambda a, m, d, mode: PreviewChoice(
            keep_deleted=d, restore_to_folder=True))
        r = publish(PublishJob(folder=str(otro), url=self.bare, user="T",
                               email="t@x.y", check=False,
                               ignore=["secreto.env"]), cb)
        self.assertEqual(r.status, "ok", r.message)
        self.assert_delta(local_before, snapshot(otro), added=[".gitignore"])
        self.assertEqual((otro / "secreto.env").read_text("utf-8"),
                         "MIO-LOCAL\n")
        self.assert_delta(remote_before, remote_snapshot(self.bare),
                          added=[".gitignore"], what="GitHub")


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class DestructiveOperationsAreAlwaysRecoverable(SafetyCase):
    """Invariante 4: lo que se pierde a propósito queda a salvo."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bare = make_bare(self.tmp)
        self.origen = Path(self.tmp) / "origen"
        write(self.origen, "a.txt", "A-v1\n")
        write(self.origen, "b.txt", "B-v1\n")
        self.assertEqual(publish(PublishJob(
            folder=str(self.origen), url=self.bare, user="T", email="t@x.y",
            preview=False, check=False)).status, "ok")
        self.pc2 = Path(self.tmp) / "pc2"
        clone(self.bare, self.pc2)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def clone_snapshot(self):
        """Foto de lo que hay hoy en GitHub, clonándolo aparte."""
        return snapshot(clone(self.bare,
                              Path(tempfile.mkdtemp(dir=self.tmp)) / "espejo"))

    def publish_origen(self):
        self.assertEqual(publish(PublishJob(
            folder=str(self.origen), url=self.bare, user="T", email="t@x.y",
            preview=False, check=False)).status, "ok")

    def test_sync_mirror_keeps_everything_recoverable(self):
        write(self.origen, "a.txt", "A-v2\n")
        self.publish_origen()
        write(self.pc2, "a.txt", "MI-VERSION\n")
        write(self.pc2, "solo-mio.txt", "IMPORTANTE\n")
        write(self.pc2, "notas/idea.md", "# idea\n")
        before = snapshot(self.pc2)
        r = sync.apply(self.pc2, self.bare, mode="mirror",
                       identity=("t", "t@x"))
        self.assertEqual(r.status, "ok", r.message)
        after = snapshot(self.pc2)
        _, modified, deleted = delta(before, after)
        self.assert_recoverable(self.pc2, r.backup_branch,
                                list(deleted) + list(modified), before)
        # Y el resultado tiene que ser exactamente lo que hay en GitHub.
        self.assertEqual(after, self.clone_snapshot())

    def test_sync_merge_keeps_overwritten_versions_recoverable(self):
        write(self.origen, "a.txt", "A-DE-GITHUB\n")
        self.publish_origen()
        write(self.pc2, "a.txt", "MI-VERSION\n")
        write(self.pc2, "solo-mio.txt", "IMPORTANTE\n")
        before = snapshot(self.pc2)
        r = sync.apply(self.pc2, self.bare, mode="merge",
                       identity=("t", "t@x"))
        self.assertEqual(r.status, "ok", r.message)
        after = snapshot(self.pc2)
        self.assertEqual(after["solo-mio.txt"], before["solo-mio.txt"])
        _, modified, deleted = delta(before, after)
        self.assertEqual(deleted, [])
        self.assert_recoverable(self.pc2, r.backup_branch, modified, before)

    def test_sync_conflict_aborts_without_touching_anything(self):
        (self.origen / "b.txt").unlink()
        self.publish_origen()
        write(self.pc2, "b.txt", "editado por mí\n")
        before = snapshot(self.pc2)
        r = sync.apply(self.pc2, self.bare, mode="merge",
                       identity=("t", "t@x"))
        self.assertEqual(r.status, "error")
        self.assert_untouched(before, snapshot(self.pc2))
        # Y el repositorio no puede quedarse a medio combinar.
        merging = subprocess.run(["git", "rev-parse", "-q", "--verify",
                                  "MERGE_HEAD"], cwd=self.pc2,
                                 capture_output=True)
        self.assertNotEqual(merging.returncode, 0,
                            "el repositorio quedó a medio combinar")
        self.assertFalse(any("U" in line[:2] for line in
                             git(self.pc2, "status", "--porcelain")
                             .splitlines()), "quedaron conflictos sin marcar")
        # Y se puede seguir trabajando con normalidad.
        r2 = sync.apply(self.pc2, self.bare, mode="mirror",
                        identity=("t", "t@x"))
        self.assertEqual(r2.status, "ok", r2.message)

    def test_restore_version_keeps_everything_recoverable(self):
        write(self.origen, "a.txt", "A-v2\n")
        write(self.origen, "nuevo.txt", "N\n")
        self.publish_origen()
        before = snapshot(self.origen)
        first = history.list_versions(self.origen)[-1]
        r = history.restore_version(self.origen, first.sha,
                                    identity=("t", "t@x"))
        self.assertEqual(r.status, "ok", r.message)
        after = snapshot(self.origen)
        _, modified, deleted = delta(before, after)
        self.assertIn("nuevo.txt", deleted)
        self.assert_recoverable(self.origen, r.backup_branch,
                                list(deleted) + list(modified), before)

    def test_uncommitted_work_is_saved_before_restoring(self):
        write(self.origen, "borrador.txt", "TRABAJO SIN GUARDAR\n")
        before = snapshot(self.origen)
        first = history.list_versions(self.origen)[-1]
        r = history.restore_version(self.origen, first.sha,
                                    identity=("t", "t@x"))
        self.assertEqual(r.status, "ok", r.message)
        _, modified, deleted = delta(before, snapshot(self.origen))
        self.assert_recoverable(self.origen, r.backup_branch,
                                list(deleted) + list(modified), before)

    def test_backup_branch_is_never_reused(self):
        g = Git()
        first = g.backup(self.pc2, identity=("t", "t@x"))
        write(self.pc2, "x.txt", "X\n")
        second = g.backup(self.pc2, identity=("t", "t@x"))
        self.assertTrue(first and second and first != second)
        self.assertIn(first, g.local_branches(self.pc2))
        self.assertIn(second, g.local_branches(self.pc2))

    def test_publishing_never_destroys_another_local_branch(self):
        """«La carpeta manda» renombra la rama actual al destino con
        `branch -M`, que es --force: si ya existía una rama local con ese
        nombre y sus propios commits, se borraba y solo quedaba en el
        reflog (que un `git gc` se lleva). Tiene que quedar a salvo.
        """
        proj = Path(self.tmp) / "conramas"
        bare = make_bare(self.tmp, "remoto-ramas")
        write(proj, "a.txt", "A\n")
        git(proj, "init", "-q", "-b", "dev", ".")
        git(proj, "add", "-A")
        git(proj, "-c", "user.name=t", "-c", "user.email=t@x",
            "commit", "-qm", "dev")
        git(proj, "checkout", "-q", "-b", "main")
        write(proj, "solo_main.txt", "SOLO EN MAIN\n")
        git(proj, "add", "-A")
        git(proj, "-c", "user.name=t", "-c", "user.email=t@x",
            "commit", "-qm", "unico-de-main")
        git(proj, "checkout", "-q", "dev")
        sha = subprocess.run(["git", "rev-parse", "main"], cwd=proj,
                             capture_output=True, text=True).stdout.strip()

        r = publish(PublishJob(folder=str(proj), url=bare, user="T",
                               email="t@x.y", preview=False, check=False,
                               branch="main"))
        self.assertEqual(r.status, "ok", r.message)
        g = Git()
        apartadas = [b for b in g.local_branches(proj) if "apartada" in b]
        self.assertEqual(len(apartadas), 1, g.local_branches(proj))
        # Y apunta exactamente al commit de antes: recuperable byte a byte.
        self.assertEqual(
            subprocess.run(["git", "rev-parse", apartadas[0]], cwd=proj,
                           capture_output=True, text=True).stdout.strip(),
            sha)
        self.assertEqual(
            subprocess.run(["git", "show", apartadas[0] + ":solo_main.txt"],
                           cwd=proj, capture_output=True,
                           text=True).stdout, "SOLO EN MAIN\n")


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class PublishingSeveralProjectsAtOnce(SafetyCase):
    """Varios proyectos a la vez, cada uno con su casilla del TXT: en cada
    carpeta solo aparece lo que se pidió (su TXT y el .gitignore que lo
    excluye; en agregar/editar, también lo que solo estaba en GitHub); en
    cada repositorio, exactamente su carpeta; y nada de un proyecto acaba
    en otro, ni en su repositorio ni en su TXT."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.proyectos, self.bares = [], []
        for i in range(3):
            proj = Path(self.tmp) / f"p{i}"
            write(proj, "comun.txt", f"proyecto {i}\n")
            write(proj, f"solo-{i}/x.py", f"print({i})\n")
            self.proyectos.append(proj)
            self.bares.append(make_bare(self.tmp, f"r{i}"))
        # p0 guarda un secreto ignorado; p2 publica en agregar/editar
        # sobre un GitHub que ya tiene algo que su carpeta no tiene.
        write(self.proyectos[0], ".gitignore", "*.env\n")
        write(self.proyectos[0], "claves.env", "TOKEN=1\n")
        semilla = Path(self.tmp) / "semilla"
        write(semilla, "de-github.md", "ya estaba\n")
        r = publish(PublishJob(folder=str(semilla), url=self.bares[2],
                               user="T", email="t@x.y", preview=False,
                               check=False))
        self.assertEqual(r.status, "ok", r.message)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_each_folder_and_each_repo_get_exactly_their_own(self):
        antes = [snapshot(p) for p in self.proyectos]
        jobs = [PublishJob(folder=str(p), url=b, user="T", email="t@x.y",
                           preview=False, check=False, name=f"p{i}",
                           consolidate=(i != 1),
                           mode="addedit" if i == 2 else "mirror")
                for i, (p, b) in enumerate(zip(self.proyectos, self.bares))]
        resultados = publish_many(jobs, workers=3)
        self.assertEqual([r.status for r in resultados], ["ok"] * 3,
                         [r.message for r in resultados])
        self.assert_delta(antes[0], snapshot(self.proyectos[0]),
                          added=["p0.txt"], modified=[".gitignore"],
                          what="p0")
        self.assert_untouched(antes[1], snapshot(self.proyectos[1]),
                              what="p1")
        self.assert_delta(antes[2], snapshot(self.proyectos[2]),
                          added=["p2.txt", ".gitignore", "de-github.md"],
                          what="p2")
        self.assertEqual(sorted(remote_snapshot(self.bares[0])),
                         [".gitignore", "comun.txt", "solo-0/x.py"])
        self.assertEqual(sorted(remote_snapshot(self.bares[1])),
                         ["comun.txt", "solo-1/x.py"])
        self.assertEqual(sorted(remote_snapshot(self.bares[2])),
                         [".gitignore", "comun.txt", "de-github.md",
                          "solo-2/x.py"])
        for i in (0, 2):
            texto = (self.proyectos[i] / f"p{i}.txt").read_text(
                encoding="utf-8")
            self.assertIn(f"solo-{i}/x.py", texto)
            for j in range(3):
                if j != i:
                    self.assertNotIn(f"solo-{j}/", texto)
        self.assertNotIn("TOKEN=1", (self.proyectos[0] / "p0.txt")
                         .read_text(encoding="utf-8"))


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class TheProjectTxtNeverReachesGitHub(SafetyCase):
    """El TXT que une todo el proyecto lleva TODO el código junto: es el
    archivo que menos debe subirse por error. Aquí se defiende que:

      · se crea donde se dijo y en ningún otro sitio;
      · NO llega a GitHub, ni la primera vez ni en las siguientes;
      · si en una publicación anterior se subió, deja de subirse, pero
        sigue en la carpeta;
      · si falla al generarse, la publicación sigue y no sube restos;
      · no se cuela a sí mismo dentro de su propio contenido.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bare = make_bare(self.tmp)
        self.proj = Path(self.tmp) / "proj"
        write(self.proj, "a.txt", "A\n")
        write(self.proj, "src/app.js", "console.log(1)\n")
        self.txt = self.proj / "proj.txt"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def job(self, **kw):
        base = dict(folder=str(self.proj), url=self.bare, user="T",
                    email="t@x.y", preview=False, check=False,
                    consolidate=True)
        base.update(kw)
        return PublishJob(**base)

    def test_the_txt_is_created_but_never_published(self):
        before = snapshot(self.proj)
        r = publish(self.job())
        self.assertEqual(r.status, "ok", r.message)
        # En la carpeta aparecen solo el TXT y el .gitignore que lo excluye.
        self.assert_delta(before, snapshot(self.proj),
                          added=["proj.txt", ".gitignore"])
        self.assertTrue(self.txt.is_file())
        self.assertEqual(r.txt_path, str(self.txt))
        self.assertEqual(r.txt_stats["files"], 2)
        # En GitHub está el proyecto… y nada más.
        subido = sorted(remote_snapshot(self.bare))
        self.assertEqual(subido, [".gitignore", "a.txt", "src/app.js"])
        self.assertNotIn("proj.txt", subido)
        # La exclusión queda escrita: protege también a futuras subidas.
        self.assertIn("/proj.txt",
                      (self.proj / ".gitignore").read_text(encoding="utf-8"))

    def test_publishing_again_still_keeps_it_out(self):
        self.assertEqual(publish(self.job()).status, "ok")
        write(self.proj, "b.txt", "B\n")
        self.assertEqual(publish(self.job()).status, "ok")
        subido = sorted(remote_snapshot(self.bare))
        self.assertEqual(subido, [".gitignore", "a.txt", "b.txt",
                                  "src/app.js"])

    def test_a_txt_of_ours_published_by_mistake_stops_being_published(self):
        """El caso grave: un TXT NUESTRO ya se subió (se publicó sin la
        casilla, o con otra herramienta). Al publicar con la casilla debe
        desaparecer de GitHub y SEGUIR en la carpeta."""
        self.txt.write_text("MODULO RAIZ: proj\nGENERADO: antes\n",
                            encoding="utf-8")
        self.assertEqual(publish(self.job(consolidate=False)).status, "ok")
        self.assertIn("proj.txt", remote_snapshot(self.bare))
        r = publish(self.job())
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(r.txt_path, str(self.txt))    # se rehace el mismo
        self.assertNotIn("proj.txt", remote_snapshot(self.bare))
        self.assertTrue(self.txt.is_file(), "el TXT no puede desaparecer "
                                            "de la carpeta")

    def test_a_published_file_of_yours_is_never_unpublished(self):
        """El mismo nombre, pero el archivo es del usuario y estaba
        publicado. Desrastrearlo lo borraría de GitHub sin que nadie lo
        pidiera: tiene que seguir allí."""
        self.txt.write_text("MIS APUNTES\n", encoding="utf-8")
        self.assertEqual(publish(self.job(consolidate=False)).status, "ok")
        antes = remote_snapshot(self.bare)
        self.assertIn("proj.txt", antes)
        r = publish(self.job())
        self.assertEqual(r.status, "ok", r.message)
        despues = remote_snapshot(self.bare)
        self.assertEqual(antes["proj.txt"], despues["proj.txt"],
                         "el archivo del usuario cambió en GitHub")
        self.assertEqual(self.txt.read_text(encoding="utf-8"), "MIS APUNTES\n")
        self.assertNotIn("proj-unificado.txt", despues)

    def test_the_txt_does_not_contain_itself(self):
        self.assertEqual(publish(self.job()).status, "ok")
        contenido = self.txt.read_text(encoding="utf-8")
        self.assertIn("MODULO: a.txt", contenido)
        self.assertIn("MODULO: src/app.js", contenido)
        self.assertNotIn("MODULO: proj.txt", contenido)

    def test_a_txt_outside_the_project_needs_no_exclusion(self):
        fuera = Path(self.tmp) / "fuera" / "todo.txt"
        before = snapshot(self.proj)
        r = publish(self.job(consolidate_path=str(fuera)))
        self.assertEqual(r.status, "ok", r.message)
        self.assertTrue(fuera.is_file())
        # Nada que excluir: no se toca la carpeta, ni para el .gitignore.
        self.assert_untouched(before, snapshot(self.proj))
        self.assertFalse((self.proj / ".gitignore").exists())
        self.assertNotIn("todo.txt", remote_snapshot(self.bare))

    def test_without_the_box_no_txt_is_created_at_all(self):
        before = snapshot(self.proj)
        r = publish(self.job(consolidate=False))
        self.assertEqual(r.status, "ok", r.message)
        self.assert_untouched(before, snapshot(self.proj))
        self.assertFalse(self.txt.exists())
        self.assertEqual((r.txt_path, r.txt_stats, r.txt_error), ("", {}, ""))

    def test_a_failing_txt_neither_stops_nor_publishes_leftovers(self):
        """Si generar el TXT falla, el proyecto se publica igual y lo que
        haya quedado a medias NO se sube: el patrón se pone antes."""
        import gitpush_studio.core.publish as pub

        def explota(project_dir, output_file, *a, **kw):
            Path(output_file).write_text("A MEDIO ESCRIBIR\n",
                                         encoding="utf-8")
            raise OSError("disco lleno")

        original = pub.consolidate_project
        pub.consolidate_project = explota
        try:
            r = publish(self.job())
        finally:
            pub.consolidate_project = original
        self.assertEqual(r.status, "ok", r.message)
        self.assertIn("disco lleno", r.txt_error)
        self.assertEqual(r.txt_path, "")
        self.assertNotIn("proj.txt", remote_snapshot(self.bare))
        self.assertTrue(self.txt.is_file())      # el resto se queda en casa

    def test_it_never_overwrites_a_file_of_yours_with_the_same_name(self):
        """El peor caso del nombre por defecto: la carpeta «proj» ya tiene
        su propio «proj.txt» con el trabajo del usuario. `open(…, "wb")`
        no pregunta: lo machacaría, y además el .gitignore lo sacaría de
        GitHub, así que se perderían las dos copias de un solo clic.
        """
        suyo = self.proj / "proj.txt"
        suyo.write_text("APUNTES DE AÑOS\n", encoding="utf-8")
        dicho = []
        r = publish(self.job(), Callbacks(
            log=lambda t, tag="out": dicho.append(t)))
        self.assertEqual(r.status, "ok", r.message)
        # Y se DICE, que si no el usuario no encuentra su TXT.
        self.assertTrue(any("no es mío" in t and "proj-unificado" in t
                            for t in dicho), dicho)
        # Su archivo, intacto…
        self.assertEqual(suyo.read_text(encoding="utf-8"), "APUNTES DE AÑOS\n")
        # …y seguido publicándose: nadie pidió excluirlo.
        self.assertIn("proj.txt", remote_snapshot(self.bare))
        # El TXT generado se aparta y es ese el que queda fuera.
        self.assertEqual(r.txt_path, str(self.proj / "proj-unificado.txt"))
        self.assertTrue((self.proj / "proj-unificado.txt").is_file())
        self.assertNotIn("proj-unificado.txt", remote_snapshot(self.bare))

    def test_with_no_free_name_it_says_so_and_publishes_anyway(self):
        """Caso extremo: todos los nombres candidatos están ocupados por
        archivos del usuario. No se pisa ninguno, se explica por qué no
        hay TXT, y el proyecto se publica igual: el TXT es un extra."""
        self.txt.write_text("MIO\n", encoding="utf-8")
        for n in range(1, 100):
            cola = "" if n == 1 else "-%d" % n
            (self.proj / ("proj-unificado%s.txt" % cola)).write_text(
                "TAMBIEN MIO\n", encoding="utf-8")
        antes = snapshot(self.proj)
        r = publish(self.job())
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(r.txt_path, "")
        self.assertIn("nombre libre", r.txt_error)
        # Ni un archivo del usuario cambió; solo apareció el .gitignore
        # que el propio proyecto necesita… o ni eso.
        _, modificados, borrados = delta(antes, snapshot(self.proj))
        self.assertEqual((modificados, borrados), ([], []))

    def test_regenerating_its_own_txt_does_overwrite_it(self):
        """Un TXT que sí generamos nosotros se rehace, no se acumulan
        copias «-unificado-2», «-unificado-3»…"""
        self.assertEqual(publish(self.job()).status, "ok")
        primero = self.txt.read_text(encoding="utf-8")
        write(self.proj, "c.txt", "C\n")
        r = publish(self.job())
        self.assertEqual(r.txt_path, str(self.txt))
        self.assertNotEqual(self.txt.read_text(encoding="utf-8"), primero)
        self.assertEqual(sorted(p.name for p in self.proj.glob("*.txt")),
                         ["a.txt", "c.txt", "proj.txt"])

    @unittest.skipUnless(hasattr(os, "symlink"), "sin enlaces simbólicos")
    def test_a_link_that_points_inside_the_project_is_still_inside(self):
        """Un destino que parece estar fuera pero, siguiendo el enlace,
        cae DENTRO del proyecto. Si se mira la ruta sin resolver, no se
        añade al .gitignore… y git lo encuentra dentro y lo sube.
        """
        (self.proj / "salida").mkdir()
        enlace = Path(self.tmp) / "dumps"
        try:
            os.symlink(str(self.proj / "salida"), str(enlace))
        except (OSError, NotImplementedError):
            self.skipTest("este sistema no permite crear enlaces")
        r = publish(self.job(consolidate_path=str(enlace)))
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(r.txt_path, str(self.proj / "salida" / "proj.txt"))
        subido = remote_snapshot(self.bare)
        self.assertNotIn("salida/proj.txt", subido)
        self.assertIn("/salida/proj.txt",
                      (self.proj / ".gitignore").read_text(encoding="utf-8"))

    def test_the_txt_respects_what_the_user_excluded(self):
        """Lo que está en el cuadro de ignorar no se sube… y tampoco se
        copia dentro del TXT."""
        write(self.proj, "secreto/clave.env", "TOKEN=abc\n")
        r = publish(self.job(ignore=["secreto/"]))
        self.assertEqual(r.status, "ok", r.message)
        contenido = self.txt.read_text(encoding="utf-8")
        self.assertNotIn("TOKEN=abc", contenido)
        self.assertNotIn("secreto/clave.env", remote_snapshot(self.bare))
        self.assertTrue((self.proj / "secreto" / "clave.env").is_file())

    def test_the_txt_also_respects_the_patterns_only_git_understands(self):
        """El TXT lleva TODO el código junto, así que es el archivo donde
        más duele colar un secreto. Si el filtro del TXT no entiende las
        reglas de git («**/», clases, «!»), el usuario escribe «**/.env»,
        ve que no se sube… y lo encuentra dentro del TXT.
        """
        write(self.proj, ".env", "RAIZ=secreto1\n")
        write(self.proj, "sub/.env", "HONDO=secreto2\n")
        write(self.proj, "Datos/tabla.csv", "fila\n")
        r = publish(self.job(ignore=["**/.env", "[Dd]atos/*.csv"]))
        self.assertEqual(r.status, "ok", r.message)
        contenido = self.txt.read_text(encoding="utf-8")
        for secreto in ("RAIZ=secreto1", "HONDO=secreto2"):
            self.assertNotIn(secreto, contenido)
        self.assertNotIn("MODULO: .env", contenido)
        self.assertNotIn("MODULO: sub/.env", contenido)
        self.assertNotIn("MODULO: Datos/tabla.csv", contenido)
        # Y tampoco a GitHub, claro.
        subido = remote_snapshot(self.bare)
        for ruta in (".env", "sub/.env", "Datos/tabla.csv"):
            self.assertNotIn(ruta, subido)
            self.assertTrue((self.proj / ruta).is_file(), ruta)


if __name__ == "__main__":
    unittest.main()
