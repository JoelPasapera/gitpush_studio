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
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.gitenv import HAVE_GIT, clone, git, init_bare

from gitpush_studio.core import history, sync
from gitpush_studio.core.git import Git
from gitpush_studio.core.publish import (Callbacks, PreviewChoice, PublishJob,
                                         publish)


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

    def test_failed_push_leaves_the_folder_intact(self):
        before = snapshot(self.proj)
        r = publish(self.job(url=str(Path(self.tmp) / "no-existe.git")))
        self.assertEqual(r.status, "error")
        self.assert_untouched(before, snapshot(self.proj))

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


if __name__ == "__main__":
    unittest.main()
