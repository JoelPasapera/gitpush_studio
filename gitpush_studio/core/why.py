# -*- coding: utf-8 -*-
"""«¿Por qué no se sube este archivo?» (o por qué sí).

Un archivo que no aparece en GitHub puede faltar por muchas razones: el
.gitignore del proyecto, el de una subcarpeta, la lista privada de la
app (el cuadro de ignorar), la lista global de git del equipo, estar
dentro de otro repositorio, pesar más de lo que GitHub admite… Aquí se
pregunta a git qué regla exacta lo deja fuera (`git check-ignore -v`) y
se explica en una frase, con el archivo y la línea de la regla.
"""

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from . import secret_rules as rules
from .exclude import read_private
from .fs import fmt_size
from .git import Git
from .ignore import IgnoreMatcher, clean_patterns
from .pack import is_generated_txt
from .scan import LIMIT_HARD, LIMIT_WARN

PUBLISHED = "se sube"
NOT_PUBLISHED = "no se sube"
UNKNOWN = "no existe"


@dataclass
class WhyAnswer:
    path: str
    verdict: str
    reasons: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def lines(self) -> List[str]:
        head = {PUBLISHED: "✔ Se sube", NOT_PUBLISHED: "✖ No se sube",
                UNKNOWN: "? No existe"}[self.verdict]
        out = [f"{head}: «{self.path}»"]
        out += ["  · " + reason for reason in self.reasons]
        out += ["  ℹ " + note for note in self.notes]
        return out


def relative_path(folder: str, chosen: str) -> Tuple[str, str]:
    """(ruta relativa con «/», problema) de lo que se eligió o escribió."""
    text = (chosen or "").strip().strip('"').strip("'")
    if not text:
        return "", "indica un archivo o una carpeta"
    path = Path(text)
    if not path.is_absolute():
        path = Path(folder) / path
    base = os.path.realpath(folder)
    real = os.path.realpath(str(path.parent)) if path.parent != path \
        else os.path.realpath(str(path))
    target = os.path.join(real, path.name)
    if os.path.normcase(target) == os.path.normcase(base):
        return "", "esa es la carpeta del proyecto entera"
    prefix = base.rstrip(os.sep) + os.sep
    if not os.path.normcase(target).startswith(os.path.normcase(prefix)):
        return "", "no está dentro de la carpeta del proyecto"
    return target[len(prefix):].replace(os.sep, "/"), ""


def _nested_repository(folder: str, rel: str) -> str:
    """La carpeta (relativa) de otro repositorio que contiene `rel`."""
    parts = rel.split("/")
    for depth in range(1, len(parts)):
        candidate = "/".join(parts[:depth])
        if os.path.lexists(os.path.join(folder, candidate, ".git")):
            return candidate
    return ""


def _describe_source(folder: str, source: str, line: str,
                     pattern: str, private: Sequence[str]) -> str:
    """La regla de `git check-ignore -v`, explicada."""
    where = source.replace("\\", "/")
    if where.endswith("info/exclude"):
        if pattern in private:
            return (f"lo deja fuera la lista privada de la app (el cuadro "
                    f"«Archivos y carpetas a ignorar»): regla «{pattern}». "
                    "Para volver a publicarlo, quítala del cuadro.")
        return (f"lo deja fuera la lista privada de git del repositorio "
                f"(.git/info/exclude, línea {line}): regla «{pattern}».")
    absolute = os.path.normcase(os.path.abspath(os.path.join(folder, where)))
    inside = os.path.normcase(os.path.abspath(folder)) + os.sep
    if absolute.startswith(inside):
        relative = absolute[len(inside):].replace(os.sep, "/")
        return (f"lo deja fuera «{relative}» (línea {line}): regla "
                f"«{pattern}».")
    return (f"lo deja fuera tu lista global de git ({source}, línea "
            f"{line}): regla «{pattern}». Afecta a todos tus proyectos.")


def _check_ignore(git: Git, folder: str, rel: str) -> Optional[Tuple[str,
                                                                     str,
                                                                     str]]:
    """(origen, línea, patrón) de la regla que decide, o None."""
    process = git.run(["check-ignore", "-v", "--no-index", "-z", "--stdin"],
                      cwd=folder, check=False, echo=False, binary=True,
                      input=os.fsencode(rel) + b"\0")
    if process.returncode not in (0, 1):
        return None
    fields = (process.stdout or b"").split(b"\0")
    if len(fields) < 4 or not fields[2]:
        return None
    source = os.fsdecode(fields[0])
    line = os.fsdecode(fields[1])
    pattern = os.fsdecode(fields[2])
    return source, line, pattern


def _is_tracked(git: Git, folder: str, rel: str) -> bool:
    process = git.run(["ls-files", "--error-unmatch", "--", rel],
                      cwd=folder, check=False, echo=False)
    return process.returncode == 0


def _box_pattern(rel: str, patterns: Sequence[str]) -> str:
    """El primer patrón del cuadro que deja fuera `rel` ("" si ninguno)."""
    lowered = rel.lower()
    for pattern in clean_patterns(patterns):
        if IgnoreMatcher([pattern]).ignored_path(lowered):
            return pattern
    return ""


def explain(folder: str, chosen: str, box: Sequence[str] = (),
            git: Optional[Git] = None) -> WhyAnswer:
    """Por qué `chosen` (ruta absoluta o relativa al proyecto) se sube o
    no al publicar `folder` con el cuadro de ignorar `box`."""
    rel, problem = relative_path(folder, chosen)
    if problem:
        return WhyAnswer(chosen, UNKNOWN, [problem])
    answer = WhyAnswer(rel, PUBLISHED)
    full = os.path.join(folder, rel)
    try:
        info = os.lstat(full)
    except OSError:
        answer.verdict = UNKNOWN
        answer.reasons.append("no está en la carpeta (¿se borró o cambió de "
                              "nombre?)")
        return answer
    is_dir = stat.S_ISDIR(info.st_mode)

    nested = _nested_repository(folder, rel)
    if nested:
        answer.verdict = NOT_PUBLISHED
        answer.reasons.append(f"está dentro de «{nested}», que es otro "
                              "repositorio de git (tiene su propia carpeta "
                              ".git): sus archivos no se publican aquí. "
                              "Publícalo aparte o quita su .git.")
        return answer
    if rel.split("/")[0] == ".git":
        answer.verdict = NOT_PUBLISHED
        answer.reasons.append("es parte de .git, los datos internos de git: "
                              "nunca se publican como archivos.")
        return answer

    box_rule = _box_pattern(rel, box)
    is_repo = os.path.isdir(os.path.join(folder, ".git"))
    git = git or Git()
    rule = None
    tracked = False
    if is_repo:
        private = list(read_private(folder, git).all)
        check = rel + "/" if is_dir else rel
        rule = _check_ignore(git, folder, check)
        tracked = not is_dir and _is_tracked(git, folder, rel)
        if rule is not None and rule[2].startswith("!"):
            answer.notes.append(
                f"una regla con «!» lo vuelve a incluir ({rule[0]}, línea "
                f"{rule[1]}: «{rule[2]}»)")
            rule = None
        if rule is not None:
            answer.verdict = NOT_PUBLISHED
            answer.reasons.append(_describe_source(folder, *rule, private))
    if box_rule and (rule is None or box_rule not in rule[2]):
        answer.verdict = NOT_PUBLISHED
        answer.reasons.append(f"lo deja fuera el cuadro «Archivos y carpetas "
                              f"a ignorar»: regla «{box_rule}» (sin "
                              "escribirla en el .gitignore).")
    if answer.verdict == NOT_PUBLISHED and tracked:
        answer.notes.append("ya estaba publicado: al publicar, la app lo "
                            "quita de GitHub (sigue en tu carpeta) para que "
                            "deje de subirse")
    if not is_repo and not box_rule:
        from .walk import project_entries
        entries, _skipped = project_entries(folder, box, use_git=False)
        listed = {entry_rel for entry_rel, _p, _s in entries}
        if not is_dir and rel not in listed:
            answer.verdict = NOT_PUBLISHED
            answer.reasons.append("lo deja fuera el .gitignore del proyecto.")

    if answer.verdict == PUBLISHED:
        _why_published(answer, folder, rel, info, is_dir, tracked, is_repo)
    return answer


def _why_published(answer: WhyAnswer, folder: str, rel: str,
                   info: os.stat_result, is_dir: bool, tracked: bool,
                   is_repo: bool) -> None:
    """Se sube: lo que conviene saber de todos modos."""
    if is_dir:
        try:
            empty = not any(os.scandir(os.path.join(folder, rel)))
        except OSError:
            empty = False
        if empty:
            answer.verdict = NOT_PUBLISHED
            answer.reasons.append("es una carpeta vacía, y git solo guarda "
                                  "archivos: pon dentro un archivo (por "
                                  "ejemplo .gitkeep) para que aparezca.")
            return
        answer.reasons.append("ninguna regla deja fuera la carpeta (lo que "
                              "hay dentro, archivo a archivo, puede ser otra "
                              "historia: pregunta por cada uno).")
        return
    answer.reasons.append("ninguna regla lo deja fuera.")
    if stat.S_ISLNK(info.st_mode):
        answer.notes.append("es un enlace simbólico: se publica el enlace, "
                            "no lo que hay al otro lado")
    if info.st_size > LIMIT_HARD:
        answer.notes.append(f"pesa {fmt_size(info.st_size)}: GitHub rechaza "
                            "los archivos de más de 100 MB. La revisión te "
                            "dejará elegir entre dejarlo fuera o subirlo con "
                            "Git LFS.")
    elif info.st_size > LIMIT_WARN:
        answer.notes.append(f"pesa {fmt_size(info.st_size)}: GitHub avisa a "
                            "partir de 50 MB")
    by_name = rules.name_rule(rel)
    if by_name is not None:
        answer.notes.append(f"la revisión de seguridad te avisará: "
                            f"{by_name[0]}")
    if is_generated_txt(os.path.join(folder, rel)):
        answer.notes.append("es un TXT generado por la app: si es el del "
                            "proyecto, la app lo deja fuera al publicar")
    if is_repo and tracked:
        answer.notes.append("ya está en el repositorio (rastreado)")
