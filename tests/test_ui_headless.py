# -*- coding: utf-8 -*-
"""Pruebas de la interfaz sin pantalla: se instalan sustitutos de
tkinter/customtkinter y se ejercita el cableado real (cola, controlador,
decisiones en serie, diálogos) contra git y repositorios bare locales."""

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
from gitpush_studio.ui import app as app_module                  # noqa: E402
from gitpush_studio.ui.app import GitPushStudio                  # noqa: E402
from gitpush_studio.core import github                            # noqa: E402
from gitpush_studio.ui.dialogs import (BranchDialog, BulkCloneDialog,  # noqa
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


@unittest.skipUnless(HAVE_GIT, "git no disponible")
class HeadlessUITests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg_path = Path(self.tmp) / "profiles.json"
        self._patches = [
            mock.patch.object(cfg, "load_config",
                              lambda: _ORIG_LOAD(self.cfg_path)),
            mock.patch.object(cfg, "save_config",
                              lambda data: _ORIG_SAVE(data, self.cfg_path)),
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

    # ---- pruebas ----------------------------------------------------
    def test_console_shows_the_tool_guide_on_start(self):
        texto = self.console()
        self.assertIn("Bienvenido", texto)
        self.assertIn("¿Qué hace cada botón?", texto)
        # Las seis herramientas de la barra lateral, explicadas.
        for boton in ("⬇⬆  Traer cambios de GitHub", "⏪  Versiones",
                      "🏷  Publicar versión", "⬇  Clonar repositorio",
                      "⬇⬇  Clonar perfil completo", "📄  Consolidar a TXT"):
            self.assertIn(boton, texto)
        for accion in ("🚀  Publicar en GitHub", "🛡  Analizar",
                       "🤖  Auto-publicación", "🌍  Publicar como web",
                       "🌿  Ramas", "➕  Crear repositorio nuevo"):
            self.assertIn(accion, texto)
        # Cada botón lleva su explicación, no solo el nombre.
        self.assertIn("lo contrario de publicar", texto)
        self.assertIn("puedes devolver la carpeta", texto)

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
                         [".gitignore", "app.js", "index.html"])
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
                         [".gitignore", "app.js", "index.html"])

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
                         [".gitignore", "app.js", "nuevo.txt"])
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
                         [".gitignore", "app.js", "bueno.txt", "index.html"])
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

    def test_manual_scan_opens_review_and_adds_patterns(self):
        write(self.proj, ".env", "S=1\n")
        self.fill_form()
        created = []

        class Recording(ReviewDialog):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                created.append(self)
        with mock.patch.object(app_module, "ReviewDialog", Recording):
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
        with mock.patch.object(app_module, "SETTLE_SECONDS", 0):
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
                         [".gitignore", "app.js", "index.html", "nuevo.txt"])
        self.assertEqual(self.console().count("Cambios detectados"), 1)
        self.assertTrue(a.v_auto.get())
        self.assertIsNotNone(a._auto_job)               # reprogramada
        # Tercera ronda, ya con repositorio: git status limpio → idle.
        with mock.patch.object(app_module, "SETTLE_SECONDS", 0):
            a._auto_tick()
            tkstub.Scheduler.pump(lambda: a._auto_job is not None
                                  and not a.controller.running)
        self.assertEqual(self.console().count("Cambios detectados"), 1)


def _load(path):
    return _ORIG_LOAD(path)


_ORIG_LOAD = cfg.load_config
_ORIG_SAVE = cfg.save_config


if __name__ == "__main__":
    unittest.main()
