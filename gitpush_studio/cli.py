# -*- coding: utf-8 -*-
"""Uso sin interfaz gráfica.

    python -m gitpush_studio.cli publish --folder RUTA --url URL [opciones]
    python -m gitpush_studio.cli publish --profile NOMBRE [--profile OTRO …]
    python -m gitpush_studio.cli batch --project RUTA[=repo] [--project OTRA …]
    python -m gitpush_studio.cli scan RUTA [--ignore PATRÓN …]
    python -m gitpush_studio.cli pack RUTA [-o SALIDA.txt]
    python -m gitpush_studio.cli analyze RUTA [--html SALIDA.html] [--json SALIDA.json]
    python -m gitpush_studio.cli status RUTA
    python -m gitpush_studio.cli licencias
    python -m gitpush_studio.cli pull RUTA --url URL [--branch] [--mode merge|mirror] [--yes]
    python -m gitpush_studio.cli versions RUTA
    python -m gitpush_studio.cli restore RUTA SHA
    python -m gitpush_studio.cli repos USUARIO
    python -m gitpush_studio.cli branches --url URL
    python -m gitpush_studio.cli release RUTA --url URL --token T --tag v1.2.0 [--title] [--notes] [--no-zip]

Varios --profile se publican a la vez (los que comparten repositorio,
en serie). `batch` hace lo mismo con carpetas sueltas de una misma
cuenta, creando en GitHub los repositorios que falten. Con --yes no se
pregunta nada: útil para scripts.
"""

import argparse
import os
import sys
import threading
from pathlib import Path

from . import APP_NAME, APP_VERSION
from .core import history, licenses, release, sync
from .core.analytics import analyze_project, summary_lines
from .core.batch import BatchEntry, plan_batch, suggest_repo_name
from .core.config import load_config, saved_token
from .core.errors import StepError
from .core.fs import fmt_size
from .core.git import Git
from .core.github import list_user_repos
from .core.pack import consolidate_project, free_txt_path
from .core.publish import (Callbacks, PreviewChoice, PublishJob,
                            publish_many)
from .core.report_html import write_html, write_json
from .core.scan import scan_project, stats_lines
from .core.urls import is_remote_url, parse_owner_repo, repo_name_from_url

_print_lock = threading.Lock()


def _say(prefix, text):
    with _print_lock:
        print(f"{prefix}{text}", flush=True)


def _print_findings(findings, prefix=""):
    for f in findings:
        _say(prefix, f"  [{f['kind']} · {f.get('confidence', 'alta')}] "
                     f"{f['path']}  — {f['reason']}")


def _make_callbacks(job, assume_yes, many):
    prefix = f"[{job.label}] " if many else ""

    def log(text, tag="out"):
        if tag != "cmd" or not many:
            _say(prefix, text)

    def ask_review(findings):
        _print_findings(findings, prefix)
        if assume_yes:
            return "continue", []
        with _print_lock:
            ans = input(f"{prefix}[c]ontinuar, [i]gnorar todos, "
                        "[x] cancelar: ").strip().lower()
        if ans.startswith("i"):
            return "ignore", [f["path"] for f in findings]
        return ("cancel", []) if ans.startswith("x") else ("continue", [])

    def ask_preview(added, modified, deleted, mode):
        for title, items in (("nuevos", added), ("modificados", modified),
                             ("a borrar", deleted)):
            if items:
                _say(prefix, f"  {title}: {len(items)}")
                for p in items[:20]:
                    _say(prefix, f"    {p}")
                if len(items) > 20:
                    _say(prefix, f"    … y {len(items) - 20} más")
        if assume_yes:
            return True
        with _print_lock:
            def ask_paths(question):
                raw = input(f"{prefix}{question} (rutas separadas por coma; "
                            "Enter para ninguna): ")
                return [p.strip() for p in raw.split(",") if p.strip()]
            keep = ask_paths("MANTENER en GitHub") if deleted else []
            skip_new = ask_paths("NO subir") if added else []
            skip_mod = ask_paths("NO actualizar") if modified else []
            go = input(f"{prefix}¿Subir? [s/N]: ").strip().lower() \
                .startswith("s")
        return PreviewChoice(go=go, keep_deleted=keep, skip_added=skip_new,
                             skip_modified=skip_mod)

    return Callbacks(log=log, ask_review=ask_review,
                     ask_preview=ask_preview)


def _token(a):
    """Token para conectarse: --token, GITHUB_TOKEN o el guardado."""
    return (getattr(a, "token", "") or os.environ.get("GITHUB_TOKEN", "")
            or saved_token(load_config()[0]))


def _license_key(a):
    key = licenses.normalize_key(getattr(a, "license", "") or "")
    if key is None:
        sys.exit(f"Licencia desconocida: «{a.license}». Mira las "
                 "disponibles con: gitpush_studio licencias")
    return key


def _jobs_from_args(a):
    jobs = []
    if a.profile:
        data, warning = load_config()
        if warning:
            print("⚠ " + warning, file=sys.stderr)
        for name in a.profile:
            state = data.get("profiles", {}).get(name)
            if state is None:
                sys.exit(f"No existe el perfil «{name}».")
            jobs.append(PublishJob.from_state(state, name=name))
    elif a.folder and a.url:
        jobs.append(PublishJob(
            folder=str(Path(a.folder)), url=a.url, branch=a.branch,
            user=a.user or "", email=a.email or "",
            message=a.message or "Actualizar proyecto completo",
            force=a.force, check=not a.no_check, mode=a.mode,
            preview=not a.no_preview, ignore=a.ignore or [],
            stamp=a.stamp, auto_message=a.auto_message,
            license=_license_key(a), license_owner=a.license_owner or ""))
    else:
        sys.exit("Indica --profile NOMBRE o bien --folder y --url.")
    return jobs


def _apply_txt_options(jobs, a):
    """Casilla «unir en un TXT» para la línea de órdenes."""
    destino = getattr(a, "txt_path", "") or ""
    # Pedir un destino es pedir el TXT: exigir además --txt solo serviría
    # para que la orden no hiciera nada y nadie dijera por qué.
    quiere = bool(getattr(a, "txt", False)) or bool(destino)
    if destino and len(jobs) > 1:
        sys.exit("--txt-path solo vale para un proyecto: con varios, cada "
                 "TXT se guarda dentro de su propia carpeta.")
    for job in jobs:
        job.consolidate = quiere
        job.consolidate_path = destino


def _report(results):
    """Línea por proyecto; devuelve el código de salida."""
    bad = 0
    for r in results:
        mark = {"ok": "✔", "cancelled": "✋", "blocked": "🤖"}.get(r.status,
                                                                    "✖")
        detail = r.web_url if r.ok else (r.message.splitlines() or [""])[0]
        print(f"{mark} {r.job.label}: {r.status} ({r.seconds:.1f} s) "
              f"{detail}")
        if r.txt_error:
            print(f"   📄 sin TXT: {r.txt_error}")
        elif r.txt_path:
            st = r.txt_stats or {}
            print(f"   📄 {r.txt_path} ({st.get('files', 0)} archivos, "
                  f"{fmt_size(st.get('bytes', 0))})")
        bad += not r.ok
    return 1 if bad else 0


def cmd_publish(a):
    jobs = _jobs_from_args(a)
    many = len(jobs) > 1
    token = _token(a)
    for job in jobs:
        job.token = token
    _apply_txt_options(jobs, a)
    return _report(publish_many(
        jobs, callbacks_for=lambda j: _make_callbacks(j, a.yes, many),
        workers=a.workers))


def _batch_entry(spec, con_txt):
    """Interpreta «RUTA», «RUTA=repo» o «RUTA=URL».

    Una ruta puede contener un «=» (`/datos/informe=final`), así que si
    lo escrito es TAL CUAL una carpeta existente, no se parte: partir por
    el primer «=» publicaría la carpeta equivocada en un repositorio con
    el nombre de media ruta. Si hay que partir, se hace por el ÚLTIMO
    «=», que es el separador que el usuario quiso.
    """
    spec = spec.strip()
    if spec and Path(spec).is_dir():
        return BatchEntry(folder=spec, repo=suggest_repo_name(spec),
                          consolidate=con_txt)
    folder, sep, resto = spec.rpartition("=")
    if not sep:
        folder, resto = spec, ""
    folder, resto = folder.strip(), resto.strip()
    url = ""
    if sep and _parece_url(resto):
        # «carpeta=https://…» o «carpeta=/ruta/espejo.git»: se usa esa
        # URL tal cual, sin componer ni crear nada. Se acepta también la
        # que NO es válida, para que el error hable de la URL y no diga
        # «ese nombre de repositorio lleva barras».
        url, resto = resto, repo_name_from_url(resto)
    repo = resto if sep else suggest_repo_name(folder)
    return BatchEntry(folder=folder, repo=repo, url=url,
                      consolidate=con_txt)


def _parece_url(text):
    """Un nombre de repositorio no lleva barras ni esquema: si las lleva,
    quien escribió quería dar un destino, no un nombre."""
    return (is_remote_url(text) or "://" in text
            or text.startswith("git@") or "/" in text or "\\" in text)


def cmd_batch(a):
    """Varias carpetas de golpe a la misma cuenta: el equivalente en
    línea de órdenes del diálogo «Publicar varias carpetas»."""
    entries = [_batch_entry(spec, not a.no_txt) for spec in a.project]
    state = {"branch": a.branch, "mode": a.mode,
             "message": a.message or "Actualizar proyecto completo",
             "user": a.user or "", "email": a.email or "",
             "ignore": a.ignore or [], "check": not a.no_check,
             "preview": not a.no_preview, "force": a.force,
             "license": _license_key(a),
             "license_owner": a.license_owner or ""}
    token = _token(a)
    plan = plan_batch(entries, state, token=token, owner=a.owner or "",
                      create_missing=not a.no_create and bool(token),
                      private=not a.public,
                      on_progress=lambda label, i, t: print(
                          f"[{i}/{t}] preparando {label}…"))
    if not plan.ok:
        for p in plan.problems:
            print("✖ " + p, file=sys.stderr)
        return 2
    if plan.created:
        print("➕ creados en GitHub: " + ", ".join(plan.created))
    print(f"🚀 publicando {len(plan.jobs)} proyectos en "
          f"{plan.owner or 'GitHub'}…")
    return _report(publish_many(
        plan.jobs,
        callbacks_for=lambda j: _make_callbacks(j, a.yes, True),
        workers=a.workers))


def cmd_scan(a):
    findings, stats = scan_project(a.folder, a.ignore or [])
    for texto, _ in stats_lines(stats):
        print(texto)
    if not findings:
        print("✔ Sin archivos grandes ni secretos a la vista.")
        return 0
    print(f"⚠ {len(findings)} hallazgo(s):")
    _print_findings(findings)
    return 2


def cmd_pack(a):
    folder = Path(a.folder).resolve()
    pedido = Path(a.output) if a.output else \
        folder.parent / f"{folder.name}.txt"
    # Nunca encima de un archivo que no sea un TXT de la app (ni a través
    # de un enlace): se elige otro nombre y se dice cuál.
    out, renombrado = free_txt_path(pedido)
    if renombrado:
        print(f"«{pedido}» ya existe y no es un TXT de la app: no lo toco.")
    st = consolidate_project(folder, out, a.ignore or [])
    print(f"✔ {out}  ({st['files']} archivos, {fmt_size(st['bytes'])}, "
          f"{st['seconds']:.1f} s"
          + (", truncado" if st["truncated"] else "") + ")")
    return 0


def cmd_analyze(a):
    """Analíticas del proyecto: resumen en pantalla y, si se pide, el
    informe HTML y el JSON."""
    folder = Path(a.folder)
    if not folder.is_dir():
        print(f"✖ No existe la carpeta «{a.folder}».", file=sys.stderr)
        return 2
    rep = analyze_project(folder, a.ignore or [],
                          respect_ignores=not a.no_ignores,
                          history=not a.no_history)
    print("\n".join(summary_lines(rep, top=a.top)))
    if a.html:
        print(f"✔ informe HTML: {write_html(rep, a.html)}")
    if a.json:
        print(f"✔ informe JSON: {write_json(rep, a.json)}")
    return 0


def cmd_licenses(a):
    for key, name, what in licenses.CATALOG:
        marca = "·" if licenses.is_bundled(key) else " "
        print(f" {marca} {key:14} {name:18} {what}")
    print("\n · = incluida, funciona sin conexión")
    return 0


def cmd_status(a):
    git = Git()
    if not git.is_repo(a.folder):
        print("La carpeta aún no es un repositorio.")
        return 2
    # Lo mismo que miraría publicar: un repositorio anidado no se sube,
    # así que no es un cambio pendiente.
    dirty = bool(git.pending_changes(a.folder))
    print("Hay cambios sin publicar." if dirty else "Todo publicado.")
    return 1 if dirty else 0


def _identity(a):
    return (a.user, a.email) if getattr(a, "user", None) and \
        getattr(a, "email", None) else None


def cmd_pull(a):
    try:
        rep = sync.inspect(a.folder, a.url, a.branch, token=_token(a))
    except StepError as e:
        print(f"✖ {e}")
        return 2
    if rep.error:
        print(f"✖ {rep.error}")
        return 2
    if not rep.remote_exists:
        print(f"La rama «{a.branch}» no existe en el remoto.")
        return 2
    espejo = a.mode == "mirror"
    if rep.blocked(a.mode):
        print(f"✖ {rep.blocked(a.mode)}")
        return 2
    if rep.identical or (rep.up_to_date and not espejo):
        print("✔ Tu carpeta ya está al día.")
        return 0
    if espejo:
        grupos = (("llegarán", rep.mirror_added),
                  ("cambiarán", rep.mirror_modified),
                  ("desaparecerán", rep.mirror_deleted))
    else:
        grupos = (("llegarán", rep.added), ("cambiarán", rep.modified),
                  ("desaparecerán", rep.deleted),
                  ("se juntarán tus cambios con los de GitHub", rep.combined),
                  ("gana la versión de GitHub (la tuya queda en la copia)",
                   rep.overwritten))
    for title, items in grupos:
        if items:
            print(f"  {title}: {len(items)}")
            for p in items[:20]:
                print(f"    {p}")
    if not espejo and rep.conflicts:
        print(f"  ✖ no se pueden combinar: {len(rep.conflicts)}")
        for ruta, motivo in rep.conflicts[:20]:
            print(f"    {ruta} — {motivo}")
        print("  Usa --mode mirror (tu versión queda en una rama de respaldo).")
        return 1
    if rep.local_changes:
        destino = ("se descartan; quedan en una rama de respaldo" if espejo
                   else "se quedan como están; antes se guarda una copia")
        print(f"  cambios locales sin publicar: {len(rep.local_changes)} "
              f"({destino})")
    if not a.yes and not input("¿Traer los cambios? [s/N]: ").strip() \
            .lower().startswith("s"):
        return 130
    r = sync.apply(a.folder, a.url, a.branch, mode=a.mode,
                   identity=_identity(a), token=_token(a),
                   expected=rep.remote_sha,
                   log=lambda t, tag="out": print(t))
    return 0 if r.status == "ok" else 1


def cmd_versions(a):
    vs = history.list_versions(a.folder, limit=a.limit)
    if not vs:
        print("Sin versiones todavía.")
        return 2
    for v in vs:
        extra = f"  [{v.branch}]" if v.is_backup else \
            f"  ({v.files} archivos, +{v.insertions}/-{v.deletions})"
        print(f"{v.short}  {v.date}  {v.subject}{extra}")
    return 0


def cmd_restore(a):
    """Una versión (commit nuevo) o un respaldo «respaldo-…» (sus archivos
    vuelven a la carpeta, sin commit)."""
    r = history.restore(a.folder, a.sha, identity=_identity(a),
                        log=lambda t, tag="out": print(t))
    return 0 if r.status == "ok" else 1


def cmd_repos(a):
    """Lista los repositorios de un perfil y dice cuánto costó pedirlos."""
    repos = list_user_repos(a.user, _token(a),
                            on_note=lambda t: print(t, file=sys.stderr))
    for r in repos:
        marca = "🔒" if r["private"] else ("↳" if r["fork"] else " ")
        print(f" {marca} {r['name']:30} {r['default_branch']:12} "
              f"{r['size_kb']} KB")
    return 0


def cmd_branches(a):
    names = Git().remote_branches(a.url, token=_token(a))
    print("\n".join(names) if names else "(ninguna)")
    return 0


def cmd_release(a):
    owner, repo = parse_owner_repo(a.url)
    if not (a.owner and a.repo) and not owner:
        sys.exit("La URL no es de GitHub: indica --owner y --repo.")
    tag = a.tag or release.suggest_tag(a.folder)
    r = release.create_release(
        a.folder, a.url, a.owner or owner, a.repo or repo, a.token, tag,
        a.title or "", a.notes or "", attach_zip=not a.no_zip,
        branch=a.branch, log=lambda t, tag="out": print(t))
    return 0 if r.status == "ok" else 1


def build_parser():
    p = argparse.ArgumentParser(prog="gitpush_studio",
                                description=f"{APP_NAME} {APP_VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    pub = sub.add_parser("publish", help="publicar uno o varios proyectos")
    pub.add_argument("--profile", action="append",
                     help="perfil guardado (repetible)")
    pub.add_argument("--folder")
    pub.add_argument("--url")
    pub.add_argument("--branch", default="main")
    pub.add_argument("--mode", choices=("mirror", "addedit"),
                     default="mirror")
    pub.add_argument("--message", "-m")
    pub.add_argument("--user")
    pub.add_argument("--email")
    pub.add_argument("--ignore", action="append", metavar="PATRÓN")
    pub.add_argument("--stamp", action="store_true",
                     help="añadir fecha y hora al mensaje")
    pub.add_argument("--auto-message", action="store_true",
                     help="describir los cambios en el mensaje del commit")
    pub.add_argument("--force", action="store_true")
    pub.add_argument("--no-check", action="store_true",
                     help="omitir la revisión de seguridad")
    pub.add_argument("--no-preview", action="store_true")
    pub.add_argument("--yes", "-y", action="store_true",
                     help="no preguntar nada")
    pub.add_argument("--workers", type=int, default=3,
                     help="publicaciones simultáneas")
    pub.add_argument("--token", help="token de GitHub; por defecto, "
                                     "GITHUB_TOKEN o el guardado en la app")
    pub.add_argument("--license", default="",
                     help="clave de la licencia (apache-2.0, mit…); vacío, "
                          "ninguna")
    pub.add_argument("--license-owner", help="titular del copyright")
    pub.add_argument("--txt", action="store_true",
                     help="unir además el proyecto en un único TXT (se "
                          "queda en tu equipo; no se sube)")
    pub.add_argument("--txt-path", help="dónde guardar ese TXT (implica "
                                        "--txt); por defecto, dentro del "
                                        "proyecto")
    pub.set_defaults(fn=cmd_publish)

    bt = sub.add_parser("batch", help="publicar varias carpetas a la vez "
                                      "en una misma cuenta")
    bt.add_argument("--project", action="append", required=True,
                    metavar="RUTA[=REPO|=URL]",
                    help="carpeta a publicar; con «=nombre» se indica el "
                         "repositorio, con «=URL» se usa esa URL tal cual, "
                         "y si no se deduce de la carpeta (repetible)")
    bt.add_argument("--owner", help="tu usuario de GitHub; con token se "
                                    "detecta solo")
    bt.add_argument("--token", help="token de GitHub; por defecto, "
                                    "GITHUB_TOKEN o el guardado en la app")
    bt.add_argument("--no-create", action="store_true",
                    help="no crear en GitHub los repositorios que falten")
    bt.add_argument("--public", action="store_true",
                    help="crear los nuevos como públicos (por defecto, "
                         "privados)")
    bt.add_argument("--no-txt", action="store_true",
                    help="no generar el TXT de cada proyecto (por defecto "
                         "sí se genera)")
    bt.add_argument("--branch", default="main")
    bt.add_argument("--mode", choices=("mirror", "addedit"),
                    default="mirror")
    bt.add_argument("--message", "-m")
    bt.add_argument("--user")
    bt.add_argument("--email")
    bt.add_argument("--ignore", action="append", metavar="PATRÓN")
    bt.add_argument("--force", action="store_true")
    bt.add_argument("--no-check", action="store_true",
                    help="omitir la revisión de seguridad")
    bt.add_argument("--no-preview", action="store_true")
    bt.add_argument("--yes", "-y", action="store_true",
                    help="no preguntar nada")
    bt.add_argument("--workers", type=int, default=3,
                    help="publicaciones simultáneas")
    bt.add_argument("--license", default="",
                    help="clave de la licencia (apache-2.0, mit…); vacío, "
                         "ninguna")
    bt.add_argument("--license-owner", help="titular del copyright")
    bt.set_defaults(fn=cmd_batch)

    sc = sub.add_parser("scan", help="revisión de seguridad de una carpeta")
    sc.add_argument("folder")
    sc.add_argument("--ignore", action="append", metavar="PATRÓN")
    sc.set_defaults(fn=cmd_scan)

    pk = sub.add_parser("pack", help="consolidar una carpeta a TXT")
    pk.add_argument("folder")
    pk.add_argument("-o", "--output")
    pk.add_argument("--ignore", action="append", metavar="PATRÓN")
    pk.set_defaults(fn=cmd_pack)

    an = sub.add_parser("analyze", help="analíticas del proyecto: lenguajes, "
                                        "líneas, duplicados, historial")
    an.add_argument("folder")
    an.add_argument("--html", metavar="SALIDA.html",
                    help="guardar el informe con gráficos")
    an.add_argument("--json", metavar="SALIDA.json",
                    help="guardar todos los datos, archivo a archivo")
    an.add_argument("--ignore", action="append", metavar="PATRÓN")
    an.add_argument("--no-ignores", action="store_true",
                    help="analizar también lo que .gitignore excluye")
    an.add_argument("--no-history", action="store_true",
                    help="no leer el historial de git")
    an.add_argument("--top", type=int, default=8,
                    help="lenguajes que se listan (8 por defecto)")
    an.set_defaults(fn=cmd_analyze)

    lic = sub.add_parser("licencias", help="licencias disponibles")
    lic.set_defaults(fn=cmd_licenses)

    stt = sub.add_parser("status", help="¿hay cambios sin publicar?")
    stt.add_argument("folder")
    stt.set_defaults(fn=cmd_status)

    pl = sub.add_parser("pull", help="traer cambios de GitHub a la carpeta")
    pl.add_argument("folder")
    pl.add_argument("--url", required=True)
    pl.add_argument("--branch", default="main")
    pl.add_argument("--mode", choices=("merge", "mirror"), default="merge")
    pl.add_argument("--user")
    pl.add_argument("--email")
    pl.add_argument("--token")
    pl.add_argument("--yes", "-y", action="store_true")
    pl.set_defaults(fn=cmd_pull)

    vs = sub.add_parser("versions", help="historial de versiones")
    vs.add_argument("folder")
    vs.add_argument("--limit", type=int, default=30)
    vs.set_defaults(fn=cmd_versions)

    rs = sub.add_parser("restore", help="volver a una versión (commit "
                        "nuevo) o recuperar un respaldo «respaldo-…» (sus "
                        "archivos, sin commit)")
    rs.add_argument("folder")
    rs.add_argument("sha")
    rs.add_argument("--user")
    rs.add_argument("--email")
    rs.set_defaults(fn=cmd_restore)

    rp = sub.add_parser("repos", help="repositorios de un perfil")
    rp.add_argument("user")
    rp.add_argument("--token")
    rp.set_defaults(fn=cmd_repos)

    br = sub.add_parser("branches", help="ramas que existen en el remoto")
    br.add_argument("--url", required=True)
    br.add_argument("--token")
    br.set_defaults(fn=cmd_branches)

    rl = sub.add_parser("release", help="etiquetar y crear una release")
    rl.add_argument("folder")
    rl.add_argument("--url", required=True)
    rl.add_argument("--token", required=True)
    rl.add_argument("--tag", help="por defecto, la siguiente vX.Y.Z")
    rl.add_argument("--branch", default="main")
    rl.add_argument("--title")
    rl.add_argument("--notes")
    rl.add_argument("--owner")
    rl.add_argument("--repo")
    rl.add_argument("--no-zip", action="store_true")
    rl.set_defaults(fn=cmd_release)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except KeyboardInterrupt:
        print("\n✋ Interrumpido.")
        return 130
    except BrokenPipeError:
        # Quien leía cerró la tubería («… | head -5»). Es normal, no un
        # error: se redirige la salida al vacío para que Python no
        # intente vaciar el buffer sobre un descriptor ya muerto y
        # acabe soltando una traza por pantalla.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
