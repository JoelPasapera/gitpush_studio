# -*- coding: utf-8 -*-
"""Listar los repositorios de un perfil por GraphQL.

Sin red: se sustituye `github.api` y se comprueba qué se pide, cuántas
veces y con qué resultado. Lo importante es que GraphQL ahorre
peticiones cuando se puede y que, cuando no se pueda, nadie se entere
porque REST sigue estando ahí.
"""

import unittest
from unittest import mock

from gitpush_studio.core import github
from gitpush_studio.core.errors import StepError


def nodo(nombre, rama="main", fork=False, privado=False, kb=12,
         dueno="ana"):
    return {"name": nombre,
            "url": f"https://github.com/{dueno}/{nombre}",
            "isFork": fork, "isPrivate": privado, "diskUsage": kb,
            "defaultBranchRef": {"name": rama} if rama else None,
            "owner": {"login": dueno}}


def respuesta(nodos, siguiente=False, cursor="C1", login="ana"):
    return 200, {"data": {"repositoryOwner": {
        "login": login,
        "repositories": {
            "pageInfo": {"hasNextPage": siguiente, "endCursor": cursor},
            "nodes": nodos}}}}


class GraphQLListingTests(unittest.TestCase):

    def espia(self, respuestas):
        """Sustituye github.api y registra cada llamada."""
        llamadas = []

        def falso(method, path, token="", payload=None, **kw):
            llamadas.append((method, path, payload, token))
            r = respuestas.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return falso, llamadas

    def test_one_request_brings_the_whole_profile(self):
        falso, llamadas = self.espia([respuesta([nodo("web"), nodo("api")])])
        notas = []
        with mock.patch.object(github, "api", falso):
            repos = github.list_user_repos("ana", "ghp_x",
                                           on_note=notas.append)
        self.assertEqual([r["name"] for r in repos], ["web", "api"])
        self.assertEqual(len(llamadas), 1)          # con REST serían 2
        metodo, ruta, carga, token = llamadas[0]
        self.assertEqual((metodo, ruta), ("POST", "/graphql"))
        self.assertEqual(carga["variables"], {"login": "ana", "after": None})
        self.assertIn("repositoryOwner", carga["query"])
        self.assertEqual(token, "ghp_x")
        self.assertIn("2 repositorio(s) en 1 petición (GraphQL)", notas[0])

    def test_every_field_the_downloader_needs_is_mapped(self):
        falso, _ = self.espia([respuesta([
            nodo("privado", rama="develop", privado=True, kb=345),
            nodo("copia", fork=True),
            nodo("vacio", rama=None, kb=None)])])
        with mock.patch.object(github, "api", falso):
            repos = github.list_user_repos("ana", "ghp_x")
        privado, copia, vacio = repos
        self.assertEqual(privado["clone_url"],
                         "https://github.com/ana/privado.git")
        self.assertTrue(privado["private"])
        self.assertEqual(privado["default_branch"], "develop")
        self.assertEqual(privado["size_kb"], 345)
        self.assertEqual(privado["owner"], "ana")
        self.assertTrue(copia["fork"])
        # Un repositorio recién creado no tiene rama ni tamaño: sin reventar.
        self.assertEqual(vacio["default_branch"], "")
        self.assertEqual(vacio["size_kb"], 0)

    def test_more_than_one_hundred_repos_are_paged_by_cursor(self):
        falso, llamadas = self.espia([
            respuesta([nodo(f"r{i}") for i in range(100)], siguiente=True,
                      cursor="CURSOR-2"),
            respuesta([nodo("r100")])])
        notas = []
        with mock.patch.object(github, "api", falso):
            repos = github.list_user_repos("ana", "ghp_x",
                                           on_note=notas.append)
        self.assertEqual(len(repos), 101)
        self.assertEqual(len(llamadas), 2)
        self.assertEqual(llamadas[1][2]["variables"]["after"], "CURSOR-2")
        self.assertIn("101 repositorio(s) en 2 peticiones (GraphQL)",
                      notas[0])

    def test_without_a_token_graphql_is_not_even_attempted(self):
        """GraphQL exige autenticación: sin token, directo a REST."""
        falso, llamadas = self.espia([
            (200, [{"name": "web", "clone_url": "https://x/web.git",
                    "fork": False, "private": False, "size": 3,
                    "default_branch": "main",
                    "owner": {"login": "ana"}}])])
        with mock.patch.object(github, "api", falso):
            repos = github.list_user_repos("ana")
        self.assertEqual([r["name"] for r in repos], ["web"])
        self.assertTrue(all(ruta != "/graphql" for _, ruta, _, _ in llamadas))

    def test_if_graphql_fails_rest_takes_over_silently(self):
        falso, llamadas = self.espia([
            (200, {"errors": [{"message": "Field 'diskUsage' doesn't exist"}]}),
            (200, {"login": "ana"}),                       # whoami
            (200, [{"name": "web", "clone_url": "https://x/web.git",
                    "fork": False, "private": False, "size": 3}])])
        notas = []
        with mock.patch.object(github, "api", falso):
            repos = github.list_user_repos("ana", "ghp_x",
                                           on_note=notas.append)
        self.assertEqual([r["name"] for r in repos], ["web"])
        self.assertEqual(llamadas[0][1], "/graphql")
        self.assertIn("uso el método de siempre", notas[0])
        self.assertIn("(REST)", notas[1])

    def test_an_unknown_user_ends_with_the_usual_clear_message(self):
        falso, _ = self.espia([
            (200, {"data": {"repositoryOwner": None}}),    # GraphQL: nada
            (200, {"login": "otro"}),                      # whoami
            (404, {"message": "Not Found"})])              # REST: 404
        with mock.patch.object(github, "api", falso):
            with self.assertRaises(StepError) as cm:
                github.list_user_repos("noexiste", "ghp_x")
        self.assertIn("no existe en GitHub", str(cm.exception))

    def test_an_invalid_token_says_so_without_wasting_requests(self):
        """Con el token mal, REST fallaría igual y peor explicado: no
        tiene sentido gastar dos peticiones más para empeorar el aviso."""
        falso, llamadas = self.espia([(401, {"message": "Bad credentials"})]
                                     * 3)
        notas = []
        with mock.patch.object(github, "api", falso):
            with self.assertRaises(StepError) as cm:
                github.list_user_repos("ana", "malo", on_note=notas.append)
        self.assertEqual(str(cm.exception), github.TOKEN_INVALID)
        self.assertEqual(len(llamadas), 1)
        self.assertEqual(notas, [])

    def test_rest_gives_the_same_message_for_a_bad_token(self):
        falso, _ = self.espia([
            (200, {"errors": [{"message": "no disponible"}]}),   # GraphQL cae
            (401, {}),                                          # whoami
            (401, {"message": "Bad credentials"})])             # REST
        with mock.patch.object(github, "api", falso):
            with self.assertRaises(StepError) as cm:
                github.list_user_repos("ana", "malo")
        self.assertEqual(str(cm.exception), github.TOKEN_INVALID)

    def test_a_page_without_a_new_cursor_does_not_loop(self):
        """Si GitHub dice «hay más» pero no da cursor, seguir pidiendo
        devolvería la misma página treinta veces."""
        sin_cursor = (200, {"data": {"repositoryOwner": {
            "login": "ana",
            "repositories": {"pageInfo": {"hasNextPage": True,
                                          "endCursor": None},
                             "nodes": [nodo("web")]}}}})
        falso, llamadas = self.espia([sin_cursor] * 40)
        with mock.patch.object(github, "api", falso):
            repos = github.list_user_repos("ana", "ghp_x")
        self.assertEqual(len(llamadas), 1)
        self.assertEqual([r["name"] for r in repos], ["web"])

    def test_the_same_cursor_twice_also_stops(self):
        repetido = (200, {"data": {"repositoryOwner": {
            "login": "ana",
            "repositories": {"pageInfo": {"hasNextPage": True,
                                          "endCursor": "IGUAL"},
                             "nodes": [nodo("web")]}}}})
        falso, llamadas = self.espia([repetido] * 40)
        with mock.patch.object(github, "api", falso):
            repos = github.list_user_repos("ana", "ghp_x")
        self.assertEqual(len(llamadas), 2)     # pide una vez más y para
        self.assertEqual(len(repos), 2)

    def test_a_node_without_a_name_is_ignored(self):
        """Un nodo vacío se convertiría en una carpeta sin nombre y en un
        «git clone \"\"»."""
        falso, _ = self.espia([respuesta([{"name": "", "url": ""}, None,
                                          nodo("bueno")])])
        with mock.patch.object(github, "api", falso):
            repos = github.list_user_repos("ana", "ghp_x")
        self.assertEqual([r["name"] for r in repos], ["bueno"])

    def test_whoami_survives_a_200_that_is_not_an_object(self):
        """Un proxy corporativo puede devolver 200 con una página de
        error; eso no puede tumbar la aplicación con AttributeError."""
        for cuerpo in ([], "<html>bloqueado</html>", None, 42):
            with mock.patch.object(github, "api",
                                   lambda *a, **k: (200, cuerpo)):
                self.assertEqual(github.whoami("ghp_x"), "")

    def test_graphql_helper_reports_query_errors(self):
        with mock.patch.object(github, "api",
                               lambda *a, **k: (200, {"errors": [
                                   {"message": "algo falló"}]})):
            with self.assertRaises(StepError) as cm:
                github.graphql("query", {}, "ghp_x")
        self.assertIn("algo falló", str(cm.exception))

    def test_rest_also_reports_how_many_requests_it_took(self):
        pagina = [{"name": f"r{i}", "clone_url": "https://x/r.git",
                   "fork": False, "private": False, "size": 1}
                  for i in range(100)]
        falso, _ = self.espia([
            (200, {"errors": [{"message": "no disponible"}]}),  # GraphQL cae
            (200, {"login": "otro"}),                           # whoami
            (200, pagina), (200, pagina[:5])])
        notas = []
        with mock.patch.object(github, "api", falso):
            repos = github.list_user_repos("ana", "ghp_x", on_note=notas.append)
        self.assertEqual(len(repos), 105)
        # whoami + dos páginas = 3 peticiones, frente a 1 de GraphQL.
        self.assertIn("3 peticiones (REST)", notas[-1])


if __name__ == "__main__":
    unittest.main()
