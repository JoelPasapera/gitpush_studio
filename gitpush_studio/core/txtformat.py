# -*- coding: utf-8 -*-
"""El formato del TXT del proyecto: cómo se escribe y cómo se vuelve a leer.

Escribir y leer viven juntos para que no puedan desincronizarse: las
mismas constantes que usa `pack` para generar el TXT son las que usa
este lector para reconocerlo.

Un TXT del proyecto es:

    MODULO RAIZ: nombre
    … presentación y árbol de carpetas …
    ================================================================

    MODULO: ruta/del/archivo.py
    ----------------------------------------------------------------
    contenido del archivo

    ================================================================

    MODULO: otra/ruta.txt
    …

    ################################################################
    RESUMEN
    ################################################################

El lector (`parse_txt`) entiende ese formato exacto y también lo que
suele devolver una IA cuando se le pasa el TXT y se le pide que cambie
algo: solo los módulos tocados, sin la raya, sin los separadores, con el
contenido entre marcas ``` de Markdown, con texto suyo antes o después…

Para no confundir con una cabecera algo que solo lo parece (un README
que explica este formato), una línea «MODULO: ruta» solo abre un módulo
si va al principio del texto, justo después de un separador («====»,
«####» o el cierre de un bloque ```), o si la sigue la raya «----» o la
apertura de un bloque ```. Dentro de un bloque ``` todo es contenido
hasta su cierre.

Lo que el TXT no trae entero (los binarios, los enlaces, lo que no se
pudo leer, lo omitido para la IA) aparece como una nota «<…>» en lugar
del contenido: el lector lo marca (`TxtModule.note`) para que nadie lo
tome por el contenido de verdad del archivo.
"""

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

# ------------------------------------------------------------------ #
#  Escritura                                                          #
# ------------------------------------------------------------------ #
ROOT_PREFIX = "MODULO RAIZ: "
PROFILE_PREFIX = "PERFIL DE GITHUB: "
MODULE_PREFIX = "MODULO: "
# Los bloques de diferencias del TXT «solo lo que cambió» no son el
# contenido de un archivo: llevan otra cabecera para que nadie los pueda
# aplicar como si lo fueran.
DIFF_PREFIX = "DIFERENCIAS: "

RULE_WIDTH = 80
DASH_LINE = "-" * RULE_WIDTH
EQUALS_LINE = "=" * RULE_WIDTH
HASH_LINE = "#" * RULE_WIDTH

SEP_DASH = (DASH_LINE + "\n").encode("ascii")
SEP_EQ = ("\n" + EQUALS_LINE + "\n\n").encode("ascii")

# Cómo EMPIEZAN los TXT que genera la aplicación (ver `pack`).
TXT_MARKERS = (ROOT_PREFIX.rstrip().encode("utf-8"),
               PROFILE_PREFIX.rstrip().encode("utf-8"))
TXT_STARTS = (ROOT_PREFIX.encode("utf-8"),
              (HASH_LINE + "\n" + PROFILE_PREFIX).encode("utf-8"))

# Comienzo de cada nota «<…>». El tipo es lo que devuelve el lector.
NOTE_BINARY = "ARCHIVO BINARIO OMITIDO"
NOTE_LINK = "ENLACE SIMBÓLICO"
NOTE_SPECIAL = "OMITIDO:"
NOTE_READ_ERROR = "ERROR AL LEER ARCHIVO"
NOTE_CUT = "ARCHIVO CORTADO"
NOTE_FOR_AI = "OMITIDO PARA IA"
NOTE_DELETED = "BORRADO"
NOTE_SECRET = "SECRETO TACHADO"
# Un archivo que no cabe en una sola parte del TXT dividido: el trozo de
# una parte acaba en «SIGUE EN…» y el de la siguiente empieza en «VIENE
# DE…». Ninguno de los dos trozos es el archivo entero.
NOTE_CONTINUES = "SIGUE EN LA PARTE"
NOTE_CONTINUED = "VIENE DE LA PARTE"

NOTE_KINDS = (
    (NOTE_BINARY, "binario"),
    (NOTE_LINK, "enlace"),
    (NOTE_SPECIAL, "especial"),
    (NOTE_READ_ERROR, "error"),
    (NOTE_CUT, "cortado"),
    (NOTE_FOR_AI, "omitido"),
    (NOTE_DELETED, "borrado"),
    (NOTE_SECRET, "secreto"),
)

# Notas que van DESPUÉS de un contenido que no está entero.
_INCOMPLETE_PREFIXES = (NOTE_CUT, NOTE_READ_ERROR + " a mitad",
                        NOTE_CONTINUES)
# Y la que va ANTES de un contenido al que le falta el principio.
_INCOMPLETE_START = NOTE_CONTINUED


def module_header(display_path: str) -> bytes:
    """«MODULO: ruta» y la raya, como bytes UTF-8."""
    return (MODULE_PREFIX + display_path + "\n").encode("utf-8") + SEP_DASH


def note(text: str) -> bytes:
    """Una nota «<texto>» en su propia línea, como bytes UTF-8."""
    return ("<" + text + ">\n").encode("utf-8")


# ------------------------------------------------------------------ #
#  Lectura                                                            #
# ------------------------------------------------------------------ #
RULE_MIN = 20          # una raya de separación tiene al menos 20 signos

_HEADER = re.compile(r"^(?:#{1,6}[ \t]+)?(?:\*\*)?MODULO:[ \t]*"
                     r"(?P<path>.*?)[ \t]*(?:\*\*)?[ \t]*$")
_FENCE_OPEN = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})[ \t]*"
                         r"(?P<info>[^`\s]*)[ \t]*$")
_FENCE_CLOSE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})[ \t]*$")
_REPO_BANNER = re.compile(r"^REPOSITORIO \d+/\d+: (?P<owner>[^/\s]+)/"
                          r"(?P<name>.+?)[ \t]*$")
_NOTE_LINE = re.compile(r"^<(?P<text>.+)>[ \t]*$")
# «· PARTE 2 DE 3» al final del nombre de la raíz (TXT dividido).
_PART_SUFFIX = re.compile(r"\s*·\s*PARTE \d+ DE \d+\s*$")
PART_TITLE = "{root} · PARTE {number} DE {total}"
# Lo que envuelve a veces la ruta: comillas, acentos graves, «».
_PATH_WRAPPERS = "`'\"«»*"


@dataclass(frozen=True)
class TxtModule:
    """Un archivo del TXT, tal como se leyó.

    `lines` es el cuerpo, una entrada por línea y sin saltos de línea
    («\\r» incluido). Si el cuerpo venía entre marcas ``` y se quitaron,
    `fenced_lines` guarda la versión con ellas: un archivo de Markdown
    puede empezar y acabar de verdad por ```, y quien aplica decide cuál
    de las dos se parece más al archivo real.
    """
    path: str
    repo: str
    lines: Tuple[str, ...]
    fenced_lines: Optional[Tuple[str, ...]]
    line_number: int
    note: str
    incomplete: bool
    terminated: bool
    ignored_tail: str

    @property
    def text(self) -> str:
        """El cuerpo como texto, acabado en un salto de línea (o vacío)."""
        if not self.lines:
            return ""
        return "\n".join(self.lines) + "\n"


@dataclass(frozen=True)
class ParsedTxt:
    """Lo que se leyó de un TXT (completo o un trozo devuelto por una IA).

    `kind` es "proyecto" (un TXT de la app de un proyecto), "perfil" (el
    de un perfil con varios repositorios) o "fragmento" (cualquier otra
    cosa: lo habitual en la respuesta de una IA).
    """
    kind: str
    root: str
    modules: Tuple[TxtModule, ...]


def _is_rule(line: str, char: str) -> bool:
    stripped = line.strip()
    return len(stripped) >= RULE_MIN and stripped == char * len(stripped)


def _clean_path(raw: str) -> str:
    """La ruta de una cabecera sin adornos: comillas, acentos graves,
    negritas, «./» del principio; las barras de Windows pasan a «/»."""
    path = raw.strip()
    previous = None
    while path != previous:
        previous = path
        path = path.strip().strip(_PATH_WRAPPERS).strip()
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _note_kind(text: str) -> str:
    for prefix, kind in NOTE_KINDS:
        if text.startswith(prefix):
            return kind
    return ""


class _Reader:
    """Recorre las líneas del TXT una vez, de arriba abajo."""

    def __init__(self, text: str) -> None:
        # Solo «\n» separa líneas: un «\r» suelto en mitad de una línea
        # es parte de su contenido. El «\r» final (CRLF) se quita para
        # reconocer la estructura, y también del contenido: quien aplica
        # usa el final de línea del archivo real.
        self.lines: List[str] = [line[:-1] if line.endswith("\r") else line
                                 for line in text.split("\n")]
        self.count = len(self.lines)

    # ---- tipos de línea ------------------------------------------------
    def header_path(self, index: int) -> str:
        """La ruta si la línea parece una cabecera «MODULO: ruta» (sin
        mirar el contexto), o ""."""
        match = _HEADER.match(self.lines[index])
        if match is None:
            return ""
        return _clean_path(match.group("path"))

    def previous_nonblank(self, index: int) -> Optional[int]:
        k = index - 1
        while k >= 0 and not self.lines[k].strip():
            k -= 1
        return k if k >= 0 else None

    def next_nonblank(self, index: int) -> Optional[int]:
        k = index + 1
        while k < self.count and not self.lines[k].strip():
            k += 1
        return k if k < self.count else None

    def is_separator(self, index: int) -> bool:
        line = self.lines[index]
        return (_is_rule(line, "=") or _is_rule(line, "#")
                or _FENCE_CLOSE.match(line) is not None)

    def opens_body(self, index: int) -> bool:
        """¿La línea que sigue a una cabecera la confirma (raya o ```)?"""
        following = index + 1
        if following >= self.count:
            return False
        line = self.lines[following]
        return _is_rule(line, "-") or _FENCE_OPEN.match(line) is not None

    def accepted_header(self, index: int) -> str:
        """La ruta si la línea abre de verdad un módulo, o ""."""
        path = self.header_path(index)
        if not path:
            return ""
        before = self.previous_nonblank(index)
        if before is None or self.is_separator(before):
            return path
        if self.opens_body(index):
            return path
        return ""

    def is_terminator(self, index: int) -> bool:
        """¿Es esta «====» el separador que cierra un módulo? Lo es si
        después solo hay líneas en blanco y luego otra cabecera, un
        bloque «####» (el resumen, otro repositorio), el cierre de un
        bloque ``` o el final del texto."""
        if not _is_rule(self.lines[index], "="):
            return False
        after = self.next_nonblank(index)
        if after is None:
            return True
        line = self.lines[after]
        return (bool(self.header_path(after)) or _is_rule(line, "#")
                or _FENCE_CLOSE.match(line) is not None)

    # ---- búsqueda de los límites ---------------------------------------
    def next_module_start(self, start: int) -> int:
        """La siguiente cabecera aceptada a partir de `start` (o el final)."""
        k = start
        while k < self.count:
            if self.accepted_header(k):
                return k
            k += 1
        return self.count

    def dashed_header(self, index: int) -> bool:
        """¿Es una cabecera seguida de su raya «----» (el formato exacto)?"""
        following = index + 1
        return (following < self.count and bool(self.header_path(index))
                and _is_rule(self.lines[following], "-"))

    def plain_body(self, start: int,
                   dashed: bool) -> Tuple[int, int, str, bool]:
        """Límites de un cuerpo sin ```: (fin del cuerpo, dónde seguir
        leyendo, texto descartado tras el último separador, ¿tenía
        separador?).

        El cuerpo acaba en el primer separador «====» de cierre o en la
        cabecera del módulo siguiente. Si el módulo tenía su raya (el
        formato exacto de la app), solo cuenta como siguiente una
        cabecera con su raya: un README que cite una cabecera entre ```
        no parte el archivo en dos. Si no la tenía (una IA que la quitó),
        vale cualquier cabecera aceptada.

        Si no hay ni separador ni cabecera, el cuerpo acaba en el ÚLTIMO
        «====» que haya (lo que venga detrás, el comentario final de una
        IA, se descarta y se informa) y, si tampoco hay ninguno, al final
        del texto.
        """
        k = start
        while k < self.count:
            if self.is_terminator(k):
                after = self.next_nonblank(k)
                return k, (self.count if after is None else after), "", True
            if dashed and self.dashed_header(k):
                return k, k, "", True
            if not dashed and self.accepted_header(k):
                return k, k, "", True
            k += 1
        last_rule = None
        for j in range(start, self.count):
            if _is_rule(self.lines[j], "="):
                last_rule = j
        if last_rule is None:
            return self.count, self.count, "", False
        tail = "\n".join(self.lines[last_rule + 1:]).strip()
        return last_rule, self.count, tail, True

    def closing_fence(self, start: int, fence: str) -> Optional[int]:
        """Índice del cierre de un bloque abierto con `fence` (``` o ~~~
        de al menos la misma longitud), o None."""
        for k in range(start, self.count):
            match = _FENCE_CLOSE.match(self.lines[k])
            if match is None:
                continue
            closing = match.group("fence")
            if closing[0] == fence[0] and len(closing) >= len(fence):
                return k
        return None


def _trim_blank_tail(lines: List[str]) -> List[str]:
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    return lines[:end]


def _detect_kind(reader: _Reader) -> Tuple[str, str]:
    first = reader.next_nonblank(-1)
    if first is None:
        return "fragmento", ""
    line = reader.lines[first]
    if line.startswith(ROOT_PREFIX):
        root = line[len(ROOT_PREFIX):].strip()
        part = _PART_SUFFIX.search(root)
        if part is not None:
            root = root[:part.start()].strip()
        return "proyecto", root
    if _is_rule(line, "#") and first + 1 < reader.count:
        second = reader.lines[first + 1]
        if second.startswith(PROFILE_PREFIX):
            return "perfil", second[len(PROFILE_PREFIX):].strip()
    return "fragmento", ""


def _classify(lines: List[str]) -> Tuple[str, bool]:
    """(tipo de nota o "", ¿incompleto?) de un cuerpo ya recortado."""
    if len(lines) == 1:
        match = _NOTE_LINE.match(lines[0])
        if match is not None:
            kind = _note_kind(match.group("text"))
            if kind:
                return kind, kind == "cortado"
    if lines:
        match = _NOTE_LINE.match(lines[-1])
        if match is not None and match.group("text").startswith(
                _INCOMPLETE_PREFIXES):
            return "", True
        match = _NOTE_LINE.match(lines[0])
        if match is not None and match.group("text").startswith(
                _INCOMPLETE_START):
            return "", True
    return "", False


def parse_txt(text: str) -> ParsedTxt:
    """Lee los módulos de un TXT del proyecto o de un trozo de él."""
    reader = _Reader(text)
    kind, root = _detect_kind(reader)
    modules: List[TxtModule] = []
    repo = ""
    index = 0
    while index < reader.count:
        line = reader.lines[index]
        if kind == "perfil":
            banner = _REPO_BANNER.match(line)
            if banner is not None:
                repo = banner.group("name")
        path = reader.accepted_header(index)
        if not path:
            index += 1
            continue

        body_start = index + 1
        dashed = (body_start < reader.count
                  and _is_rule(reader.lines[body_start], "-"))
        if dashed:
            body_start += 1
        fenced_lines: Optional[Tuple[str, ...]] = None
        body: Optional[List[str]] = None
        tail = ""
        terminated = True
        next_index = reader.count

        opening = (_FENCE_OPEN.match(reader.lines[body_start])
                   if body_start < reader.count else None)
        if opening is not None:
            close = reader.closing_fence(body_start + 1,
                                         opening.group("fence"))
            if close is not None:
                body = reader.lines[body_start + 1:close]
                fenced_lines = tuple(reader.lines[body_start:close + 1])
                next_index = reader.next_module_start(close + 1)
                leftover = [ln for ln in reader.lines[close + 1:next_index]
                            if ln.strip() and not _is_rule(ln, "=")
                            and not _is_rule(ln, "#")]
                tail = "\n".join(leftover).strip()
        if body is None:
            end, next_index, tail, terminated = reader.plain_body(body_start,
                                                                  dashed)
            body = reader.lines[body_start:end]

        trimmed = _trim_blank_tail(list(body))
        note_kind, incomplete = _classify(trimmed)
        modules.append(TxtModule(
            path=path, repo=repo, lines=tuple(trimmed),
            fenced_lines=fenced_lines, line_number=index + 1,
            note=note_kind, incomplete=incomplete, terminated=terminated,
            ignored_tail=tail))
        index = max(next_index, index + 1)
    return ParsedTxt(kind=kind, root=root, modules=tuple(modules))
