# -*- coding: utf-8 -*-
"""El contenido de cada archivo tal como entra en el TXT.

Las mismas reglas para un archivo de 1 KB que para uno de 10 GB:

  · binario → no se vuelca. Lo decide el principio del archivo (un NUL
    en los primeros 4 KB, como hace git; ver `textenc.detect`), así que
    un binario enorme sin extensión conocida solo cuesta una lectura
    pequeña;
  · UTF-8, lo normal → se copia tal cual, sin decodificar ni recodificar;
  · bytes que no son UTF-8: si antes de ellos no hubo ningún carácter
    UTF-8 de varios bytes, el archivo está en la codificación clásica de
    Windows (ANSI: cp1252 en español, la de los CSV de Excel o los .txt
    antiguos; ver `textenc.legacy_encoding`) y se convierte, así que
    «año» sigue siendo «año»; si sí lo hubo, es UTF-8 con algún byte
    suelto roto, que se muestra como «�».
    En los dos casos el TXT entero es siempre UTF-8 válido;
  · UTF-16 (lo que escriben PowerShell, regedit o el Bloc de notas
    antiguo, con BOM o sin él) → se convierte a UTF-8: es texto legible,
    no un binario;
  · el BOM del principio se quita: en mitad del TXT no significa nada;
  · los blancos del final se recortan y el cuerpo acaba en un único
    salto de línea.

Los archivos grandes pasan por bloques (`stream_body`): la memoria usada
no depende de su tamaño. Un carácter partido entre dos bloques se
recompone con el siguiente, así que el resultado es idéntico, byte a
byte, al de procesar el archivo entero (las pruebas lo comprueban con
bloques de un solo byte).
"""

import codecs
import io
import os

from .textenc import detect, legacy_encoding

CHUNK = 1 << 20            # bloque de copia de los archivos grandes
HOLD_MAX = 8 << 20         # blancos seguidos que se guardan esperando
_PROBE = 4096              # lo que mira `detect` para decidir

# Los mismos que quita `bytes.rstrip()`.
_BLANCOS = frozenset(b" \t\n\r\x0b\x0c")


def content_end(blk):
    """Posición justo después del último byte que no es blanco (0 si todo
    lo es). No copia el bloque salvo en el caso raro de una cola de
    blancos muy larga."""
    i = len(blk)
    tope = i - 64 if i > 64 else 0
    while i > tope and blk[i - 1] in _BLANCOS:
        i -= 1
    if i > tope or i == 0:
        return i
    return len(bytes(blk[:i]).rstrip())


def _es_utf8(raw):
    try:
        raw.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


class TextBody(object):
    """Convierte, bloque a bloque, el contenido de un archivo de texto en
    lo que entra en el TXT, y lo entrega a `write` (bytes UTF-8).

        cuerpo = TextBody(out.write, codificacion, bom)
        cuerpo.feed(bloque)          # tantas veces como haga falta
        cuerpo.close()               # resuelve lo pendiente y pone el \\n

    Nunca se queda con una referencia a un bloque recibido: quien llama
    puede reutilizar su búfer en cuanto `feed` vuelve.
    """

    def __init__(self, write, encoding="utf-8", bom=0):
        self._write = write
        self._saltar = bom
        # Decodificador para lo que no es UTF-8 limpio (UTF-16 desde el
        # principio; ANSI o UTF-8 con «�» desde el primer byte inválido).
        self._ancho = (None if encoding == "utf-8" else
                       codecs.getincrementaldecoder(encoding)("replace"))
        self.encoding = encoding
        self._multibyte = bool(bom)  # ¿ya hubo UTF-8 de varios bytes?
        self._resto = b""           # carácter UTF-8 partido entre bloques
        self._blancos = []          # blancos del final, aún sin escribir
        self._nblancos = 0
        self.written = 0            # bytes entregados a `write`

    # ---- entrada --------------------------------------------------------
    def feed(self, data, final=False):
        if self._saltar:
            k = min(self._saltar, len(data))
            data = data[k:]
            self._saltar -= k
        if self._ancho is not None:
            self._texto(self._ancho.decode(data, final))
            return
        if self._resto:
            data = self._resto + data
            self._resto = b""
        if not data:
            return
        if data.isascii():
            self._emit(data)
            return
        try:
            _, usado = codecs.utf_8_decode(data, "strict", final)
        except UnicodeDecodeError as e:
            # Lo ya escrito era válido y `data` empieza en un carácter
            # entero: decodificar desde aquí da lo mismo que decodificar
            # el archivo de una vez.
            self._ancho = self._desde_el_fallo(data, e.start)
            self._texto(self._ancho.decode(data, final))
            return
        if not self._multibyte:
            entero = data if usado == len(data) else bytes(data[:usado])
            self._multibyte = not entero.isascii()
        if usado < len(data):
            self._resto = bytes(data[usado:])
            self._emit(memoryview(data)[:usado])
        else:
            self._emit(data)

    def _desde_el_fallo(self, data, pos):
        """Decodificador a partir del primer byte que no es UTF-8.

        Si hasta ahí todo era ASCII, el archivo está en ANSI: lo anterior
        se lee igual en las dos codificaciones, así que da lo mismo que
        haberlo convertido desde el principio. Si ya hubo caracteres
        UTF-8 de varios bytes, es UTF-8 con un byte roto. Solo depende
        del contenido, no de cómo se partió en bloques.
        """
        if not self._multibyte and bytes(data[:pos]).isascii():
            self.encoding = legacy_encoding()
        else:
            self.encoding = "utf-8"
        return codecs.getincrementaldecoder(self.encoding)("replace")

    def close(self):
        """Fin del archivo: lo pendiente se resuelve (un carácter que quedó
        a medias es «�»), los blancos del final se descartan y el cuerpo
        termina en un salto de línea."""
        self.feed(b"", final=True)
        self._blancos = []
        self._nblancos = 0
        self._out(b"\n")

    # ---- salida ---------------------------------------------------------
    def _texto(self, texto):
        if texto:
            self._emit(texto.encode("utf-8"))

    def _emit(self, blk):
        fin = content_end(blk)
        if fin == 0:
            # Todo blancos: esperan a ver si viene algo detrás. Una cola
            # descomunal no se guarda en memoria: se escribe tal cual.
            self._blancos.append(bytes(blk))
            self._nblancos += len(blk)
            if self._nblancos > HOLD_MAX:
                self._soltar()
            return
        if self._blancos:
            self._soltar()
        if fin == len(blk):
            self._out(blk)
            return
        self._out(memoryview(blk)[:fin])
        self._blancos.append(bytes(blk[fin:]))
        self._nblancos = len(blk) - fin

    def _soltar(self):
        for blk in self._blancos:
            self._out(blk)
        self._blancos = []
        self._nblancos = 0

    def _out(self, blk):
        self._write(blk)
        self.written += len(blk)


def small_body(raw):
    """El cuerpo de un archivo leído entero: bytes listos para el TXT
    (acaban en un salto de línea), o None si es binario.

    El caso de siempre —UTF-8 sin BOM que ya acaba en un salto de línea—
    no copia nada: se devuelve el mismo objeto.
    """
    codificacion, bom = detect(raw, strict=True)
    if codificacion is None:
        return None
    if codificacion == "utf-8" and not bom and (raw.isascii()
                                                 or _es_utf8(raw)):
        fin = content_end(raw)
        if fin == len(raw) - 1 and raw[-1] == 10:
            return raw
        return raw[:fin] + b"\n"
    partes = []
    cuerpo = TextBody(partes.append, codificacion, bom)
    cuerpo.feed(raw)
    cuerpo.close()
    return b"".join(partes)


def _fill(fh, mv, quiero):
    """Lee hasta `quiero` bytes en `mv`: menos solo si se acaba el
    archivo."""
    hay = 0
    while hay < quiero:
        n = fh.readinto(mv[hay:quiero])
        if not n:
            break
        hay += n
    return hay


def stream_body(fd, size, write, tick=None, chunk=CHUNK):
    """El cuerpo de un archivo ya abierto (`fd`, que medía `size` bytes al
    abrirlo), copiado por bloques a `write` sin cargarlo entero.

    `tick(bytes_escritos)` se llama tras cada bloque (progreso, y puede
    lanzar una excepción para detener la copia). Devuelve (estado,
    bytes_escritos, error):

        "binary"  no se escribió nada: es binario;
        "text"    entero;
        "cut"     creció mientras se leía: está tal como medía al abrirlo;
        "error"   falló la lectura a mitad: está lo leído hasta ahí.

    Se lee como mucho `size` bytes: un archivo que no para de crecer (un
    registro en uso) no alarga la copia sin fin.

    Solo los fallos al LEER se convierten en "error"; uno al escribir (el
    disco lleno) sale como excepción, porque el TXT ya no vale.
    """
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
    except (AttributeError, OSError):
        pass                                  # solo existe en Linux
    fh = io.FileIO(fd, "rb", closefd=False)
    # El primer bloque llega siempre a lo que mira `detect`: decidir si es
    # binario no puede depender del tamaño de bloque.
    buf = bytearray(max(1, min(max(chunk, _PROBE), size)))
    mv = memoryview(buf)
    try:
        n = _fill(fh, mv, min(len(buf), size))
    except OSError as e:
        return "error", 0, e
    codificacion, bom = detect(bytes(mv[:min(n, _PROBE)]), strict=True)
    if codificacion is None:
        return "binary", 0, None
    cuerpo = TextBody(write, codificacion, bom)
    restante = size - n
    while True:
        # El búfer lleno se pasa tal cual (sin copiar); solo el último
        # bloque, más corto, se copia.
        cuerpo.feed(buf if n == len(buf) else bytes(mv[:n]))
        if tick is not None:
            tick(cuerpo.written)
        if restante <= 0:
            break
        try:
            n = _fill(fh, mv, min(chunk, len(buf), restante))
        except OSError as e:
            cuerpo.close()
            return "error", cuerpo.written, e
        if not n:
            break                              # se encogió mientras tanto
        restante -= n
    try:
        crecio = bool(fh.read(1))
    except OSError:
        crecio = False
    cuerpo.close()
    return ("cut" if crecio else "text"), cuerpo.written, None
