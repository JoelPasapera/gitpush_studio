# -*- coding: utf-8 -*-
"""Consolidar proyectos en un único TXT (árbol + contenido), en streaming."""

import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from .atomic import atomic_open
from .errors import StepError
from .fs import MB, display_name, fmt_size, looks_binary
from .preamble import profile_text, project_text, summary_text
from .reader import Cut, Link, Special, iter_payloads
from .walk import project_entries, render_tree

PACK_MAX_FILE = 2 * MB           # contenido máximo por archivo
PACK_MAX_TOTAL = 100 * MB        # tamaño máximo del TXT (un proyecto)
PACK_MAX_TOTAL_MULTI = 200 * MB  # tamaño máximo del TXT (perfil entero)

SEP_DASH = b"-" * 80 + b"\n"
SEP_EQ = b"\n" + b"=" * 80 + b"\n\n"

# Cómo EMPIEZAN los TXT que genera la aplicación. Sirven para distinguir
# «este TXT es mío, puedo rehacerlo» de «este archivo es del usuario, no
# se toca». Se exige el comienzo exacto: unas notas que CITEN esa
# cabecera más abajo no son un TXT nuestro.
TXT_MARKERS = (b"MODULO RAIZ:", b"PERFIL DE GITHUB:")
_TXT_STARTS = (b"MODULO RAIZ: ", b"#" * 80 + b"\nPERFIL DE GITHUB: ")


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
    return cabeza.startswith(_TXT_STARTS)


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


def _body_for(size, raw, max_file_bytes, counters):
    if isinstance(raw, Link):
        counters["links"] = counters.get("links", 0) + 1
        return (f"<ENLACE SIMBÓLICO → {display_name(str(raw))}: no se sigue "
                "ni se copia lo que hay al otro lado>\n").encode("utf-8")
    if isinstance(raw, Special):
        counters["special"] = counters.get("special", 0) + 1
        return f"<OMITIDO: {raw} (tubería, dispositivo…)>\n".encode("utf-8")
    if raw is None:
        if size > max_file_bytes:
            counters["big"] += 1
            return f"<ARCHIVO OMITIDO POR TAMAÑO ({fmt_size(size)})>\n" \
                .encode("utf-8")
        counters["binaries"] += 1
        return f"<ARCHIVO BINARIO OMITIDO ({fmt_size(size)})>\n" \
            .encode("utf-8")
    if isinstance(raw, bytes):
        if raw and looks_binary(raw):
            counters["binaries"] += 1
            return f"<ARCHIVO BINARIO OMITIDO ({fmt_size(size)})>\n" \
                .encode("utf-8")
        body = bytes(raw).rstrip()
        if not body.isascii():
            try:
                body.decode("utf-8")          # ya es UTF-8: se copia
            except UnicodeDecodeError:
                body = body.decode("utf-8", "replace").encode("utf-8")
        if isinstance(raw, Cut):
            counters["cut"] = counters.get("cut", 0) + 1
            return body + ("\n<ARCHIVO CORTADO: creció mientras se leía; "
                           f"aquí solo están sus primeros "
                           f"{fmt_size(max_file_bytes)}>\n").encode("utf-8")
        return body + b"\n"
    return f"<ERROR AL LEER ARCHIVO: {raw}>\n".encode("utf-8")


def write_modules(out, entries, counters, max_file_bytes, max_total_bytes,
                  written=0, flush_at=1 << 18):
    """Vuelca los módulos al TXT escribiendo bytes.

    El contenido que ya es UTF-8 válido se copia tal cual, sin
    decodificar ni recodificar; las piezas se acumulan y se escriben en
    bloques grandes. Devuelve (truncado, bytes_escritos).
    """
    buf, pending = [], 0
    for rel, size, raw in iter_payloads(entries, max_file_bytes):
        body = _body_for(size, raw, max_file_bytes, counters)
        piece = (b"MODULO: " + display_name(rel).encode("utf-8") + b"\n"
                 + SEP_DASH + body + SEP_EQ)
        buf.append(piece)
        pending += len(piece)
        written += len(piece)
        if pending >= flush_at:
            out.write(b"".join(buf))
            buf.clear()
            pending = 0
        if written > max_total_bytes:
            buf.append(("<SALIDA TRUNCADA: se alcanzó el límite de "
                        f"{fmt_size(max_total_bytes)}>\n").encode("utf-8"))
            out.write(b"".join(buf))
            return True, written
    if buf:
        out.write(b"".join(buf))
    return False, written


def _project_block(title_lines, root_name, entries, extra=""):
    return (title_lines + extra
            + "ESTRUCTURA DEL PROYECTO\n" + "-" * 30 + "\n"
            + render_tree(display_name(root_name),
                          [display_name(r) for r, _, _ in entries])
            + "\n\n" + "=" * 80 + "\n\n").encode("utf-8")


def consolidate_project(project_dir, output_file, extra_patterns=(),
                        respect_ignores=True, max_file_bytes=PACK_MAX_FILE,
                        max_total_bytes=PACK_MAX_TOTAL, use_git=True):
    """Empaqueta un proyecto en un solo TXT (árbol + contenido).

    Respeta el .gitignore y los patrones dados, omite .git, marca los
    binarios y los archivos enormes, y nunca se incluye a sí mismo.
    """
    t0 = time.time()
    project_dir = Path(project_dir).resolve()
    output_file = _output_path(output_file)
    entries, skipped = project_entries(project_dir, extra_patterns,
                                       respect_ignores=respect_ignores,
                                       exclude_file=output_file,
                                       use_git=use_git)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    counters = {"binaries": 0, "big": 0}
    header = _project_block(
        f"MODULO RAIZ: {display_name(project_dir.name)}\n"
        f"GENERADO: {datetime.now():%Y-%m-%d %H:%M}   ·   "
        f"ARCHIVOS INCLUIDOS: {len(entries)}\n\n",
        project_dir.name, entries,
        extra=project_text(project_dir.name, len(entries),
                           max_file_bytes) + "\n")
    # Se escribe en un temporal y se cambia de una vez: si algo falla a
    # mitad (disco lleno, un nombre imposible), el TXT anterior sigue
    # entero en vez de quedar uno cortado.
    with atomic_open(output_file, fsync=False) as out:
        out.write(header)
        truncated, _ = write_modules(out, entries, counters, max_file_bytes,
                                     max_total_bytes, written=len(header))
        stats = {"files": len(entries), "binaries": counters["binaries"],
                 "big": counters["big"], "ignored": skipped,
                 "bytes": out.tell(), "seconds": time.time() - t0}
        out.write(summary_text(stats, truncated).encode("utf-8"))

    stats["bytes"] = output_file.stat().st_size
    stats["truncated"] = truncated
    return stats


def consolidate_each(repo_dirs, extra_patterns=(), respect_ignores=True,
                     on_progress=None, workers=1):
    """Genera un TXT independiente DENTRO de cada carpeta (repo/repo.txt).

    Secuencial a propósito: cada proyecto ya lee sus archivos en
    paralelo por dentro, y anidar otro nivel de hilos resultó más lento
    por contención (medido).
    """
    t0 = time.time()
    repo_dirs = sorted((Path(d) for d in repo_dirs),
                       key=lambda p: p.name.lower())
    total = len(repo_dirs)
    results, lock, done = {}, threading.Lock(), [0]

    def work(d):
        try:
            # El repositorio clonado puede traer ya un «repo.txt» suyo:
            # nunca se pisa, se busca un nombre libre.
            out, _ = free_txt_path(d / f"{d.name}.txt")
            st = consolidate_project(d, out, extra_patterns,
                                     respect_ignores=respect_ignores)
            res = (str(out), st["files"], st["bytes"], None)
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
                     respect_ignores=True, profile_url="",
                     max_file_bytes=PACK_MAX_FILE,
                     max_total_bytes=PACK_MAX_TOTAL_MULTI, use_git=True):
    """Une los repositorios de un PERFIL en un único TXT, en streaming.

    Abre con una cabecera de perfil y un árbol general; después va el
    contenido detallado de cada repositorio. El siguiente repositorio
    se enumera en otro hilo mientras se escribe el actual.
    """
    t0 = time.time()
    output_file = _output_path(output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    repo_dirs = sorted((Path(d) for d in repo_dirs),
                       key=lambda p: p.name.lower())
    profile_url = profile_url or f"https://github.com/{username}"

    counters = {"binaries": 0, "big": 0}
    total_files = total_ignored = 0
    truncated = False

    def scan(d):
        return project_entries(d, extra_patterns,
                               respect_ignores=respect_ignores,
                               exclude_file=output_file, use_git=use_git)

    header = ("#" * 80 + "\n"
              f"PERFIL DE GITHUB: {username}\n"
              + "#" * 80 + "\n\n"
              + profile_text(username, profile_url, len(repo_dirs),
                             max_file_bytes) + "\n"
              f"Perfil: {profile_url}\n"
              f"Repositorios: {len(repo_dirs)}   ·   "
              f"Generado: {datetime.now():%Y-%m-%d %H:%M}\n\n"
              "ESTRUCTURA DEL PERFIL "
              "(raíz: usuario · ramas: repositorios)\n"
              + "-" * 30 + "\n"
              + render_profile_tree(username, [display_name(d.name)
                                               for d in repo_dirs])
              + "\n\n" + "=" * 80 + "\n\n").encode("utf-8")

    with atomic_open(output_file, fsync=False) as out:
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
                truncated, written = write_modules(
                    out, entries, counters, max_file_bytes,
                    max_total_bytes, written=written)
                if truncated:
                    break
        stats = {"repos": len(repo_dirs), "files": total_files,
                 "binaries": counters["binaries"], "big": counters["big"],
                 "ignored": total_ignored, "bytes": out.tell(),
                 "seconds": time.time() - t0}
        out.write(summary_text(stats, truncated).encode("utf-8"))

    stats["bytes"] = output_file.stat().st_size
    stats["truncated"] = truncated
    return stats
