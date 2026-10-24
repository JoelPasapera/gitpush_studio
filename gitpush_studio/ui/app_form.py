# -*- coding: utf-8 -*-
"""El formulario principal: construirlo, leerlo y rellenarlo (estado,
licencia, modo, patrones de ignorar y datos del proyecto actual).

Mezcla de `GitPushStudio`: usa sus widgets y su estado (`self.…`);
no tiene constructor propio."""


import customtkinter as ctk

from ..core import config as cfg
from ..core.licenses import (CATALOG as LICENSES, DEFAULT as LICENSE_DEFAULT,
                             NAMES as LICENSE_NAMES, is_bundled)
from ..core.urls import parse_owner_repo, repo_web_url
from .theme import BG, BORDER, GREEN, MUTED, PANEL2, TEXT
from .widgets import (browse_folder_into, button, card, checkbox, entry, hint,
                      option_menu, segmented, set_text)

DEFAULT_IGNORE = "node_modules/"
DEFAULT_MESSAGE = "Actualizar proyecto completo"


class FormMixin:

    def _build_form(self, form):
        f = self.fonts
        c = card(form, f, "📁  Carpeta del proyecto")
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(6, 0))
        row.grid_columnconfigure(0, weight=1)
        self.e_folder = entry(row, f, r"C:\Users\PC\Proyectos\mi-app")
        self.e_folder.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        button(row, f, "Examinar…", lambda: browse_folder_into(
            self.e_folder, "Selecciona la carpeta del proyecto"), width=110
               ).grid(row=0, column=1)
        hint(c, f, "La carpeta local que quieres subir. También puedes pegar "
                   "rutas estilo Git Bash (/c/Users/…). ¿Aún no la tienes? "
                   "Usa «⬇ Clonar repositorio…» en la barra lateral.")

        c = card(form, f, "👤  Identidad Git")
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(6, 0))
        row.grid_columnconfigure((0, 1), weight=1)
        self.e_user = entry(row, f, "Nombre de usuario")
        self.e_user.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.e_email = entry(row, f, "Correo")
        self.e_email.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.v_local = ctk.BooleanVar(value=True)
        checkbox(c, f, "Aplicar la identidad solo a este repositorio "
                       "(recomendado)", self.v_local
                 ).pack(anchor="w", padx=16, pady=(8, 0))
        hint(c, f, "Si dejas los campos vacíos se usará la identidad ya "
                   "configurada en tu equipo.")

        c = card(form, f, "🔗  Repositorio de GitHub")
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(6, 0))
        row.grid_columnconfigure(0, weight=3)
        row.grid_columnconfigure(1, weight=1)
        self.e_url = entry(row, f, "https://github.com/usuario/mi-repositorio.git")
        self.e_url.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.e_branch = entry(row, f, "main")
        self.e_branch.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.e_branch.insert(0, "main")
        row2 = ctk.CTkFrame(c, fg_color="transparent")
        row2.pack(fill="x", padx=16, pady=(8, 0))
        button(row2, f, "➕  Crear repositorio nuevo…", self.open_create_repo,
               height=32, width=220).pack(side="left")
        button(row2, f, "🌍  Publicar como web…", self.open_pages, height=32,
               width=200).pack(side="left", padx=(8, 0))
        button(row2, f, "🌿  Ramas…", self.open_branches, height=32,
               width=120).pack(side="left", padx=(8, 0))
        hint(c, f, "«➕» crea el repositorio con tu token y rellena la URL "
                   "sola. «🌍» activa GitHub Pages para que tu proyecto sea "
                   "una página web con enlace propio (requiere repositorio "
                   "público y haber publicado antes). «🌿» enseña las ramas "
                   "que hay en GitHub para elegir el destino; a la derecha "
                   "de la URL va la rama (normalmente «main»).")

        c = card(form, f, "🙈  Archivos y carpetas a ignorar (.gitignore)")
        self.t_ignore = ctk.CTkTextbox(
            c, height=84, font=f.mono, corner_radius=8, fg_color=BG,
            border_color=BORDER, border_width=1, text_color=TEXT, wrap="none",
            scrollbar_button_color=BORDER)
        self.t_ignore.pack(fill="x", padx=16, pady=(6, 0))
        self.t_ignore.insert("1.0", DEFAULT_IGNORE + "\n")
        hint(c, f, "Un patrón por línea: un nombre de carpeta («app»), uno "
                   "de archivo («notas.md») o un comodín («*.log»). Se "
                   "añaden al .gitignore del proyecto sin borrar lo que ya "
                   "tenga, y lo que ya se estuviera subiendo deja de "
                   "subirse (el archivo sigue en tu carpeta; en modo espejo "
                   "desaparecerá de GitHub, y lo verás en la vista previa "
                   "antes de confirmar). La revisión de seguridad también "
                   "puede añadir líneas aquí, y el consolidador a TXT "
                   "respeta esta misma lista.")

        c = card(form, f, "⚖️  Licencia")
        rowl = ctk.CTkFrame(c, fg_color="transparent")
        rowl.pack(fill="x", padx=16, pady=(6, 0))
        rowl.grid_columnconfigure(1, weight=1)
        self.om_license = option_menu(rowl, f, self._license_labels(),
                                      command=self._license_changed,
                                      width=230, height=32)
        self.om_license.set(self._license_label(LICENSE_DEFAULT))
        self.om_license.grid(row=0, column=0, sticky="w")
        self.e_owner = entry(rowl, f, "A nombre de (opcional)")
        self.e_owner.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self.lbl_license = ctk.CTkLabel(c, text="", font=f.small,
                                        text_color=MUTED, justify="left",
                                        wraplength=640)
        self.lbl_license.pack(anchor="w", padx=16, pady=(8, 0))
        hint(c, f, "Se añade un archivo LICENSE solo si el proyecto no tiene "
                   "ninguno: nunca se toca el que ya exista. Si dejas vacío "
                   "«a nombre de», se usa tu nombre de «Identidad Git» o el "
                   "usuario de la URL. Apache 2.0, MIT y GPL 3.0 vienen "
                   "incluidas; las demás se descargan de GitHub la primera "
                   "vez y quedan guardadas.")
        self._license_changed()

        c = card(form, f, "📝  Mensaje del commit")
        self.e_msg = entry(c, f, DEFAULT_MESSAGE)
        self.e_msg.pack(fill="x", padx=16, pady=(6, 0))
        self.e_msg.insert(0, DEFAULT_MESSAGE)
        self.v_stamp = ctk.BooleanVar(value=False)
        checkbox(c, f, "Añadir fecha y hora al mensaje", self.v_stamp
                 ).pack(anchor="w", padx=16, pady=(8, 0))
        self.v_auto_msg = ctk.BooleanVar(value=False)
        checkbox(c, f, "Describir los cambios automáticamente (p. ej. «Añade "
                       "2 imágenes y actualiza 3 archivos en src/»)",
                 self.v_auto_msg).pack(anchor="w", padx=16, pady=(8, 12))

        c = card(form, f, "🤖  Auto-publicación")
        rowa = ctk.CTkFrame(c, fg_color="transparent")
        rowa.pack(fill="x", padx=16, pady=(6, 0))
        self.v_auto = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(rowa, text="Vigilar la carpeta y publicar sola",
                      variable=self.v_auto, font=f.body, text_color=TEXT,
                      progress_color=GREEN, button_color=TEXT,
                      button_hover_color="#ffffff", fg_color=PANEL2,
                      command=self.toggle_auto).pack(side="left")
        ctk.CTkLabel(rowa, text="   Comprobar cada", font=f.small,
                     text_color=MUTED).pack(side="left")
        self.om_interval = option_menu(rowa, f, ["2 min", "5 min", "10 min",
                                                 "30 min"],
                                       command=self._interval_changed)
        self.om_interval.set("10 min")
        self.om_interval.pack(side="left", padx=(8, 0))
        hint(c, f, "Si hubo cambios y ya dejaste de guardar, publica sola "
                   "(con la revisión de seguridad incluida: si encuentra "
                   "algo, se pausa y te avisa en vez de subir). Por "
                   "seguridad empieza apagada cada vez que abres la app.")

        c = card(form, f, "🔀  Modo de publicación")
        self.seg_mode = segmented(c, f, ["Agregar / editar", "Espejo"],
                                  command=self._mode_changed)
        self.seg_mode.set("Agregar / editar")
        self.seg_mode.pack(anchor="w", padx=16, pady=(6, 0))
        self.lbl_mode = ctk.CTkLabel(c, text="", font=f.small, text_color=MUTED,
                                     justify="left", wraplength=640)
        self.lbl_mode.pack(anchor="w", padx=16, pady=(8, 12))

        c = card(form, f, "⚙️  Opciones")
        self.v_force = ctk.BooleanVar(value=False)
        self.chk_force = checkbox(c, f, "Push forzado (-f): sobrescribe el "
                                        "historial del repositorio remoto",
                                  self.v_force, danger=True)
        self.chk_force.pack(anchor="w", padx=16, pady=(6, 0))
        self.v_check = ctk.BooleanVar(value=True)
        checkbox(c, f, "Revisión de seguridad antes de publicar (archivos "
                       "grandes y secretos)", self.v_check
                 ).pack(anchor="w", padx=16, pady=(8, 0))
        self.v_preview = ctk.BooleanVar(value=True)
        checkbox(c, f, "Vista previa antes de publicar (enseña qué cambiará "
                       "en GitHub)", self.v_preview
                 ).pack(anchor="w", padx=16, pady=(8, 0))
        self.v_use_token = ctk.BooleanVar(value=True)
        checkbox(c, f, "Conectarme con mi token guardado (evita que GitHub "
                       "pregunte la cuenta cada vez)", self.v_use_token
                 ).pack(anchor="w", padx=16, pady=(8, 0))
        hint(c, f, "El push forzado reemplaza lo que haya en GitHub por tu "
                   "versión local: úsalo solo si sabes lo que haces. Si no "
                   "has guardado ningún token, la última opción no hace "
                   "nada y se usa tu sesión de Git de siempre.")
        self._mode_changed()

    @staticmethod
    def _license_labels():
        return ["Sin licencia"] + [f"{name}" for _, name, _ in LICENSES]

    @staticmethod
    def _license_label(key):
        return LICENSE_NAMES.get(key, "Sin licencia")

    def _license_key(self):
        etiqueta = self.om_license.get()
        for key, name, _ in LICENSES:
            if name == etiqueta:
                return key
        return ""

    def _license_changed(self, _value=None):
        key = self._license_key()
        if not key:
            self.lbl_license.configure(
                text="No se añadirá ningún archivo LICENSE.")
            return
        descripcion = next(d for k, _, d in LICENSES if k == key)
        extra = "" if is_bundled(key) else "  ·  se descarga de GitHub"
        self.lbl_license.configure(text=descripcion.capitalize() + "." + extra)

    def _is_addedit(self):
        return "Agregar" in self.seg_mode.get()

    def _mode_changed(self, _value=None):
        """El push forzado borraría lo que hay en GitHub: en modo
        «agregar/editar» no está disponible."""
        if self._is_addedit():
            self.lbl_mode.configure(
                text="Sube lo nuevo y actualiza lo que coincida por nombre y "
                     "ruta. Lo que esté en GitHub y no tengas en tu carpeta "
                     "se queda intacto: nunca borra nada.")
            if self.v_force.get():
                self.v_force.set(False)
                self.log("El push forzado se desactiva en modo "
                         "«agregar/editar»: borraría lo que hay en GitHub.",
                         "dim")
            self.chk_force.configure(state="disabled")
        else:
            self.lbl_mode.configure(
                text="GitHub queda idéntico a tu carpeta. Si borraste algo en "
                     "local, también se borra en GitHub. El historial de "
                     "commits anterior se conserva.")
            self.chk_force.configure(state="normal")

    def collect_state(self):
        return {
            "folder": self.e_folder.get().strip().strip('"'),
            "user": self.e_user.get().strip(),
            "email": self.e_email.get().strip(),
            "url": self.e_url.get().strip(),
            "branch": self.e_branch.get().strip() or "main",
            "ignore": self.t_ignore.get("1.0", "end").rstrip("\n"),
            "license": self._license_key(),
            "license_owner": self.e_owner.get().strip(),
            "message": self.e_msg.get().strip(),
            "local_identity": bool(self.v_local.get()),
            "stamp": bool(self.v_stamp.get()),
            "auto_message": bool(self.v_auto_msg.get()),
            "force": bool(self.v_force.get()),
            "check": bool(self.v_check.get()),
            "mode": "addedit" if self._is_addedit() else "mirror",
            "preview": bool(self.v_preview.get()),
            "use_token": bool(self.v_use_token.get()),
            "auto_interval": self._interval_minutes(),
        }

    def apply_state(self, s):
        s = cfg.clean_state(s)
        if not s:
            return
        set_text(self.e_folder, s.get("folder", ""))
        set_text(self.e_user, s.get("user", ""))
        set_text(self.e_email, s.get("email", ""))
        set_text(self.e_url, s.get("url", ""))
        set_text(self.e_branch, s.get("branch", "main") or "main")
        set_text(self.e_msg, s.get("message", DEFAULT_MESSAGE))
        self.t_ignore.delete("1.0", "end")
        self.t_ignore.insert("1.0", s.get("ignore", DEFAULT_IGNORE))
        self.om_license.set(self._license_label(
            s.get("license", LICENSE_DEFAULT)))
        set_text(self.e_owner, s.get("license_owner", ""))
        self._license_changed()
        self.v_local.set(s.get("local_identity", True))
        self.v_stamp.set(s.get("stamp", False))
        self.v_auto_msg.set(s.get("auto_message", False))
        self.v_force.set(s.get("force", False))
        self.v_check.set(s.get("check", True))
        self.v_preview.set(s.get("preview", True))
        self.v_use_token.set(s.get("use_token", True))
        self.seg_mode.set("Espejo" if s.get("mode") == "mirror"
                          else "Agregar / editar")
        self._mode_changed()
        self.om_interval.set(f"{s.get('auto_interval', 10)} min")

    def set_branch(self, name):
        set_text(self.e_branch, name)
        self.log(f"Rama de destino: «{name}».", "dim")

    def ignore_lines(self):
        return [ln.strip() for ln in
                self.t_ignore.get("1.0", "end").splitlines() if ln.strip()]

    def add_ignore_patterns(self, paths):
        current = self.ignore_lines()
        new = [p for p in paths if p not in current]
        if not new:
            return
        self.t_ignore.delete("1.0", "end")
        self.t_ignore.insert("1.0", "\n".join(current + new) + "\n")
        self.log("Añadido al cuadro de ignorar: " + ", ".join(new), "dim")

    def guess_owner(self):
        """Usuario de GitHub más probable: el de la URL del formulario, el
        del último repositorio publicado o, en su defecto, ninguno."""
        for url in (self.e_url.get(), self.last_repo_web):
            owner = parse_owner_repo(url)[0]
            if owner:
                return owner
        for profile in self.profiles.values():
            owner = parse_owner_repo(profile.get("url", ""))[0]
            if owner:
                return owner
        return ""

    def set_current_project(self, folder, url):
        if self.v_auto.get():
            self.v_auto.set(False)
            self.toggle_auto()
        set_text(self.e_folder, str(folder))
        if url:
            set_text(self.e_url, url)
        self.log("Proyecto cargado en el formulario.", "dim")

    def set_repo_url(self, clone_url, web_url=""):
        set_text(self.e_url, clone_url)
        self.last_repo_web = web_url or repo_web_url(clone_url)
