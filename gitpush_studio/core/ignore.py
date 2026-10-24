# -*- coding: utf-8 -*-
"""Coincidencia estilo .gitignore (simplificada) y edición del .gitignore.

Cuando la carpeta ya es un repositorio, la fuente de verdad es git
(véase `git.Git.list_files`): esta implementación se usa para carpetas
sin repositorio y para aplicar el cuadro de ignorar sobre listas
planas. Simplificaciones conocidas: no admite negaciones («!»), trata
«**» como «*» y compara sin distinguir mayúsculas.

Las rutas concretas se convierten en patrón con `anchor_pattern`, que
escapa los comodines con «\\» igual que hace git: un archivo llamado
«datos[2024].csv» no es un patrón, es un nombre, y sin escapar «[2024]»
se leería como «uno de estos caracteres» y el archivo se subiría
IGUALMENTE. Aquí se entienden esos escapes cuando el patrón es una ruta
literal, que es como los escribe la aplicación.
"""

import os
import re
from pathlib import Path

from .atomic import write_bytes
from .errors import StepError

_GLOB_CHARS = ("*", "?", "[")
# Lo que hay que escapar para que un nombre se lea como nombre.
_NEEDS_ESCAPE = ("\\", "*", "?", "[", "]")
_ESCAPED = re.compile(r"\\(.)")


def escape_pattern(text):
    """Escapa los caracteres que git leería como comodín."""
    out = []
    for ch in text:
        if ch in _NEEDS_ESCAPE:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def unescape_pattern(text):
    """«clave\\[1\\].env» → «clave[1].env»."""
    return _ESCAPED.sub(r"\1", text)


def trim_pattern(raw):
    """Recorta un patrón como lo hace git.

    Git descarta los espacios del final de cada línea del .gitignore
    salvo que vayan escapados con «\\». Hay que seguir la misma regla: un
    archivo que de verdad acaba en espacio solo se puede excluir con el
    espacio escapado, y recortarlo aquí dejaría un patrón que apunta a
    otro archivo.
    """
    p = (raw or "").strip("\t\r\n")
    i = len(p)
    while i > 0 and p[i - 1] == " ":
        barras, j = 0, i - 2
        while j >= 0 and p[j] == "\\":
            barras += 1
            j -= 1
        if barras % 2:                    # el espacio está escapado
            break
        i -= 1
    return p[:i]


def has_wildcards(text):
    """¿Quedan comodines SIN escapar? Un «\\*» es un asterisco de verdad."""
    i, n = 0, len(text)
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] in _GLOB_CHARS:
            return True
        i += 1
    return False


def segment_regex(seg):
    """Traduce UN tramo de patrón a expresión regular.

    Dentro de un tramo, «*» y «?» no cruzan la barra (eso es lo que hace
    git, y no lo que hace `fnmatch`, donde «*» se come las barras). Los
    escapes se respetan: «\\*» es un asterisco literal.
    """
    out, i, n = [], 0, len(seg)
    while i < n:
        c = seg[i]
        if c == "\\" and i + 1 < n:
            out.append(re.escape(seg[i + 1]))
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            cierre = seg.find("]", i + 2)
            if cierre < 0:                     # corchete sin cerrar
                out.append(re.escape(c))
            else:
                cuerpo = seg[i + 1:cierre]
                if cuerpo.startswith("!"):     # git usa «!», re usa «^»
                    cuerpo = "^" + cuerpo[1:]
                out.append("[" + cuerpo + "]")
                i = cierre + 1
                continue
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def pattern_regex(p):
    """Patrón de .gitignore con ruta → expresión regular anclada.

    «**» entre barras significa «cero o más carpetas», al final «todo lo
    que cuelgue». Sin esto, un «**/.env» se traduciría con `fnmatch` a
    algo que exige una barra y NO casaría con un «.env» de la raíz: el
    archivo se quedaría publicado y dentro del TXT aunque el usuario
    pidiera excluirlo.
    """
    tramos = p.split("/")
    partes = []
    for i, seg in enumerate(tramos):
        ultimo = i == len(tramos) - 1
        if seg == "**":
            partes.append(".*" if ultimo else "(?:[^/]+/)*")
        else:
            partes.append(segment_regex(seg))
            if not ultimo and not tramos[i + 1] == "**":
                partes.append("/")
            elif not ultimo:
                partes.append("/")
    cuerpo = "".join(partes)
    # La ruta exacta o cualquier cosa por debajo (una carpeta excluida
    # se lleva su contenido).
    return "(?:%s)(?:/.*)?\\Z" % cuerpo


class IgnoreMatcher:
    """Patrones de ignorado precompilados.

    En vez de recorrer todos los patrones por cada archivo, compila una
    sola vez:
      · nombres literales      → conjuntos, coste O(1) por archivo
      · comodines de nombre    → UNA regex combinada
      · patrones con ruta      → UNA regex combinada (anclados a la raíz)

    Las reglas que se siguen son las de git, no las de `fnmatch`: «**»
    cruza carpetas, «*» y «?» no, «[…]» admite «!» para negar el
    conjunto y «\\» escapa. Equivocarse aquí deja publicado (y dentro del
    TXT) un archivo que se mandó excluir.
    """

    __slots__ = ("lit_names", "lit_dirs", "lit_paths", "name_rx", "path_rx",
                 "empty")

    def __init__(self, patterns):
        lit_names, lit_dirs, lit_paths = set(), set(), set()
        globs, paths = [], []
        for raw in patterns or ():
            p = trim_pattern(raw)
            if not p or p[0] in "#!":
                continue
            only_dir = p.endswith("/")
            p = p.rstrip("/")
            anchored = p.startswith("/")
            p = p.lstrip("/")
            if not p:
                continue
            # Una ruta o un nombre sin comodines sin escapar es literal:
            # se compara tal cual, que además es más rápido que una regex.
            # «**/nombre» es «ese nombre a cualquier profundidad», que
            # para git es exactamente un patrón de NOMBRE: se le quita el
            # «**/» y entra por la vía rápida. Si lo que queda lleva
            # barra («**/secretos/*»), el «**» tiene que conservarse: ahí
            # sí significa «cero o más carpetas» y lo traduce la regex.
            # Y la «/» del principio deja de importar: «/**/x» es «x a
            # cualquier profundidad», no «x en la raíz» (tratarlo como
            # anclado dejaba fuera solo el de la raíz y metía en el TXT
            # todos los demás).
            if p.startswith("**/") and "/" not in p[3:]:
                anchored = False
            while p.startswith("**/") and "/" not in p[3:]:
                p = p[3:]
            if not p:
                continue
            comodines = has_wildcards(p)
            literal = unescape_pattern(p).lower()
            if anchored or "/" in p:
                if comodines:
                    paths.append(p.lower())
                else:
                    lit_paths.add(literal)
            elif comodines:
                globs.append(p.lower())
            else:
                lit_dirs.add(literal)
                if not only_dir:
                    lit_names.add(literal)
        self.lit_names = lit_names
        self.lit_dirs = lit_dirs
        self.lit_paths = lit_paths
        self.name_rx = re.compile(
            "|".join(segment_regex(g) + "\\Z" for g in globs)
        ) if globs else None
        # Un patrón con ruta cubre la ruta exacta y todo lo que cuelga.
        self.path_rx = re.compile(
            "|".join(pattern_regex(p) for p in paths)) if paths else None
        self.empty = not (lit_names or lit_dirs or lit_paths or globs
                          or paths)

    def _literal_path_hit(self, rel_lower):
        """La ruta exacta, o cualquier carpeta de la que cuelgue."""
        if rel_lower in self.lit_paths:
            return True
        corte = rel_lower.rfind("/")
        while corte > 0:
            if rel_lower[:corte] in self.lit_paths:
                return True
            corte = rel_lower.rfind("/", 0, corte)
        return False

    def ignored(self, rel_lower, name_lower, is_dir):
        """Para recorridos que podan directorios: solo mira esta entrada."""
        if self.empty:
            return False
        if name_lower in (self.lit_dirs if is_dir else self.lit_names):
            return True
        if self.lit_paths and self._literal_path_hit(rel_lower):
            return True
        if self.name_rx is not None and self.name_rx.match(name_lower):
            return True
        if self.path_rx is not None and self.path_rx.match(rel_lower):
            return True
        return False

    def ignored_path(self, rel_lower):
        """Para listas planas (sin poda): comprueba el archivo y cada
        carpeta ancestro, como haría el recorrido."""
        if self.empty:
            return False
        parts = rel_lower.split("/")
        prefix = ""
        for seg in parts[:-1]:
            prefix = seg if not prefix else prefix + "/" + seg
            if self.ignored(prefix, seg, True):
                return True
        return self.ignored(rel_lower, parts[-1], False)


def clean_patterns(patterns):
    """Quita vacíos y espacios; admite una lista o un texto multilínea."""
    if isinstance(patterns, str):
        patterns = patterns.splitlines()
    return [p.strip() for p in (patterns or ()) if p and p.strip()]


def read_gitignore(folder):
    """Líneas del .gitignore de `folder` ([] si no hay). «utf-8-sig»: los
    editores de Windows ponen una marca BOM al principio, y con ella la
    primera regla («\ufeff.env») no casaría con nada."""
    gi = Path(folder) / ".gitignore"
    if not gi.is_file():
        return []
    try:
        return gi.read_text(encoding="utf-8-sig",
                            errors="replace").splitlines()
    except OSError:
        return []


def nested_rules(lines):
    """Reglas útiles de un .gitignore (sin comentarios ni vacías)."""
    out = []
    for ln in lines:
        t = trim_pattern(ln)
        if t and not t.startswith("#"):
            out.append(t)
    return out


def merged_patterns(folder, extra_patterns):
    """Cuadro de ignorar + .gitignore raíz, sin duplicados ni comentarios."""
    patterns = clean_patterns(extra_patterns)
    seen = set(patterns)
    for ln in read_gitignore(folder):
        ln = ln.strip()
        if ln and not ln.startswith("#") and ln not in seen:
            patterns.append(ln)
            seen.add(ln)
    return patterns


def append_gitignore(folder, patterns):
    """Añade al .gitignore los patrones que aún no estén. Devuelve los
    añadidos (vacío si no hizo falta escribir).

    Solo AÑADE: lo que ya había queda byte a byte igual (finales de
    línea, marca BOM, espacios), así que el diff del .gitignore muestra
    únicamente las líneas nuevas. Se escribe de forma atómica: un corte
    a mitad no deja un .gitignore truncado (que haría publicar lo que
    excluía). Si el .gitignore es un enlace, no se toca: escribir a
    través de él cambiaría un archivo de otro sitio.
    """
    gi = Path(folder) / ".gitignore"
    if os.path.islink(gi):
        raise StepError("Tu .gitignore es un enlace a otro archivo: no lo "
                        "modifico. Cámbialo por un archivo normal o añade "
                        "tú las exclusiones.")
    try:
        crudo = gi.read_bytes() if gi.is_file() else b""
    except OSError as e:
        raise StepError(f"No pude leer el .gitignore ({e}).") from None
    existentes = {ln.strip() for ln in
                  crudo.decode("utf-8-sig", "replace").splitlines()}
    new = []
    for p in clean_patterns(patterns):
        if p not in existentes and p not in new:
            new.append(p)
    if not new:
        return []
    nl = b"\r\n" if b"\r\n" in crudo else b"\n"
    extra = b""
    if crudo and not crudo.endswith((b"\n", b"\r")):
        extra += nl
    extra += nl.join(p.encode("utf-8") for p in new) + nl
    write_bytes(gi, crudo + extra)
    return new


def anchor_pattern(rel_path):
    """Convierte una RUTA CONCRETA en un patrón que solo la excluye a
    ella: «/sub/.env», y no cualquier «.env» del proyecto.

    Los comodines del nombre se escapan: «datos[2024].csv» tiene que
    excluir ese archivo y no «datos2024.csv». Anclar con «/» sirve
    además para que un nombre que empiece por «#» o «!» no se lea como
    comentario ni como negación.
    """
    # Solo se recortan los saltos de línea: un `strip()` se comería el
    # espacio final de «informe final .txt» y el patrón apuntaría a otro
    # archivo (o a ninguno). Y la barra invertida se trata como separador
    # únicamente donde lo es: en Linux y macOS es un carácter más del
    # nombre, y convertirla partiría la ruta en dos.
    p = rel_path.strip("\r\n")
    if os.sep == "\\" or os.altsep == "\\":
        p = p.replace("\\", "/")
    if not p:
        return p
    p = "/".join(escape_pattern(seg) for seg in p.split("/"))
    if p.endswith(" "):
        # Git recortaría el espacio final: escapado, lo conserva.
        p = p[:-1] + "\\ "
    return p if p.startswith("/") else "/" + p
