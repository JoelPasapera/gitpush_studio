# -*- coding: utf-8 -*-
"""Diferencias línea a línea, escritas desde cero (algoritmo de Myers).

Para qué sirve: enseñar qué cambia en un archivo antes de tocarlo (al
aplicar un TXT devuelto por una IA, en la vista previa de la publicación)
y aplicar cambios línea a línea conservando, byte a byte, todas las
líneas que no cambian.

Cómo funciona, por capas:

1. Cada línea se convierte en un número entero: líneas iguales reciben
   el mismo número. Comparar enteros es mucho más barato que comparar
   cadenas, sobre todo en líneas largas.
2. Se apartan el principio y el final comunes: en el caso de siempre
   (unas pocas líneas cambiadas en un archivo largo) solo queda por
   comparar un tramo pequeño.
3. Las líneas que solo existen en uno de los dos lados no pueden formar
   pareja nunca: se quitan antes de buscar y no cuestan nada. Quitar
   elementos que no aparecen en el otro lado no cambia la subsecuencia
   común más larga, así que el resultado sigue siendo óptimo.
4. Lo que queda se resuelve con el algoritmo de Myers («An O(ND)
   Difference Algorithm and Its Variations», 1986) en su variante de
   memoria lineal: se avanza a la vez desde el principio y desde el
   final hasta que los dos caminos se tocan; ese punto parte el problema
   en dos más pequeños, que se resuelven igual. Se usa una pila explícita
   en vez de recursión: ni un archivo enorme agota la pila de Python.
5. Si una comparación se vuelve carísima (dos versiones que no se
   parecen en casi nada), se corta por el punto que más avanzó. El
   resultado sigue siendo CORRECTO —una lista válida de cambios que
   convierte un lado en el otro—, solo que quizá no la más corta.

El resultado básico es la lista de parejas (i, j) de líneas iguales, en
orden creciente en los dos lados. A partir de ella se derivan los
bloques de cambios (`Opcode`) y los trozos con contexto (`Hunk`) que se
enseñan en pantalla o en formato «diff unificado».
"""

from typing import Hashable, Iterator, List, NamedTuple, Optional, Sequence, Tuple

# Etiquetas de los bloques, las mismas que usa `difflib` para que
# cualquiera que lo conozca entienda el resultado.
EQUAL = "equal"
REPLACE = "replace"
DELETE = "delete"
INSERT = "insert"

# Presupuesto de trabajo de una comparación, en «pasos» (cada diagonal
# examinada y cada línea recorrida en una serpiente cuentan uno). Unos
# dos millones de pasos son del orden de un segundo en Python. Con él se
# decide cuántas vueltas puede dar cada búsqueda antes de cortar por el
# mejor punto alcanzado: muchas en un archivo pequeño (el resultado es
# siempre el óptimo) y menos en uno enorme y muy cambiado, para que el
# coste total quede acotado.
STEP_BUDGET = 2_000_000
MIN_ROUNDS = 64
MAX_ROUNDS = 4096
# Si aun así el trabajo se dispara (cajas muy estrechas, muchísimos
# cortes), lo que quede se da por cambiado entero: sigue siendo correcto.
HARD_BUDGET_FACTOR = 4

# Contexto por defecto de un trozo: líneas iguales antes y después.
DEFAULT_CONTEXT = 3

Pair = Tuple[int, int]


class Opcode(NamedTuple):
    """Un bloque: `a[a_start:a_end]` pasa a ser `b[b_start:b_end]`."""
    tag: str
    a_start: int
    a_end: int
    b_start: int
    b_end: int


class DiffLine(NamedTuple):
    """Una línea de un trozo, lista para enseñarla.

    `tag` es " " (igual), "-" (sale) o "+" (entra). Los números de línea
    empiezan en 1; el del lado en el que la línea no existe es None.
    """
    tag: str
    text: str
    old_number: Optional[int]
    new_number: Optional[int]


class Hunk(NamedTuple):
    """Un trozo con contexto, como los «@@ -l,s +l,s @@» de git."""
    old_start: int
    old_length: int
    new_start: int
    new_length: int
    lines: List[DiffLine]


# ------------------------------------------------------------------ #
#  Preparación                                                        #
# ------------------------------------------------------------------ #
def _number_lines(a: Sequence[Hashable],
                  b: Sequence[Hashable]) -> Tuple[List[int], List[int]]:
    """Cada elemento pasa a ser un entero; iguales → mismo entero."""
    codes: dict = {}

    def number(sequence: Sequence[Hashable]) -> List[int]:
        numbered: List[int] = []
        for item in sequence:
            code = codes.get(item)
            if code is None:
                code = len(codes)
                codes[item] = code
            numbered.append(code)
        return numbered

    return number(a), number(b)


# ------------------------------------------------------------------ #
#  Myers con memoria lineal                                          #
# ------------------------------------------------------------------ #
class _Budget:
    """Pasos de trabajo que quedan para toda una comparación."""
    __slots__ = ("left",)

    def __init__(self, steps: int) -> None:
        self.left = steps

    def spend(self, steps: int) -> None:
        self.left -= steps

    @property
    def exhausted(self) -> bool:
        return self.left <= 0


def rounds_for(total_lines: int, budget: int = STEP_BUDGET) -> int:
    """Vueltas que puede dar cada búsqueda según el tamaño a comparar.

    Cada corte por «demasiado caro» avanza del orden de las vueltas dadas
    y cuesta su cuadrado, así que el coste total es más o menos
    líneas × vueltas: con vueltas = presupuesto / líneas queda acotado.
    """
    if total_lines <= 0:
        return MAX_ROUNDS
    rounds = budget // total_lines
    if rounds < MIN_ROUNDS:
        return MIN_ROUNDS
    if rounds > MAX_ROUNDS:
        return MAX_ROUNDS
    return rounds


def _split_point(xs: List[int], a_lo: int, a_hi: int,
                 ys: List[int], b_lo: int, b_hi: int,
                 max_rounds: int, budget: _Budget) -> Optional[Pair]:
    """Punto (x, y) por el que partir la caja `xs[a_lo:a_hi]` frente a
    `ys[b_lo:b_hi]`, o None si en la caja no hay nada que emparejar (o
    se acabó el presupuesto: entonces la caja se da por cambiada entera).

    Se avanza desde el principio («camino de ida») y desde el final
    («camino de vuelta») una edición por vuelta. En cada diagonal k
    (k = x - y) se guarda hasta dónde llegó cada camino. Cuando los dos
    caminos se solapan en una diagonal, el final de la serpiente de ida
    (el tramo de líneas iguales que siguió a la última edición) está en
    un camino de edición mínima: es el punto de corte.

    Las diagonales que se salen de la cuadrícula se descartan de las
    vueltas siguientes (como hace diff-match-patch): un camino que ya se
    salió nunca vuelve a entrar.

    Requisito: la caja no está vacía por ningún lado y su primer y último
    elemento son distintos (el principio y el final comunes ya se
    apartaron).
    """
    if budget.exhausted:
        return None
    n = a_hi - a_lo
    m = b_hi - b_lo
    max_d = (n + m + 1) // 2
    offset = max_d + 1
    length = 2 * max_d + 3
    forward = [-1] * length
    backward = [-1] * length
    forward[offset + 1] = 0
    backward[offset + 1] = 0
    delta = n - m
    # Con delta impar el solape se descubre en el camino de ida; con delta
    # par, en el de vuelta.
    check_on_forward = (delta % 2) != 0
    k1_start = 0
    k1_end = 0
    k2_start = 0
    k2_end = 0
    best_x = 0
    best_y = 0

    def makes_progress(x: int, y: int) -> bool:
        """¿Parte la caja en dos más pequeñas de verdad?"""
        if x < 0 or y < 0 or x > n or y > m:
            return False
        if x == 0 and y == 0:
            return False
        return not (x == n and y == m)

    for d in range(max_d + 1):
        steps = 0
        # ---- camino de ida ----------------------------------------
        for k1 in range(-d + k1_start, d + 1 - k1_end, 2):
            index = offset + k1
            if k1 == -d or (k1 != d and forward[index - 1] < forward[index + 1]):
                x1 = forward[index + 1]              # baja: entra una línea
            else:
                x1 = forward[index - 1] + 1          # derecha: sale una línea
            y1 = x1 - k1
            if x1 < 0 or y1 < 0:
                continue                             # vecino sin calcular
            snake_from = x1
            while x1 < n and y1 < m and xs[a_lo + x1] == ys[b_lo + y1]:
                x1 += 1
                y1 += 1
            steps += 1 + x1 - snake_from
            forward[index] = x1
            if x1 > n:
                k1_end += 2                          # se salió por la derecha
                continue
            if y1 > m:
                k1_start += 2                        # se salió por abajo
                continue
            if x1 + y1 > best_x + best_y:
                best_x = x1
                best_y = y1
            if check_on_forward:
                mirror = offset + delta - k1
                if 0 <= mirror < length and backward[mirror] != -1:
                    if x1 >= n - backward[mirror] and makes_progress(x1, y1):
                        budget.spend(steps)
                        return a_lo + x1, b_lo + y1
        # ---- camino de vuelta -------------------------------------
        for k2 in range(-d + k2_start, d + 1 - k2_end, 2):
            index = offset + k2
            if k2 == -d or (k2 != d and backward[index - 1] < backward[index + 1]):
                x2 = backward[index + 1]
            else:
                x2 = backward[index - 1] + 1
            y2 = x2 - k2
            if x2 < 0 or y2 < 0:
                continue                             # vecino sin calcular
            snake_from = x2
            while (x2 < n and y2 < m
                   and xs[a_hi - 1 - x2] == ys[b_hi - 1 - y2]):
                x2 += 1
                y2 += 1
            steps += 1 + x2 - snake_from
            backward[index] = x2
            if x2 > n:
                k2_end += 2                          # se salió por la izquierda
                continue
            if y2 > m:
                k2_start += 2                        # se salió por arriba
                continue
            if not check_on_forward:
                mirror = offset + delta - k2
                if 0 <= mirror < length and forward[mirror] != -1:
                    x1 = forward[mirror]
                    y1 = x1 - (mirror - offset)
                    if x1 >= n - x2 and makes_progress(x1, y1):
                        budget.spend(steps)
                        return a_lo + x1, b_lo + y1
        budget.spend(steps)
        if d + 1 >= max_rounds or budget.exhausted:
            # Demasiado caro: se corta por el punto de ida que más avanzó.
            # Sigue siendo una partición válida; solo puede que no sea la
            # de menos cambios.
            if makes_progress(best_x, best_y):
                return a_lo + best_x, b_lo + best_y
            return None
    return None


def _myers_pairs(xs: List[int], ys: List[int],
                 max_rounds: int, budget: _Budget) -> List[Pair]:
    """Parejas (i, j) de una subsecuencia común de `xs` e `ys` (la más
    larga salvo que se corte por caro), en orden creciente."""
    pairs: List[Pair] = []
    pending: List[Tuple[int, int, int, int]] = [(0, len(xs), 0, len(ys))]
    while pending:
        a_lo, a_hi, b_lo, b_hi = pending.pop()
        # Principio común de esta caja.
        while a_lo < a_hi and b_lo < b_hi and xs[a_lo] == ys[b_lo]:
            pairs.append((a_lo, b_lo))
            a_lo += 1
            b_lo += 1
        # Final común de esta caja.
        while a_lo < a_hi and b_lo < b_hi and xs[a_hi - 1] == ys[b_hi - 1]:
            a_hi -= 1
            b_hi -= 1
            pairs.append((a_hi, b_hi))
        if a_lo == a_hi or b_lo == b_hi:
            continue                     # solo quedan altas o solo bajas
        split = _split_point(xs, a_lo, a_hi, ys, b_lo, b_hi, max_rounds,
                             budget)
        if split is None:
            continue                     # nada emparejable: todo cambia
        x, y = split
        pending.append((a_lo, x, b_lo, y))
        pending.append((x, a_hi, y, b_hi))
    pairs.sort()
    return pairs


def matching_pairs(a: Sequence[Hashable], b: Sequence[Hashable],
                   step_budget: int = STEP_BUDGET) -> List[Pair]:
    """Parejas (i, j) con `a[i] == b[j]` que forman una subsecuencia
    común de longitud máxima (salvo en casos carísimos, ver arriba), en
    orden creciente de i y de j.

    Es la base de todo lo demás: lo que no está en una pareja, sale (de
    `a`) o entra (de `b`).
    """
    xs, ys = _number_lines(a, b)
    n = len(xs)
    m = len(ys)

    prefix = 0
    while prefix < n and prefix < m and xs[prefix] == ys[prefix]:
        prefix += 1
    suffix = 0
    while (suffix < n - prefix and suffix < m - prefix
           and xs[n - 1 - suffix] == ys[m - 1 - suffix]):
        suffix += 1

    pairs: List[Pair] = []
    for i in range(prefix):
        pairs.append((i, i))

    middle_a = xs[prefix:n - suffix]
    middle_b = ys[prefix:m - suffix]
    present_in_b = set(middle_b)
    present_in_a = set(middle_a)
    # Posiciones (dentro del tramo central) de lo que sí puede emparejarse.
    kept_a = [i for i, code in enumerate(middle_a) if code in present_in_b]
    kept_b = [j for j, code in enumerate(middle_b) if code in present_in_a]
    reduced_a = [middle_a[i] for i in kept_a]
    reduced_b = [middle_b[j] for j in kept_b]
    rounds = rounds_for(len(reduced_a) + len(reduced_b), step_budget)
    budget = _Budget(step_budget * HARD_BUDGET_FACTOR)
    for i, j in _myers_pairs(reduced_a, reduced_b, rounds, budget):
        pairs.append((prefix + kept_a[i], prefix + kept_b[j]))

    for k in range(suffix):
        pairs.append((n - suffix + k, m - suffix + k))
    return pairs


# ------------------------------------------------------------------ #
#  Bloques y trozos                                                   #
# ------------------------------------------------------------------ #
def opcodes_from_pairs(pairs: Sequence[Pair], n: int, m: int) -> List[Opcode]:
    """Bloques consecutivos que convierten `a` (n líneas) en `b` (m)."""
    opcodes: List[Opcode] = []
    i = 0
    j = 0
    k = 0
    total = len(pairs)
    while k <= total:
        if k < total:
            next_i, next_j = pairs[k]
        else:
            next_i, next_j = n, m            # centinela: el final
        if i < next_i and j < next_j:
            opcodes.append(Opcode(REPLACE, i, next_i, j, next_j))
        elif i < next_i:
            opcodes.append(Opcode(DELETE, i, next_i, j, j))
        elif j < next_j:
            opcodes.append(Opcode(INSERT, i, i, j, next_j))
        if k == total:
            break
        # Tramo de parejas consecutivas en diagonal: un solo bloque igual.
        run_end = k + 1
        while (run_end < total
               and pairs[run_end][0] == pairs[run_end - 1][0] + 1
               and pairs[run_end][1] == pairs[run_end - 1][1] + 1):
            run_end += 1
        length = run_end - k
        opcodes.append(Opcode(EQUAL, next_i, next_i + length,
                              next_j, next_j + length))
        i = next_i + length
        j = next_j + length
        k = run_end
    return opcodes


def opcodes(a: Sequence[Hashable], b: Sequence[Hashable]) -> List[Opcode]:
    """Bloques que convierten `a` en `b` (iguales, cambiados, salen, entran)."""
    return opcodes_from_pairs(matching_pairs(a, b), len(a), len(b))


def change_counts(codes: Sequence[Opcode]) -> Tuple[int, int]:
    """(líneas que entran, líneas que salen)."""
    added = 0
    removed = 0
    for code in codes:
        if code.tag in (REPLACE, DELETE):
            removed += code.a_end - code.a_start
        if code.tag in (REPLACE, INSERT):
            added += code.b_end - code.b_start
    return added, removed


def has_changes(codes: Sequence[Opcode]) -> bool:
    for code in codes:
        if code.tag != EQUAL:
            return True
    return False


def grouped_opcodes(codes: Sequence[Opcode],
                    context: int = DEFAULT_CONTEXT) -> Iterator[List[Opcode]]:
    """Agrupa los bloques en trozos con `context` líneas iguales alrededor
    de cada cambio. Dos cambios separados por menos de 2·context líneas
    iguales van en el mismo trozo."""
    work = list(codes)
    if not has_changes(work):
        return
    first = work[0]
    if first.tag == EQUAL:
        work[0] = Opcode(EQUAL, max(first.a_start, first.a_end - context),
                         first.a_end,
                         max(first.b_start, first.b_end - context),
                         first.b_end)
    last = work[-1]
    if last.tag == EQUAL:
        work[-1] = Opcode(EQUAL, last.a_start,
                          min(last.a_end, last.a_start + context),
                          last.b_start,
                          min(last.b_end, last.b_start + context))
    double_context = context + context
    group: List[Opcode] = []
    for code in work:
        if code.tag == EQUAL and code.a_end - code.a_start > double_context:
            group.append(Opcode(EQUAL, code.a_start,
                                min(code.a_end, code.a_start + context),
                                code.b_start,
                                min(code.b_end, code.b_start + context)))
            yield group
            group = []
            code = Opcode(EQUAL, max(code.a_start, code.a_end - context),
                          code.a_end,
                          max(code.b_start, code.b_end - context),
                          code.b_end)
        group.append(code)
    if group and not (len(group) == 1 and group[0].tag == EQUAL):
        yield group


def hunks(a: Sequence[str], b: Sequence[str],
          context: int = DEFAULT_CONTEXT,
          codes: Optional[Sequence[Opcode]] = None) -> List[Hunk]:
    """Trozos con contexto para enseñar en pantalla.

    `a` y `b` son las líneas tal como se quieren mostrar. Si ya se tienen
    los bloques (calculados con otra clave de comparación, por ejemplo
    sin los blancos del final), se pasan en `codes`.
    """
    if codes is None:
        codes = opcodes(a, b)
    result: List[Hunk] = []
    for group in grouped_opcodes(codes, context):
        first = group[0]
        last = group[-1]
        lines: List[DiffLine] = []
        for code in group:
            if code.tag == EQUAL:
                for offset in range(code.a_end - code.a_start):
                    i = code.a_start + offset
                    j = code.b_start + offset
                    lines.append(DiffLine(" ", a[i], i + 1, j + 1))
                continue
            for i in range(code.a_start, code.a_end):
                lines.append(DiffLine("-", a[i], i + 1, None))
            for j in range(code.b_start, code.b_end):
                lines.append(DiffLine("+", b[j], None, j + 1))
        result.append(Hunk(first.a_start + 1, last.a_end - first.a_start,
                           first.b_start + 1, last.b_end - first.b_start,
                           lines))
    return result


def _unified_range(start: int, length: int) -> str:
    """El «l,s» de una cabecera @@, con las mismas reglas que GNU diff:
    sin longitud si es 1, y el número de la línea ANTERIOR si es 0."""
    if length == 1:
        return str(start)
    if length == 0:
        return f"{start - 1},0"
    return f"{start},{length}"


def hunk_header(hunk: Hunk) -> str:
    return (f"@@ -{_unified_range(hunk.old_start, hunk.old_length)} "
            f"+{_unified_range(hunk.new_start, hunk.new_length)} @@")


def unified_diff(a: Sequence[str], b: Sequence[str], old_label: str = "a",
                 new_label: str = "b", context: int = DEFAULT_CONTEXT,
                 codes: Optional[Sequence[Opcode]] = None) -> str:
    """Las diferencias en formato «diff unificado» (el de git), como
    texto. Las líneas de `a` y `b` van sin su salto de línea final.
    Devuelve "" si no hay diferencias."""
    parts: List[str] = []
    for hunk in hunks(a, b, context, codes):
        if not parts:
            parts.append(f"--- {old_label}")
            parts.append(f"+++ {new_label}")
        parts.append(hunk_header(hunk))
        for line in hunk.lines:
            parts.append(line.tag + line.text)
    if not parts:
        return ""
    return "\n".join(parts) + "\n"


def apply_opcodes(a: Sequence[Hashable], b: Sequence[Hashable],
                  codes: Sequence[Opcode]) -> List[Hashable]:
    """Reconstruye `b` a partir de `a` y los bloques (para comprobar que
    unos bloques son coherentes): lo igual sale de `a`, lo nuevo de `b`."""
    out: List[Hashable] = []
    for code in codes:
        if code.tag == EQUAL:
            out.extend(a[code.a_start:code.a_end])
        else:
            out.extend(b[code.b_start:code.b_end])
    return out
