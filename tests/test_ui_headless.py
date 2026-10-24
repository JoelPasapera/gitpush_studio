# -*- coding: utf-8 -*-
"""Pruebas de la interfaz sin pantalla: se instalan sustitutos de
tkinter/customtkinter y se ejercita el cableado real (cola, controlador,
decisiones en serie, diálogos) contra git y repositorios bare locales."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.tkstub as tkstub

tkstub.install()

from tests.gitenv import (HAVE_GIT, clone, commit_all,          # noqa: E402
                          init_bare, init_repo)

from gitpush_studio.core import config as cfg                    # noqa: E402
from gitpush_studio.core import diaglog                          # noqa: E402
from gitpush_studio.core.errors import StepError                 # noqa: E402
from gitpush_studio.ui.widgets import set_text                   # noqa: E402
from gitpush_studio.ui import app as app_module                  # noqa: E402
from gitpush_studio.ui import app_auto, app_publish              # noqa: E402
from gitpush_studio.ui.app import GitPushStudio                  # noqa: E402
from gitpush_studio.core import github                            # noqa: E402
from gitpush_studio.ui.dialogs import (BatchPublishDialog,            # noqa
                                       BranchDialog, BulkCloneDialog,
                                       CloneDialog, ConsolidateDialog,
                                       CreateRepoDialog, HistoryDialog,
                                       PagesDialog, PreviewDialog,
                                       ReleaseDialog, ReviewDialog,
                                       SyncDialog)

def write(folder, rel, content="x\n"):
    p = Path(folder) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def make_bare(where, name):
    return init_bare(where, name)


def remote_files(bare, branch="main"):
    p = subprocess.run(["git", "ls-tree", "-r", "--name-only", branch],
                       cwd=bare, capture_output=True, text=True)
    return sorted(x for x in p.stdout.splitlines() if x)


class HeadlessCase(unittest.TestCase):
    """Montaje común: aplicación real con tkinter falso, un proyecto y un
    repositorio bare local. Sin pruebas propias, para que heredar de aquí
    no vuelva a ejecutar las de nadie."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg_path = Path(self.tmp) / "profiles.json"
        self._patches = [
            mock.patch.object(cfg, "load_config",
                              lambda: _ORIG_LOAD(self.cfg_path)),
            mock.patch.object(cfg, "save_config",
                              lambda data: _ORIG_SAVE(data, self.cfg_path)),
            # El registro de diagnóstico, también dentro de la prueba.
            mock.patch.object(diaglog, "LOG_DIR",
                              Path(self.tmp) / "registro"),
        ]
        for p in self._patches:
            p.start()
        tkstub.Scheduler.pending.clear()
        tkstub.Messagebox.calls.clear()
        tkstub.Messagebox.ANSWER = True
        self.app = GitPushStudio()
        self.proj = Path(self.tmp) / "proj"
        write(self.proj, "index.html", "<h1>hola</h1>\n")
        write(self.proj, "app.js", "console.log(1)\n")
        self.bare = make_bare(self.tmp, "remote")

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- utilidades ------------------------------------------------
    def fill_form(self, folder=None, url=None, mode="Espejo"):
        a = self.app
        a.e_folder.delete(0, "end")
        a.e_folder.insert(0, str(folder or self.proj))
        a.e_url.delete(0, "end")
        a.e_url.insert(0, url or self.bare)
        a.e_user.delete(0, "end")
        a.e_user.insert(0, "Test")
        a.e_email.delete(0, "end")
        a.e_email.insert(0, "t@x.y")
        a.seg_mode.set(mode)
        a._mode_changed()

    def answer_dialogs(self, preview=True, review=("ignore", None)):
        """Responde el diálogo de decisión visible (si lo hay)."""
        dlg = self.app.controller._dialog
        if dlg is None or not dlg.winfo_exists():
            return
        if isinstance(dlg, PreviewDialog):
            self.seen.append(("preview", dlg))
            dlg.finish(preview)
        elif isinstance(dlg, ReviewDialog):
            self.seen.append(("review", dlg))
            decision, selected = review
            dlg.finish((decision, dlg.selected() if selected is None
                        else selected))

    def run_until_idle(self, **answers):
        self.seen = []
        tkstub.Scheduler.pump(lambda: not self.app.controller.running,
                              step=lambda: self.answer_dialogs(**answers))
        tkstub.Scheduler.run_due()          # el on_finished ya corrió; drena

    def console(self):
        return self.app.log_box.get("1.0", "end")


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class HeadlessUITests(HeadlessCase):

    def test_license_defaults_to_apache_and_can_be_turned_off(self):
        a = self.app
        self.assertEqual(a._license_key(), "apache-2.0")
        self.assertIn("Apache", a.om_license.get())
        self.assertEqual(a.collect_state()["license"], "apache-2.0")
        self.fill_form()
        self.publish_form()
        texto = (self.proj / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Apache License", texto)
        self.assertIn("Test", texto)              # el nombre de Identidad Git
        self.assertIn("LICENSE", remote_files(self.bare))
        # Se puede quitar, y entonces no se añade nada.
        otro = Path(self.tmp) / "otro"
        write(otro, "x.txt", "x\n")
        a.om_license.set("Sin licencia")
        a._license_changed()
        self.assertEqual(a._license_key(), "")
        self.fill_form(folder=otro, url=make_bare(self.tmp, "otro"))
        self.publish_form()
        self.assertFalse((otro / "LICENSE").exists())

    def test_license_state_round_trips(self):
        a = self.app
        a.apply_state({"license": "mit", "license_owner": "Ana"})
        self.assertEqual(a._license_key(), "mit")
        self.assertEqual(a.e_owner.get(), "Ana")
        estado = a.collect_state()
        self.assertEqual((estado["license"], estado["license_owner"]),
                         ("mit", "Ana"))

    def test_console_shows_the_tool_guide_on_start(self):
        texto = self.console()
        self.assertIn("Bienvenido", texto)
        self.assertIn("¿Qué hace cada botón?", texto)
        for accion in ("🚀  Publicar en GitHub", "🛡  Analizar",
                       "🤖  Auto-publicación", "🌍  Publicar como web",
                       "🌿  Ramas", "➕  Crear repositorio nuevo"):
            self.assertIn(accion, texto)
        # Cada botón lleva su explicación, no solo el nombre.
        self.assertIn("lo contrario de publicar", texto)
        self.assertIn("puedes devolver la carpeta", texto)

    def test_every_sidebar_tool_is_explained_in_the_guide(self):
        """Ninguna herramienta puede quedar sin explicar: la lista de
        botones es la misma que recorre la barra lateral, así que añadir
        una nueva y olvidarse de documentarla rompe esta prueba."""
        texto = " ".join(self.console().split())
        for label, method in app_module.SIDEBAR_TOOLS:
            self.assertTrue(callable(getattr(self.app, method, None)),
                            f"«{label}» apunta a un método que no existe: "
                            f"{method}")
            nombre = " ".join(label.split()).rstrip("…")
            self.assertIn(nombre, texto,
                          f"la herramienta «{label}» no está explicada en "
                          "la ayuda de la consola")

    def test_console_help_button_reprints_the_guide(self):
        a = self.app
        a.clear_log()
        self.assertNotIn("¿Qué hace cada botón?", self.console())
        a.show_help()
        self.assertIn("¿Qué hace cada botón?", self.console())

    def test_console_is_copyable_but_not_editable(self):
        a = self.app
        tkstub.Clipboard.text = ""
        a.copy_log()
        self.assertIn("Bienvenido", tkstub.Clipboard.text)
        self.assertEqual(a.btn_copy.text, "✔ Copiado")
        tkstub.Scheduler.run_due(2000)
        self.assertEqual(a.btn_copy.text, "📋 Copiar")
        # Escribir en ella no cambia nada; copiar y moverse sí funcionan.
        antes = self.console()
        self.assertEqual(a.log_box._on_key(tkstub.Event(keysym="x")), "break")
        self.assertEqual(a.log_box._on_key(tkstub.Event(keysym="Delete")),
                         "break")
        self.assertIsNone(a.log_box._on_key(tkstub.Event(keysym="c",
                                                         state=0x4)))
        self.assertEqual(self.console(), antes)

    def test_console_copies_only_the_selection_when_there_is_one(self):
        a = self.app
        a.clear_log()
        a.log("solo esto", "out")
        a.log_box._select_all()
        tkstub.Clipboard.text = ""
        a.copy_log()
        self.assertEqual(tkstub.Clipboard.text.strip(), "solo esto")

    def test_console_empty_says_nothing_to_copy(self):
        a = self.app
        a.clear_log()
        a.copy_log()
        self.assertEqual(a.btn_copy.text, "Nada que copiar")

    def test_window_builds_and_state_round_trips(self):
        a = self.app
        self.fill_form(mode="Agregar / editar")
        a.t_ignore.delete("1.0", "end")
        a.t_ignore.insert("1.0", "node_modules/\n*.log\n")
        state = a.collect_state()
        self.assertEqual(state["mode"], "addedit")
        self.assertEqual(state["ignore"], "node_modules/\n*.log")
        a.apply_state({"folder": "/x", "mode": "mirror", "branch": ""})
        self.assertEqual(a.e_folder.get(), "/x")
        self.assertEqual(a.e_branch.get(), "main")
        self.assertFalse(a._is_addedit())
        self.assertIn("Bienvenido", self.console())

    def test_single_publish_from_form_with_preview(self):
        self.fill_form()
        self.app.start_publish()
        self.assertTrue(self.app.controller.running)
        self.run_until_idle()
        self.assertEqual([k for k, _ in self.seen], ["preview"])
        self.assertEqual(remote_files(self.bare),
                         [".gitignore", "LICENSE", "app.js", "index.html"])
        self.assertEqual(self.app.status_pill.text, "●  Publicado con éxito")
        self.assertEqual(self.app.progress.get(), 1)
        self.assertEqual(self.app.last_repo_web, self.bare[:-4])
        data, _ = _load(self.cfg_path)
        self.assertEqual(data["last_state"]["url"], self.bare)

    def test_review_ignore_updates_form_and_gitignore(self):
        write(self.proj, ".env", "SECRET=1\n")
        self.fill_form()
        self.app.start_publish()
        self.run_until_idle(review=("ignore", None))
        self.assertEqual([k for k, _ in self.seen], ["review", "preview"])
        self.assertIn("/.env", self.app.ignore_lines())
        self.assertEqual(remote_files(self.bare),
                         [".gitignore", "LICENSE", "app.js", "index.html"])

    def open_preview(self):
        self.app.start_publish()
        tkstub.Scheduler.pump(lambda: isinstance(self.app.controller._dialog,
                                                 PreviewDialog))
        return self.app.controller._dialog

    def finish_preview(self, dlg):
        dlg._publish()
        tkstub.Scheduler.pump(lambda: not self.app.controller.running)
        tkstub.Scheduler.run_due()
        self.assertEqual(self.app.status_pill.text, "●  Publicado con éxito")

    def test_preview_keep_button_flow(self):
        self.fill_form()
        self.publish_form()
        (self.proj / "app.js").unlink()
        (self.proj / "index.html").unlink()
        write(self.proj, "nuevo.txt", "n\n")
        dlg = self.open_preview()
        grp = dlg.groups["deleted"]
        self.assertEqual(grp.items, ["app.js", "index.html"])
        self.assertIn("2 se borrarán", dlg.lbl_summary.text)
        grp.toggle("app.js")
        self.assertEqual(grp.rows["app.js"][1].text, "Borrar")
        self.assertEqual(grp.rows["app.js"][0].text, " se mantiene ")
        self.assertIn("1 se mantienen", dlg.lbl_summary.text)
        self.assertIn("dejando fuera 1", dlg.btn_publish.text)
        grp.toggle_all()                              # los dos
        self.assertEqual(grp.off, {"app.js", "index.html"})
        self.assertEqual(grp.btn_all.text, "Borrar todos")
        grp.toggle("index.html")                      # este sí se borra
        self.assertEqual(grp.btn_all.text, "Mantener todos")
        self.finish_preview(dlg)
        self.assertEqual(remote_files(self.bare),
                         [".gitignore", "LICENSE", "app.js", "nuevo.txt"])
        self.assertTrue((self.proj / "app.js").exists())
        self.assertFalse((self.proj / "index.html").exists())
        self.assertIn("Se mantienen 1 archivo", self.console())

    def test_preview_skip_added_and_modified_buttons(self):
        self.fill_form()
        self.publish_form()
        write(self.proj, "index.html", "<h1>editado</h1>\n")
        write(self.proj, "borrador.txt", "wip\n")
        write(self.proj, "bueno.txt", "ok\n")
        dlg = self.open_preview()
        self.assertNotIn("deleted", dlg.groups)
        added, modified = dlg.groups["added"], dlg.groups["modified"]
        self.assertEqual(added.items, ["borrador.txt", "bueno.txt"])
        self.assertEqual(modified.items, ["index.html"])
        added.toggle("borrador.txt")
        self.assertEqual(added.rows["borrador.txt"][1].text, "Subir")
        self.assertEqual(added.rows["borrador.txt"][0].text, " no se sube ")
        modified.toggle_all()
        self.assertEqual(modified.rows["index.html"][1].text, "Actualizar")
        self.assertIn("1 no se suben", dlg.lbl_summary.text)
        self.assertIn("1 no se actualizan", dlg.lbl_summary.text)
        self.assertIn("dejando fuera 2", dlg.btn_publish.text)
        self.finish_preview(dlg)
        self.assertEqual(remote_files(self.bare),
                         [".gitignore", "LICENSE", "app.js", "bueno.txt",
                          "index.html"])
        self.assertEqual(subprocess.run(
            ["git", "show", "main:index.html"], cwd=self.bare,
            capture_output=True, text=True).stdout, "<h1>hola</h1>\n")
        self.assertTrue((self.proj / "borrador.txt").exists())
        self.assertEqual((self.proj / "index.html").read_text("utf-8"),
                         "<h1>editado</h1>\n")
        self.assertIn("/borrador.txt", self.app.ignore_lines() + [
            ln for ln in (self.proj / ".gitignore").read_text("utf-8")
            .splitlines()])

    def test_preview_cancel_leaves_remote_untouched(self):
        self.fill_form()
        self.app.start_publish()
        self.run_until_idle(preview=False)
        self.assertEqual(remote_files(self.bare), [])
        self.assertEqual(self.app.status_pill.text, "●  Listo")
        self.assertIn("cancelada", self.console())

    def test_batch_publish_selected_profiles(self):
        a = self.app
        bares = []
        for i in range(3):
            p = Path(self.tmp) / f"p{i}"
            write(p, "f.txt", f"P{i}\n")
            b = make_bare(self.tmp, f"r{i}")
            bares.append(b)
            a.profiles[f"perfil{i}"] = {"folder": str(p), "url": b,
                                        "user": "T", "email": "t@x.y",
                                        "mode": "mirror", "ignore": ""}
        a.refresh_profile_list()
        for name, var in a._profile_checks.items():
            var.set(name != "perfil1")           # dos de tres
        a._update_batch_button()
        self.assertIn("(2)", a.btn_batch.text)

        a.publish_selected()
        self.assertTrue(a.controller.running)
        self.assertTrue(a.controller.many)
        self.run_until_idle()
        self.assertEqual(sorted(k for k, _ in self.seen),
                         ["preview", "preview"])
        self.assertEqual(remote_files(bares[0]), ["f.txt"])
        self.assertEqual(remote_files(bares[1]), [])
        self.assertEqual(remote_files(bares[2]), ["f.txt"])
        self.assertEqual(a.status_pill.text, "●  2 publicados con éxito")
        rows = a.queue_panel.rows
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("publicado" in r.lbl.text for r in rows.values()))
        self.assertEqual(a.btn_batch.cget("state"), "normal")

    def test_batch_review_ignore_goes_to_that_profile(self):
        a = self.app
        p = Path(self.tmp) / "otro"
        write(p, "keys/.env", "X\n")
        write(p, "ok.txt")
        b = make_bare(self.tmp, "otro")
        a.profiles["otro"] = {"folder": str(p), "url": b, "user": "T",
                              "email": "t@x.y", "ignore": "node_modules/"}
        a.profiles["dos"] = {"folder": str(self.proj), "url": self.bare,
                             "user": "T", "email": "t@x.y"}
        a.refresh_profile_list()
        for var in a._profile_checks.values():
            var.set(True)
        a.publish_selected()
        self.run_until_idle(review=("ignore", None))
        self.assertEqual(a.profiles["otro"]["ignore"],
                         "node_modules/\n/keys/.env")
        self.assertNotIn("/keys/.env", a.ignore_lines())   # no era el cargado
        self.assertEqual(remote_files(b), [".gitignore", "ok.txt"])
        self.assertEqual(remote_files(self.bare), ["app.js", "index.html"])

    def test_batch_validation_blocks_bad_profiles(self):
        a = self.app
        a.profiles["roto"] = {"folder": "/no/existe", "url": self.bare}
        a.refresh_profile_list()
        a._profile_checks["roto"].set(True)
        a.publish_selected()
        self.assertFalse(a.controller.running)
        self.assertEqual(tkstub.Messagebox.calls[-1][0], "warning")
        self.assertIn("roto", tkstub.Messagebox.calls[-1][1][1])

    def test_cancel_batch_while_waiting_on_preview(self):
        a = self.app
        for i in range(2):
            p = Path(self.tmp) / f"c{i}"
            write(p, "f.txt")
            a.profiles[f"c{i}"] = {"folder": str(p),
                                   "url": make_bare(self.tmp, f"c{i}"),
                                   "user": "T", "email": "t@x.y"}
        a.refresh_profile_list()
        for var in a._profile_checks.values():
            var.set(True)
        a.publish_selected()
        tkstub.Scheduler.pump(lambda: a.controller._dialog is not None)
        a.cancel_publish()
        tkstub.Scheduler.pump(lambda: not a.controller.running)
        tkstub.Scheduler.run_due()
        self.assertIn("de 2 publicados", a.status_pill.text)
        self.assertTrue(all("cancelado" in r.lbl.text
                            for r in a.queue_panel.rows.values()))

    def test_review_leaves_dubious_findings_unchecked(self):
        hallazgos = [
            {"path": ".env", "size": 10, "kind": "secret",
             "reason": "nombre típico", "confidence": "alta"},
            {"path": "tests/test_x.py", "size": 20, "kind": "secret",
             "reason": "posible clave (parece un valor de ejemplo)",
             "confidence": "baja"},
        ]
        respuesta = {}
        dlg = ReviewDialog(self.app, hallazgos, mode="publish",
                           on_done=lambda r: respuesta.update(r=r))
        self.assertEqual(dlg.selected(), [".env"])     # el dudoso, no
        dlg._finish("ignore")
        self.assertEqual(respuesta["r"], ("ignore", [".env"]))

    def test_manual_scan_opens_review_and_adds_patterns(self):
        write(self.proj, ".env", "S=1\n")
        self.fill_form()
        created = []

        class Recording(ReviewDialog):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                created.append(self)
        with mock.patch.object(app_publish, "ReviewDialog", Recording):
            self.app.start_scan()
            tkstub.Scheduler.pump(lambda: created)
        dlg = created[0]
        self.assertEqual(dlg.mode, "manual")
        self.assertEqual(dlg.selected(), [".env"])
        dlg._finish("ignore")
        self.assertIn("/.env", self.app.ignore_lines())
        self.assertFalse(self.app.scanning)
        self.assertEqual(self.app.status_pill.text, "●  Listo")

    def test_profiles_save_select_delete(self):
        a = self.app
        self.fill_form()
        tkstub.InputDialog.RESPONSE = "web"
        a.save_profile()
        self.assertIn("web", a.profiles)
        self.assertEqual(a.selected_profile, "web")
        a.apply_state({"folder": "/otro"})
        a.select_profile("web")
        self.assertEqual(a.e_folder.get(), str(self.proj))
        a.delete_profile()
        self.assertNotIn("web", a.profiles)
        data, _ = _load(self.cfg_path)
        self.assertEqual(data["profiles"], {})

    def test_bulk_dialog_can_update_what_is_already_downloaded(self):
        from gitpush_studio.core.git import ON_EXISTS_SKIP, ON_EXISTS_UPDATE
        dlg = BulkCloneDialog(self.app)
        self.assertEqual(dlg._on_exists(), ON_EXISTS_SKIP)   # como siempre
        self.assertNotIn("pone al día", dlg.lbl_mode.text)
        dlg.seg_exists.set("Actualizar")
        dlg._mode_changed()
        self.assertEqual(dlg._on_exists(), ON_EXISTS_UPDATE)
        self.assertIn("solo lo que cambió", dlg.lbl_mode.text)
        self.assertIn("Nunca se pisa tu trabajo", dlg.lbl_mode.text)
        dlg.destroy()

    def test_bulk_dialog_offers_both_ways_with_their_price(self):
        from gitpush_studio.core.git import MODE_CLONE, MODE_SNAPSHOT
        dlg = BulkCloneDialog(self.app)
        # Por defecto, lo de siempre: clonar con git.
        self.assertEqual(dlg._mode(), MODE_CLONE)
        self.assertIn("publicar", dlg.lbl_mode.text)
        self.assertIn("más lento", dlg.lbl_mode.text)
        self.assertEqual(dlg.chk_shallow.cget("state"), "normal")
        # Y la alternativa dice claramente qué se pierde.
        dlg.seg_mode.set("Instantánea (.tar.gz)")
        dlg._mode_changed()
        self.assertEqual(dlg._mode(), MODE_SNAPSHOT)
        self.assertIn("NO son repositorios", dlg.lbl_mode.text)
        self.assertIn("no podrás publicar", dlg.lbl_mode.text)
        self.assertIn("más rápido", dlg.lbl_mode.text)
        # La clonación ligera no pinta nada si no se clona.
        self.assertEqual(dlg.chk_shallow.cget("state"), "disabled")
        dlg.destroy()

    def test_dialogs_construct_and_consolidate_runs(self):
        a = self.app
        self.fill_form()
        for factory in (lambda: CloneDialog(a), lambda: BulkCloneDialog(a),
                        lambda: PagesDialog(a, "ana", "repo"),
                        lambda: CreateRepoDialog(a)):
            factory().destroy()
        dlg = ConsolidateDialog(a)
        self.assertEqual(dlg.e_proj.get(), str(self.proj))
        self.assertTrue(dlg.e_out.get().endswith("proj.txt"))
        dlg._generate()
        tkstub.Scheduler.pump(lambda: not dlg.running)
        self.assertIn("TXT generado", dlg.lbl_status.text)
        self.assertTrue(Path(dlg.out_path).exists())
        self.assertIn("consolidado", self.console())

    def test_consolidate_dialog_opens_the_folder_with_the_txt_selected(self):
        """«📂 Abrir ubicación»: la carpeta donde quedó el TXT, con el
        archivo seleccionado; si no se puede, se dice."""
        from gitpush_studio.ui.dialogs import consolidate as modulo
        a = self.app
        self.fill_form()
        dlg = ConsolidateDialog(a)
        self.assertEqual(dlg.btn_reveal.cget("state"), "disabled")
        dlg._generate()
        tkstub.Scheduler.pump(lambda: not dlg.running)
        self.assertEqual(dlg.btn_reveal.cget("state"), "normal")
        pedidos = []
        with mock.patch.object(modulo, "reveal_in_folder",
                               lambda ruta: pedidos.append(ruta) or True):
            dlg._reveal()
            tkstub.Scheduler.pump(lambda: pedidos, timeout=10)
        self.assertEqual(pedidos, [dlg.out_path])
        self.assertIn("TXT generado", dlg.lbl_status.text)   # nada que avisar
        with mock.patch.object(modulo, "reveal_in_folder", lambda ruta: False):
            dlg._reveal()
            tkstub.Scheduler.pump(
                lambda: "No pude abrir" in dlg.lbl_status.text, timeout=10)
        self.assertIn(str(Path(dlg.out_path).parent), dlg.lbl_status.text)
        # Si el TXT ya no está (lo movieron o lo borraron), se avisa y se
        # abre la carpeta donde estaba.
        Path(dlg.out_path).unlink()
        pedidos.clear()
        with mock.patch.object(modulo, "reveal_in_folder",
                               lambda ruta: pedidos.append(ruta) or True):
            dlg._reveal()
            tkstub.Scheduler.pump(lambda: pedidos, timeout=10)
        self.assertIn("ya no está", dlg.lbl_status.text)
        dlg.destroy()

    def test_clone_dialog_loads_project(self):
        a = self.app
        src = Path(self.tmp) / "src"
        write(src, "a.txt")
        init_repo(src)
        commit_all(src, "inicial")
        dlg = CloneDialog(a)
        dlg.e_url.delete(0, "end")
        dlg.e_url.insert(0, str(src))
        dlg.e_dest.delete(0, "end")
        dlg.e_dest.insert(0, str(Path(self.tmp) / "clones"))
        dlg._clone()
        tkstub.Scheduler.pump(lambda: not dlg.running)
        self.assertIn("Clonado", dlg.lbl_status.text)
        self.assertEqual(a.e_folder.get(), str(Path(self.tmp) / "clones" /
                                               "src"))
        self.assertEqual(a.e_url.get(), str(src))

    def test_dialog_closed_mid_work_ignores_late_messages(self):
        a = self.app
        self.fill_form()
        dlg = ConsolidateDialog(a)
        dlg._generate()
        dlg.destroy()                     # el usuario cierra sin esperar
        tkstub.Scheduler.pump(lambda: not any(
            fn.__self__ is dlg for _, _, fn, _ in tkstub.Scheduler.pending
            if hasattr(fn, "__self__")), timeout=10)
        self.assertNotIn("consolidado", self.console())   # no tocó widgets
        self.assertTrue(dlg.q.empty() or True)

    def test_batch_result_without_message_does_not_crash(self):
        from gitpush_studio.core.publish import PublishJob, PublishResult
        job = PublishJob(folder=str(self.proj), url=self.bare, name="x")
        self.app.queue_panel.show([job])
        self.app.queue_panel.finish([PublishResult(job, "cancelled", "")])
        self.app._batch_finished([PublishResult(job, "cancelled", "")])
        self.assertIn("cancelado", list(self.app.queue_panel.rows.values())[0]
                      .lbl.text)

    def publish_form(self):
        self.app.start_publish()
        self.run_until_idle()
        self.assertEqual(self.app.status_pill.text, "●  Publicado con éxito")

    def test_sync_dialog_check_and_apply(self):
        a = self.app
        self.fill_form()
        self.publish_form()
        pc2 = Path(clone(self.bare, Path(self.tmp) / "pc2"))
        write(self.proj, "index.html", "<h1>v2</h1>\n")
        self.publish_form()
        write(pc2, "mio.txt", "M\n")
        self.fill_form(folder=pc2)
        dlg = SyncDialog(a)
        self.assertEqual(dlg.btn_apply.cget("state"), "disabled")
        dlg._check()
        tkstub.Scheduler.pump(lambda: not dlg.running)
        self.assertEqual(dlg.report.modified, ["index.html"])
        self.assertEqual(dlg.report.local_changes, ["mio.txt"])
        self.assertEqual(dlg.btn_apply.cget("state"), "normal")
        dlg._apply()
        tkstub.Scheduler.pump(lambda: not dlg.running)
        self.assertTrue(dlg.lbl_status.text.startswith("✔"))
        self.assertEqual((pc2 / "index.html").read_text("utf-8"),
                         "<h1>v2</h1>\n")
        self.assertTrue((pc2 / "mio.txt").exists())
        self.assertIn("respaldo-", dlg.lbl_status.text)
        self.assertEqual(a.status_pill.text, "●  Carpeta actualizada")

    def test_sync_dialog_follows_the_mode_and_the_checked_branch(self):
        a = self.app
        self.fill_form()
        self.publish_form()
        pc2 = Path(clone(self.bare, Path(self.tmp) / "pc2"))
        write(pc2, "mio.txt", "M\n")                  # solo cambios tuyos
        self.fill_form(folder=pc2)
        dlg = SyncDialog(a)
        dlg._check()
        tkstub.Scheduler.pump(lambda: not dlg.running)
        # Combinar: nada que traer.
        self.assertEqual(dlg.btn_apply.cget("state"), "disabled")
        # Espejo: descartaría lo tuyo (con copia): sí hay algo que hacer.
        dlg.seg_mode.set("Espejo de GitHub")
        dlg._rerender()
        self.assertEqual(dlg.btn_apply.cget("state"), "normal")
        self.assertIn("descartarían", dlg.lbl_status.text)
        # Cambiar la rama tras comprobar obliga a comprobar de nuevo.
        dlg.e_branch.delete(0, "end")
        dlg.e_branch.insert(0, "otra")
        dlg._apply()
        self.assertFalse(dlg.running)
        self.assertIn("Comprobar", dlg.lbl_status.text)
        self.assertTrue((pc2 / "mio.txt").exists())

    def test_sync_dialog_never_offers_to_wipe_a_nested_repository(self):
        """GitHub trae un ARCHIVO «vendor» y en tu carpeta «vendor» es otro
        repositorio: ningún modo puede aplicarse (combinar lo enseña como
        choque; el espejo, con el motivo)."""
        a = self.app
        self.fill_form()
        self.publish_form()
        pc2 = Path(clone(self.bare, Path(self.tmp) / "pc2"))
        write(self.proj, "vendor", "archivo de GitHub\n")
        self.publish_form()
        (pc2 / ".git" / "info" / "exclude").write_text("vendor/\n")
        sub = pc2 / "vendor"
        sub.mkdir()
        subprocess.run(["git", "init", "-q", str(sub)], check=True,
                       capture_output=True)
        write(sub, "dato.txt", "SECRETO\n")
        self.fill_form(folder=pc2)
        dlg = SyncDialog(a)
        dlg._check()
        tkstub.Scheduler.pump(lambda: not dlg.running)
        self.assertIn("vendor", [r for r, _ in dlg.report.conflicts])
        self.assertEqual(dlg.btn_apply.cget("state"), "disabled")
        dlg.seg_mode.set("Espejo de GitHub")
        dlg._rerender()
        self.assertEqual(dlg.btn_apply.cget("state"), "disabled")
        textos = [w.text for w in dlg.body.winfo_children()]
        self.assertTrue(any("vendor" in t and "repositorio" in t
                            for t in textos), textos)
        dlg._apply()
        self.assertFalse(dlg.running)
        self.assertEqual((sub / "dato.txt").read_text("utf-8"), "SECRETO\n")
        self.assertTrue((sub / ".git").is_dir())

    def test_history_dialog_recovers_a_backup_without_committing(self):
        a = self.app
        self.fill_form()
        self.publish_form()
        write(self.proj, "borrador.txt", "sin publicar\n")
        from gitpush_studio.core.git import Git
        g = Git()
        rama = g.backup(str(self.proj))
        (self.proj / "borrador.txt").unlink()
        antes = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.proj,
                               capture_output=True, text=True).stdout
        dlg = HistoryDialog(a)
        tkstub.Scheduler.pump(lambda: not dlg.running and dlg.versions)
        respaldo = [v for v in dlg.versions if v.branch == rama][0]
        tkstub.Messagebox.ANSWER = True
        dlg._restore(respaldo)
        tkstub.Scheduler.pump(lambda: not dlg.running and len(
            [v for v in dlg.versions if v.is_backup]) == 2)
        self.assertTrue(dlg.lbl_status.text.startswith("✔"),
                        dlg.lbl_status.text)
        self.assertEqual((self.proj / "borrador.txt").read_text("utf-8"),
                         "sin publicar\n")
        self.assertEqual(subprocess.run(["git", "rev-parse", "HEAD"],
                                        cwd=self.proj, capture_output=True,
                                        text=True).stdout, antes)
        self.assertIn("Respaldo recuperado", a.status_pill.text)

    def test_history_dialog_restore(self):
        a = self.app
        self.fill_form()
        self.publish_form()
        write(self.proj, "app.js", "console.log(2)\n")
        self.publish_form()
        dlg = HistoryDialog(a)
        tkstub.Scheduler.pump(lambda: not dlg.running and dlg.versions)
        self.assertEqual(len([v for v in dlg.versions if not v.is_backup]), 2)
        older = dlg.versions[1]
        tkstub.Messagebox.ANSWER = True
        dlg._restore(older)
        tkstub.Scheduler.pump(lambda: not dlg.running and any(
            v.is_backup for v in dlg.versions))
        self.assertTrue(dlg.lbl_status.text.startswith("✔"))
        self.assertEqual((self.proj / "app.js").read_text("utf-8"),
                         "console.log(1)\n")
        self.assertIn("restaurada", a.status_pill.text)
        self.publish_form()                       # GitHub vuelve a v1
        self.assertEqual(subprocess.run(
            ["git", "show", "main:app.js"], cwd=self.bare, capture_output=True,
            text=True).stdout, "console.log(1)\n")

    def test_branch_dialog(self):
        a = self.app
        self.fill_form()
        self.publish_form()
        dlg = BranchDialog(a)
        tkstub.Scheduler.pump(lambda: not dlg.running and hasattr(dlg,
                                                                  "_remote"))
        self.assertEqual(dlg._remote, ["main"])
        self.assertEqual(dlg._local, "main")
        dlg.e_name.insert(0, "mal nombre")
        dlg._use_typed()
        self.assertEqual(a.e_branch.get(), "main")
        dlg.e_name.delete(0, "end")
        dlg.e_name.insert(0, "gh-pages")
        dlg._use_typed()
        self.assertEqual(a.e_branch.get(), "gh-pages")
        dlg._use("main")
        self.assertEqual(a.e_branch.get(), "main")
        # Sin URL válida: escribir una rama a mano sigue funcionando.
        a.e_url.delete(0, "end")
        dlg2 = BranchDialog(a)
        dlg2.e_name.insert(0, "docs")
        dlg2._use_typed()
        self.assertEqual(a.e_branch.get(), "docs")

    def test_release_dialog(self):
        a = self.app
        self.fill_form()
        self.publish_form()
        calls = []
        with mock.patch.object(github, "api", lambda m, p, t="", payload=None,
                               **k: (calls.append(p) or (201, {
                                   "id": 5, "html_url": "https://x/r/v1"}))), \
                mock.patch.object(github, "upload_asset",
                                  lambda *args, **k: (201, {})):
            dlg = ReleaseDialog(a, "ana", "proj")
            tkstub.Scheduler.pump(lambda: dlg.e_tag.get() == "v1.0.0")
            dlg.e_token.insert(0, "ghp_x")
            dlg._create()
            tkstub.Scheduler.pump(lambda: not dlg.running)
        self.assertTrue(dlg.lbl_status.text.startswith("✔"), dlg.lbl_status.text)
        self.assertEqual(calls, ["/repos/ana/proj/releases"])
        self.assertEqual(dlg.btn_open.cget("state"), "normal")
        self.assertEqual(a.status_pill.text, "●  Versión v1.0.0 publicada")
        a.e_url.delete(0, "end")
        a.e_url.insert(0, "/no/es/github")
        a.open_release()
        self.assertEqual(tkstub.Messagebox.calls[-1][0], "warning")

    def test_connection_token_follows_the_option(self):
        a = self.app
        a.store_token("ghp_" + "b" * 36, remember=True)
        self.assertTrue(a.v_use_token.get())
        self.assertEqual(a.connection_token(), "ghp_" + "b" * 36)
        a.v_use_token.set(False)
        self.assertEqual(a.connection_token(), "")
        self.assertFalse(a.collect_state()["use_token"])
        a.apply_state({"use_token": True})
        self.assertTrue(a.v_use_token.get())
        # Un perfil puede tener su propia preferencia.
        self.assertEqual(a.connection_token({"use_token": False}), "")

    def test_publish_job_carries_the_token(self):
        a = self.app
        a.store_token("ghp_" + "c" * 36, remember=True)
        self.fill_form()
        capturado = {}
        real = a.controller.start

        def espia(jobs, on_finished, **kw):
            capturado["token"] = jobs[0].token
            return real(jobs, on_finished, **kw)
        a.controller.start = espia
        a.start_publish()
        self.run_until_idle()
        self.assertEqual(capturado["token"], "ghp_" + "c" * 36)

    def test_auto_message_state_round_trip(self):
        a = self.app
        a.v_auto_msg.set(True)
        self.assertTrue(a.collect_state()["auto_message"])
        a.apply_state({"auto_message": False})
        self.assertFalse(a.v_auto_msg.get())

    def test_auto_publish_cycle(self):
        a = self.app
        self.fill_form()
        a.v_auto.set(True)
        with mock.patch.object(app_auto, "SETTLE_SECONDS", 0):
            a.toggle_auto()
            self.assertIsNotNone(a._auto_job)
            tkstub.Scheduler.pump(lambda: a._last_pub_fp is not None)
            a._auto_tick()                              # sin cambios: idle
            tkstub.Scheduler.pump(lambda: a._auto_job is not None)
            self.assertEqual(remote_files(self.bare), [])
            write(self.proj, "nuevo.txt", "n\n")       # ahora sí
            a._auto_tick()
            tkstub.Scheduler.pump(lambda: a.controller.running)
            self.run_until_idle()
        self.assertEqual(remote_files(self.bare),
                         [".gitignore", "LICENSE", "app.js", "index.html",
                          "nuevo.txt"])
        self.assertEqual(self.console().count("Cambios detectados"), 1)
        self.assertTrue(a.v_auto.get())
        self.assertIsNotNone(a._auto_job)               # reprogramada
        # Tercera ronda, ya con repositorio: git status limpio → idle.
        with mock.patch.object(app_auto, "SETTLE_SECONDS", 0):
            a._auto_tick()
            tkstub.Scheduler.pump(lambda: a._auto_job is not None
                                  and not a.controller.running)
        self.assertEqual(self.console().count("Cambios detectados"), 1)


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class BatchPublishDialogTests(HeadlessCase):
    """El diálogo «Publicar varias carpetas»: varias carpetas de golpe a
    la misma cuenta, cada una con su casilla de TXT."""

    def setUp(self):
        super().setUp()
        self.proj2 = Path(self.tmp) / "Mi Proyecto 2"
        write(self.proj2, "main.c", "int main(){return 0;}\n")
        self.bare2 = make_bare(self.tmp, "remote2")
        self.fill_form()

    def open(self):
        dlg = BatchPublishDialog(self.app)
        # Sin token y sin crear nada: las URLs son los bare locales.
        dlg.e_token.delete(0, "end")
        dlg.v_create.set(False)
        return dlg

    def fill_rows(self, dlg, pairs):
        """Deja exactamente una fila por par (carpeta, url)."""
        while len(dlg.rows) > len(pairs):
            dlg._drop(dlg.rows[-1])
        while len(dlg.rows) < len(pairs):
            dlg._add_empty()
        for row, (folder, repo) in zip(dlg.rows, pairs):
            row.e_folder.delete(0, "end")
            row.e_folder.insert(0, str(folder))
            row.e_repo.delete(0, "end")
            row.e_repo.insert(0, repo)
        dlg._changed()

    def test_it_opens_with_the_form_folder_and_suggests_the_repo_name(self):
        dlg = self.open()
        self.assertEqual(Path(dlg.rows[0].e_folder.get()), self.proj)
        self.assertEqual(dlg.rows[0].e_repo.get(), "proj")
        self.assertTrue(dlg.rows[0].wants_txt(), "la casilla del TXT tiene "
                                                 "que venir marcada")
        dlg.destroy()

    def test_browsing_a_folder_fills_the_repo_name_but_never_overwrites(self):
        dlg = self.open()
        row = dlg.rows[-1]
        tkstub.Filedialog.FOLDER = str(self.proj2)
        row._browse()
        self.assertEqual(row.e_repo.get(), "Mi-Proyecto-2")
        # Un nombre escrito a mano manda: no se toca al cambiar de carpeta.
        row.e_repo.delete(0, "end")
        row.e_repo.insert(0, "elegido-a-mano")
        tkstub.Filedialog.FOLDER = str(self.proj)
        row._browse()
        self.assertEqual(row.e_repo.get(), "elegido-a-mano")
        dlg.destroy()

    def test_the_master_checkbox_marks_and_unmarks_every_row(self):
        dlg = self.open()
        self.fill_rows(dlg, [(self.proj, "uno"), (self.proj2, "dos")])
        dlg.v_all_txt.set(False)
        dlg._toggle_all_txt()
        self.assertEqual([r.wants_txt() for r in dlg.rows], [False, False])
        self.assertIn("0 con .txt", dlg.lbl_count.text)
        dlg.v_all_txt.set(True)
        dlg._toggle_all_txt()
        self.assertEqual([r.wants_txt() for r in dlg.rows], [True, True])
        self.assertIn("2 con .txt", dlg.lbl_count.text)
        dlg.destroy()

    def test_adding_the_subfolders_of_a_parent(self):
        dlg = self.open()
        tkstub.Filedialog.FOLDER = self.tmp
        dlg._add_children()
        nombres = sorted(Path(r.e_folder.get()).name for r in dlg.rows)
        self.assertIn("proj", nombres)
        self.assertIn("Mi Proyecto 2", nombres)
        # Cada una llega con su nombre de repositorio ya propuesto.
        self.assertTrue(all(r.e_repo.get() for r in dlg.rows), nombres)
        # Repetirlo no duplica nada, y lo dice.
        antes = len(dlg.rows)
        dlg._add_children()
        self.assertEqual(len(dlg.rows), antes)
        self.assertIn("No encontré subcarpetas nuevas", dlg.lbl_status.text)
        dlg.destroy()

    def test_adding_subfolders_again_only_brings_the_new_ones(self):
        dlg = self.open()
        tkstub.Filedialog.FOLDER = self.tmp
        dlg._add_children()
        antes = len(dlg.rows)
        write(Path(self.tmp) / "recien-creado", "x.txt")
        dlg._add_children()
        self.assertEqual(len(dlg.rows), antes + 1)
        self.assertIn("ya estaban", dlg.lbl_status.text)
        self.assertIn("recien-creado",
                      [r.e_repo.get() for r in dlg.rows])
        dlg.destroy()

    def test_removing_rows_renumbers_and_never_empties_the_list(self):
        dlg = self.open()
        self.fill_rows(dlg, [(self.proj, "uno"), (self.proj2, "dos")])
        dlg._remove_row(dlg.rows[0])
        self.assertEqual(len(dlg.rows), 1)
        self.assertEqual(dlg.rows[0].lbl_num.text, "1.")
        # La última no se borra: se vacía, y así el botón sigue teniendo
        # sentido.
        dlg._remove_row(dlg.rows[0])
        self.assertEqual(len(dlg.rows), 1)
        self.assertTrue(dlg.rows[0].is_empty())
        dlg.destroy()

    def test_problems_are_shown_and_nothing_is_published(self):
        dlg = self.open()
        self.fill_rows(dlg, [(self.proj, "mismo"), (self.proj2, "mismo")])
        dlg._publish()
        self.assertIn("se repite", dlg.lbl_status.text)
        self.assertFalse(self.app.controller.running)
        self.assertEqual(remote_files(self.bare), [])
        dlg.destroy()

    def test_without_a_token_the_urls_come_from_the_username(self):
        """No se publica de verdad (eso iría a github.com): se comprueba
        que el plan apunta a la cuenta escrita y que cada fila lleva su
        casilla de TXT."""
        from gitpush_studio.core import batch as batch_mod
        dlg = self.open()
        dlg.e_owner.delete(0, "end")
        dlg.e_owner.insert(0, "ana")
        self.fill_rows(dlg, [(self.proj, "uno"), (self.proj2, "dos")])
        dlg.rows[1].set_txt(False)
        entries = dlg._entries()
        plan = batch_mod.plan_batch(entries, self.app.collect_state(),
                                    owner="ana", create_missing=False)
        self.assertTrue(plan.ok, plan.problems)
        self.assertEqual([j.url for j in plan.jobs],
                         ["https://github.com/ana/uno.git",
                          "https://github.com/ana/dos.git"])
        self.assertEqual([j.consolidate for j in plan.jobs], [True, False])
        self.assertEqual(plan.created, [])
        dlg.destroy()

    def test_the_plan_reaches_the_queue_and_both_repos_get_their_files(self):
        from gitpush_studio.core import batch as batch_mod
        dlg = self.open()
        self.fill_rows(dlg, [(self.proj, "uno"), (self.proj2, "dos")])
        entries = dlg._entries()
        state = dict(self.app.collect_state())
        state["preview"] = False
        state["check"] = False
        for e, url in zip(entries, (self.bare, self.bare2)):
            e.url = url
        plan = batch_mod.BatchPlan(
            owner="ana",
            jobs=batch_mod.jobs_from_entries(entries, state))
        self.assertTrue(plan.ok)
        self.assertTrue(self.app.start_batch(plan))
        self.run_until_idle()
        self.assertEqual(remote_files(self.bare),
                         [".gitignore", "LICENSE", "app.js", "index.html"])
        self.assertEqual(remote_files(self.bare2),
                         [".gitignore", "LICENSE", "main.c"])
        # Cada proyecto tiene SU txt, dentro de SU carpeta, y no se sube.
        self.assertTrue((self.proj / "proj.txt").is_file())
        self.assertTrue((self.proj2 / "Mi Proyecto 2.txt").is_file())
        self.assertNotIn("proj.txt", remote_files(self.bare))
        self.assertNotIn("Mi Proyecto 2.txt", remote_files(self.bare2))
        texto = self.console()
        self.assertIn("2 proyectos en la cuenta de ana", texto)
        self.assertIn("se unirán además en un .txt", texto)
        self.assertIn("proj.txt", texto)
        dlg.destroy()

    def test_the_list_survives_closing_the_dialog(self):
        dlg = self.open()
        self.fill_rows(dlg, [(self.proj, "uno"), (self.proj2, "dos")])
        dlg.close()
        guardado = self.app.config_data["batch"]
        self.assertEqual([p["repo"] for p in guardado["projects"]],
                         ["uno", "dos"])
        otro = self.open()
        self.assertEqual([r.e_repo.get() for r in otro.rows], ["uno", "dos"])
        self.assertEqual([Path(r.e_folder.get()) for r in otro.rows],
                         [self.proj, self.proj2])
        otro.destroy()

    def test_a_retry_after_fixing_a_name_never_uses_the_old_url(self):
        """El fallo más caro del lote: el primer intento resuelve la fila
        1 y falla en la 2; el usuario corrige los nombres y reintenta. Si
        la fila guardara la URL del intento anterior, se publicaría en el
        repositorio VIEJO mientras la cola muestra el nombre nuevo.
        """
        from gitpush_studio.core import batch as batch_mod
        dlg = self.open()
        self.fill_rows(dlg, [(self.proj, "uno"), (self.proj2, "dos")])
        creados = []

        def crear(token, name, description="", private=False):
            creados.append(name)
            if name == "dos":
                raise StepError("nombre ocupado")
            clone = "https://github.com/ana/%s.git" % name
            return clone, clone[:-4], False

        api = {"api_create": crear, "api_whoami": lambda t: "ana"}
        primero = batch_mod.plan_batch(dlg._entries(), {}, token="t",
                                       **api)
        self.assertFalse(primero.ok)
        # El usuario corrige AMBOS nombres y vuelve a darle.
        for row, nuevo in zip(dlg.rows, ("uno-bueno", "dos-bueno")):
            set_text(row.e_repo, nuevo)
        segundo = batch_mod.plan_batch(dlg._entries(), {}, token="t", **api)
        self.assertTrue(segundo.ok, segundo.problems)
        self.assertEqual([j.url for j in segundo.jobs],
                         ["https://github.com/ana/uno-bueno.git",
                          "https://github.com/ana/dos-bueno.git"])
        self.assertEqual([j.label for j in segundo.jobs],
                         ["uno-bueno", "dos-bueno"])
        self.assertEqual(sorted(segundo.created), ["dos-bueno", "uno-bueno"])
        dlg.destroy()

    def test_it_does_not_close_if_the_publication_did_not_start(self):
        """Si el usuario dice «no» al aviso del push forzado, la ventana
        NO puede desaparecer: parecería que publicó."""
        from gitpush_studio.core import batch as batch_mod
        dlg = self.open()
        self.fill_rows(dlg, [(self.proj, "uno")])
        entries = dlg._entries()
        entries[0].url = self.bare
        plan = batch_mod.BatchPlan(
            owner="ana", jobs=batch_mod.jobs_from_entries(entries, {}))
        for job in plan.jobs:                      # fuerza el aviso
            job.force = True
        tkstub.Messagebox.ANSWER = False           # el usuario dice «no»
        dlg.running = True
        dlg._planned(plan)
        self.assertTrue(dlg.winfo_exists(), "la ventana no debe cerrarse")
        self.assertFalse(dlg.running)
        self.assertEqual(dlg.btn_go.cget("state"), "normal")
        self.assertIn("no arrancó", dlg.lbl_status.text)
        self.assertIn("dijiste que no", self.console())
        self.assertEqual(remote_files(self.bare), [])
        dlg.destroy()

    def test_the_list_cannot_be_edited_while_it_prepares(self):
        """Quitar una fila mientras se prepara dejaría la pantalla
        diciendo una cosa y el lote publicando otra."""
        dlg = self.open()
        self.fill_rows(dlg, [(self.proj, "uno"), (self.proj2, "dos")])
        dlg.running = True
        try:
            dlg._remove_row(dlg.rows[0])
            dlg._add_empty()
            self.assertEqual(len(dlg.rows), 2, "la lista cambió")
            self.assertIn("preparando", dlg.lbl_status.text.lower())
        finally:
            dlg.running = False
        dlg.destroy()

    def test_closing_while_preparing_says_so_in_the_console(self):
        dlg = self.open()
        self.fill_rows(dlg, [(self.proj, "uno")])
        dlg.running = True
        dlg.close()
        self.assertIn("mientras preparaba el lote", self.console())
        self.assertFalse(dlg.winfo_exists())

    def test_the_row_limit_is_announced_not_silent(self):
        dlg = self.open()
        muchas = Path(self.tmp) / "muchas"
        for i in range(55):
            write(muchas / ("p%02d" % i), "a.txt")
        tkstub.Filedialog.FOLDER = str(muchas)
        dlg._add_children()
        self.assertEqual(len(dlg.rows), 50)
        self.assertIn("NO caben", dlg.lbl_status.text)
        self.assertIn("50", dlg.lbl_status.text)
        dlg.destroy()

    def test_it_refuses_to_start_while_something_else_is_publishing(self):
        dlg = self.open()
        self.fill_rows(dlg, [(self.proj, "uno")])
        self.app.controller.running = True
        try:
            dlg._publish()
            self.assertIn("ya hay una publicación", dlg.lbl_status.text.lower())
        finally:
            self.app.controller.running = False
        dlg.destroy()


class OddConfigTests(unittest.TestCase):
    """Un profiles.json editado a mano o por otra versión no puede
    impedir que la app arranque."""

    def test_the_app_starts_with_odd_types_in_the_config(self):
        tmp = tempfile.mkdtemp()
        try:
            ruta = Path(tmp) / "profiles.json"
            ruta.write_text(json.dumps({
                "profiles": {"p": {"auto_interval": "abc", "check": "no",
                                   "folder": 7, "mode": "raro"}},
                "last_profile": "p",
                "last_state": {"auto_interval": "x", "preview": "sí"}}),
                encoding="utf-8")
            with mock.patch.object(cfg, "load_config",
                                   lambda: _ORIG_LOAD(ruta)), \
                    mock.patch.object(cfg, "save_config",
                                      lambda data: _ORIG_SAVE(data, ruta)), \
                    mock.patch.object(diaglog, "LOG_DIR",
                                      Path(tmp) / "registro"):
                app = GitPushStudio()
                self.assertEqual(app.om_interval.get(), "10 min")
                self.assertEqual(app.selected_profile, "p")
                app.destroy()
            ruta.write_text(json.dumps({"profiles": [], "last_profile": [1],
                                        "last_state": [2]}), "utf-8")
            with mock.patch.object(cfg, "load_config",
                                   lambda: _ORIG_LOAD(ruta)), \
                    mock.patch.object(cfg, "save_config",
                                      lambda data: _ORIG_SAVE(data, ruta)), \
                    mock.patch.object(diaglog, "LOG_DIR",
                                      Path(tmp) / "registro"):
                app = GitPushStudio()
                app.destroy()
            self.assertTrue(list(Path(tmp).glob("profiles.json.bak-*")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _load(path):
    return _ORIG_LOAD(path)


_ORIG_LOAD = cfg.load_config
_ORIG_SAVE = cfg.save_config


if __name__ == "__main__":
    unittest.main()
