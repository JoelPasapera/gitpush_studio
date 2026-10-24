# -*- coding: utf-8 -*-
"""Preparar varias publicaciones a la vez (core.batch).

Lo que se defiende aquí es que NADA sale de la lista sin estar
comprobado: un lote con cualquier problema no produce ni un trabajo, y
cada fila manda sobre su carpeta, su repositorio y su TXT. También que
las carpetas y los repositorios repetidos se detectan antes de tocar
GitHub: publicar dos carpetas al mismo repositorio es justo la forma de
que una se coma a la otra.

Las publicaciones de verdad (git push a repositorios bare locales) están
en test_git_and_publish y test_safety; aquí no hace falta red ni disco.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from gitpush_studio.core.batch import (MAX_NAME, BatchEntry, check_repo_name,
                                       jobs_from_entries, plan_batch,
                                       project_folders, repo_url,
                                       resolve_entries, suggest_repo_name,
                                       validate_entries)
from gitpush_studio.core.errors import StepError


class FakeGitHub:
    """Sustituto de create_repo/whoami: registra lo que se le pide."""

    def __init__(self, login="ana", existing=(), fail=None):
        self.login = login
        self.existing = {n.lower() for n in existing}
        self.fail = dict(fail or {})
        self.created = []
        self.asked = []

    def create(self, token, name, description="", private=False):
        self.asked.append((name, private))
        if name in self.fail:
            raise StepError(self.fail[name])
        clone = f"https://github.com/{self.login}/{name}.git"
        if name.lower() in self.existing:
            return clone, clone[:-4], True
        self.existing.add(name.lower())
        self.created.append(name)
        return clone, clone[:-4], False

    def who(self, token):
        return self.login if token == "bueno" else ""


class NamesTests(unittest.TestCase):

    def test_folder_name_becomes_a_valid_repo_name(self):
        casos = {"/x/Mi App v2": "Mi-App-v2",
                 "/x/proyecto__raro!!": "proyecto__raro",
                 "/x/  espacios  ": "espacios",
                 "/x/.oculto": "oculto",
                 # Español: la tilde y la ñ conservan la letra, no se
                 # convierten en guiones («Cálculo» no es «C-lculo»).
                 "/x/ñandú": "nandu",
                 "/x/Cálculo Numérico": "Calculo-Numerico",
                 "/x/Año 2026": "Ano-2026"}
        for folder, esperado in casos.items():
            self.assertEqual(suggest_repo_name(folder), esperado, folder)

    def test_a_path_object_works_as_well_as_a_string(self):
        self.assertEqual(suggest_repo_name(Path("/x/Mi App")), "Mi-App")
        self.assertEqual(BatchEntry(folder=Path("/x/y")).folder, "/x/y")

    def test_what_has_no_ascii_equivalent_becomes_a_dash(self):
        self.assertEqual(suggest_repo_name("/x/проект"), "")
        self.assertEqual(suggest_repo_name("/x/api 日本"), "api")

    def test_a_folder_without_a_usable_name_suggests_nothing(self):
        for folder in ("", "/", "/x/---", "/x/..."):
            self.assertEqual(suggest_repo_name(folder), "", repr(folder))

    def test_long_names_are_cut_to_what_github_accepts(self):
        largo = "/x/" + "a" * 300
        self.assertEqual(len(suggest_repo_name(largo)), MAX_NAME)

    def test_repo_name_errors_say_what_is_wrong(self):
        self.assertIsNone(check_repo_name("api_v2.1-beta"))
        self.assertIn("Escribe el nombre", check_repo_name("  "))
        self.assertIn("solo admite", check_repo_name("mi repo"))
        self.assertIn("solo admite", check_repo_name("repo/otro"))
        self.assertIn("caracteres", check_repo_name("x" * (MAX_NAME + 1)))


class ValidationTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.a = Path(self.tmp) / "a"
        self.b = Path(self.tmp) / "b"
        self.a.mkdir()
        self.b.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def entry(self, folder, repo, txt=True):
        return BatchEntry(folder=str(folder), repo=repo, consolidate=txt)

    def test_a_correct_list_has_no_problems(self):
        self.assertEqual(validate_entries([self.entry(self.a, "uno"),
                                           self.entry(self.b, "dos")]), [])

    def test_an_empty_list_is_a_problem(self):
        self.assertEqual(len(validate_entries([])), 1)

    def test_a_missing_folder_is_reported_with_its_number(self):
        problemas = validate_entries([self.entry(self.a, "uno"),
                                      self.entry("/no/existe/jamas", "dos")])
        self.assertEqual(len(problemas), 1)
        self.assertIn("Proyecto 2", problemas[0])
        self.assertIn("no existe", problemas[0])

    def test_the_same_folder_twice_is_refused(self):
        problemas = validate_entries([self.entry(self.a, "uno"),
                                      self.entry(self.a, "dos")])
        self.assertTrue(any("misma carpeta" in p for p in problemas))

    def test_the_same_folder_written_differently_is_still_the_same(self):
        otra = str(self.a) + "//."
        problemas = validate_entries([self.entry(self.a, "uno"),
                                      self.entry(otra, "dos")])
        self.assertTrue(any("misma carpeta" in p for p in problemas))

    def test_two_folders_to_the_same_repo_is_refused(self):
        """Es el error que pisaría un proyecto con el otro."""
        problemas = validate_entries([self.entry(self.a, "mismo"),
                                      self.entry(self.b, "MISMO")])
        self.assertTrue(any("se repite" in p for p in problemas))

    def test_a_folder_inside_another_folder_of_the_batch_is_refused(self):
        """git rechaza `git add -A` cuando encuentra un repositorio dentro
        de otro, y si el de dentro acaba primero el de fuera se queda con
        un enlace a un commit que allí no existe. Tres resultados
        posibles y ninguno es el que se pidió: se corta antes."""
        dentro = self.a / "sub"
        dentro.mkdir()
        problemas = validate_entries([self.entry(self.a, "fuera"),
                                      self.entry(dentro, "dentro")])
        self.assertTrue(any("está dentro del" in p for p in problemas),
                        problemas)

    def test_sibling_folders_with_a_common_prefix_are_fine(self):
        """«proy2» no está dentro de «proy»: comparar por prefijo de texto
        sin exigir el separador bloquearía un lote perfectamente válido."""
        p1 = Path(self.tmp) / "proy"
        p2 = Path(self.tmp) / "proy2"
        p1.mkdir()
        p2.mkdir()
        self.assertEqual(validate_entries([self.entry(p1, "uno"),
                                           self.entry(p2, "dos")]), [])

    def test_two_rows_pointing_at_the_same_url_are_refused(self):
        """Aunque los nombres escritos sean distintos: es lo que pasaría
        si una fila arrastrara la URL de un intento anterior."""
        misma = "https://github.com/ana/repo.git"
        problemas = validate_entries([
            BatchEntry(folder=str(self.a), repo="nombre-nuevo", url=misma),
            BatchEntry(folder=str(self.b), repo="otro-nombre", url=misma)])
        self.assertTrue(any("mismo repositorio" in p for p in problemas),
                        problemas)

    def test_the_same_url_written_with_and_without_git_is_the_same(self):
        problemas = validate_entries([
            BatchEntry(folder=str(self.a), repo="uno",
                       url="https://github.com/ana/repo.git"),
            BatchEntry(folder=str(self.b), repo="dos",
                       url="https://github.com/ana/repo")])
        self.assertTrue(any("mismo repositorio" in p for p in problemas),
                        problemas)

    def test_repos_whose_name_ends_in_git_letters_are_not_confused(self):
        """«digit» y «digi» son repositorios distintos: recortar «.git»
        con rstrip se comería la «t»."""
        self.assertEqual(validate_entries([
            BatchEntry(folder=str(self.a), repo="digit",
                       url="https://github.com/ana/digit.git"),
            BatchEntry(folder=str(self.b), repo="digi",
                       url="https://github.com/ana/digi.git")]), [])

    def test_all_the_problems_come_back_at_once(self):
        problemas = validate_entries([self.entry("", ""),
                                      self.entry(self.a, "mal nombre")])
        self.assertGreaterEqual(len(problemas), 3)


class ResolveTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.a = Path(self.tmp) / "a"
        self.a.mkdir()
        self.gh = FakeGitHub(existing=["viejo"])
        self.api = {"api_create": self.gh.create, "api_whoami": self.gh.who}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_with_a_token_the_owner_is_detected_and_missing_repos_created(self):
        filas = [BatchEntry(folder=str(self.a), repo="nuevo"),
                 BatchEntry(folder=str(self.a), repo="viejo")]
        dueno, problemas = resolve_entries(filas, token="bueno", **self.api)
        self.assertEqual((dueno, problemas), ("ana", []))
        self.assertEqual(self.gh.created, ["nuevo"])
        self.assertEqual([e.url for e in filas],
                         ["https://github.com/ana/nuevo.git",
                          "https://github.com/ana/viejo.git"])
        self.assertEqual([e.created for e in filas], [True, False])

    def test_visibility_is_passed_through(self):
        filas = [BatchEntry(folder=str(self.a), repo="x")]
        resolve_entries(filas, token="bueno", private=True, **self.api)
        self.assertEqual(self.gh.asked, [("x", True)])

    def test_an_invalid_token_stops_everything_before_creating_anything(self):
        filas = [BatchEntry(folder=str(self.a), repo="x")]
        dueno, problemas = resolve_entries(filas, token="malo", **self.api)
        self.assertEqual(dueno, "")
        self.assertTrue(any("token" in p.lower() for p in problemas))
        self.assertEqual(self.gh.created, [])

    def test_a_repo_that_cannot_be_created_is_reported_by_name(self):
        self.gh.fail["ocupado"] = "No se pudo crear (422)"
        filas = [BatchEntry(folder=str(self.a), repo="ocupado"),
                 BatchEntry(folder=str(self.a), repo="libre")]
        _, problemas = resolve_entries(filas, token="bueno", **self.api)
        self.assertEqual(len(problemas), 1)
        self.assertIn("ocupado", problemas[0])
        self.assertEqual(filas[0].url, "")          # esa fila no tiene URL

    def test_without_a_token_the_url_is_composed_from_the_username(self):
        filas = [BatchEntry(folder=str(self.a), repo="uno")]
        dueno, problemas = resolve_entries(filas, owner="ana",
                                           create_missing=False, **self.api)
        self.assertEqual((dueno, problemas), ("ana", []))
        self.assertEqual(filas[0].url, repo_url("ana", "uno"))
        self.assertEqual(self.gh.asked, [])         # no se llama a GitHub

    def test_without_a_token_and_without_a_username_it_says_so(self):
        filas = [BatchEntry(folder=str(self.a), repo="uno")]
        _, problemas = resolve_entries(filas, create_missing=False, **self.api)
        self.assertTrue(any("usuario" in p for p in problemas))

    def test_the_account_of_the_token_wins_over_what_was_typed(self):
        """Si el token es de «ana», el repositorio se crea en «ana»: la
        URL tiene que apuntar ahí y no a lo que se escribió."""
        filas = [BatchEntry(folder=str(self.a), repo="uno")]
        dueno, _ = resolve_entries(filas, token="bueno", owner="otro",
                                    **self.api)
        self.assertEqual(dueno, "ana")
        self.assertEqual(filas[0].url, "https://github.com/ana/uno.git")

    def test_an_explicit_url_is_never_overwritten(self):
        """Si la fila ya trae URL (un espejo, otro servidor, lo que pasa
        la línea de órdenes con «carpeta=URL»), manda ella: ni se compone
        otra ni se crea nada en GitHub."""
        mia = "https://gitlab.example/ana/espejo.git"
        filas = [BatchEntry(folder=str(self.a), repo="espejo", url=mia),
                 BatchEntry(folder=str(self.a), repo="normal")]
        dueno, problemas = resolve_entries(filas, token="bueno", **self.api)
        self.assertEqual(problemas, [])
        self.assertEqual(filas[0].url, mia)
        self.assertEqual(filas[1].url, "https://github.com/ana/normal.git")
        self.assertEqual(self.gh.created, ["normal"])
        self.assertEqual(dueno, "ana")

    def test_with_every_url_given_github_is_never_contacted(self):
        filas = [BatchEntry(folder=str(self.a), repo="uno",
                            url="/ruta/local/uno.git")]
        dueno, problemas = resolve_entries(filas, **self.api)
        self.assertEqual((problemas, dueno), ([], ""))
        self.assertEqual(filas[0].url, "/ruta/local/uno.git")
        self.assertEqual((self.gh.asked, self.gh.created), ([], []))

    def test_progress_is_reported_once_per_project(self):
        filas = [BatchEntry(folder=str(self.a), repo=n) for n in "xyz"]
        vistos = []
        resolve_entries(filas, token="bueno",
                        on_progress=lambda label, i, t: vistos.append((i, t)),
                        **self.api)
        self.assertEqual(vistos, [(1, 3), (2, 3), (3, 3)])


class JobsTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.a = Path(self.tmp) / "a"
        self.b = Path(self.tmp) / "b"
        self.a.mkdir()
        self.b.mkdir()
        self.state = {"branch": "produccion", "mode": "addedit",
                      "message": "Subida del lote", "license": "mit",
                      "ignore": "node_modules/\n*.log", "check": True,
                      "preview": False, "user": "Ana", "email": "a@x.y",
                      "folder": "/se/ignora", "url": "https://no/vale.git",
                      "consolidate_path": "/ruta/vieja.txt"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def jobs(self):
        filas = [BatchEntry(folder=str(self.a), repo="uno", consolidate=True,
                            url="https://github.com/ana/uno.git"),
                 BatchEntry(folder=str(self.b), repo="dos", consolidate=False,
                            url="https://github.com/ana/dos.git")]
        return jobs_from_entries(filas, self.state, token="t0ken")

    def test_each_row_rules_its_own_folder_url_and_txt(self):
        uno, dos = self.jobs()
        self.assertEqual(Path(uno.folder), self.a)
        self.assertEqual(Path(dos.folder), self.b)
        self.assertEqual(uno.url, "https://github.com/ana/uno.git")
        self.assertEqual(dos.url, "https://github.com/ana/dos.git")
        self.assertTrue(uno.consolidate)
        self.assertFalse(dos.consolidate)

    def test_the_shared_options_reach_every_job(self):
        for job in self.jobs():
            self.assertEqual(job.branch, "produccion")
            self.assertEqual(job.mode, "addedit")
            self.assertEqual(job.message, "Subida del lote")
            self.assertEqual(job.license, "mit")
            self.assertEqual(job.ignore, ["node_modules/", "*.log"])
            self.assertEqual((job.user, job.email), ("Ana", "a@x.y"))
            self.assertEqual(job.token, "t0ken")

    def test_the_txt_of_each_project_goes_into_its_own_folder(self):
        """Un destino fijo heredado del formulario haría que el segundo
        proyecto sobrescribiera el TXT del primero."""
        uno, dos = self.jobs()
        self.assertEqual(uno.consolidate_path, "")
        self.assertEqual(uno.txt_destination(), self.a / "a.txt")
        self.assertEqual(dos.txt_destination(), self.b / "b.txt")

    def test_every_job_is_valid_and_labelled(self):
        for job in self.jobs():
            self.assertIsNone(job.validate(), job.label)
        self.assertEqual([j.label for j in self.jobs()], ["uno", "dos"])


class PlanTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.a = Path(self.tmp) / "a"
        self.a.mkdir()
        self.gh = FakeGitHub()
        self.api = {"api_create": self.gh.create, "api_whoami": self.gh.who}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_good_plan_carries_the_jobs_and_what_was_created(self):
        filas = [BatchEntry(folder=str(self.a), repo="uno")]
        plan = plan_batch(filas, {"branch": "main"}, token="bueno", **self.api)
        self.assertTrue(plan.ok)
        self.assertEqual(plan.owner, "ana")
        self.assertEqual(plan.created, ["uno"])
        self.assertEqual(len(plan.jobs), 1)

    def test_a_list_with_problems_produces_no_jobs_at_all(self):
        """Ni uno: un lote a medias es peor que ninguno."""
        filas = [BatchEntry(folder=str(self.a), repo="uno"),
                 BatchEntry(folder="/no/existe", repo="dos")]
        plan = plan_batch(filas, {}, token="bueno", **self.api)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.jobs, [])
        self.assertEqual(self.gh.created, [])      # no se creó nada en GitHub

    def test_a_url_that_is_not_a_remote_cancels_the_batch(self):
        """Lo que no caza la validación de filas (el nombre es válido y la
        carpeta existe) tiene que cazarlo la última comprobación con las
        mismas reglas que una publicación suelta. Si no, el lote arranca y
        falla a mitad, con parte ya subida."""
        filas = [BatchEntry(folder=str(self.a), repo="x",
                            url="/no/existe/x.git")]
        plan = plan_batch(filas, {}, owner="ana", create_missing=False,
                          **self.api)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.jobs, [])
        self.assertTrue(any("https://" in p for p in plan.problems),
                        plan.problems)
        self.assertTrue(any(p.startswith("x:") for p in plan.problems),
                        plan.problems)

    def test_a_github_problem_also_cancels_the_whole_batch(self):
        self.gh.fail["dos"] = "nombre ocupado"
        filas = [BatchEntry(folder=str(self.a), repo="uno"),
                 BatchEntry(folder=str(self.a / "sub"), repo="dos")]
        (self.a / "sub").mkdir()
        plan = plan_batch(filas, {}, token="bueno", **self.api)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.jobs, [])


class ProjectFoldersTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name in ("uno", "dos", ".git", ".oculta", "node_modules",
                     "venv", "__pycache__"):
            (self.tmp / name).mkdir()
        (self.tmp / "archivo.txt").write_text("x", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_only_the_folders_that_look_like_projects(self):
        nombres = [Path(p).name for p in project_folders(self.tmp)]
        self.assertEqual(nombres, ["dos", "uno"])

    def test_a_folder_that_does_not_exist_gives_an_empty_list(self):
        self.assertEqual(project_folders("/no/existe/jamas"), [])

    def test_the_limit_is_respected(self):
        self.assertEqual(len(project_folders(self.tmp, limit=1)), 1)


if __name__ == "__main__":
    unittest.main()
