# -*- coding: utf-8 -*-
"""Pruebas de las partes puras del núcleo (no necesitan git)."""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

from gitpush_studio.core import config, git, ignore, urls
from gitpush_studio.core.fs import fmt_size, normalize_path
from gitpush_studio.core.fs import relative_inside
from gitpush_studio.core.publish import PublishJob


class IgnoreMatcherTests(unittest.TestCase):

    def matcher(self, *patterns):
        return ignore.IgnoreMatcher(patterns)

    def test_literal_name_matches_files_and_dirs(self):
        m = self.matcher("node_modules")
        self.assertTrue(m.ignored("node_modules", "node_modules", True))
        self.assertTrue(m.ignored("a/node_modules", "node_modules", False))
        self.assertFalse(m.ignored("a/node_modules2", "node_modules2", True))

    def test_dir_only_pattern_does_not_match_a_file(self):
        m = self.matcher("build/")
        self.assertTrue(m.ignored("build", "build", True))
        self.assertFalse(m.ignored("build", "build", False))

    def test_glob_on_name(self):
        m = self.matcher("*.log")
        self.assertTrue(m.ignored("logs/app.log", "app.log", False))
        self.assertFalse(m.ignored("logs/app.txt", "app.txt", False))

    def test_path_pattern_is_anchored_and_covers_children(self):
        m = self.matcher("src/generated")
        self.assertTrue(m.ignored("src/generated", "generated", True))
        self.assertTrue(m.ignored("src/generated/x.py", "x.py", False))
        self.assertFalse(m.ignored("other/src/generated", "generated", True))

    def test_leading_slash_anchors_to_root(self):
        """Antes «/.env» nunca coincidía con nada; ahora solo con la raíz."""
        m = self.matcher("/.env")
        self.assertTrue(m.ignored(".env", ".env", False))
        self.assertFalse(m.ignored("sub/.env", ".env", False))

    def test_comments_and_negations_are_skipped(self):
        m = self.matcher("# comentario", "!keep.txt", "   ")
        self.assertTrue(m.empty)

    def test_ignored_path_checks_ancestors(self):
        m = self.matcher("node_modules/", "*.tmp")
        self.assertTrue(m.ignored_path("web/node_modules/x/y.js"))
        self.assertTrue(m.ignored_path("cache/a.tmp"))
        self.assertFalse(m.ignored_path("web/src/app.js"))

    def test_anchor_pattern(self):
        self.assertEqual(ignore.anchor_pattern("sub/.env"), "/sub/.env")
        self.assertEqual(ignore.anchor_pattern("/x"), "/x")
        self.assertEqual(ignore.anchor_pattern("ruta con espacios.txt"),
                         "/ruta con espacios.txt")

    def test_anchor_pattern_escapes_wildcards_in_the_name(self):
        """`anchor_pattern` recibe RUTAS, no patrones.

        Sin escapar, «datos[2024].csv» se leería como «uno de estos
        caracteres» y git subiría el archivo igualmente: es el camino por
        el que un archivo que se mandó excluir acaba publicado.
        """
        self.assertEqual(ignore.anchor_pattern("datos[2024].csv"),
                         r"/datos\[2024\].csv")
        self.assertEqual(ignore.anchor_pattern("informe*.pem"),
                         r"/informe\*.pem")
        self.assertEqual(ignore.anchor_pattern("sub/x?.log"),
                         r"/sub/x\?.log")

    def test_an_anchored_pattern_excludes_its_file_and_only_its_file(self):
        """La propiedad que importa: el patrón que sale de una ruta
        excluye ESA ruta, y ninguna otra parecida."""
        casos = {"datos[2024].csv": "datos2024.csv",
                 "informe*.pem": "informeXX.pem",
                 "x?.log": "xy.log",
                 "a]b.txt": "ab.txt"}
        for ruta, parecida in casos.items():
            m = self.matcher(ignore.anchor_pattern(ruta))
            self.assertTrue(m.ignored_path(ruta.lower()),
                            f"«{ruta}» no quedó excluido")
            self.assertFalse(m.ignored_path(parecida.lower()),
                             f"«{parecida}» quedó excluido sin pedirlo")

    def test_escaped_names_are_matched_at_any_depth_too(self):
        m = self.matcher(ignore.anchor_pattern("sub/dir[1]"))
        self.assertTrue(m.ignored_path("sub/dir[1]"))
        self.assertTrue(m.ignored_path("sub/dir[1]/dentro/a.txt"))
        self.assertFalse(m.ignored_path("sub/dir1/dentro/a.txt"))
        self.assertFalse(m.ignored_path("otro/dir[1]/a.txt"))

    def test_a_pattern_the_user_types_keeps_working_as_a_wildcard(self):
        """Lo que escribe el usuario en el cuadro de ignorar NO se
        escapa: ahí «*.log» tiene que seguir siendo un comodín."""
        m = self.matcher("*.log", "build/", "temp?")
        self.assertTrue(m.ignored_path("salida.log"))
        self.assertTrue(m.ignored_path("build/app.js"))
        self.assertTrue(m.ignored_path("temp1"))
        self.assertFalse(m.ignored_path("salida.txt"))

    def test_a_trailing_space_in_the_name_survives(self):
        """Git descarta los espacios del final de una línea del .gitignore
        salvo que vayan escapados. Recortarlos aquí dejaría un patrón que
        apunta a OTRO archivo («…final» en vez de «…final »).
        """
        pat = ignore.anchor_pattern("informe final ")
        self.assertEqual(pat, "/informe final\\ ")
        m = self.matcher(pat)
        self.assertTrue(m.ignored_path("informe final "))
        self.assertFalse(m.ignored_path("informe final"))

    def test_an_unescaped_trailing_space_is_dropped_like_git_does(self):
        m = self.matcher("basura.log   ")
        self.assertTrue(m.ignored_path("basura.log"))
        self.assertTrue(self.matcher("   ").empty)

    def test_a_backslash_is_a_separator_only_where_it_is_one(self):
        """En Linux y macOS «back\\slash.txt» es UN archivo: convertir la
        barra en separador partiría la ruta y no excluiría nada."""
        pat = ignore.anchor_pattern("back\\slash.txt")
        if os.sep == "\\" or os.altsep == "\\":
            self.assertEqual(pat, "/back/slash.txt")
        else:
            self.assertEqual(pat, "/back\\\\slash.txt")
            self.assertTrue(self.matcher(pat).ignored_path("back\\slash.txt"))

    def test_trim_pattern(self):
        self.assertEqual(ignore.trim_pattern("x.txt  "), "x.txt")
        self.assertEqual(ignore.trim_pattern("x.txt\\ "), "x.txt\\ ")
        self.assertEqual(ignore.trim_pattern("x\\\\  "), "x\\\\")
        self.assertEqual(ignore.trim_pattern("\n node_modules/ \n"),
                         " node_modules/")
        self.assertEqual(ignore.trim_pattern("   "), "")

    def test_a_path_pattern_also_matches_without_walking_ancestors(self):
        """`ignored_path` recorre los ancestros, así que una carpeta
        excluida se detecta por el tramo. Pero `ignored` también se llama
        suelta, con la ruta completa, y ahí el patrón tiene que cubrir
        por sí mismo todo lo que cuelga de la carpeta.
        """
        m = self.matcher("a/bu*")
        self.assertTrue(m.ignored("a/build/out.o", "out.o", False))
        self.assertTrue(m.ignored("a/build/dentro/hondo.o", "hondo.o", False))
        self.assertTrue(m.ignored("a/build", "build", True))
        self.assertFalse(m.ignored("bu/x.txt", "x.txt", False))
        self.assertFalse(m.ignored("a/otro/out.o", "out.o", False))

    def test_wildcard_helpers(self):
        self.assertTrue(ignore.has_wildcards("*.log"))
        self.assertTrue(ignore.has_wildcards("a[1]"))
        self.assertFalse(ignore.has_wildcards(r"a\[1\]"))
        self.assertFalse(ignore.has_wildcards(r"a\*b"))
        self.assertEqual(ignore.unescape_pattern(r"a\[1\].env"), "a[1].env")
        self.assertEqual(ignore.escape_pattern("a[1]*?.env"),
                         r"a\[1\]\*\?.env")

    def test_append_gitignore_only_adds_missing(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".gitignore").write_text("a/\n# c\nb\n",
                                                encoding="utf-8")
            new = ignore.append_gitignore(d, ["b", "c", ""])
            self.assertEqual(new, ["c"])
            self.assertEqual(ignore.read_gitignore(d), ["a/", "# c", "b", "c"])
            self.assertEqual(ignore.append_gitignore(d, ["c"]), [])


class UrlTests(unittest.TestCase):

    def test_owner_repo(self):
        for u in ("https://github.com/ana/proj.git", "https://github.com/ana/proj",
                  "git@github.com:ana/proj.git", "https://github.com/ana/proj/"):
            self.assertEqual(urls.parse_owner_repo(u), ("ana", "proj"), u)
        self.assertEqual(urls.parse_owner_repo("https://gitlab.com/a/b"),
                         (None, None))

    def test_repo_name_from_url(self):
        self.assertEqual(urls.repo_name_from_url("https://github.com/a/b.git"),
                         "b")
        self.assertEqual(urls.repo_name_from_url("/tmp/repos/x.git"), "x")

    def test_username(self):
        self.assertEqual(urls.parse_username("https://github.com/Ana-1/"),
                         "Ana-1")
        self.assertEqual(urls.parse_username("ana"), "ana")
        self.assertEqual(urls.parse_username("-mal"), "")
        self.assertEqual(urls.parse_username("a" * 40), "")

    def test_web_url(self):
        self.assertEqual(urls.repo_web_url("git@github.com:a/b.git"),
                         "https://github.com/a/b")
        self.assertEqual(urls.repo_web_url("https://github.com/a/b.git/"),
                         "https://github.com/a/b")

    def test_remote_url_accepts_local_bare_repo(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(urls.is_remote_url(d))
        self.assertTrue(urls.is_remote_url("git@github.com:a/b.git"))
        self.assertFalse(urls.is_remote_url("ftp://x"))
        self.assertFalse(urls.is_remote_url(""))

    def test_http_origin(self):
        self.assertEqual(urls.http_origin("https://github.com/a/b.git"),
                         "https://github.com/")
        self.assertEqual(urls.http_origin("git@github.com:a/b"), "")


class GitParserTests(unittest.TestCase):

    def test_name_status_with_rename(self):
        out = "A\0new.txt\0M\0mod.txt\0R100\0old.txt\0renamed.txt\0D\0gone\0"
        added, modified, deleted = git.parse_name_status_z(out)
        self.assertEqual(added, ["new.txt"])
        self.assertEqual(modified, ["mod.txt", "renamed.txt"])
        self.assertEqual(deleted, ["gone"])

    def test_status_z_ignore_deletions(self):
        out = " M a.txt\0 D b.txt\0?? c.txt\0R  new\0old\0"
        self.assertEqual(git.parse_status_z(out),
                         ["a.txt", "b.txt", "c.txt", "new"])
        self.assertEqual(git.parse_status_z(out, ignore_deletions=True),
                         ["a.txt", "c.txt", "new"])
        self.assertEqual(git.parse_status_z(""), [])

    def test_auth_env_uses_config_env_not_argv(self):
        env = git.auth_env("https://github.com/a/b.git", "tok",
                           base_env={"GIT_CONFIG_COUNT": "2"})
        self.assertEqual(env["GIT_CONFIG_COUNT"], "3")
        self.assertEqual(env["GIT_CONFIG_KEY_2"],
                         "http.https://github.com/.extraheader")
        self.assertIn("basic", env["GIT_CONFIG_VALUE_2"])
        self.assertNotIn("tok", env["GIT_CONFIG_VALUE_2"])
        self.assertEqual(git.auth_env("git@github.com:a/b", "tok"), {})
        self.assertEqual(git.auth_env("https://github.com/a/b", ""), {})

    def test_push_error_classification(self):
        self.assertIn("credenciales",
                      git.push_error_message("fatal: authentication failed"))
        self.assertIn("no existe",
                      git.push_error_message("remote: repository not found"))
        self.assertIn("alguien más",
                      git.push_error_message("! [rejected] non-fast-forward"))
        self.assertIn("nada que publicar",
                      git.push_error_message("error: src refspec main does "
                                             "not match any"))


class ConfigTests(unittest.TestCase):

    def test_missing_file_gives_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            data, warn = config.load_config(Path(d) / "profiles.json")
            self.assertIsNone(warn)
            self.assertEqual(data["profiles"], {})

    def test_corrupt_file_is_backed_up_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "profiles.json"
            path.write_text("{ esto no es json", encoding="utf-8")
            data, warn = config.load_config(path)
            self.assertIsNotNone(warn)
            self.assertFalse(path.exists())
            backups = list(Path(d).glob("profiles.json.bak-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"),
                             "{ esto no es json")
            config.save_config(data, path)          # no toca la copia
            self.assertEqual(len(list(Path(d).glob("profiles.json.bak-*"))),
                             1)

    def test_save_is_atomic_and_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "profiles.json"
            data = config.defaults()
            data["profiles"]["p"] = {"folder": "x", "url": "y"}
            config.save_config(data, path)
            self.assertEqual(sorted(os.listdir(d)), ["profiles.json"])
            self.assertEqual(json.loads(path.read_text("utf-8"))["profiles"],
                             {"p": {"folder": "x", "url": "y"}})
            data2, warn = config.load_config(path)
            self.assertIsNone(warn)
            self.assertEqual(data2["version"], config.SCHEMA_VERSION)

    def test_odd_types_never_lose_profiles_silently(self):
        """`profiles` como lista (JSON válido) se cambiaba por {} sin
        aviso y el siguiente guardado borraba los perfiles para siempre."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "profiles.json"
            original = json.dumps({"profiles": [{"folder": "x"}],
                                   "last_profile": ["no", "hashable"],
                                   "last_state": [1, 2]})
            path.write_text(original, encoding="utf-8")
            data, warn = config.load_config(path)
            self.assertIn("dañadas", warn)
            self.assertEqual((data["profiles"], data["last_profile"],
                              data["last_state"]), ({}, None, {}))
            copias = list(Path(d).glob("profiles.json.bak-*"))
            self.assertEqual([c.read_text("utf-8") for c in copias],
                             [original])
            config.save_config(data, path)          # la copia sigue ahí
            self.assertEqual(len(list(Path(d).glob("profiles.json.bak-*"))),
                             1)

    def test_valid_profiles_survive_next_to_a_broken_one(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "profiles.json"
            path.write_text(json.dumps({"profiles": {
                "bueno": {"folder": "x"}, "malo": "texto"}}), "utf-8")
            data, warn = config.load_config(path)
            self.assertEqual(data["profiles"], {"bueno": {"folder": "x"}})
            self.assertIn("malo", warn)

    def test_a_bom_from_notepad_is_not_corruption(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "profiles.json"
            path.write_bytes(b"\xef\xbb\xbf" + json.dumps(
                {"profiles": {"p": {"folder": "x"}}}).encode("utf-8"))
            data, warn = config.load_config(path)
            self.assertIsNone(warn)
            self.assertEqual(data["profiles"], {"p": {"folder": "x"}})

    def test_if_the_original_cannot_be_moved_it_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "profiles.json"
            path.write_text("{ roto", encoding="utf-8")
            with mock.patch.object(config.os, "replace",
                                   side_effect=PermissionError("bloqueado")):
                data, warn = config.load_config(path)
            self.assertIn("no se guardará", warn)
            with self.assertRaises(OSError):
                config.save_config(data, path)
            self.assertEqual(path.read_text("utf-8"), "{ roto")

    def test_form_state_types_are_cleaned(self):
        s = config.clean_state({"check": "no", "preview": False,
                                "auto_interval": "abc", "mode": "raro",
                                "folder": 5, "url": "https://x",
                                "otra_cosa": [1]})
        self.assertNotIn("check", s)
        self.assertIs(s["preview"], False)
        self.assertEqual(s["auto_interval"], 10)
        self.assertNotIn("mode", s)
        self.assertNotIn("folder", s)
        self.assertEqual(s["url"], "https://x")
        self.assertEqual(s["otra_cosa"], [1])
        self.assertEqual(config.clean_state(["no", "dict"]), {})
        self.assertEqual(config.clean_state({"auto_interval": 30})
                         ["auto_interval"], 30)


class GitignoreEditTests(unittest.TestCase):

    def test_appending_keeps_every_existing_byte(self):
        """Antes se reescribía entero: finales de línea, BOM y espacios
        del .gitignore del usuario cambiaban en cada publicación."""
        from gitpush_studio.core.ignore import append_gitignore
        with tempfile.TemporaryDirectory() as d:
            gi = Path(d) / ".gitignore"
            original = b"\xef\xbb\xbf*.log\r\nbuild/  \r\n# nota\r\nnode_modules/"
            gi.write_bytes(original)
            nuevos = append_gitignore(d, ["/salida.txt", "*.log",
                                          "/salida.txt"])
            self.assertEqual(nuevos, ["/salida.txt"])
            datos = gi.read_bytes()
            self.assertTrue(datos.startswith(original))
            self.assertEqual(datos[len(original):], b"\r\n/salida.txt\r\n")
            self.assertEqual(append_gitignore(d, ["*.log"]), [])
            self.assertEqual(gi.read_bytes(), datos)

    @unittest.skipUnless(os.name == "posix", "enlaces simbólicos")
    def test_a_linked_gitignore_is_never_written_through(self):
        from gitpush_studio.core.errors import StepError
        from gitpush_studio.core.ignore import append_gitignore
        with tempfile.TemporaryDirectory() as d:
            ajeno = Path(d) / "ajeno.txt"
            ajeno.write_text("NO TOCAR\n")
            proj = Path(d) / "proj"
            proj.mkdir()
            os.symlink(str(ajeno), str(proj / ".gitignore"))
            with self.assertRaises(StepError):
                append_gitignore(proj, ["*.tmp"])
            self.assertEqual(ajeno.read_text(), "NO TOCAR\n")


class CliTests(unittest.TestCase):

    def test_a_closed_pipe_is_not_an_error(self):
        """`gitpush_studio … | head -5` cierra la tubería antes de que
        terminemos de escribir; eso no puede soltar una traza."""
        from gitpush_studio import cli

        def explota(_a):
            raise BrokenPipeError(32, "Broken pipe")
        with mock.patch.object(cli, "build_parser") as parser:
            parser.return_value.parse_args.return_value = \
                SimpleNamespace(fn=explota)
            self.assertEqual(cli.main([]), 0)      # sin propagar

    def test_ctrl_c_is_reported_and_not_a_traceback(self):
        from gitpush_studio import cli

        def corta(_a):
            raise KeyboardInterrupt
        with mock.patch.object(cli, "build_parser") as parser:
            parser.return_value.parse_args.return_value = \
                SimpleNamespace(fn=corta)
            self.assertEqual(cli.main([]), 130)

    def test_every_subcommand_is_reachable(self):
        from gitpush_studio import cli
        p = cli.build_parser()
        for orden in ("publish", "batch", "scan", "pack", "licencias",
                      "status", "pull", "versions", "restore", "repos",
                      "branches", "release"):
            sub = [a for a in p._subparsers._group_actions[0].choices
                   if a == orden]
            self.assertEqual(sub, [orden], f"falta la orden «{orden}»")

    def test_batch_reads_folder_repo_and_folder_url(self):
        """«--project carpeta=nombre» y «--project carpeta=URL» son dos
        cosas distintas: la segunda no debe crear nada en GitHub."""
        from gitpush_studio import cli
        a = cli.build_parser().parse_args(
            ["batch", "--project", "/x/alfa",
             "--project", "/x/beta=mi-repo",
             "--project", "/x/gamma=https://github.com/ana/g.git"])
        self.assertEqual(a.project, ["/x/alfa", "/x/beta=mi-repo",
                                     "/x/gamma=https://github.com/ana/g.git"])
        self.assertFalse(a.no_txt)        # el TXT viene activado
        self.assertFalse(a.public)        # y los repos nuevos, privados
        self.assertEqual(a.mode, "mirror")
        self.assertEqual(a.branch, "main")

    def test_a_project_path_containing_an_equals_sign_is_not_split(self):
        """«--project /datos/informe=final»: si esa carpeta existe tal
        cual, partir por el «=» publicaría otra carpeta (o se quejaría de
        una ruta que el usuario no escribió)."""
        from gitpush_studio import cli
        with tempfile.TemporaryDirectory() as d:
            raro = Path(d) / "informe=final"
            normal = Path(d) / "informe"
            raro.mkdir()
            normal.mkdir()
            e = cli._batch_entry(str(raro), True)
            self.assertEqual(e.folder, str(raro))
            self.assertEqual(e.repo, "informe-final")
            self.assertEqual(e.url, "")
            # Con un nombre detrás sí se parte, por el ÚLTIMO «=».
            e2 = cli._batch_entry(str(normal) + "=mi-repo", True)
            self.assertEqual((e2.folder, e2.repo), (str(normal), "mi-repo"))

    def test_a_destination_after_the_equals_is_taken_as_a_url(self):
        from gitpush_studio import cli
        e = cli._batch_entry("/x/p=https://github.com/ana/r.git", True)
        self.assertEqual(e.url, "https://github.com/ana/r.git")
        self.assertEqual(e.repo, "r")
        # Aunque la URL no valga: el error debe hablar de la URL, no
        # decir que el nombre del repositorio lleva barras.
        malo = cli._batch_entry("/x/p=/no/existe/x.git", True)
        self.assertEqual(malo.url, "/no/existe/x.git")
        self.assertEqual(malo.repo, "x")

    def test_txt_path_implies_txt(self):
        """Pedir dónde guardarlo es pedirlo: exigir además --txt solo
        serviría para que la orden no hiciera nada en silencio."""
        from gitpush_studio import cli
        job = SimpleNamespace(consolidate=False, consolidate_path="")
        cli._apply_txt_options([job], SimpleNamespace(
            txt=False, txt_path="/x/uno.txt"))
        self.assertTrue(job.consolidate)
        self.assertEqual(job.consolidate_path, "/x/uno.txt")

    def test_txt_path_with_several_projects_is_refused(self):
        """Un destino fijo haría que el segundo TXT pisara el primero."""
        from gitpush_studio import cli
        jobs = [SimpleNamespace(consolidate=False, consolidate_path=""),
                SimpleNamespace(consolidate=False, consolidate_path="")]
        with self.assertRaises(SystemExit):
            cli._apply_txt_options(
                jobs, SimpleNamespace(txt=True, txt_path="/x/uno.txt"))
        # Con uno solo sí vale.
        cli._apply_txt_options(jobs[:1], SimpleNamespace(
            txt=True, txt_path="/x/uno.txt"))
        self.assertEqual((jobs[0].consolidate, jobs[0].consolidate_path),
                         (True, "/x/uno.txt"))


class FsTests(unittest.TestCase):

    def test_fmt_size(self):
        self.assertEqual(fmt_size(512), "512 B")
        self.assertEqual(fmt_size(2048), "2 KB")
        self.assertEqual(fmt_size(3 * 1024 ** 2), "3 MB")
        self.assertEqual(fmt_size(int(1.5 * 1024 ** 3)), "1.5 GB")

    def test_normalize_path_strips_quotes(self):
        self.assertEqual(normalize_path('  "/tmp/x"  '), str(Path("/tmp/x")))
        self.assertEqual(normalize_path(""), "")


class RelativeInsideTests(unittest.TestCase):
    """«¿Este archivo está DENTRO de la carpeta del proyecto?».

    De esta respuesta depende si el TXT se añade al .gitignore. Comparar
    por prefijo de texto sin exigir el separador diría que «/tmp/proj2»
    está dentro de «/tmp/proj», y entonces se escribiría en el
    .gitignore del proyecto una ruta que no le corresponde.
    """

    def test_a_file_inside_gives_its_relative_path(self):
        self.assertEqual(relative_inside("/tmp/proj/a.txt", "/tmp/proj"),
                         "a.txt")
        self.assertEqual(relative_inside("/tmp/proj/sub/a.txt", "/tmp/proj"),
                         "sub/a.txt")

    def test_a_sibling_folder_with_the_same_prefix_is_outside(self):
        for fuera in ("/tmp/proj2/a.txt", "/tmp/proj-viejo/a.txt",
                      "/tmp/projeto/a.txt"):
            self.assertEqual(relative_inside(fuera, "/tmp/proj"), "",
                             f"«{fuera}» NO está dentro de /tmp/proj")

    def test_the_folder_itself_and_anything_above_are_outside(self):
        self.assertEqual(relative_inside("/tmp/proj", "/tmp/proj"), "")
        self.assertEqual(relative_inside("/tmp/a.txt", "/tmp/proj"), "")
        self.assertEqual(relative_inside("/otro/a.txt", "/tmp/proj"), "")

    def test_a_trailing_separator_in_the_folder_changes_nothing(self):
        self.assertEqual(relative_inside("/tmp/proj/a.txt", "/tmp/proj/"),
                         "a.txt")
        self.assertEqual(relative_inside("/tmp/proj2/a.txt", "/tmp/proj/"),
                         "")

    @unittest.skipUnless(hasattr(os, "symlink"), "sin enlaces simbólicos")
    def test_a_link_pointing_inside_is_reported_as_inside(self):
        """Con un enlace que apunta DENTRO del proyecto, `abspath` diría
        «está fuera» y nadie lo añadiría al .gitignore… pero git lo
        encontraría dentro y lo subiría."""
        with tempfile.TemporaryDirectory() as d:
            proy = Path(d) / "proy"
            (proy / "salida").mkdir(parents=True)
            enlace = Path(d) / "atajo"
            try:
                os.symlink(str(proy / "salida"), str(enlace))
            except (OSError, NotImplementedError):
                self.skipTest("este sistema no permite crear enlaces")
            self.assertEqual(relative_inside(enlace / "x.txt", proy),
                             "salida/x.txt")

    @unittest.skipUnless(hasattr(os, "symlink"), "sin enlaces simbólicos")
    def test_a_link_pointing_outside_is_reported_as_outside(self):
        with tempfile.TemporaryDirectory() as d:
            proy = Path(d) / "proy"
            fuera = Path(d) / "fuera"
            proy.mkdir()
            fuera.mkdir()
            enlace = proy / "atajo"
            try:
                os.symlink(str(fuera), str(enlace))
            except (OSError, NotImplementedError):
                self.skipTest("este sistema no permite crear enlaces")
            self.assertEqual(relative_inside(enlace / "x.txt", proy), "")


class TxtDestinationTests(unittest.TestCase):

    def job(self, folder, destino=""):
        return PublishJob(folder=folder, url="https://github.com/a/b.git",
                          consolidate=True, consolidate_path=destino)

    def test_by_default_it_is_named_after_the_project(self):
        self.assertEqual(self.job(str(Path("/tmp/mi-proy"))).txt_destination(),
                         Path("/tmp/mi-proy/mi-proy.txt"))

    def test_an_explicit_file_is_respected(self):
        destino = str(Path("/tmp/otro/todo.txt"))
        self.assertEqual(self.job("/tmp/p", destino).txt_destination(),
                         Path(destino))

    def test_an_existing_folder_as_destination_gets_the_default_name(self):
        with tempfile.TemporaryDirectory() as d:
            proy = Path(d) / "proyecto"
            proy.mkdir()
            self.assertEqual(self.job(str(proy), d).txt_destination(),
                             Path(d) / "proyecto.txt")


if __name__ == "__main__":
    unittest.main()
