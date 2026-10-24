# -*- coding: utf-8 -*-
"""Publicar desde la ventana principal: el análisis manual, una
publicación, la de los perfiles marcados y la de varias carpetas; el
cableado con el controlador que las ejecuta en segundo plano.

Mezcla de `GitPushStudio`: usa sus widgets y su estado (`self.…`);
no tiene constructor propio."""

import threading
from pathlib import Path
from tkinter import messagebox

from .. import APP_NAME
from ..core.errors import StepError
from ..core.fs import fmt_size, normalize_path
from ..core.ignore import anchor_pattern, clean_patterns
from ..core.publish import PublishJob
from ..core.scan import scan_project, stats_lines
from .dialogs import ReviewDialog
from .jobs import first_line


class PublishingMixin:

    def start_scan(self):
        if self.controller.running or self.scanning:
            return
        folder = normalize_path(self.e_folder.get())
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning(APP_NAME, "Indica primero una carpeta de "
                                             "proyecto que exista.")
            return
        self.scanning = True
        self.btn_scan.configure(state="disabled", text="⏳  Analizando…")
        self.btn_publish.configure(state="disabled")
        self.set_status("●  Analizando…", "work")
        self.log("─" * 60, "dim")
        self.log("🛡 Analizando el proyecto en busca de archivos grandes y "
                 "secretos…", "out")
        patterns = self.ignore_lines()

        def work():
            try:
                findings, stats = scan_project(folder, patterns)
            except (StepError, OSError) as e:
                self.q.put(("log", f"✖ Falló el análisis: {e}", "err"))
                findings, stats = [], {"files": 0, "seconds": 0,
                                       "truncated": False}
            self.q.put(("scan_result", findings, stats))
        threading.Thread(target=work, daemon=True).start()

    def _finish_scan(self, findings, stats):
        self.scanning = False
        self.btn_scan.configure(state="normal", text="🛡  Analizar")
        self.btn_publish.configure(state="normal")
        for texto, etiqueta in stats_lines(stats):
            self.log("  " + texto, etiqueta)
        if not findings:
            self.log("✔ Sin archivos grandes ni secretos a la vista.", "ok")
            self.set_status("●  Sin riesgos", "ok")
            return
        self.log(f"⚠ {len(findings)} hallazgo(s). Revisa la ventana.", "warn")
        self.set_status("●  Hallazgos — revisa", "warn")
        ReviewDialog(self, findings, mode="manual",
                     on_done=lambda res: self._manual_review_done(*res))

    def _manual_review_done(self, decision, selected):
        if decision == "ignore" and selected:
            self.add_ignore_patterns([anchor_pattern(p) for p in selected])
            self.log("✔ Se excluirán en la próxima publicación.", "ok")
        self.set_status("●  Listo", "idle")

    def _begin_work(self, text):
        self.btn_publish.configure(state="disabled", text=text)
        self.btn_scan.configure(state="disabled")
        self.btn_batch.configure(state="disabled")
        self.progress.set(0)
        self.set_status("●  Trabajando…", "work")
        self.log("─" * 60, "dim")

    def _end_work(self):
        self.btn_publish.configure(state="normal", text="🚀  Publicar en GitHub")
        self.btn_scan.configure(state="normal")
        self._update_batch_button()

    def _confirm_force(self, count=1):
        what = ("Todo lo que haya en el repositorio remoto se reemplazará "
                "por tu versión local." if count == 1 else
                f"{count} proyectos tienen el push forzado activado: sus "
                "repositorios remotos se reemplazarán por la versión local.")
        return messagebox.askyesno(
            APP_NAME, "Has activado el push forzado (-f).\n\n" + what
            + "\n\n¿Continuar?")

    def start_publish(self, auto=False):
        if self.controller.running or self.scanning:
            return
        state = self.collect_state()
        job = PublishJob.from_state(state, auto=auto)
        job.token = self.connection_token()
        error = job.validate()
        if error:
            if auto:
                self._auto_abort(error)
            else:
                messagebox.showwarning(APP_NAME, error)
            return
        if job.force and not job.addedit and not auto \
                and not self._confirm_force():
            return
        self.config_data["last_state"] = state
        self.save_config()
        self.last_repo_web = ""
        self._begin_work("⏳  Publicando…")
        self.controller.start([job], lambda results: self._publish_finished(
            results[0], auto))

    def _publish_finished(self, r, auto):
        self._end_work()
        self._log_txt(r)
        if r.ok:
            self.progress.set(1)
            self.last_repo_web = r.web_url
            self.set_status("●  Publicado con éxito", "ok")
            if self._auto_fp:
                self._last_pub_fp = self._auto_fp
            if not self.last_pages_url:
                self.log("  ¿Es una página web? Actívala con «🌍 Publicar "
                         "como web…».", "dim")
            self.bell()
        elif r.status == "blocked":
            self.progress.set(0)
            self.set_status("●  Hallazgos — revisa con 🛡", "warn")
            self._auto_abort(r.message)
        elif r.status == "cancelled":
            self.progress.set(0)
            self.set_status("●  Listo", "idle")
        else:
            self.set_status("●  Error — revisa la consola", "err")
        self._auto_fp = None
        if auto and self.v_auto.get():
            self._schedule_auto()

    def publish_selected(self):
        if self.controller.running or self.scanning:
            return
        names = [n for n in sorted(self._profile_checks)
                 if self._profile_checks[n].get()]
        if not names:
            messagebox.showinfo(APP_NAME, "Marca en la lista los perfiles "
                                          "que quieras publicar.")
            return
        jobs, problems = [], []
        for name in names:
            profile = self.profiles.get(name, {})
            job = PublishJob.from_state(profile, name=name)
            job.token = self.connection_token(profile)
            error = job.validate()
            if error:
                problems.append(f"• {name}: {error}")
            else:
                jobs.append(job)
        if problems:
            messagebox.showwarning(APP_NAME, "Revisa estos perfiles antes de "
                                             "publicar:\n\n" + "\n".join(problems))
            return
        forced = sum(1 for j in jobs if j.force and not j.addedit)
        if forced and not self._confirm_force(forced):
            return
        if self.v_auto.get():
            self.v_auto.set(False)
            self.toggle_auto()
        self._begin_work(f"⏳  Publicando {len(jobs)}…")
        self.log(f"🚀🚀 Publicando {len(jobs)} proyectos: "
                 + ", ".join(j.label for j in jobs), "out")
        self.controller.start(jobs, self._batch_finished)

    def start_batch(self, plan):
        """Lanza un lote ya preparado por «Publicar varias carpetas».

        Devuelve True solo si la publicación arrancó. Cuando devuelve
        False DICE POR QUÉ en la consola: quien llama cierra su ventana
        según esta respuesta, y un False callado dejaría al usuario
        creyendo que ha publicado.
        """
        jobs = plan.jobs
        if not jobs:
            self.log("✖ El lote llegó vacío: no hay nada que publicar.",
                     "err")
            return False
        if self.controller_running() or self.scanning:
            self.log("✖ No arranco el lote: ya hay una publicación o un "
                     "análisis en marcha. Espera a que termine y vuelve a "
                     "darle.", "warn")
            return False
        forced = sum(1 for j in jobs if j.force and not j.addedit)
        if forced and not self._confirm_force(forced):
            self.log("✋ Lote cancelado: dijiste que no al aviso del push "
                     "forzado. No se ha subido nada.", "warn")
            return False
        if self.v_auto.get():
            self.v_auto.set(False)
            self.toggle_auto()
        con_txt = sum(1 for j in jobs if j.consolidate)
        cuantos = f"{len(jobs)} proyectos" if len(jobs) != 1 else "1 proyecto"
        self._begin_work(f"⏳  Publicando {len(jobs)}…")
        self.log(f"🚀🚀 Publicando {cuantos} en la cuenta de "
                 f"{plan.owner or 'GitHub'}: "
                 + ", ".join(j.label for j in jobs), "out")
        if con_txt:
            self.log(f"  {con_txt} de ellos se unirán además en un .txt "
                     "dentro de su carpeta (no se sube).", "dim")
        if not self.controller.start(jobs, self._batch_finished):
            self._end_work()
            self.log("✖ No arranco el lote: el controlador de publicación "
                     "está ocupado.", "err")
            return False
        return True

    def _batch_finished(self, results):
        self._end_work()
        ok = sum(1 for r in results if r.ok)
        bad = [r for r in results if not r.ok]
        self.progress.set(1 if not bad else 0)
        self.log("─" * 60, "dim")
        for r in results:
            mark = {"ok": "✔", "cancelled": "✋", "blocked": "🛡"}.get(r.status,
                                                                       "✖")
            detail = r.web_url if r.ok else first_line(r.message)
            self.log(f"{mark} {r.job.label}: {detail} ({r.seconds:.1f} s)",
                     "ok" if r.ok else "warn")
            self._log_txt(r)
        if not bad:
            self.set_status(f"●  {ok} publicados con éxito", "ok")
            self.bell()
        else:
            self.set_status(f"●  {ok} de {len(results)} publicados", "warn")

    def _log_txt(self, r):
        """Resumen del TXT del proyecto, si se pidió."""
        if r.txt_error:
            self.log(f"   📄 sin TXT: {r.txt_error}", "warn")
        elif r.txt_path:
            st = r.txt_stats or {}
            self.log(f"   📄 {r.txt_path} ({st.get('files', 0)} archivos, "
                     f"{fmt_size(st.get('bytes', 0))})", "dim")

    def cancel_publish(self):
        self.controller.cancel()

    def on_review_ignored(self, job, patterns):
        """La revisión excluyó rutas: se reflejan donde vive ese proyecto
        (el perfil guardado y, si es el cargado, también el formulario)."""
        if job.name and job.name in self.profiles:
            prof = self.profiles[job.name]
            lines = clean_patterns(prof.get("ignore", ""))
            new = [p for p in patterns if p not in lines]
            if new:
                prof["ignore"] = "\n".join(lines + new)
                self.save_config()
                self.log(f"[{job.name}] Añadido al perfil: {', '.join(new)}",
                         "dim")
        if not job.name or job.name == self.selected_profile:
            self.add_ignore_patterns(patterns)
