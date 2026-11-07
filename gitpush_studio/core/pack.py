# -*- coding: utf-8 -*-
"""Consolidar proyectos en un único TXT (árbol + contenido), en streaming.

Sin límite de tamaño: cualquier archivo de texto entra ENTERO, mida lo
que mida, y el TXT crece lo que haga falta. Lo que no es texto legible
(los binarios) se menciona con su tamaño. La memoria usada no depende ni
del tamaño de los archivos ni del del proyecto:

  · los archivos pequeños se leen por delante, en paralelo y por lotes
    (`reader.iter_contents`), mientras se escribe lo anterior;
  · los grandes se copian por bloques (`txtbody.stream_body`);
  · el TXT se escribe con un búfer grande en un temporal que sustituye al
    anterior solo al terminar bien: si se detiene, falla o el disco se
    llena, el TXT anterior sigue entero.

Para no dejar el equipo sin disco, la escritura se detiene (sin guardar
nada) si el espacio libre baja de una reserva mínima.
"""

import errno
import os
import shutil
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import gitblobs
from .analytics import generated_reason
from .atomic import atomic_open
from .errors import Cancelled, StepError
from .fs import MB, display_name, fmt_size
from .preamble import changes_text, profile_text, project_text, summary_text
from .reader import (LARGE, SEQUENTIAL, Cut, Link, Special, iter_contents,
                     open_regular)
from .redact import Redactor, redact_bytes
from .txtbody import small_body, stream_body
from .txtchanges import (CHANGE_MODES, CHANGES_DIFF, CHANGES_NONE,
                         changes_since_last_commit, select_changed,
                         write_deleted, write_diff_modules)
from .txtformat import (NOTE_BINARY, NOTE_CUT, NOTE_FOR_AI, NOTE_LINK,
                        NOTE_READ_ERROR, NOTE_SPECIAL, SEP_EQ, TXT_STARTS,
                        module_header, note)
from .txtparts import estimate_tokens, split_txt, without_own_parts
from .walk import project_entries, render_tree

WRITE_BUFFER = 1 * MB           # búfer de escritura del TXT
PROGRESS_EVERY = 0.15           # segundos entre avisos de progreso
ROOM_EVERY = 64 * MB            # cada cuánto se mira el espacio libre
ROOM_RESERVE = 512 * MB         # libre que se respeta (o el 5 % del disco)

CANCELLED = ("Generación del TXT detenida. No se ha guardado nada: si ya "
             "había un TXT, sigue como estaba.")

# Cómo EMPIEZAN los TXT que genera la aplicación (`txtformat.TXT_STARTS`)
# sirve para distinguir «este TXT es mío, puedo rehacerlo» de «este
# archivo es del usuario, no se toca». Se exige el comienzo exacto: unas
# notas que CITEN esa cabecera más abajo no son un TXT nuestro.

# Lo que se cuenta al volcar (la base del resumen final).
_COUNTERS = ("text", "binaries", "links", "special", "errors", "cut",
             "text_bytes", "for_ai", "redacted", "redacted_files", "deleted",
             "diffs", "from_git")

# Lo que se mira del principio de un archivo grande para saber si es
# generado o minificado (para la opción «para una IA»).
NOISE_PEEK = 64 * 1024


@dataclass(frozen=True)
class PackOptions:
    """Cómo se escribe el TXT. Sin tocar nada, el TXT de siempre.

    for_ai        deja fuera (con una nota) lo que a una IA no le sirve
                  y le gasta contexto: archivos de bloqueo de dependencias,
                  minificados, generados y código de terceros copiado;
    redact        tacha los secretos («••••••••», ver `redact`);
    changes       "" todo el proyecto; "archivos" solo los archivos que
                  cambiaron desde el último commit, enteros; "diferencias"
                  solo sus líneas cambiadas (ver `txtchanges`);
    split_tokens  si es mayor que 0, reparte el TXT en partes de como
                  mucho esos tokens (ver `txtparts`).
    """
    for_ai: bool = False
    redact: bool = False
    changes: str = CHANGES_NONE
    split_tokens: int = 0


DEFAULT_OPTIONS = PackOptions()


def is_generated_txt(path):
    """¿Ese archivo es un TXT generado por la aplicación? Un enlace nunca
    lo es: rehacerlo escribiría donde apunte."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return False
        with open(path, "rb") as fh:
            cabeza = fh.read(256)
    except OSError:
        return False
    return cabeza.startswith(TXT_STARTS)


def _free(p):
    """¿Se puede escribir el TXT en `p` sin pisar nada ajeno?"""
    if not os.path.lexists(p):     # un enlace roto SÍ existe: está ocupado
        return True
    return is_generated_txt(p)


def free_txt_path(destino, limite=99):
    """Ruta donde escribir el TXT SIN pisar nada del usuario.

    El nombre por defecto es el de la carpeta, así que un proyecto
    llamado «notas» que ya tenga su propio «notas.txt» acabaría con los
    apuntes machacados. Aquí se comprueba antes. Si en el destino hay
    algo que no generamos nosotros (un archivo, una carpeta, o un enlace
    aunque apunte a algo que no existe), se busca «nombre-unificado.txt»,
    «nombre-unificado-2.txt»… Rehacer un TXT que sí es nuestro es justo
    lo que se quiere, y eso sí se sobrescribe.

    Devuelve (ruta, se_cambio_el_nombre).
    """
    p = Path(destino)
    if _free(p):
        return p, False
    sufijo = p.suffix or ".txt"
    for n in range(1, limite + 1):
        cola = "" if n == 1 else "-%d" % n
        cand = p.with_name(p.stem + "-unificado" + cola + sufijo)
        if _free(cand):
            return cand, True
    raise StepError(f"No encuentro un nombre libre para el TXT junto a "
                    f"«{p.name}»: ya existen {limite} variantes. Elige otro "
                    "destino.")


def _output_path(output_file):
    """Ruta final del TXT: la carpeta se resuelve (puede ser un enlace a
    otra carpeta, y hay que saber si cae dentro del proyecto), el NOMBRE
    no: si fuera un enlace, resolverlo escribiría donde apunta."""
    p = Path(output_file)
    return p.parent.resolve() / p.name


# ------------------------------------------------------------------ #
#  Espacio, progreso y cancelación                                    #
# ------------------------------------------------------------------ #
def _reserve(total):
    """Lo que se deja libre en un disco de `total` bytes: 512 MB, o el
    5 % en un disco pequeño (un pendrive)."""
    return min(ROOM_RESERVE, total // 20)


def check_room(folder, written):
    """Lanza StepError si en el disco de `folder` queda menos de la
    reserva. `written` es lo que ya ocupa el TXT (para el mensaje)."""
    try:
        uso = shutil.disk_usage(str(folder))
    except OSError:
        return
    if uso.free < _reserve(uso.total):
        raise StepError(
            f"Me detuve para no llenar el disco: en «{folder}» quedaban "
            f"{fmt_size(uso.free)} libres y el TXT ya ocupaba "
            f"{fmt_size(written)}. No se ha guardado nada (si ya había un "
            "TXT, sigue como estaba). Libera espacio, guárdalo en otro "
            "disco o excluye lo más pesado con el cuadro de ignorar.")


def _disk_full(e):
    return (getattr(e, "errno", None) == errno.ENOSPC
            or getattr(e, "winerror", None) in (39, 112))


class _Watch(object):
    """Progreso, cancelación y espacio libre mientras se escribe el TXT.

    Se llama con los bytes escritos hasta el momento: tras cada archivo y
    en cada bloque de los grandes, así que «Detener» responde también en
    mitad de un archivo de varios GB.
    """

    def __init__(self, folder, on_progress=None, cancel=None, total=0):
        self.folder = folder
        self.on_progress = on_progress
        self.cancel = cancel
        self.total = total            # archivos (0 si no se sabe aún)
        self.done = 0
        self._aviso = 0.0
        self._sitio = ROOM_EVERY

    def __call__(self, written):
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled(CANCELLED)
        if written >= self._sitio:
            self._sitio = written + ROOM_EVERY
            check_room(self.folder, written)
        if self.on_progress is not None:
            ahora = time.monotonic()
            if ahora >= self._aviso:
                self._aviso = ahora + PROGRESS_EVERY
                self.on_progress(self.done, self.total, written)

    def finish(self, written):
        if self.on_progress is not None:
            self.on_progress(self.done, self.total or self.done, written)


@contextmanager
def _writing(output_file):
    """El TXT en un temporal con un búfer grande; si el sistema dice que
    el disco está lleno, un mensaje que se entienda."""
    try:
        with atomic_open(output_file, fsync=False,
                         buffering=WRITE_BUFFER) as out:
            yield out
    except OSError as e:
        if _disk_full(e):
            raise StepError(
                f"El disco se llenó mientras se escribía el TXT en "
                f"«{output_file.parent}». No se ha guardado nada (si ya "
                "había un TXT, sigue como estaba). Libera espacio o "
                "guárdalo en otro disco.") from e
        raise


# ------------------------------------------------------------------ #
#  El cuerpo de cada archivo                                          #
# ------------------------------------------------------------------ #
def new_counters():
    return dict.fromkeys(_COUNTERS, 0)


def _reason(e):
    """El motivo de un fallo de lectura SIN la ruta completa: el TXT se
    comparte, y la ruta diría cómo se llama tu usuario y tus carpetas."""
    if isinstance(e, OSError) and e.strerror:
        return e.strerror
    return str(e) or type(e).__name__


def _binary_note(size):
    return note(f"{NOTE_BINARY} ({fmt_size(size)})")


def _cut_note(size):
    return note(f"{NOTE_CUT}: creció mientras se leía; aquí está tal "
                f"como era al abrirlo ({fmt_size(size)})")


def _unopened(raw, counters):
    """La nota de lo que no se abrió: un enlace, algo especial o un
    fallo."""
    if isinstance(raw, Link):
        counters["links"] += 1
        return note(f"{NOTE_LINK} → {display_name(str(raw))}: no se "
                    "sigue ni se copia lo que hay al otro lado")
    if isinstance(raw, Special):
        counters["special"] += 1
        return note(f"{NOTE_SPECIAL} {raw} (tubería, dispositivo…)")
    counters["errors"] += 1
    return note(f"{NOTE_READ_ERROR}: {_reason(raw)}")


def _small(size, raw, counters):
    """(cuerpo, ¿es texto?) de lo que llegó leído entero (o que no se
    abrió). Solo el texto se puede tachar."""
    if raw is None:                             # binario por extensión
        counters["binaries"] += 1
        return _binary_note(size), False
    if not isinstance(raw, bytes):
        return _unopened(raw, counters), False
    cuerpo = small_body(raw)
    if cuerpo is None:
        counters["binaries"] += 1
        return _binary_note(len(raw)), False
    counters["text"] += 1
    counters["text_bytes"] += len(cuerpo)
    return cuerpo, True


class _Counted:
    """Cuenta lo que pasa por `write` (con los secretos ya tachados, lo
    escrito no coincide con lo leído)."""

    def __init__(self, write):
        self._write = write
        self.count = 0

    def __call__(self, data):
        self._write(data)
        self.count += len(data)


def _large(write, path, counters, watch, base, rel="", redact=False):
    """Copia por bloques un archivo grande. Devuelve los bytes escritos."""
    abierto = open_regular(path, SEQUENTIAL)
    if not isinstance(abierto, tuple):
        nota = _unopened(abierto, counters)
        write(nota)
        return len(nota)
    fd, medida = abierto
    tick = None if watch is None else (lambda n: watch(base + n))
    counted = _Counted(write)
    redactor = Redactor(counted, rel) if redact else None
    sink = redactor.feed if redactor is not None else counted
    try:
        estado, _leido, error = stream_body(fd, medida, sink, tick)
    finally:
        os.close(fd)
    if redactor is not None:
        redactor.close()
        _count_redacted(counters, redactor.count)
    escrito = counted.count
    if estado == "binary":
        counters["binaries"] += 1
        nota = _binary_note(medida)
    elif estado == "error":
        counters["errors"] += 1
        nota = note(f"{NOTE_READ_ERROR} a mitad: {_reason(error)}. "
                    "Falta el resto")
    else:
        counters["text"] += 1
        counters["text_bytes"] += escrito
        if estado == "text":
            return escrito
        counters["cut"] += 1
        nota = _cut_note(medida)
    write(nota)
    return escrito + len(nota)


def _count_redacted(counters, count):
    if count:
        counters["redacted"] += count
        counters["redacted_files"] += 1


def _peek(path, limit=NOISE_PEEK):
    """El principio de un archivo grande (sin seguir enlaces)."""
    abierto = open_regular(path)
    if not isinstance(abierto, tuple):
        return b""
    fd, _size = abierto
    try:
        return os.read(fd, limit)
    except OSError:
        return b""
    finally:
        os.close(fd)


def noise_reason(rel, size, head):
    """Por qué un archivo no le aporta a una IA ("" si sí): de bloqueo,
    minificado, generado o de terceros. `head` es su principio (o todo)."""
    lineas = head.count(b"\n") + (0 if head.endswith(b"\n") else 1)
    media = len(head) / max(1, lineas)
    return generated_reason(rel, head[:4096], media, size)


def _noise_for(rel, path, size, raw):
    """El motivo de omitirlo para la IA ("" si se incluye)."""
    motivo = generated_reason(rel)               # solo por la ruta
    if motivo:
        return motivo
    if raw is LARGE:
        return noise_reason(rel, size, _peek(path))
    if isinstance(raw, bytes):
        return noise_reason(rel, size, raw[:NOISE_PEEK])
    return ""


def _contents(entries, git_folder, counters):
    """El lector de contenidos: de git los que no cambiaron, si compensa
    (ver `gitblobs`), y del disco todo lo demás."""
    if git_folder is not None and gitblobs.worth_it(git_folder, entries):
        return gitblobs.iter_contents_with_git(entries, git_folder,
                                               stats=counters)
    return iter_contents(entries)


def write_modules(out, entries, counters, watch=None, written=0,
                  options=None, offsets=None, git_folder=None):
    """Vuelca el contenido de cada archivo al TXT, en el orden de
    `entries` ((rel, ruta, tamaño)). Devuelve los bytes escritos en total
    (`written` más lo de ahora).

    El contenido que ya es UTF-8 válido se copia tal cual, sin
    decodificar ni recodificar. Si se pasa `offsets` (una lista), se le
    añade (posición, ruta) del comienzo de cada módulo: es lo que usa el
    reparto en partes. Con `git_folder` (la carpeta del repositorio), lo
    que no cambió puede leerse de git de una vez.
    """
    options = options or DEFAULT_OPTIONS
    for clave in _COUNTERS:
        counters.setdefault(clave, 0)
    write = out.write
    contenidos = _contents(entries, git_folder, counters)
    try:
        for rel, path, size, raw in contenidos:
            if offsets is not None:
                offsets.append((written, display_name(rel)))
            cabeza = module_header(display_name(rel))
            write(cabeza)
            written += len(cabeza)
            motivo = _noise_for(rel, path, size, raw) if options.for_ai \
                else ""
            if motivo:
                counters["for_ai"] += 1
                cuerpo = note(f"{NOTE_FOR_AI}: {motivo} ({fmt_size(size)})")
                write(cuerpo)
                written += len(cuerpo)
            elif raw is LARGE:
                written += _large(write, path, counters, watch, written,
                                  rel=rel, redact=options.redact)
            else:
                cuerpo, es_texto = _small(size, raw, counters)
                if es_texto and options.redact:
                    cuerpo, tachados = redact_bytes(cuerpo, rel)
                    _count_redacted(counters, tachados)
                if es_texto and isinstance(raw, Cut):
                    counters["cut"] += 1
                    cuerpo = cuerpo + _cut_note(len(raw))
                write(cuerpo)
                written += len(cuerpo)
            write(SEP_EQ)
            written += len(SEP_EQ)
            if watch is not None:
                watch.done += 1
                watch(written)
    finally:
        contenidos.close()        # sin hilos leyendo por delante
    return written


def _stats(counters, **extra):
    st = {"files": 0, "ignored": 0}
    st.update(counters)
    st.update(extra)
    return st


# ------------------------------------------------------------------ #
#  Un proyecto, cada repositorio, un perfil                           #
# ------------------------------------------------------------------ #
def _project_block(title_lines, root_name, entries, extra=""):
    return (title_lines + extra
            + "ESTRUCTURA DEL PROYECTO\n" + "-" * 30 + "\n"
            + render_tree(display_name(root_name),
                          [display_name(r) for r, _, _ in entries])
            + "\n\n" + "=" * 80 + "\n\n").encode("utf-8")


def consolidate_project(project_dir, output_file, extra_patterns=(),
                        respect_ignores=True, use_git=True,
                        on_progress=None, cancel=None, options=None):
    """Empaqueta un proyecto en un solo TXT (árbol + contenido).

    Respeta el .gitignore y los patrones dados, omite .git, menciona los
    binarios sin volcarlos y nunca se incluye a sí mismo. Los archivos de
    texto entran enteros, midan lo que midan.

    `on_progress(hechos, total, bytes_escritos)` informa por el camino;
    si `cancel` (un threading.Event) se activa, lanza `Cancelled` y no
    se guarda nada. `options` (un `PackOptions`) elige lo de la IA:
    omitir lo que no aporta, tachar secretos, solo lo que cambió y
    dividir en partes.

    Devuelve las estadísticas, con `tokens` (estimación) y `parts` (las
    rutas de las partes, si se dividió).
    """
    options = options or DEFAULT_OPTIONS
    if options.changes not in CHANGE_MODES:
        raise ValueError(f"modo desconocido: {options.changes!r}")
    t0 = time.time()
    project_dir = Path(project_dir).resolve()
    output_file = _output_path(output_file)
    entries, skipped = project_entries(project_dir, extra_patterns,
                                       respect_ignores=respect_ignores,
                                       exclude_file=output_file,
                                       use_git=use_git)
    entries = without_own_parts(entries, output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    change_set = None
    module_entries, deleted = entries, []
    if options.changes:
        change_set = changes_since_last_commit(str(project_dir))
        module_entries, deleted = select_changed(entries, change_set,
                                                 extra_patterns)
    total = len(module_entries) + len(deleted)
    counters = new_counters()
    nombre = display_name(project_dir.name)
    if change_set is None:
        title = (f"MODULO RAIZ: {nombre}\n"
                 f"GENERADO: {datetime.now():%Y-%m-%d %H:%M}   ·   "
                 f"ARCHIVOS INCLUIDOS: {len(entries)}\n\n")
        extra = project_text(project_dir.name, len(entries),
                             options=options) + "\n"
    else:
        title = (f"MODULO RAIZ: {nombre}\n"
                 f"GENERADO: {datetime.now():%Y-%m-%d %H:%M}   ·   "
                 f"CAMBIOS DESDE: {change_set.reference()}\n"
                 f"ARCHIVOS CAMBIADOS: {total}\n\n")
        extra = changes_text(project_dir.name, change_set,
                             len(module_entries), len(deleted),
                             options) + "\n"
    header = _project_block(title, project_dir.name, entries, extra=extra)
    watch = _Watch(output_file.parent, on_progress, cancel, total)
    offsets = []
    # Se escribe en un temporal y se cambia de una vez: si algo falla a
    # mitad (disco lleno, un nombre imposible, «Detener»), el TXT
    # anterior sigue entero en vez de quedar uno cortado.
    with _writing(output_file) as out:
        out.write(header)
        written = len(header)
        if options.changes == CHANGES_DIFF:
            written = write_diff_modules(
                out.write, str(project_dir), module_entries, deleted,
                change_set, counters, written, redact=options.redact,
                noise_reason=noise_reason if options.for_ai else None,
                watch=watch, offsets=offsets)
        else:
            written = write_modules(out, module_entries, counters, watch,
                                    written=written, options=options,
                                    offsets=offsets,
                                    git_folder=str(project_dir))
            if deleted:
                written = write_deleted(out.write, deleted, change_set,
                                        written, offsets)
        counters["deleted"] = len(deleted)
        summary_offset = written
        stats = _stats(counters, files=total, ignored=skipped,
                       bytes=written, seconds=time.time() - t0,
                       changes=options.changes)
        out.write(summary_text(stats).encode("utf-8"))

    stats["bytes"] = output_file.stat().st_size
    stats["tokens"] = estimate_tokens(stats["bytes"])
    stats["parts"] = []
    if options.split_tokens:
        partes = split_txt(output_file, offsets, summary_offset,
                           options.split_tokens, title=project_dir.name)
        stats["parts"] = [str(parte) for parte in partes]
    watch.finish(stats["bytes"])
    return stats


def consolidate_each(repo_dirs, extra_patterns=(), respect_ignores=True,
                     on_progress=None, workers=1, cancel=None):
    """Genera un TXT independiente DENTRO de cada carpeta (repo/repo.txt).

    Secuencial a propósito: cada proyecto ya lee sus archivos en
    paralelo por dentro, y anidar otro nivel de hilos resultó más lento
    por contención (medido). Si `cancel` se activa, se detiene entre
    repositorios o en mitad de uno (ese TXT no se guarda).
    """
    t0 = time.time()
    repo_dirs = sorted((Path(d) for d in repo_dirs),
                       key=lambda p: p.name.lower())
    total = len(repo_dirs)
    results, lock, done = {}, threading.Lock(), [0]

    def work(d):
        if cancel is not None and cancel.is_set():
            raise Cancelled(CANCELLED)
        try:
            # El repositorio clonado puede traer ya un «repo.txt» suyo:
            # nunca se pisa, se busca un nombre libre.
            out, _ = free_txt_path(d / f"{d.name}.txt")
            st = consolidate_project(d, out, extra_patterns,
                                     respect_ignores=respect_ignores,
                                     cancel=cancel)
            res = (str(out), st["files"], st["bytes"], None)
        except Cancelled:
            raise
        except Exception as e:                              # noqa: BLE001
            res = (None, 0, 0, f"{d.name}: {e}")
        if on_progress:
            with lock:
                done[0] += 1
                on_progress(d.name, done[0], total)
        return res

    if total:
        with ThreadPoolExecutor(max_workers=max(1, min(workers,
                                                       total))) as ex:
            for d, res in zip(repo_dirs, ex.map(work, repo_dirs)):
                results[d.name] = res

    made, failed, total_files, total_bytes = [], [], 0, 0
    for d in repo_dirs:
        path, nfiles, nbytes, err = results[d.name]
        if err:
            failed.append(err)
        else:
            made.append(path)
            total_files += nfiles
            total_bytes += nbytes
    return {"count": len(made), "failed": failed, "files": total_files,
            "bytes": total_bytes, "paths": made,
            "seconds": time.time() - t0}


def render_profile_tree(username, repo_names):
    """Árbol general del perfil: la raíz es el usuario y cada
    repositorio es una rama."""
    lines = [username + "/"]
    names = sorted(repo_names, key=str.lower)
    for i, name in enumerate(names):
        last = i == len(names) - 1
        lines.append(("└── " if last else "├── ") + name + "/")
    return "\n".join(lines)


def consolidate_many(username, repo_dirs, output_file, extra_patterns=(),
                     respect_ignores=True, profile_url="", use_git=True,
                     on_progress=None, cancel=None):
    """Une los repositorios de un PERFIL en un único TXT, en streaming.

    Abre con una cabecera de perfil y un árbol general; después va el
    contenido detallado de cada repositorio. El siguiente repositorio
    se enumera en otro hilo mientras se escribe el actual. Como en
    `consolidate_project`, sin límite de tamaño, con progreso
    (`hechos`, 0 —el total no se sabe hasta el final—, bytes) y
    cancelable.
    """
    t0 = time.time()
    output_file = _output_path(output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    repo_dirs = sorted((Path(d) for d in repo_dirs),
                       key=lambda p: p.name.lower())
    profile_url = profile_url or f"https://github.com/{username}"

    counters = new_counters()
    total_files = total_ignored = 0

    def scan(d):
        return project_entries(d, extra_patterns,
                               respect_ignores=respect_ignores,
                               exclude_file=output_file, use_git=use_git)

    header = ("#" * 80 + "\n"
              f"PERFIL DE GITHUB: {username}\n"
              + "#" * 80 + "\n\n"
              + profile_text(username, profile_url, len(repo_dirs)) + "\n"
              f"Perfil: {profile_url}\n"
              f"Repositorios: {len(repo_dirs)}   ·   "
              f"Generado: {datetime.now():%Y-%m-%d %H:%M}\n\n"
              "ESTRUCTURA DEL PERFIL "
              "(raíz: usuario · ramas: repositorios)\n"
              + "-" * 30 + "\n"
              + render_profile_tree(username, [display_name(d.name)
                                               for d in repo_dirs])
              + "\n\n" + "=" * 80 + "\n\n").encode("utf-8")

    watch = _Watch(output_file.parent, on_progress, cancel)
    with _writing(output_file) as out:
        out.write(header)
        written = len(header)
        with ThreadPoolExecutor(max_workers=2) as ex:
            nxt = ex.submit(scan, repo_dirs[0]) if repo_dirs else None
            for i, d in enumerate(repo_dirs, 1):
                entries, ign = nxt.result()
                nxt = (ex.submit(scan, repo_dirs[i])
                       if i < len(repo_dirs) else None)
                total_files += len(entries)
                total_ignored += ign
                block = _project_block(
                    "#" * 80 + "\n"
                    f"REPOSITORIO {i}/{len(repo_dirs)}: "
                    f"{username}/{display_name(d.name)}\n" + "#" * 80
                    + "\n\n", display_name(d.name), entries,
                    extra=f"Archivos incluidos: {len(entries)}\n\n")
                out.write(block)
                written += len(block)
                written = write_modules(out, entries, counters, watch,
                                        written=written, git_folder=str(d))
        stats = _stats(counters, repos=len(repo_dirs), files=total_files,
                       ignored=total_ignored, bytes=written,
                       seconds=time.time() - t0)
        out.write(summary_text(stats).encode("utf-8"))

    stats["bytes"] = output_file.stat().st_size
    watch.finish(stats["bytes"])
    return stats
