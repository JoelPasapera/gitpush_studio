# -*- coding: utf-8 -*-
"""Publicar VARIOS proyectos a la vez en la misma cuenta de GitHub.

Una fila por proyecto: su carpeta, el nombre de su repositorio y si
además se quiere su TXT (marcado por defecto). Lo demás —rama, modo,
licencia, mensaje, lo que se ignora— se toma del formulario principal y
se aplica a todos, así que no hay que repetir nada.

El diálogo solo recoge datos y los comprueba. `core.batch.plan_batch` es
quien resuelve las URLs y crea en GitHub los repositorios que falten, en
un hilo; cuando el plan está listo, la publicación se lanza con la misma
cola que ya existe (`PublishController`).
"""

from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from ...core.batch import (BatchEntry, plan_batch, project_folders,
                           suggest_repo_name, validate_entries)
from ...core.fs import normalize_path
from ..base import WorkerDialog
from ..theme import (AMBER, BORDER, GREEN_T, MUTED, PANEL, PANEL2, RED, TEXT)
from ..widgets import (button, checkbox, entry, hint, option_menu, panel,
                       scrollable, segmented, separator, set_text, shorten)

MAX_ROWS = 50


class ProjectRow:
    """Una fila: carpeta · nombre del repositorio · TXT · quitar.

    No hereda de ningún widget a propósito: así ningún método suyo puede
    tapar uno de tkinter (`update`, `destroy`…).
    """

    def __init__(self, parent, fonts, entry_data, on_remove, on_changed):
        self.fonts = fonts
        self.data = entry_data
        self.on_changed = on_changed
        self.frame = ctk.CTkFrame(parent, fg_color=PANEL2, corner_radius=10,
                                  border_width=1, border_color=BORDER)
        self.frame.pack(fill="x", padx=16, pady=(0, 8))
        self.frame.grid_columnconfigure(1, weight=1)

        self.lbl_num = ctk.CTkLabel(self.frame, text="1.", font=fonts.small,
                                    text_color=MUTED, width=22)
        self.lbl_num.grid(row=0, column=0, sticky="w", padx=(10, 0),
                          pady=(10, 0))
        self.e_folder = entry(self.frame, fonts, "Carpeta del proyecto")
        self.e_folder.grid(row=0, column=1, sticky="ew", pady=(10, 0))
        self.e_folder.bind("<FocusOut>", self._folder_typed)
        button(self.frame, fonts, "Examinar…", self._browse, width=100,
               height=32, font=fonts.small
               ).grid(row=0, column=2, padx=(8, 10), pady=(10, 0))

        bottom = ctk.CTkFrame(self.frame, fg_color="transparent")
        bottom.grid(row=1, column=0, columnspan=3, sticky="ew",
                    padx=(10, 10), pady=(6, 10))
        bottom.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(bottom, text="repositorio:", font=fonts.small,
                     text_color=MUTED).grid(row=0, column=0, sticky="w")
        self.e_repo = entry(bottom, fonts, "nombre-del-repositorio")
        self.e_repo.configure(height=30)
        self.e_repo.grid(row=0, column=1, sticky="ew", padx=(8, 10))
        self.v_txt = ctk.BooleanVar(value=bool(entry_data.consolidate))
        self.chk_txt = checkbox(bottom, fonts, "📄 unir en un .txt",
                                self.v_txt, command=self._txt_toggled)
        self.chk_txt.grid(row=0, column=2, sticky="w", padx=(0, 10))
        button(bottom, fonts, "🗑", lambda: on_remove(self), width=36,
               height=30, kind="ghost", text_color=AMBER, font=fonts.small
               ).grid(row=0, column=3, sticky="e")

        set_text(self.e_folder, entry_data.folder)
        set_text(self.e_repo, entry_data.repo)

    # ------------------------------------------------------------------
    def _browse(self):
        chosen = filedialog.askdirectory(title="Carpeta del proyecto")
        if not chosen:
            return
        set_text(self.e_folder, str(Path(chosen)))
        self._suggest_repo()
        self.on_changed()

    def _folder_typed(self, _event=None):
        self._suggest_repo()
        self.on_changed()

    def _suggest_repo(self):
        """Propone el nombre solo si el usuario no ha escrito uno: lo que
        él haya puesto manda siempre."""
        if self.e_repo.get().strip():
            return
        name = suggest_repo_name(self.e_folder.get())
        if name:
            set_text(self.e_repo, name)

    def _txt_toggled(self):
        self.on_changed()

    def set_number(self, n):
        self.lbl_num.configure(text=f"{n}.")

    def set_txt(self, value):
        self.v_txt.set(bool(value))

    def wants_txt(self):
        return bool(self.v_txt.get())

    def collect(self):
        """Una fila NUEVA con lo que hay ahora mismo en los campos.

        Nueva a propósito: `resolve_entries` escribe la URL dentro de la
        fila, y si se devolviera siempre el mismo objeto, un segundo
        intento (tras corregir un nombre, por ejemplo) se encontraría la
        URL del intento anterior ya puesta, se la daría por buena y
        publicaría en el repositorio VIEJO mientras la consola muestra el
        nombre nuevo. Los campos son la única fuente de verdad.
        """
        return BatchEntry(folder=normalize_path(self.e_folder.get()),
                          repo=self.e_repo.get().strip(),
                          consolidate=self.wants_txt())

    def is_empty(self):
        return not (self.e_folder.get().strip() or self.e_repo.get().strip())

    def destroy(self):
        self.frame.destroy()


class BatchPublishDialog(WorkerDialog):
    TITLE = "Publicar varios proyectos"
    SIZE = (780, 720)
    MIN_SIZE = (680, 520)

    def __init__(self, app):
        super().__init__(app)
        f = self.fonts
        self.rows = []
        pad = {"padx": 22}
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(header, text="🚀🚀  Publicar varios proyectos",
                     font=f.title, text_color=TEXT
                     ).pack(anchor="w", pady=(18, 0), **pad)
        ctk.CTkLabel(header, text="Varias carpetas de golpe, cada una a su "
                                  "repositorio, todas en tu misma cuenta de "
                                  "GitHub.", font=f.small, text_color=MUTED
                     ).pack(anchor="w", pady=(2, 12), **pad)

        body = scrollable(self)
        body.grid(row=1, column=0, sticky="nsew")

        # ---- cuenta ---------------------------------------------------
        c1 = panel(body, f, "👤  Tu cuenta de GitHub")
        rowu = ctk.CTkFrame(c1, fg_color="transparent")
        rowu.pack(fill="x", padx=16, pady=(6, 0))
        rowu.grid_columnconfigure(1, weight=1)
        rowu.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(rowu, text="usuario:", font=f.small, text_color=MUTED
                     ).grid(row=0, column=0, sticky="w")
        self.e_owner = entry(rowu, f, "tu-usuario")
        self.e_owner.grid(row=0, column=1, sticky="ew", padx=(8, 12))
        ctk.CTkLabel(rowu, text="token:", font=f.small, text_color=MUTED
                     ).grid(row=0, column=2, sticky="w")
        self.e_token = entry(rowu, f, "ghp_… (opcional)", show="•")
        self.e_token.grid(row=0, column=3, sticky="ew", padx=(8, 8))
        button(rowu, f, "👁", lambda: self.toggle_token(self.e_token),
               width=42).grid(row=0, column=4)

        rowc = ctk.CTkFrame(c1, fg_color="transparent")
        rowc.pack(fill="x", padx=16, pady=(10, 0))
        self.v_create = ctk.BooleanVar(value=True)
        checkbox(rowc, f, "Crear en GitHub los repositorios que todavía no "
                          "existan", self.v_create).pack(side="left")
        self.om_vis = option_menu(rowc, f, ["Privados", "Públicos"],
                                  width=110)
        self.om_vis.set("Privados")
        self.om_vis.pack(side="left", padx=(12, 0))
        hint(c1, f, "Con tu token se detecta el usuario solo y se crean los "
                    "repositorios que falten; los que ya existan se "
                    "reutilizan sin tocar su contenido. Sin token, escribe "
                    "tu usuario y publica en repositorios que ya existan.",
             wraplength=680, pady=(8, 12))

        # ---- proyectos ------------------------------------------------
        c2 = panel(body, f, "📂  Proyectos a publicar", pady=(12, 0))
        self.list_frame = ctk.CTkFrame(c2, fg_color="transparent")
        self.list_frame.pack(fill="x", pady=(8, 0))

        rowa = ctk.CTkFrame(c2, fg_color="transparent")
        rowa.pack(fill="x", padx=16, pady=(2, 0))
        button(rowa, f, "➕  Añadir proyecto", self._add_empty, height=32,
               font=f.small).pack(side="left")
        button(rowa, f, "📂  Añadir las subcarpetas de…", self._add_children,
               height=32, font=f.small).pack(side="left", padx=(8, 0))
        self.lbl_count = ctk.CTkLabel(rowa, text="", font=f.small,
                                      text_color=MUTED)
        self.lbl_count.pack(side="right")

        rowt = ctk.CTkFrame(c2, fg_color="transparent")
        rowt.pack(fill="x", padx=16, pady=(10, 0))
        self.v_all_txt = ctk.BooleanVar(value=True)
        checkbox(rowt, f, "📄  Unir cada proyecto en un único .txt (marca o "
                          "desmarca todos)", self.v_all_txt,
                 command=self._toggle_all_txt).pack(side="left")
        hint(c2, f, "El .txt lleva el árbol del proyecto y el contenido de "
                    "sus archivos, se guarda dentro de la propia carpeta y "
                    "NUNCA se sube: su nombre se añade al .gitignore. Ojo, "
                    "contiene todo el código junto: guárdalo como guardarías "
                    "el proyecto.", wraplength=680, pady=(8, 12))

        # ---- opciones comunes ----------------------------------------
        c3 = panel(body, f, "⚙  Igual para todos", pady=(12, 0))
        rowb = ctk.CTkFrame(c3, fg_color="transparent")
        rowb.pack(fill="x", padx=16, pady=(6, 0))
        ctk.CTkLabel(rowb, text="rama:", font=f.small, text_color=MUTED
                     ).pack(side="left")
        self.e_branch = entry(rowb, f, "main")
        self.e_branch.configure(width=120, height=30)
        self.e_branch.pack(side="left", padx=(8, 16))
        self.seg_mode = segmented(rowb, f, ["Espejo", "Agregar / editar"],
                                  height=30, font=f.small)
        self.seg_mode.pack(side="left")
        self.lbl_common = ctk.CTkLabel(c3, text="", font=f.small,
                                       text_color=MUTED, justify="left",
                                       wraplength=680)
        self.lbl_common.pack(anchor="w", padx=16, pady=(10, 12))

        # ---- pie ------------------------------------------------------
        footer = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0)
        footer.grid(row=2, column=0, sticky="ew")
        separator(footer).pack(fill="x", side="top")
        self.lbl_status = ctk.CTkLabel(footer, text="", font=f.small,
                                       text_color=MUTED, justify="left",
                                       wraplength=700)
        self.lbl_status.pack(anchor="w", pady=(12, 0), **pad)
        rowf = ctk.CTkFrame(footer, fg_color="transparent")
        rowf.pack(fill="x", pady=(10, 16), **pad)
        rowf.grid_columnconfigure(0, weight=1)
        self.btn_go = button(rowf, f, "🚀  Publicar los proyectos",
                             self._publish, kind="primary", height=44)
        self.btn_go.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        button(rowf, f, "Cerrar", self.close, kind="ghost", width=110,
               height=44).grid(row=0, column=1)

        self._load_defaults()

    # ================================================================ #
    #  Estado inicial                                                  #
    # ================================================================ #
    def _load_defaults(self):
        app = self.app
        saved = app.config_data.get("batch") or {}
        token = app.connection_token()
        if token:
            self.e_token.insert(0, token)
        owner = saved.get("owner") or app.guess_owner()
        if owner:
            self.e_owner.insert(0, owner)
        self.v_create.set(bool(saved.get("create_missing", True)))
        self.om_vis.set("Privados" if saved.get("private", True)
                        else "Públicos")
        state = app.collect_state()
        set_text(self.e_branch, saved.get("branch") or state.get("branch")
                 or "main")
        mode = saved.get("mode") or state.get("mode") or "mirror"
        self.seg_mode.set("Espejo" if mode == "mirror" else "Agregar / editar")
        self._describe_common(state)

        proyectos = saved.get("projects") or []
        for p in proyectos[:MAX_ROWS]:
            self._add_row(BatchEntry(folder=p.get("folder", ""),
                                     repo=p.get("repo", ""),
                                     consolidate=bool(p.get("consolidate",
                                                            True))))
        if not self.rows:
            # La carpeta del formulario, para no empezar en blanco.
            folder = normalize_path(self.app.e_folder.get())
            self._add_row(BatchEntry(folder=folder,
                                     repo=suggest_repo_name(folder)))
            self._add_empty()
        self._changed()

    def _describe_common(self, state):
        licencia = state.get("license") or "ninguna"
        patrones = len([ln for ln in (state.get("ignore") or "").splitlines()
                        if ln.strip()])
        vista = "sí" if state.get("preview", True) else "no"
        self.lbl_common.configure(
            text=f"Del formulario principal: licencia {licencia} · "
                 f"{patrones} patrón(es) en el cuadro de ignorar · "
                 f"revisión de seguridad "
                 f"{'sí' if state.get('check', True) else 'no'} · vista "
                 f"previa {vista}. La vista previa pregunta una vez por "
                 "proyecto, de una en una.")

    # ================================================================ #
    #  Filas                                                           #
    # ================================================================ #
    def _add_row(self, entry_data):
        if len(self.rows) >= MAX_ROWS:
            return None
        row = ProjectRow(self.list_frame, self.fonts, entry_data,
                         self._remove_row, self._changed)
        self.rows.append(row)
        return row

    def _add_empty(self):
        if self._busy():
            return
        if self._add_row(BatchEntry()) is None:
            self.status(f"Máximo {MAX_ROWS} proyectos por tanda.", AMBER)
            return
        self._changed()

    def _add_children(self):
        if self._busy():
            return
        parent = filedialog.askdirectory(
            title="Carpeta que contiene tus proyectos")
        if not parent:
            return
        ya = {r.collect().folder_key() for r in self.rows if not r.is_empty()}
        candidatas = []
        repetidas = 0
        for folder in project_folders(parent):
            candidata = BatchEntry(folder=folder,
                                   repo=suggest_repo_name(folder),
                                   consolidate=bool(self.v_all_txt.get()))
            if candidata.folder_key() in ya:
                repetidas += 1
                continue
            ya.add(candidata.folder_key())
            candidatas.append(candidata)
        # Las filas vacías se quitan ANTES de añadir: si se quitaran
        # después, cada una desperdiciaría un hueco del máximo y se
        # descartaría un proyecto que en realidad cabía.
        if candidatas:
            for row in [r for r in self.rows if r.is_empty()]:
                self._drop(row)
        nuevas = descartadas = 0
        for candidata in candidatas:
            if self._add_row(candidata) is None:
                # Se llegó al máximo: hay que DECIRLO, no dejar fuera
                # proyectos en silencio y que el usuario crea que van.
                descartadas += 1
                continue
            nuevas += 1
        if not self.rows:                     # que nunca quede sin filas
            self._add_row(BatchEntry())
        self._changed()
        if descartadas:
            self.status(f"Añadidos {nuevas}, pero NO caben {descartadas} "
                        f"más: el máximo es {MAX_ROWS} por tanda. Publica "
                        "estos y repite con el resto.", AMBER)
        elif nuevas:
            extra = f" ({repetidas} ya estaban)" if repetidas else ""
            self.status(f"Añadidos {nuevas} proyectos de "
                        f"{Path(parent).name}{extra}.", GREEN_T)
        else:
            self.status("No encontré subcarpetas nuevas que añadir ahí.",
                        AMBER)

    def _busy(self):
        """Mientras se prepara el lote la lista no se toca: el hilo ya
        trabaja con una copia, así que editarla ahora solo conseguiría
        que lo publicado no fuera lo que se ve en pantalla."""
        if self.running:
            self.status("Espera: estoy preparando los repositorios en "
                        "GitHub.", AMBER)
            return True
        return False

    def _remove_row(self, row):
        if self._busy():
            return
        if len(self.rows) <= 1:
            # Siempre queda una fila: vaciarla es más claro que dejar la
            # lista sin nada y que el botón no explique por qué falla.
            set_text(row.e_folder, "")
            set_text(row.e_repo, "")
            self._changed()
            return
        self._drop(row)
        self._changed()

    def _drop(self, row):
        if row in self.rows:
            self.rows.remove(row)
            row.destroy()

    def _toggle_all_txt(self):
        if self._busy():
            self.v_all_txt.set(not self.v_all_txt.get())    # se deja como estaba
            return
        value = bool(self.v_all_txt.get())
        for row in self.rows:
            row.set_txt(value)
        self._changed()

    def _changed(self):
        """Renumera, actualiza el contador y el texto del botón."""
        usables = 0
        for i, row in enumerate(self.rows, 1):
            row.set_number(i)
            if not row.is_empty():
                usables += 1
        con_txt = sum(1 for r in self.rows
                      if not r.is_empty() and r.wants_txt())
        self.lbl_count.configure(
            text=f"{usables} proyecto(s) · {con_txt} con .txt")
        self.btn_go.configure(
            text=f"🚀  Publicar los {usables} proyectos" if usables != 1
            else "🚀  Publicar 1 proyecto")

    def _entries(self):
        """Filas con algo escrito, en el orden de la lista."""
        return [r.collect() for r in self.rows if not r.is_empty()]

    # ================================================================ #
    #  Publicar                                                        #
    # ================================================================ #
    def _remember(self, entries):
        self.app.config_data["batch"] = {
            "owner": self.e_owner.get().strip(),
            "create_missing": bool(self.v_create.get()),
            "private": self.om_vis.get() == "Privados",
            "branch": self.e_branch.get().strip() or "main",
            "mode": "mirror" if self.seg_mode.get() == "Espejo" else "addedit",
            "projects": [{"folder": e.folder, "repo": e.repo,
                          "consolidate": e.consolidate} for e in entries],
        }
        self.app.save_config()

    def close(self):
        # Al cerrar se guarda la lista: volver a abrir no obliga a
        # escribirlo todo otra vez.
        if self.running:
            # Se estaba preparando: el hilo sigue y puede crear
            # repositorios. Queda dicho en la consola de la aplicación,
            # que no se va con la ventana.
            self.app.log("✋ Cerraste la ventana mientras preparaba el "
                         "lote: no se publica nada, pero los repositorios "
                         "que ya se hubieran creado en GitHub siguen ahí "
                         "(vacíos). Los verás anotados aquí arriba.", "warn")
        else:
            try:
                self._remember(self._entries())
            except Exception:                                # noqa: BLE001
                pass
        super().close()

    def _publish(self):
        if self.running:
            return
        if self.app.controller_running():
            self.status("Ya hay una publicación en marcha.", AMBER)
            return
        entries = self._entries()
        problems = validate_entries(entries)
        if problems:
            self.status("✖ " + "\n✖ ".join(problems[:6]), RED)
            return
        self._remember(entries)
        token = self.e_token.get().strip()
        owner = self.e_owner.get().strip()
        create = bool(self.v_create.get()) and bool(token)
        if not token and not owner:
            self.status("Sin token hace falta tu nombre de usuario de "
                        "GitHub.", RED)
            return
        state = dict(self.app.collect_state())
        state["branch"] = self.e_branch.get().strip() or "main"
        state["mode"] = ("mirror" if self.seg_mode.get() == "Espejo"
                         else "addedit")
        self.running = True
        self.btn_go.configure(state="disabled", text="Preparando…")
        self.status(f"Preparando {len(entries)} proyectos en GitHub…", MUTED)
        self.run_in_thread(self._work, entries, state, token, owner, create,
                           self.om_vis.get() == "Privados")

    def _work(self, entries, state, token, owner, create, private):
        def progress(label, i, total):
            self.post("prep", label, i, total)

        def created(name, url):
            # A la cola de la APLICACIÓN, no a la del diálogo: así queda
            # registrado aunque el usuario cierre esta ventana justo
            # ahora. Es el mismo camino que usan los hilos de publicación.
            self.app.q.put(("job_log",
                            f"➕ Repositorio creado en GitHub: {name}  {url}",
                            "ok"))

        plan = plan_batch(entries, state, token=token, owner=owner,
                          create_missing=create, private=private,
                          on_progress=progress, on_created=created)
        self.post("plan", plan)

    def handle(self, m):
        if m[0] == "prep":
            _, label, i, total = m
            self.status(f"Preparando en GitHub {i}/{total}: "
                        f"{shorten(label, 40)}…", MUTED)
        elif m[0] == "err":
            self._reset(" ✖ " + m[1])
        elif m[0] == "plan":
            self._planned(m[1])

    def _reset(self, text="", color=RED):
        self.running = False
        self.btn_go.configure(state="normal")
        self._changed()
        if text:
            self.status(text, color)

    def _planned(self, plan):
        if not plan.ok:
            self._reset("✖ " + "\n✖ ".join(plan.problems[:6]))
            return
        self.running = False
        # La ventana solo se cierra si la publicación ARRANCÓ. Devuelve
        # False si el usuario dice «no» al aviso del push forzado o si
        # algo más empezó a publicar mientras preparábamos: cerrar ahí
        # dejaría al usuario creyendo que subió, sin haber subido nada.
        try:
            arrancada = self.app.start_batch(plan)
        except Exception as e:                               # noqa: BLE001
            self._reset(f"✖ No se pudo lanzar la publicación: {e}")
            return
        if not arrancada:
            self._reset("La publicación no arrancó. Revisa la consola de "
                        "la aplicación; tus proyectos siguen aquí.", AMBER)
            return
        self.destroy()
