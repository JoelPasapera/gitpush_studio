# -*- coding: utf-8 -*-
"""Uso sin interfaz gráfica.

    python -m gitpush_studio.cli publish --folder RUTA --url URL [opciones]
    python -m gitpush_studio.cli publish --profile NOMBRE [--profile OTRO …]
    python -m gitpush_studio.cli scan RUTA [--ignore PATRÓN …]
    python -m gitpush_studio.cli pack RUTA [-o SALIDA.txt]
    python -m gitpush_studio.cli status RUTA
    python -m gitpush_studio.cli licencias
    python -m gitpush_studio.cli pull RUTA --url URL [--branch] [--mode merge|mirror] [--yes]
    python -m gitpush_studio.cli versions RUTA
    python -m gitpush_studio.cli restore RUTA SHA
    python -m gitpush_studio.cli repos USUARIO
    python -m gitpush_studio.cli branches --url URL
    python -m gitpush_studio.cli release RUTA --url URL --token T --tag v1.2.0 [--title] [--notes] [--no-zip]

Varios --profile se publican a la vez (los que comparten repositorio,
en serie). Con --yes no se pregunta nada: útil para scripts.
"""

import argparse
import os
import sys
import threading
from pathlib import Path

from . import APP_NAME, APP_VERSION
from .core import history, licenses, release, sync
from .core.config import load_config, saved_token
from .core.fs import fmt_size
from .core.git import Git
from .core.github import list_user_repos
from .core.pack import consolidate_project
from .core.publish import (Callbacks, PreviewChoice, PublishJob,
                            publish_many)
from .core.scan import scan_project
from .core.urls import parse_owner_repo

_print_lock = threading.Lock()


def _say(prefix, text):
    with _print_lock:
        print(f"{prefix}{text}", flush=True)


def _print_findings(findings, prefix=""):
    for f in findings:
        _say(prefix, f"  [{f['kind']}] {f['path']}  — {f['reason']}")


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


def cmd_publish(a):
    jobs = _jobs_from_args(a)
    many = len(jobs) > 1
    token = _token(a)
    for job in jobs:
        job.token = token
    results = publish_many(
        jobs, callbacks_for=lambda j: _make_callbacks(j, a.yes, many),
        workers=a.workers)
    bad = 0
    for r in results:
        mark = {"ok": "✔", "cancelled": "✋", "blocked": "🤖"}.get(r.status,
                                                                    "✖")
        detail = r.web_url if r.ok else (r.message.splitlines() or [""])[0]
        print(f"{mark} {r.job.label}: {r.status} ({r.seconds:.1f} s) "
              f"{detail}")
        bad += not r.ok
    return 1 if bad else 0


def cmd_scan(a):
    findings, stats = scan_project(a.folder, a.ignore or [])
    print(f"Revisados {stats['files']} archivos en {stats['seconds']:.1f} s.")
    if not findings:
        print("✔ Sin archivos grandes ni secretos a la vista.")
        return 0
    print(f"⚠ {len(findings)} hallazgo(s):")
    _print_findings(findings)
    return 2


def cmd_pack(a):
    folder = Path(a.folder).resolve()
    out = Path(a.output) if a.output else folder.parent / f"{folder.name}.txt"
    st = consolidate_project(folder, out, a.ignore or [])
    print(f"✔ {out}  ({st['files']} archivos, {fmt_size(st['bytes'])}, "
          f"{st['seconds']:.1f} s"
          + (", truncado" if st["truncated"] else "") + ")")
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
    dirty = git.is_dirty(a.folder)
    print("Hay cambios sin publicar." if dirty else "Todo publicado.")
    return 1 if dirty else 0


def _identity(a):
    return (a.user, a.email) if getattr(a, "user", None) and \
        getattr(a, "email", None) else None


def cmd_pull(a):
    rep = sync.inspect(a.folder, a.url, a.branch, token=_token(a))
    if not rep.remote_exists:
        print(f"La rama «{a.branch}» no existe en el remoto.")
        return 2
    if rep.up_to_date:
        print("✔ Tu carpeta ya está al día.")
        return 0
    for title, items in (("llegarán", rep.added), ("cambiarán", rep.modified),
                         ("desaparecerán", rep.deleted)):
        if items:
            print(f"  {title}: {len(items)}")
            for p in items[:20]:
                print(f"    {p}")
    if rep.local_changes:
        print(f"  cambios locales sin publicar: {len(rep.local_changes)} "
              "(se guardarán en una rama de respaldo)")
    if not a.yes and not input("¿Traer los cambios? [s/N]: ").strip() \
            .lower().startswith("s"):
        return 130
    r = sync.apply(a.folder, a.url, a.branch, mode=a.mode,
                   identity=_identity(a), token=_token(a),
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
    r = history.restore_version(a.folder, a.sha, identity=_identity(a),
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
    pub.set_defaults(fn=cmd_publish)

    sc = sub.add_parser("scan", help="revisión de seguridad de una carpeta")
    sc.add_argument("folder")
    sc.add_argument("--ignore", action="append", metavar="PATRÓN")
    sc.set_defaults(fn=cmd_scan)

    pk = sub.add_parser("pack", help="consolidar una carpeta a TXT")
    pk.add_argument("folder")
    pk.add_argument("-o", "--output")
    pk.add_argument("--ignore", action="append", metavar="PATRÓN")
    pk.set_defaults(fn=cmd_pack)

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

    rs = sub.add_parser("restore", help="volver a una versión (commit nuevo)")
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
