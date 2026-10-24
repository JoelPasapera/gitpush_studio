# -*- coding: utf-8 -*-
"""Qué lenguaje es cada archivo y cómo se escriben en él los comentarios.

Es la única tabla del proyecto que sabe de sintaxis. `linecount` la usa
para separar código, comentarios y líneas en blanco; `analytics` para
agrupar por lenguaje. No guarda colores: el informe usa una paleta
comprobada para daltonismo, y la de GitHub no lo está (el azul de Python
y el de TypeScript, por ejemplo, casi no se distinguen).

Cada `Language` describe sus TOKENS como fragmentos de expresión regular
(bytes): así una regla como «en el shell, «#» solo abre comentario al
principio de palabra» se escribe una vez aquí y el clasificador no
necesita saber nada de shells. Las expresiones se compilan con
`re.MULTILINE`, de modo que «^» significa principio de línea.
"""

import os
import re
from dataclasses import dataclass

# Tipos de contenido. El informe distingue el código de verdad del resto:
# un proyecto con 40 000 líneas de JSON no tiene 40 000 líneas de código.
CODE, MARKUP, DATA, PROSE = "código", "marcado", "datos", "texto"

# --- Cadenas: (apertura literal, expresión que casa la cadena entera) ---
# Las de una línea no cruzan el salto (en casi todos los lenguajes es un
# error), así una comilla suelta no se come el resto del archivo. Las
# multilínea admiten el final del archivo como cierre.
# Ninguna puede FALLAR: si una cadena sin cerrar no casara, lo que sigue
# a la comilla se leería como código y un «#» de dentro pasaría por
# comentario. Por eso la barra invertida final admite no tener nada
# detrás («\\[\s\S]?»).
_DQ = (b'"', rb'"(?:[^"\\\n]|\\.)*"?')
_SQ = (b"'", rb"'(?:[^'\\\n]|\\.)*'?")
_TDQ = (b'"""', rb'"""(?:[^"\\]|\\[\s\S]?|"(?!""))*(?:"""|\Z)')
_TSQ = (b"'''", rb"'''(?:[^'\\]|\\[\s\S]?|'(?!''))*(?:'''|\Z)")
_TEMPLATE = (b"`", rb"`(?:[^`\\]|\\[\s\S]?)*(?:`|\Z)")     # JS, TS
_RAW_BT = (b"`", rb"`[^`]*(?:`|\Z)")                       # Go
_CS = (_DQ, _SQ)
_JS = (_TEMPLATE, _DQ, _SQ)
_PY = (_TDQ, _TSQ, _DQ, _SQ)

# --- Comentarios frecuentes -------------------------------------------
_SLASH = (rb"//",)
_C_BLOCK = ((b"/*", b"*/"),)
_HASH = (rb"#",)
# «#» solo al principio de palabra: «${#var}», «url#ancla» o «$#» no son
# comentarios en el shell ni en YAML.
_HASH_WORD = (rb"(?<![^\s;|&(])#",)
_XML_BLOCK = ((b"<!--", b"-->"),)


@dataclass(frozen=True)
class Language:
    name: str
    kind: str = CODE
    line: tuple = ()          # fragmentos regex de comentario de línea
    block: tuple = ()         # ((apertura, cierre), …) literales
    nested: bool = False      # los bloques se anidan (Rust, Swift…)
    strings: tuple = ()       # ((apertura, regex de la cadena), …)
    # Aperturas de cadena que, si EMPIEZAN la línea, son documentación
    # (los docstrings de Python): así las cuentan cloc y tokei.
    docstrings: tuple = ()

    @property
    def has_comments(self):
        return bool(self.line or self.block)


def _c_like(name, nested=False, strings=_CS, extra_line=()):
    return Language(name, CODE, _SLASH + extra_line, _C_BLOCK, nested,
                    strings)


L = Language
LANGS = {
    "Python": L("Python", CODE, _HASH, (), False, _PY,
                (b'"""', b"'''")),
    "JavaScript": _c_like("JavaScript", strings=_JS),
    "TypeScript": _c_like("TypeScript", strings=_JS),
    "JSX": _c_like("JSX", strings=_JS),
    "TSX": _c_like("TSX", strings=_JS),
    "C": _c_like("C"),
    "C++": _c_like("C++"),
    "C#": _c_like("C#"),
    "Java": _c_like("Java"),
    "Kotlin": _c_like("Kotlin", nested=True,
                      strings=(_TDQ, _DQ, _SQ)),
    "Scala": _c_like("Scala", nested=True,
                     strings=(_TDQ, _DQ, _SQ)),
    "Swift": _c_like("Swift", nested=True,
                     strings=(_TDQ, _DQ)),
    "Rust": _c_like("Rust", nested=True, strings=(_DQ,)),
    "Go": _c_like("Go", strings=(_RAW_BT, _DQ, _SQ)),
    "Dart": _c_like("Dart", nested=True,
                    strings=(_TDQ, _TSQ, _DQ, _SQ)),
    "PHP": Language("PHP", CODE, _SLASH + (rb"#(?!\[)",), _C_BLOCK, False,
                    _CS),
    "MQL": _c_like("MQL"),
    "WGSL": _c_like("WGSL", nested=True, strings=()),
    "GLSL": _c_like("GLSL", strings=()),
    "Objective-C": _c_like("Objective-C"),
    "Ruby": L("Ruby", CODE, _HASH, (), False, _CS),
    "Perl": L("Perl", CODE, _HASH_WORD, (), False, _CS),
    "Shell": L("Shell", CODE, _HASH_WORD, (), False, _CS),
    "PowerShell": L("PowerShell", CODE, _HASH_WORD, ((b"<#", b"#>"),),
                    False, _CS),
    "Batch": L("Batch", CODE, (rb"^[ \t]*(?:(?i:rem)\b|::)",), (), False,
               ()),
    "Lua": L("Lua", CODE, (rb"--",), ((b"--[[", b"]]"),), False, _CS),
    "SQL": L("SQL", CODE, (rb"--",), _C_BLOCK, False, (_SQ, _DQ)),
    "R": L("R", CODE, _HASH, (), False, _CS),
    "Haskell": L("Haskell", CODE, (rb"--",), ((b"{-", b"-}"),), True,
                 (_DQ,)),
    "Elixir": L("Elixir", CODE, _HASH, (), False, (_TDQ, _DQ, _SQ)),
    "Visual Basic": L("Visual Basic", CODE,
                      (rb"'", rb"^[ \t]*(?i:rem)\b"), (), False, (_DQ,)),
    "LaTeX": L("LaTeX", MARKUP, (rb"(?<!\\)%",), (), False, ()),
    "HTML": L("HTML", MARKUP, (), _XML_BLOCK, False, ()),
    "XML": L("XML", MARKUP, (), _XML_BLOCK, False, ()),
    "SVG": L("SVG", MARKUP, (), _XML_BLOCK, False, ()),
    "Vue": L("Vue", MARKUP, _SLASH, _XML_BLOCK + _C_BLOCK, False, _JS),
    "Svelte": L("Svelte", MARKUP, _SLASH, _XML_BLOCK + _C_BLOCK, False,
                _JS),
    "CSS": L("CSS", MARKUP, (), _C_BLOCK, False, _CS),
    "SCSS": L("SCSS", MARKUP, _SLASH, _C_BLOCK, False, _CS),
    "Less": L("Less", MARKUP, _SLASH, _C_BLOCK, False, _CS),
    "JSON": L("JSON", DATA, (), (), False, ()),
    "JSON con comentarios": L("JSON con comentarios", DATA, _SLASH,
                              _C_BLOCK, False, (_DQ,)),
    "YAML": L("YAML", DATA, _HASH_WORD, (), False, _CS),
    "TOML": L("TOML", DATA, _HASH, (), False, (_TDQ, _TSQ, _DQ, _SQ)),
    "INI": L("INI", DATA, (rb"^[ \t]*[;#]",), (), False, ()),
    "Variables de entorno": L("Variables de entorno", DATA,
                              (rb"^[ \t]*#",), (), False, ()),
    "CSV": L("CSV", DATA),
    "Protocol Buffers": _c_like("Protocol Buffers"),
    "GraphQL": L("GraphQL", DATA, _HASH, (), False, (_TDQ, _DQ)),
    "Terraform": L("Terraform", CODE, _HASH + _SLASH, _C_BLOCK, False,
                   (_DQ,)),
    "Dockerfile": L("Dockerfile", CODE, (rb"^[ \t]*#",), (), False, ()),
    "Makefile": L("Makefile", CODE, _HASH_WORD, (), False, ()),
    "CMake": L("CMake", CODE, _HASH, (), False, (_DQ,)),
    "Git": L("Git", DATA, (rb"^[ \t]*#",), (), False, ()),
    "Markdown": L("Markdown", PROSE, (), _XML_BLOCK, False, ()),
    "reStructuredText": L("reStructuredText", PROSE),
    "Texto": L("Texto", PROSE),
    "Jupyter Notebook": L("Jupyter Notebook", DATA),
    "BibTeX": L("BibTeX", DATA, (rb"(?<!\\)%",), (), False, ()),
}

EXTENSIONS = {
    ".py": "Python", ".pyw": "Python", ".pyi": "Python",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".mts": "TypeScript", ".cts": "TypeScript",
    ".jsx": "JSX", ".tsx": "TSX",
    ".c": "C", ".h": "C",
    ".cc": "C++", ".cpp": "C++", ".cxx": "C++", ".c++": "C++",
    ".hh": "C++", ".hpp": "C++", ".hxx": "C++", ".ino": "C++",
    ".cs": "C#", ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin",
    ".scala": "Scala", ".sc": "Scala", ".swift": "Swift", ".rs": "Rust",
    ".go": "Go", ".dart": "Dart", ".php": "PHP",
    ".mq4": "MQL", ".mq5": "MQL", ".mqh": "MQL",
    ".wgsl": "WGSL", ".glsl": "GLSL", ".vert": "GLSL", ".frag": "GLSL",
    ".m": "Objective-C", ".mm": "Objective-C",
    ".rb": "Ruby", ".pl": "Perl", ".pm": "Perl",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell", ".fish": "Shell",
    ".ps1": "PowerShell", ".psm1": "PowerShell", ".psd1": "PowerShell",
    ".bat": "Batch", ".cmd": "Batch",
    ".lua": "Lua", ".sql": "SQL", ".r": "R", ".hs": "Haskell",
    ".ex": "Elixir", ".exs": "Elixir", ".vb": "Visual Basic",
    ".vbs": "Visual Basic", ".bas": "Visual Basic",
    ".tex": "LaTeX", ".sty": "LaTeX", ".cls": "LaTeX", ".bib": "BibTeX",
    ".html": "HTML", ".htm": "HTML", ".xhtml": "HTML",
    ".xml": "XML", ".xsd": "XML", ".xsl": "XML", ".csproj": "XML",
    ".svg": "SVG", ".vue": "Vue", ".svelte": "Svelte",
    ".css": "CSS", ".scss": "SCSS", ".sass": "SCSS", ".less": "Less",
    ".json": "JSON", ".jsonc": "JSON con comentarios",
    ".json5": "JSON con comentarios",
    ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML",
    ".ini": "INI", ".cfg": "INI", ".conf": "INI",
    ".properties": "INI", ".env": "Variables de entorno",
    ".csv": "CSV", ".tsv": "CSV", ".proto": "Protocol Buffers",
    ".graphql": "GraphQL", ".gql": "GraphQL",
    ".tf": "Terraform", ".tfvars": "Terraform",
    ".cmake": "CMake", ".dockerfile": "Dockerfile",
    ".md": "Markdown", ".markdown": "Markdown", ".rst": "reStructuredText",
    ".txt": "Texto", ".text": "Texto", ".adoc": "Texto",
    ".ipynb": "Jupyter Notebook",
}

# Nombres que delatan el lenguaje aunque no tengan extensión (o la
# tengan engañosa). En minúsculas.
FILENAMES = {
    "makefile": "Makefile", "gnumakefile": "Makefile",
    "dockerfile": "Dockerfile", "containerfile": "Dockerfile",
    "cmakelists.txt": "CMake",
    ".gitignore": "Git", ".gitattributes": "Git", ".gitmodules": "Git",
    ".dockerignore": "Git", ".npmignore": "Git",
    ".env": "Variables de entorno", ".editorconfig": "INI",
    ".bashrc": "Shell", ".zshrc": "Shell", ".profile": "Shell",
    "gemfile": "Ruby", "rakefile": "Ruby", "vagrantfile": "Ruby",
    "jenkinsfile": "Java",
}

# Intérpretes de la línea «#!» para archivos sin extensión.
SHEBANGS = {
    "python": "Python", "python3": "Python", "python2": "Python",
    "node": "JavaScript", "deno": "TypeScript", "bun": "JavaScript",
    "sh": "Shell", "bash": "Shell", "zsh": "Shell", "dash": "Shell",
    "ksh": "Shell", "fish": "Shell", "ruby": "Ruby", "perl": "Perl",
    "php": "PHP", "pwsh": "PowerShell", "lua": "Lua", "rscript": "R",
}

# Binarios, agrupados para el reparto de tamaño. No se leen.
BINARY_GROUPS = {
    "Imágenes": (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp",
                 ".tif", ".tiff", ".psd", ".ai", ".sketch", ".heic"),
    "Audio": (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"),
    "Vídeo": (".mp4", ".avi", ".mov", ".mkv", ".webm"),
    "Fuentes": (".woff", ".woff2", ".ttf", ".otf", ".eot"),
    "Comprimidos": (".zip", ".rar", ".7z", ".gz", ".tar", ".bz2", ".xz",
                    ".tgz"),
    "Documentos": (".pdf", ".docx", ".xlsx", ".pptx", ".doc", ".xls",
                   ".ppt", ".odt", ".ods"),
    "Ejecutables y bibliotecas": (".exe", ".dll", ".so", ".dylib", ".bin",
                                  ".class", ".jar", ".pyc", ".o", ".a",
                                  ".lib", ".wasm"),
}
_BINARY_BY_EXT = {ext: grupo for grupo, exts in BINARY_GROUPS.items()
                  for ext in exts}
OTHER_BINARY = "Otros binarios"
UNKNOWN_TEXT = "Texto"

_SHEBANG = re.compile(rb"^#![ \t]*(\S+)(?:[ \t]+(\S+))?")


def binary_group(rel):
    """Grupo de un archivo binario por su extensión."""
    return _BINARY_BY_EXT.get(os.path.splitext(rel)[1].lower(),
                              OTHER_BINARY)


def language_from_shebang(head):
    """«#!/usr/bin/env python3» → «Python». `head` son los primeros bytes."""
    m = _SHEBANG.match(head or b"")
    if not m:
        return None
    exe = m.group(1).rsplit(b"/", 1)[-1]
    if exe == b"env" and m.group(2):
        exe = m.group(2)
    nombre = exe.decode("ascii", "replace").lower()
    # «python3.12» → «python3»; «node18» no existe, pero por si acaso.
    for candidato in (nombre, nombre.rstrip("0123456789."),
                      re.sub(r"\d+(\.\d+)*$", "", nombre)):
        if candidato in SHEBANGS:
            return SHEBANGS[candidato]
    return None


def detect(rel, head=b""):
    """Nombre del lenguaje de `rel`, o None si no se reconoce.

    Orden: nombre exacto → extensión → «#!» (solo sin extensión
    conocida). `head` son los primeros bytes del archivo, si se tienen.
    """
    base = os.path.basename(rel).lower()
    if base in FILENAMES:
        return FILENAMES[base]
    if base.startswith(".env.") or base.startswith("dockerfile."):
        return FILENAMES[".env"] if base.startswith(".env.") \
            else "Dockerfile"
    ext = os.path.splitext(base)[1]
    if ext in EXTENSIONS:
        return EXTENSIONS[ext]
    if head.startswith(b"#!"):
        return language_from_shebang(head)
    return None


def get(name):
    """El `Language` de un nombre (el de texto plano si no existe)."""
    return LANGS.get(name) or LANGS[UNKNOWN_TEXT]
