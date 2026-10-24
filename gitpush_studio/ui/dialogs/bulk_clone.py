# -*- coding: utf-8 -*-
"""Clonar todos los repositorios de un perfil, en paralelo y en modo
ligero, con TXT del perfil y/o por repositorio al terminar."""

from pathlib import Path

import customtkinter as ctk

from ...core.errors import StepError
from ...core.desktop import open_path
from ...core.fs import fmt_size, normalize_path
from ...core.git import (MODE_CLONE, MODE_SNAPSHOT, ON_EXISTS_SKIP,
                         ON_EXISTS_UPDATE, BulkCloner)
from ...core.github import list_user_repos
from ...core.pack import consolidate_each, consolidate_many
from ...core.urls import parse_username
from ..base import WorkerDialog
from ..theme import AMBER, BLUE, GREEN_T, MUTED, PANEL, RED, TEXT
from ..widgets import (browse_folder_into, button, checkbox, default_downloads,
                       entry, hint, option_menu, panel, progress_bar,
                       scrollable, segmented, separator)


class BulkCloneDialog(WorkerDialog):
    TITLE = "Clonar un perfil completo de GitHub"
    SIZE = (620, 640)
    MIN_SIZE = (560, 480)

    def __init__(self, app):
        super().__init__(app)
        f = self.fonts
        self.cloner = BulkCloner()
        self.base_path = ""
        self.n_ok = self.n_skip = self.n_fail = 0
        pad = {"padx": 22}

        # Estructura: cabecera fija · contenido desplazable · pie fijo
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(header, text="⬇⬇  Clonar perfil completo", font=f.title,
                     text_color=TEXT).pack(anchor="w", pady=(18, 0), **pad)
        ctk.CTkLabel(header, text="Descarga todos los repositorios de un "
                                  "perfil, en paralelo y en modo ligero.",
                     font=f.small, text_color=MUTED
                     ).pack(anchor="w", pady=(2, 12), **pad)

        body = scrollable(self)
        body.grid(row=1, column=0, sticky="nsew")

        c1 = panel(body, f, "👤  Perfil de GitHub")
        self.e_user = entry(c1, f, "usuario  o  https://github.com/usuario")
        self.e_user.pack(fill="x", padx=16, pady=(6, 0))
        rowt = ctk.CTkFrame(c1, fg_color="transparent")
        rowt.pack(fill="x", padx=16, pady=(8, 0))
        rowt.grid_columnconfigure(0, weight=1)
        self.e_token = entry(rowt, f, "Token (opcional)", show="•")
        self.e_token.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        button(rowt, f, "👁", lambda: self.toggle_token(self.e_token),
               width=42).grid(row=0, column=1)
        hint(c1, f, "El token es opcional: sube el límite de peticiones y, "
                    "si el perfil es el tuyo, incluye también tus "
                    "repositorios privados.", wraplength=540, pady=(6, 12))

        c2 = panel(body, f, "📁  Destino y opciones", pady=(12, 0))
        rowd = ctk.CTkFrame(c2, fg_color="transparent")
        rowd.pack(fill="x", padx=16, pady=(6, 0))
        rowd.grid_columnconfigure(0, weight=1)
        self.e_dest = entry(rowd, f, str(Path.home() / "Downloads"))
        self.e_dest.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        button(rowd, f, "Examinar…", lambda: browse_folder_into(
            self.e_dest, "¿Dónde guardar la carpeta del perfil?"), width=110
               ).grid(row=0, column=1)
        self.e_dest.insert(0, default_downloads(app.config_data))

        rowm = ctk.CTkFrame(c2, fg_color="transparent")
        rowm.pack(fill="x", padx=16, pady=(10, 0))
        ctk.CTkLabel(rowm, text="Cómo descargar:", font=f.small,
                     text_color=MUTED).pack(side="left")
        # Sin `command` todavía: lo conecta el final del constructor,
        # cuando ya existen los widgets que el manejador toca.
        self.seg_mode = segmented(rowm, f, ["Clonar (con git)",
                                            "Instantánea (.tar.gz)"],
                                  height=30, font=f.small)
        self.seg_mode.set("Clonar (con git)")
        self.seg_mode.pack(side="left", padx=(8, 0))
        self.lbl_mode = ctk.CTkLabel(c2, text="", font=f.small,
                                     text_color=MUTED, justify="left",
                                     wraplength=540)
        self.lbl_mode.pack(anchor="w", padx=16, pady=(8, 0))

        rowo = ctk.CTkFrame(c2, fg_color="transparent")
        rowo.pack(fill="x", padx=16, pady=(8, 0))
        self.v_shallow = ctk.BooleanVar(value=True)
        self.v_forks = ctk.BooleanVar(value=True)
        self.v_pack = ctk.BooleanVar(value=True)
        self.v_each = ctk.BooleanVar(value=False)
        self.chk_shallow = checkbox(
            rowo, f, "Clonación ligera (solo la última versión, mucho más "
                     "rápida)", self.v_shallow)
        self.chk_shallow.pack(anchor="w")
        checkbox(rowo, f, "Omitir forks (copias de proyectos ajenos)",
                 self.v_forks).pack(anchor="w", pady=(6, 0))
        checkbox(rowo, f, "Al terminar, unir todo el perfil en un solo TXT",
                 self.v_pack).pack(anchor="w", pady=(6, 0))
        checkbox(rowo, f, "Además, un TXT dentro de la carpeta de cada "
                          "repositorio", self.v_each
                 ).pack(anchor="w", pady=(6, 0))
        rowe = ctk.CTkFrame(c2, fg_color="transparent")
        rowe.pack(fill="x", padx=16, pady=(10, 0))
        ctk.CTkLabel(rowe, text="Si ya está descargado:", font=f.small,
                     text_color=MUTED).pack(side="left")
        self.seg_exists = segmented(rowe, f, ["Omitir", "Actualizar"],
                                    height=30, font=f.small)
        self.seg_exists.set("Omitir")
        self.seg_exists.pack(side="left", padx=(8, 0))

        roww = ctk.CTkFrame(c2, fg_color="transparent")
        roww.pack(fill="x", padx=16, pady=(8, 0))
        ctk.CTkLabel(roww, text="Descargas a la vez:", font=f.small,
                     text_color=MUTED).pack(side="left")
        self.om_workers = option_menu(roww, f, ["2", "4", "6", "8", "12",
                                                "16"], width=70)
        self.om_workers.set("6")
        self.om_workers.pack(side="left", padx=(8, 0))
        hint(c2, f, "Todo se guarda en una subcarpeta con el nombre del "
                    "usuario. Lo ya clonado se omite, así puedes repetir la "
                    "descarga sin rehacer trabajo. El TXT del perfil queda "
                    "como usuario/usuario.txt; los individuales, como "
                    "usuario/repo/repo.txt.\n"
                    "Sobre «descargas a la vez»: más de 6 u 8 rara vez "
                    "acelera, porque el límite lo pone tu conexión y tu "
                    "disco, no el número de descargas; pasarse hace que "
                    "compitan entre ellas y que GitHub pida bajar el ritmo. "
                    "Lo que sí acelera de verdad es la clonación ligera, que "
                    "evita descargar el historial entero.",
             wraplength=540, pady=(8, 12))

        footer = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0)
        footer.grid(row=2, column=0, sticky="ew")
        separator(footer).pack(fill="x", side="top")
        self.pbar = progress_bar(footer)
        self.pbar.pack(fill="x", pady=(12, 0), **pad)
        self.lbl_counts = ctk.CTkLabel(footer, text="", font=f.small,
                                       text_color=MUTED)
        self.lbl_counts.pack(anchor="w", pady=(6, 0), **pad)
        self.lbl_status = ctk.CTkLabel(footer, text="", font=f.small,
                                       text_color=MUTED, justify="left",
                                       wraplength=560)
        self.lbl_status.pack(anchor="w", pady=(2, 0), **pad)

        rowb = ctk.CTkFrame(footer, fg_color="transparent")
        rowb.pack(fill="x", pady=(12, 16), **pad)
        rowb.grid_columnconfigure(0, weight=1)
        self.btn_go = button(rowb, f, "Descargar todo", self._start,
                             kind="primary", height=42)
        self.btn_go.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.btn_stop = button(rowb, f, "Detener", self._stop_now,
                               kind="ghost", width=100, height=42,
                               text_color=AMBER, state="disabled")
        self.btn_stop.grid(row=0, column=1, padx=(0, 10))
        self.btn_folder = button(rowb, f, "Abrir carpeta",
                                 lambda: open_path(self.base_path),
                                 kind="ghost", width=130, height=42,
                                 text_color=BLUE, state="disabled")
        self.btn_folder.grid(row=0, column=2, padx=(0, 10))
        button(rowb, f, "Cerrar", self.close, kind="ghost", width=90,
               height=42).grid(row=0, column=3)

        saved = app.saved_token()
        if saved:
            self.e_token.insert(0, saved)
        self.seg_mode.configure(command=self._mode_changed)
        self.seg_exists.configure(command=self._mode_changed)
        self._mode_changed()

    # ------------------------------------------------------------------
    def close(self):
        self.cloner.stop()
        self.destroy()

    # ------------------------------------------------------------------
    def _mode(self):
        return MODE_SNAPSHOT if "Instant" in self.seg_mode.get() \
            else MODE_CLONE

    def _on_exists(self):
        return ON_EXISTS_UPDATE if "Actualizar" in self.seg_exists.get() \
            else ON_EXISTS_SKIP

    def _set_mode_enabled(self, enabled):
        """No todas las versiones de customtkinter admiten `state` en el
        selector; si no puede, tampoco pasa nada grave."""
        try:
            self.seg_mode.configure(state="normal" if enabled else "disabled")
        except Exception:                                   # noqa: BLE001
            pass

    def _mode_changed(self, _value=None):
        """Las dos opciones y su precio, dicho sin rodeos."""
        if self._mode() == MODE_SNAPSHOT:
            self.lbl_mode.configure(
                text="Baja el comprimido de cada repositorio y extrae solo "
                     "los archivos. Mucho más rápido y ocupa bastante menos, "
                     "pero NO son repositorios: sin historial y sin remoto, "
                     "así que desde esas carpetas no podrás publicar, traer "
                     "cambios ni ver versiones. Ideal si vas a leerlos o a "
                     "consolidarlos en un TXT.")
            self.chk_shallow.configure(state="disabled")
        else:
            self.lbl_mode.configure(
                text="Cada repositorio queda como una carpeta de git "
                     "completa: podrás publicar, traer cambios y ver "
                     "versiones desde ella. Es lo de siempre y lo más "
                     "flexible; también lo más lento, sobre todo en Windows.")
            self.chk_shallow.configure(state="normal")
        if self._on_exists() == ON_EXISTS_UPDATE:
            self.lbl_mode.configure(
                text=self.lbl_mode.cget("text") + "\n\nLo ya descargado se "
                     "pone al día trayendo solo lo que cambió (o nada, si no "
                     "cambió). Nunca se pisa tu trabajo: una carpeta con "
                     "cambios sin guardar o con commits propios se deja como "
                     "está y se te dice por qué.")

    def _stop_now(self):
        self.cloner.stop()
        self.btn_stop.configure(state="disabled")
        self.status("Deteniendo… se cancelan las descargas en curso.", AMBER)

    def _start(self):
        if self.running:
            return
        username = parse_username(self.e_user.get())
        if not username:
            self.status("Escribe un usuario válido de GitHub (o pega el "
                        "enlace de su perfil).", RED)
            return
        dest = normalize_path(self.e_dest.get())
        try:
            Path(dest).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.status(f"No puedo usar esa carpeta destino: {e}", RED)
            return
        self.running = True
        self.cloner = BulkCloner(workers=int(self.om_workers.get()))
        self.n_ok = self.n_skip = self.n_fail = 0
        self.n_update = self.n_uptodate = 0
        self.pbar.set(0)
        self.lbl_counts.configure(text="")
        self.btn_go.configure(state="disabled", text="Descargando…")
        self._set_mode_enabled(False)
        self.btn_stop.configure(state="normal")
        self.btn_folder.configure(state="disabled")
        self.status("Buscando los repositorios del perfil…", MUTED)
        self.app.log("─" * 60, "dim")
        self.app.log(
            f"⬇⬇ Descargando el perfil «{username}» "
            + ("como instantáneas (.tar.gz, sin historial)…"
               if self._mode() == MODE_SNAPSHOT else "clonando con git…"),
            "out")
        self.run_in_thread(
            self._work, username, self.e_token.get().strip(), dest,
            bool(self.v_shallow.get()), bool(self.v_forks.get()),
            bool(self.v_pack.get()), bool(self.v_each.get()),
            self.app.ignore_lines(), self._mode(), self._on_exists())

    def _work(self, username, token, dest, shallow, skip_forks, do_pack,
              do_each, patterns, mode, on_exists):
        repos = list_user_repos(username, token,
                                on_note=lambda t: self.post("nota", t))
        total_before = len(repos)
        if skip_forks:
            repos = [r for r in repos if not r["fork"]]
        if not repos:
            raise StepError("Ese perfil no tiene repositorios para descargar"
                            + (" (todos eran forks)." if total_before
                               else "."))
        base = Path(dest) / username
        self.post("plan", len(repos), sum(r["size_kb"] for r in repos),
                  str(base))
        summary = self.cloner.run(repos, base, shallow, token,
                                  on_event=lambda k, p: self.post(k, p),
                                  mode=mode, on_exists=on_exists)

        pack_stats, out, each_stats = None, "", None
        dirs = [base / r["name"] for r in repos if (base / r["name"]).is_dir()]
        # El TXT del perfil se genera PRIMERO y excluye los TXT individuales
        # (de esta u otra ejecución) para no duplicar contenido.
        if do_pack and not self.cloner.stopped and dirs:
            self.post("st", "Uniendo todo el perfil en un solo TXT…")
            out = str(base / f"{username}.txt")
            no_txt = list(patterns) + [f"{d.name}.txt" for d in dirs]
            pack_stats = consolidate_many(username, dirs, out, no_txt)
        if do_each and not self.cloner.stopped and dirs:
            self.post("st", "Generando un TXT dentro de cada repositorio…")
            each_stats = consolidate_each(
                dirs, patterns, on_progress=lambda n, i, t: self.post(
                    "st", f"TXT individual {i}/{t}: {n}…"))
        self.post("bulk_done", str(base), summary, pack_stats, out,
                  each_stats)

    # ------------------------------------------------------------------
    def _update_counts(self):
        partes = [f"✓ {self.n_ok} descargados"]
        if self.n_update:
            partes.append(f"⟳ {self.n_update} actualizados")
        if self.n_uptodate:
            partes.append(f"= {self.n_uptodate} ya al día")
        partes.append(f"↷ {self.n_skip} omitidos")
        partes.append(f"✖ {self.n_fail} fallidos")
        self.lbl_counts.configure(text="   ·   ".join(partes))

    def _finish_buttons(self):
        self.running = False
        self.btn_go.configure(state="normal", text="Descargar todo")
        self.btn_stop.configure(state="disabled")
        self._set_mode_enabled(True)

    def handle(self, m):
        kind = m[0]
        if kind == "st":
            self.status(m[1], MUTED)
        elif kind == "nota":
            self.app.log("  " + m[1], "dim")
        elif kind == "plan":
            _, n, est_kb, base = m
            self.base_path = base
            self.status(f"{n} repositorios encontrados "
                        f"(~{fmt_size(est_kb * 1024)} en GitHub con "
                        "historial completo).", MUTED)
            self.app.log(f"  {n} repositorios por descargar.", "dim")
        elif kind == "start":
            self.status(f"Clonando: {m[1]}…", MUTED)
        elif kind == "retry":
            nombre, espera, error = m[1]
            self.status(f"{nombre}: reintentando en {espera} s…", AMBER)
            self.app.log(f"  ↻ {nombre}: {error} — reintento en {espera} s.",
                         "warn")
        elif kind == "slow":
            self.status(f"GitHub pidió bajar el ritmo: pausa de {m[1]} s "
                        "antes de seguir.", AMBER)
            self.app.log(f"  ⏸ GitHub pidió bajar el ritmo; pauso {m[1]} s "
                         "y continúo con menos prisa.", "warn")
        elif kind == "done":
            name, st, secs, err, done, total = m[1]
            self.pbar.set(done / max(1, total))
            if st == "ok":
                self.n_ok += 1
                self.app.log(f"  ✓ {name} ({secs:.1f} s)", "dim")
            elif st == "update":
                self.n_update += 1
                self.app.log(f"  ⟳ {name} actualizado ({secs:.1f} s)", "dim")
            elif st == "uptodate":
                self.n_uptodate += 1
                self.app.log(f"  = {name} ya estaba al día", "dim")
            elif st == "skip":
                self.n_skip += 1
                self.app.log(f"  ↷ {name} ({err or 'ya existía'})", "dim")
            elif st == "fail":
                self.n_fail += 1
                self.app.log(f"  ✖ {name}: {err}", "err")
            self._update_counts()
        elif kind == "err":
            self._finish_buttons()
            self.status("✖ " + m[1], RED)
            self.app.log("✖ " + m[1], "err")
        elif kind == "bulk_done":
            self._bulk_done(*m[1:])

    def _bulk_done(self, base, s, ps, out, es):
        self._finish_buttons()
        self.base_path = base
        self.app.config_data["clone_dest"] = str(Path(base).parent)
        self.app.save_config()
        self.btn_folder.configure(state="normal")
        self.pbar.set(1)
        stopped = s.get("cancel", 0) > 0
        txt_line = ""
        if ps:
            txt_line = (f"\nTXT del perfil: {ps['files']} archivos de "
                        f"{ps['repos']} repos, {fmt_size(ps['bytes'])} en "
                        f"{ps['seconds']:.1f} s"
                        + ("  ·  ⚠ truncado" if ps.get("truncated") else ""))
        if es:
            txt_line += (f"\nTXT individuales: {es['count']} archivos, "
                         f"{fmt_size(es['bytes'])} en {es['seconds']:.1f} s"
                         + (f"  ·  ⚠ {len(es['failed'])} fallidos"
                            if es["failed"] else ""))
        ok = not s["fails"] and not stopped
        head = "✋ Detenido a medias." if stopped else "✔ ¡Perfil descargado!"
        cuentas = f"{s['ok']} descargados"
        if s.get("update"):
            cuentas += f", {s['update']} actualizados"
        if s.get("uptodate"):
            cuentas += f", {s['uptodate']} ya al día"
        cuentas += f", {s['skip']} omitidos, {s['fail']} fallidos"
        self.status(f"{head}  {cuentas} en {s['seconds']:.1f} s."
                    f"{txt_line}\n{base}", GREEN_T if ok else AMBER)
        self.app.log(f"✔ Perfil completo: {cuentas} en "
                     f"{s['seconds']:.1f} s → {base}",
                     "ok" if not s["fails"] else "warn")
        if ps and out:
            self.app.log(f"✔ TXT del perfil: {out} ({fmt_size(ps['bytes'])}).",
                         "ok")
        if es:
            self.app.log(f"✔ {es['count']} TXT individuales (uno por "
                         f"repositorio, {fmt_size(es['bytes'])} en total).",
                         "ok")
            for x in es["failed"]:
                self.app.log(f"  ✖ {x}", "err")
