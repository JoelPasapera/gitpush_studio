# -*- coding: utf-8 -*-
"""Qué cuenta como secreto: formatos de proveedores, nombres de archivo
que delatan claves, y cómo distinguir un valor real de un ejemplo.

Funciones puras y tablas; quien recorre los archivos es `scan`.

Tres niveles de confianza, que deciden qué hace la publicación:

    alta   casi seguro un secreto (un token con el formato exacto de un
           proveedor, una llave privada). Se marca para excluir.
    media  probable (una contraseña escrita en una asignación). También
           para la publicación automática.
    baja   parece un ejemplo o una etiqueta («<tu-token>», «changeme»,
           «Contraseña»). Se enseña sin marcar y no detiene nada.

Las reglas trabajan sobre BYTES (sin descodificar el archivo entero) y
cada una declara unas «anclas»: subcadenas en minúsculas sin las que no
puede casar. Antes de lanzar una expresión regular se mira si alguna de
sus anclas está en el texto (búsqueda en C, `bytes.__contains__`); en la
mayoría de archivos casi ninguna regla llega a ejecutarse.
"""

import fnmatch
import math
import re
from collections import Counter

ALTA, MEDIA, BAJA = "alta", "media", "baja"
RANK = {ALTA: 0, MEDIA: 1, BAJA: 2}

# ------------------------------------------------------------------ #
#  Cómo es un valor                                                   #
# ------------------------------------------------------------------ #
# Señales fuertes de «esto es de mentira»: con una basta, aunque vaya
# dentro de otra palabra («my_example_key», «dummy-token»).
PLACEHOLDER_STRONG = ("example", "ejemplo", "sample", "placeholder",
                      "changeme", "cambiame", "cámbiame", "dummy", "redacted",
                      "lorem", "ipsum", "yourtoken", "yourkey", "yoursecret",
                      "yourpassword")
# Palabras de relleno. Solo cuentan si el valor ENTERO está hecho de ellas
# (más números y signos) y al menos una es de las que delatan un ejemplo:
# «your_api_key_here», «test123», «tu_clave_aqui». Como subcadenas daban
# falsos «ejemplos»: «Contest2024!» lleva «test», «Here4You!» lleva
# «here», y en automático un «parece de ejemplo» no detiene la publicación.
PLACEHOLDER_MARKERS = frozenset((
    "your", "tu", "tus", "su", "here", "aqui", "aquí", "insert", "replace",
    "put", "pon", "enter", "escribe", "write", "test", "testing", "prueba",
    "demo", "todo", "fake", "falso", "falsa", "foo", "bar", "baz", "foobar",
    "none", "null", "nil", "undefined", "changeit", "goes", "va", "xxx"))
PLACEHOLDER_NOUNS = frozenset((
    "my", "mi", "me", "api", "key", "keys", "token", "secret", "secreto",
    "password", "passwd", "pass", "pwd", "clave", "contraseña", "contrasena",
    "value", "valor", "string", "default", "access", "auth", "client", "app",
    "user", "usuario", "the", "of", "de", "la", "el", "to", "a"))
# Valores que, escritos tal cual (sin nada más), son de relleno.
PLACEHOLDER_VALUES = frozenset((
    "password", "passwd", "secret", "changeme", "admin", "root", "pass",
    "token", "apikey", "api_key", "key", "undefined", "default", "string",
    "contraseña", "contrasena", "clave", "mypassword", "yourpassword",
    "qwerty", "letmein", "true", "false", "yes", "no", "on", "off",
    "123456", "1234567", "12345678", "123456789", "abc123", "abcdef",
    "foo", "bar", "foobar", "test", "demo", "prueba", "xxx"))
# Plantillas: el valor de verdad está en otro sitio (entorno, gestor de
# secretos, motor de plantillas). Un «$» o unas llaves sueltas DENTRO de
# una contraseña no lo son: «Xk9#mQ2$vL7p» es una contraseña de verdad.
_TEMPLATE = re.compile(
    r"\$\{[^}]*\}"                      # ${VAR}
    r"|\$\([^)]*\)"                     # $(orden)
    r"|\{\{.*?\}\}"                      # {{ var }}
    r"|\{%.*?%\}"                        # {% etiqueta %}
    r"|^\$[A-Z_][A-Z0-9_]*$"             # $VAR (en mayúsculas)
    r"|^%[A-Za-z_][A-Za-z0-9_]*%$"       # %VAR%
    r"|^<[^<>]+>$"                       # <tu-token>
    r"|^\{[A-Za-z_][\w.-]*\}$"            # {password}
    r"|^(?i:bearer|token|basic)\s+<[^<>]+>$")
# Palabras de un valor, también en camelCase: «yourApiKeyHere».
_PALABRAS = re.compile(r"[A-ZÁÉÍÓÚÑ]?[a-záéíóúñ]+|[A-ZÁÉÍÓÚÑ]+(?![a-záéíóúñ])")


def entropy(data):
    """Entropía de Shannon (bits por símbolo) de `data` (bytes o str)."""
    n = len(data)
    if not n:
        return 0.0
    return -sum(c / n * math.log2(c / n) for c in Counter(data).values())


def looks_like_placeholder(value):
    """¿El valor parece de mentira? («changeme», «<tu-token>», «${KEY}»,
    «your_api_key_here», «xxxxxxxx», «abcdefgh»).

    Equivocarse aquí hacia el «sí» es lo caro: un valor que parece de
    ejemplo se informa con confianza baja y la publicación automática no
    se detiene por él. Por eso las palabras de relleno no cuentan como
    subcadenas sueltas, y un «$» dentro de una contraseña no la convierte
    en una variable."""
    v = (value or "").strip()
    if not v:
        return True
    low = v.lower()
    if low in PLACEHOLDER_VALUES:
        return True
    if _TEMPLATE.search(v):
        return True
    if any(w in low for w in PLACEHOLDER_STRONG):
        return True
    if "xxxx" in v or "XXXX" in v:
        return True
    palabras = [w.lower() for w in _PALABRAS.findall(v)]
    if palabras and any(w in PLACEHOLDER_MARKERS for w in palabras) and all(
            w in PLACEHOLDER_MARKERS or w in PLACEHOLDER_NOUNS
            for w in palabras):
        return True
    if len(set(v)) <= 2:                       # "aaaaaaaa", "xxxxxxxx"
        return True
    if set(v) <= set("*•x-_.X"):               # «********», «xxxx-xxxx»
        return True
    run = longest = 1                          # "abcdefghijk", "12345678"
    for a, b in zip(v, v[1:]):
        run = run + 1 if ord(b) - ord(a) == 1 else 1
        longest = max(longest, run)
    return longest >= 8


def low_variety(value):
    """Pocos símbolos distintos: un token de verdad no es «ghp_xxxx…»."""
    return len(set(value)) <= 4


def word_like(value):
    """Solo letras (con tildes), espacios y guiones: una etiqueta o una
    palabra («Contraseña», «Mot de passe»), no una clave."""
    letras = False
    for c in value:
        if c.isalpha():
            letras = True
        elif c not in " -'’":
            return False
    return letras


def sentence_like(value):
    return value.count(" ") >= 2


_DOTTED = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NUMBER = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


def code_reference(value):
    """¿Es una referencia de código y no un valor? (`self.password`,
    `os.environ`, `getenv`, una variable…)."""
    return bool(_DOTTED.match(value) or _IDENT.match(value))


# ------------------------------------------------------------------ #
#  Dónde está                                                         #
# ------------------------------------------------------------------ #
# En estos archivos «clave = valor» sin comillas es un VALOR; en código
# fuente, una expresión (`password = getpass()`), que no se juzga.
_CONFIG_EXTS = frozenset((
    "", ".env", ".envrc", ".properties", ".ini", ".cfg", ".conf", ".toml",
    ".yml", ".yaml", ".tfvars", ".sh", ".bash", ".zsh", ".ps1", ".bat",
    ".cmd", ".txt", ".md", ".rst", ".npmrc", ".pypirc", ".netrc",
    ".gitconfig", ".xml", ".plist", ".json", ".config"))
_I18N_PARTS = ("locale", "locales", "i18n", "l10n", "lang", "langs",
               "translations", "translation", "messages")
_I18N_EXTS = (".po", ".pot", ".xliff", ".xlf", ".strings", ".resx")


def is_config_file(rel_lower):
    nombre = rel_lower.rsplit("/", 1)[-1]
    if nombre.startswith(".env") or nombre in ("dockerfile", "makefile"):
        return True
    punto = nombre.rfind(".")
    ext = nombre[punto:] if punto > 0 else ("." + nombre.lstrip(".")
                                             if nombre.startswith(".")
                                             else "")
    return ext in _CONFIG_EXTS or nombre.endswith((".env", "-compose.yml",
                                                   "-compose.yaml"))


def is_translation_file(rel_lower):
    if rel_lower.endswith(_I18N_EXTS):
        return True
    return any(p in _I18N_PARTS for p in rel_lower.split("/")[:-1])


# ------------------------------------------------------------------ #
#  Jueces: deciden la confianza de cada coincidencia                  #
# ------------------------------------------------------------------ #
def _text(value):
    return value.decode("utf-8", "replace") if isinstance(value, bytes) \
        else value


def judge_token(value, base, rel_lower, quoted):
    """Formato exacto de un proveedor: alta, salvo relleno evidente."""
    v = _text(value)
    if low_variety(v) or looks_like_placeholder(v):
        return BAJA
    return base


def judge_sk_key(value, base, rel_lower, quoted):
    """«sk-…» también es el principio de muchos nombres de clase CSS:
    una clave de verdad lleva dígitos y es variada."""
    v = _text(value)
    if not any(c.isdigit() for c in v) or entropy(v) < 3.0:
        return None
    return judge_token(value, base, rel_lower, quoted)


_URL_VAR = re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*$")


def judge_password_in_url(value, base, rel_lower, quoted):
    v = _text(value)
    # En una URL de un script, «$password» es una variable de la shell.
    if _URL_VAR.match(v) or looks_like_placeholder(v):
        return BAJA
    return base


def judge_assignment(value, base, rel_lower, quoted):
    """`clave = valor`: lo más ruidoso, así que lo más juzgado."""
    v = _text(value).strip()
    if not v or _NUMBER.match(v) or v.lower() in ("true", "false", "null",
                                                  "none", "nil"):
        return None
    if v.lower().startswith(("http://", "https://", "file://")):
        return None                        # una URL (las de con clave: otra regla)
    if not quoted:
        if not is_config_file(rel_lower):
            return None                    # en código es una expresión
        if code_reference(v) and not any(c.isdigit() for c in v):
            return BAJA
    if looks_like_placeholder(v) or word_like(v) or sentence_like(v):
        return BAJA
    if is_translation_file(rel_lower):
        return BAJA
    if len(v) >= 24 and entropy(v) >= 4.0:
        return ALTA
    return base


# ------------------------------------------------------------------ #
#  Reglas de contenido                                                #
# ------------------------------------------------------------------ #
class Rule:
    """Una forma de secreto. `group` es el grupo con el valor que se
    juzga (0: toda la coincidencia); `judge(valor, base, ruta, entre
    comillas)` devuelve la confianza o None para descartar.

    Las reglas más ruidosas no usan una expresión sobre todo el texto
    sino un `finder(datos, minúsculas)` que salta de ancla en ancla con
    `bytes.find` y solo entonces prueba una expresión anclada: con
    alternativas e `(?i)`, el motor de `re` intenta la regla en CADA
    posición del archivo y la revisión iba a 8 MB/s.
    """

    __slots__ = ("label", "rx", "anchors", "confidence", "group", "judge",
                 "quoted_groups", "finder")

    def __init__(self, label, pattern, anchors, confidence=ALTA, group=0,
                 judge=judge_token, quoted_groups=(), finder=None):
        self.label = label
        self.rx = re.compile(pattern) if pattern is not None else None
        self.anchors = tuple(a.lower() for a in anchors)
        self.confidence = confidence
        self.group = group
        self.judge = judge
        self.quoted_groups = quoted_groups
        self.finder = finder

    def value_of(self, m):
        """(valor, ¿venía entre comillas?) de una coincidencia."""
        if isinstance(self.group, tuple):
            for g in self.group:
                v = m.group(g)
                if v is not None:
                    return v, g in self.quoted_groups
            return b"", False
        return m.group(self.group), self.group in self.quoted_groups

    def candidates(self, data, low):
        """(valor, ¿entre comillas?, posición) de cada coincidencia."""
        if self.finder is not None:
            yield from self.finder(data, low)
            return
        for m in self.rx.finditer(data):
            valor, comillas = self.value_of(m)
            yield valor, comillas, m.start()


# --- buscadores por anclas -------------------------------------------
# Tras el nombre de la variable: sufijo, separador y valor (con o sin
# comillas). Se aplica sobre el texto EN MINÚSCULAS (mismas posiciones
# que el original: `bytes.lower` solo toca ASCII) y el valor se recorta
# del original.
_AFTER_NAME = re.compile(
    rb"(?:[_\-.]?[a-z0-9_]*)?[\"']?[ \t]*(?:=>|:=|=|:)[ \t]*"
    rb"(?:\"([^\"\r\n]{8,200})\"|'([^'\r\n]{8,200})'|"
    rb"([^\s\"'`,;<>(){}\[\]]{8,200}))")
_STEMS = (b"secret", b"password", b"passwd", b"passphrase", b"pwd",
          b"token", b"credential", b"key")
# «key» sola casa con «monkey» o «primary_key»: solo cuenta detrás de
# estos prefijos (api_key, apikey, secret-key, access_key, private_key).
_KEY_PREFIXES = (b"api", b"secret", b"access", b"private", b"signing",
                 b"encryption", b"master", b"client")


def _key_prefixed(low, i):
    antes = low[max(0, i - 12):i]
    if antes.endswith((b"_", b"-", b".")):
        antes = antes[:-1]
    return antes.endswith(_KEY_PREFIXES)


def find_assignments(data, low):
    vistos = set()
    for stem in _STEMS:
        i = low.find(stem)
        while i != -1:
            if stem != b"key" or _key_prefixed(low, i):
                m = _AFTER_NAME.match(low, i + len(stem))
                if m and m.end() not in vistos:
                    vistos.add(m.end())
                    for g in (1, 2, 3):
                        if m.group(g) is not None:
                            a, b = m.span(g)
                            yield data[a:b], g != 3, i
                            break
            i = low.find(stem, i + 1)


_URL_CRED = re.compile(rb"[^\s:/@'\"`]+:([^\s@'\"`/]{3,})@[a-z0-9.\-\[]")
_SCHEME_CHARS = frozenset(b"abcdefghijklmnopqrstuvwxyz0123456789+.-")
DB_SCHEMES = frozenset((b"postgres", b"postgresql", b"mysql", b"mariadb",
                        b"mongodb", b"mongodb+srv", b"redis", b"rediss",
                        b"amqp", b"amqps", b"mssql", b"sqlserver", b"ftp",
                        b"sftp", b"ldap", b"ldaps", b"smtp", b"smtps"))
WEB_SCHEMES = frozenset((b"http", b"https"))


def _url_finder(esquemas):
    def finder(data, low):
        i = low.find(b"://")
        while i != -1:
            k = i
            while k > 0 and low[k - 1] in _SCHEME_CHARS and i - k < 16:
                k -= 1
            if low[k:i] in esquemas:
                m = _URL_CRED.match(low, i + 3)
                if m:
                    a, b = m.span(1)
                    yield data[a:b], False, k
            i = low.find(b"://", i + 3)
    return finder


_B = rb"(?![A-Za-z0-9_\-])"           # fin de token (también tras un «-»)

GENERIC_LABEL = "posible clave o contraseña escrita"

_B64 = re.compile(rb"[A-Za-z0-9+/=]")


def judge_private_key(value, base, rel_lower, quoted):
    """Lo que sigue a «-----BEGIN … PRIVATE KEY-----»: una llave de
    verdad trae base64 enseguida; la documentación suele poner «…»."""
    cuerpo = (value or b"").split(b"-----", 1)[0]   # hasta el «END»
    if len(_B64.findall(cuerpo[:300])) >= 40:
        return ALTA
    texto = _text(cuerpo)
    if "..." in texto or "…" in texto or looks_like_placeholder(
            texto.replace("\n", "").strip() or "x"):
        return BAJA
    return MEDIA


CONTENT_RULES = (
    Rule("llave privada dentro",
         rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
         rb"([\s\S]{0,300})", (b"private key",), group=1,
         judge=judge_private_key),
    Rule("llave privada de PuTTY dentro", rb"PuTTY-User-Key-File-\d+:",
         (b"putty-user-key-file",), judge=lambda *a: ALTA),
    Rule("token de GitHub dentro",
         rb"\b(?:ghp|gho|ghu|ghs|ghr)_([A-Za-z0-9]{36})" + _B,
         (b"ghp_", b"gho_", b"ghu_", b"ghs_", b"ghr_"), group=1),
    Rule("token de GitHub dentro",
         rb"\bgithub_pat_([A-Za-z0-9_]{22,})" + _B, (b"github_pat_",),
         group=1),
    Rule("token de GitLab dentro", rb"\bglpat-([A-Za-z0-9_\-]{20,})" + _B,
         (b"glpat-",), group=1),
    Rule("clave de AWS dentro",
         rb"\b(?:AKIA|ASIA|ABIA|ACCA)([0-9A-Z]{16})\b",
         (b"akia", b"asia", b"abia", b"acca"), group=1),
    Rule("clave secreta de AWS dentro",
         rb"(?i)aws_?secret_?(?:access_?)?key[\"']?\s*[:=]\s*[\"']?"
         rb"([A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])", (b"aws",), group=1),
    Rule("clave de Google dentro", rb"\bAIza([0-9A-Za-z_\-]{35})" + _B,
         (b"aiza",), group=1),
    Rule("secreto OAuth de Google dentro",
         rb"\bGOCSPX-([A-Za-z0-9_\-]{28})" + _B, (b"gocspx-",), group=1),
    Rule("token de Slack dentro",
         rb"\bxox[abposer]-([A-Za-z0-9\-]{10,})", (b"xox",), group=1),
    Rule("token de Slack dentro", rb"\bxapp-\d-([A-Za-z0-9\-]{10,})",
         (b"xapp-",), group=1),
    Rule("webhook de Slack dentro",
         rb"https://hooks\.slack\.com/services/T[A-Za-z0-9_]+/"
         rb"B[A-Za-z0-9_]+/([A-Za-z0-9_]{8,})", (b"hooks.slack.com",),
         group=1),
    Rule("clave de Stripe dentro", rb"\b(?:sk|rk)_live_([0-9a-zA-Z]{20,})",
         (b"_live_",), group=1),
    Rule("clave de pruebas de Stripe dentro",
         rb"\b(?:sk|rk)_test_([0-9a-zA-Z]{20,})", (b"_test_",),
         confidence=MEDIA, group=1),
    Rule("clave API (sk-…) dentro",
         rb"\bsk-((?:proj-|ant-(?:api\d\d-)?|svcacct-|admin-)?"
         rb"[A-Za-z0-9_\-]{20,})", (b"sk-",), group=1, judge=judge_sk_key),
    Rule("clave de SendGrid dentro",
         rb"\bSG\.([A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43})" + _B, (b"sg.",),
         group=1),
    Rule("token de npm dentro", rb"\bnpm_([A-Za-z0-9]{36})\b", (b"npm_",),
         group=1),
    Rule("token de PyPI dentro", rb"\bpypi-(AgE[A-Za-z0-9_\-]{50,})",
         (b"pypi-age",), group=1),
    Rule("token de Hugging Face dentro", rb"\bhf_([A-Za-z0-9]{34,})\b",
         (b"hf_",), group=1),
    Rule("token de bot de Telegram dentro",
         rb"\b\d{8,10}:(AA[0-9A-Za-z_\-]{33})" + _B, (b":aa",), group=1),
    Rule("webhook de Discord dentro",
         rb"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/"
         rb"\d+/([A-Za-z0-9_\-]{20,})", (b"discord",), group=1),
    Rule("clave de Azure Storage dentro",
         rb"AccountKey=([A-Za-z0-9+/]{40,}={0,2})", (b"accountkey=",),
         group=1),
    Rule("contraseña dentro de una cadena de conexión", None, (b"://",),
         judge=judge_password_in_url, finder=_url_finder(DB_SCHEMES)),
    Rule("contraseña dentro de una URL", None, (b"://",), confidence=MEDIA,
         judge=judge_password_in_url, finder=_url_finder(WEB_SCHEMES)),
    Rule("token JWT dentro",
         rb"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ([A-Za-z0-9_\-]{10,})\."
         rb"[A-Za-z0-9_\-]{10,}", (b"eyj",), confidence=MEDIA, group=1),
    Rule(GENERIC_LABEL,
         rb"define\(\s*['\"][A-Z0-9_]*(?:PASSWORD|PASS|SECRET|KEY|TOKEN|"
         rb"SALT)[A-Z0-9_]*['\"]\s*,\s*['\"]([^'\"\r\n]{6,200})['\"]",
         (b"define(",), confidence=MEDIA, group=1, judge=judge_assignment,
         quoted_groups=(1,)),
    Rule(GENERIC_LABEL, None,
         (b"key", b"secret", b"pass", b"pwd", b"token", b"credential"),
         confidence=MEDIA, judge=judge_assignment, finder=find_assignments,
         quoted_groups=(1, 2)),
)


ALL_ANCHORS = tuple(sorted({a for r in CONTENT_RULES for a in r.anchors}))
assert all(r.anchors for r in CONTENT_RULES), "toda regla necesita anclas"


# ------------------------------------------------------------------ #
#  Nombres de archivo                                                 #
# ------------------------------------------------------------------ #
NAME_EXCLUDE = (".env.example", ".env.sample", ".env.template",
                ".env.dist", ".env.defaults", ".env.schema", "*.pub",
                "*.example", "*.sample", "*.template")

# (patrón del nombre, motivo, confianza). Solo con mirar el nombre.
NAME_RULES = (
    (".env", "nombre típico de archivo con claves", ALTA),
    (".env.*", "nombre típico de archivo con claves", ALTA),
    ("*.env", "nombre típico de archivo con claves", ALTA),
    (".envrc", "nombre típico de archivo con claves", ALTA),
    ("id_rsa*", "llave privada SSH", ALTA),
    ("id_dsa*", "llave privada SSH", ALTA),
    ("id_ecdsa*", "llave privada SSH", ALTA),
    ("id_ed25519*", "llave privada SSH", ALTA),
    ("*.ppk", "llave privada de PuTTY", ALTA),
    ("*.p12", "almacén de claves", ALTA),
    ("*.pfx", "almacén de claves", ALTA),
    ("*.jks", "almacén de claves", ALTA),
    ("*.keystore", "almacén de claves", ALTA),
    ("*.kdbx", "base de datos de contraseñas", ALTA),
    (".git-credentials", "credenciales de git", ALTA),
    (".netrc", "credenciales guardadas", ALTA),
    ("_netrc", "credenciales guardadas", ALTA),
    (".pgpass", "credenciales guardadas", ALTA),
    (".htpasswd", "contraseñas de servidor web", ALTA),
    ("credentials.json", "nombre típico de archivo con claves", ALTA),
    ("*-credentials.json", "nombre típico de archivo con claves", ALTA),
    ("service-account*.json", "nombre típico de archivo con claves", ALTA),
    ("secrets.json", "nombre típico de archivo con claves", ALTA),
    ("secrets.yml", "nombre típico de archivo con claves", ALTA),
    ("secrets.yaml", "nombre típico de archivo con claves", ALTA),
    ("*.tfstate", "estado de Terraform (suele llevar claves)", ALTA),
    ("*.tfstate.backup", "estado de Terraform (suele llevar claves)", ALTA),
    ("*.tfvars", "variables de Terraform (suelen llevar claves)", MEDIA),
    ("kubeconfig", "configuración de Kubernetes con credenciales", MEDIA),
)
# Rutas concretas (no solo el nombre).
PATH_RULES = (
    (".aws/credentials", "credenciales de AWS", ALTA),
    (".kube/config", "configuración de Kubernetes con credenciales", MEDIA),
    (".docker/config.json", "credenciales de Docker", MEDIA),
)
# Nombres que SUELEN llevar claves, pero no siempre: se decide mirando
# dentro (un .pem con solo certificados es público; un .key puede ser
# una presentación de Keynote).
INSPECT_NAMES = ("*.pem", "*.key", "*.p8", ".npmrc", ".pypirc", ".yarnrc",
                 ".yarnrc.yml")


class GlobIndex:
    """Muchos patrones de nombre («.env», «*.pem», «id_rsa*»,
    «service-account*.json») comprobados en O(número de longitudes
    distintas) en vez de uno a uno con `fnmatch`.

    Cada patrón cae en la estructura que le toca: nombres exactos en un
    diccionario; «*SUFIJO» y «PREFIJO*», en diccionarios por longitud
    (se mira `nombre[-n:]` o `nombre[:n]` una vez por longitud); los de
    la forma «PRE*SUF» en una lista corta, y cualquier otro (con «?» o
    «[»), con `fnmatch`. Si casan varios, gana el primero de la lista
    original, igual que recorriéndola en orden.
    """

    __slots__ = ("exact", "suffix", "prefix", "both", "other", "payloads")

    def __init__(self, entries):
        self.exact, self.suffix, self.prefix = {}, {}, {}
        self.both, self.other, self.payloads = [], [], []
        for i, (glob, payload) in enumerate(entries):
            self.payloads.append(payload)
            if not any(c in glob for c in "*?["):
                self.exact.setdefault(glob, i)
            elif glob.count("*") == 1 and not any(c in glob for c in "?["):
                pre, suf = glob.split("*")
                if not pre:
                    self.suffix.setdefault(len(suf), {}).setdefault(suf, i)
                elif not suf:
                    self.prefix.setdefault(len(pre), {}).setdefault(pre, i)
                else:
                    self.both.append((pre, suf, i))
            else:
                self.other.append((glob, i))

    def index(self, name):
        """Posición del primer patrón que casa con `name`, o None."""
        mejor = self.exact.get(name)
        n = len(name)
        for largo, tabla in self.suffix.items():
            if largo <= n:
                i = tabla.get(name[n - largo:])
                if i is not None and (mejor is None or i < mejor):
                    mejor = i
        for largo, tabla in self.prefix.items():
            if largo <= n:
                i = tabla.get(name[:largo])
                if i is not None and (mejor is None or i < mejor):
                    mejor = i
        for pre, suf, i in self.both:
            if (mejor is None or i < mejor) and n >= len(pre) + len(suf) \
                    and name.startswith(pre) and name.endswith(suf):
                mejor = i
        for glob, i in self.other:
            if (mejor is None or i < mejor) and \
                    fnmatch.fnmatchcase(name, glob):
                mejor = i
        return mejor

    def match(self, name):
        i = self.index(name)
        return None if i is None else self.payloads[i]


_EXCLUDE = GlobIndex([(p, True) for p in NAME_EXCLUDE])
_NAMES = GlobIndex([(p, (motivo, conf)) for p, motivo, conf in NAME_RULES])
_INSPECT = GlobIndex([(p, True) for p in INSPECT_NAMES])


def name_rule(rel):
    """(motivo, confianza) si el nombre o la ruta delatan claves, o None.
    No mira el contenido."""
    low = rel.lower()
    name = low.rsplit("/", 1)[-1]
    if _EXCLUDE.match(name):
        return None
    regla = _NAMES.match(name)
    if regla:
        return regla
    for sufijo, motivo, conf in PATH_RULES:
        if low == sufijo or low.endswith("/" + sufijo):
            return motivo, conf
    return None


def needs_inspection(rel):
    """¿Es un nombre de los que hay que mirar por dentro sí o sí (aunque
    su extensión parezca binaria)?"""
    name = rel.lower().rsplit("/", 1)[-1]
    return bool(_INSPECT.match(name)) and not _EXCLUDE.match(name)


def looks_secret_name(name):
    """Compatibilidad: ¿delata el nombre un archivo de claves?"""
    return name_rule(name) is not None


def inspect_verdict(rel, raw):
    """Para los nombres de `INSPECT_NAMES` sin nada reconocible dentro:
    (motivo, confianza) o None. Un .pem de certificados públicos, un
    .key que en realidad es una presentación (ZIP) → nada."""
    if raw is None:
        return None
    if raw.startswith(b"PK\x03\x04"):
        return None                        # Keynote y otros paquetes ZIP
    cabeza = raw[:65536]
    if b"\x00" in cabeza[:4096]:
        return ("archivo de claves en formato binario", MEDIA)
    if b"CERTIFICATE-----" in cabeza or b"PUBLIC KEY-----" in cabeza:
        return None
    return None


# ------------------------------------------------------------------ #
#  Búsqueda en un texto                                               #
# ------------------------------------------------------------------ #
def find_secrets(data, rel_lower, lowered=None):
    """Mejor coincidencia de `data` (bytes): (motivo, confianza, posición)
    o None. Recorre TODAS las coincidencias de cada regla: la primera
    puede ser un ejemplo y la de debajo, la clave de verdad."""
    low = lowered if lowered is not None else data.lower()
    # Cada ancla distinta se busca UNA vez por archivo (varias reglas
    # comparten anclas). Medido: `bytes.__contains__` (memmem en C) por
    # ancla es más rápido que cualquier alternativa con expresiones
    # regulares, también que una expresión con forma de trie.
    presentes = {a for a in ALL_ANCHORS if a in low}
    if not presentes:
        return None
    mejor = None
    for regla in CONTENT_RULES:
        if regla.anchors and presentes.isdisjoint(regla.anchors):
            continue
        for valor, entre_comillas, pos in regla.candidates(data, low):
            conf = regla.judge(valor, regla.confidence, rel_lower,
                               entre_comillas)
            if conf is None:
                continue
            if mejor is None or RANK[conf] < RANK[mejor[1]]:
                motivo = regla.label
                if conf == BAJA:
                    motivo += " (parece un valor de ejemplo)"
                mejor = (motivo, conf, pos)
                if conf == ALTA:
                    return mejor            # no hay nada mejor que buscar
    return mejor
