"""Чтение карты так, как это делает ЭБУ, — дословно по коду прошивки.

Разобрано в AZ1G301F (SH7058): поиск по оси — 0xBEB8C, чтение и интерполяция uint16 —
0xBEA50, 2D-таблица — 0xBE714, 3D — 0xBE7C8 / 0xBEB5C. Поведение:

- поиск по оси идёт сверху вниз: берётся наибольший i, где ось[i] ≤ x;
  доля = (x − ось[i]) / (ось[i+1] − ось[i]);
- x ≥ последней точки → последняя точка, доля 0; x < первой → первая, доля 0
  (за краями — крайнее значение, без экстраполяции);
- доля 0 → значение ячейки как есть, сосед не читается (точно в точке оси);
- повтор точек оси — ступенька: в самой точке берётся верхний отрезок, отрезок нулевой
  ширины не используется;
- 3D: линейно по X в строках i и i+1, затем линейно по Y между ними (билинейно);
- интерполируются СЫРЫЕ числа из ROM, пересчёт в физические (множитель, смещение
  описателя; в definitions — expression) — уже после. Для нелинейных формул RomRaider
  (AFR = 14.7/(1+x·k)) это не то же самое, что интерполировать физические значения.

Формулы осей в definitions линейные — доля между точками от пересчёта не зависит,
поэтому оси берутся в физических единицах.

Тот же код (байт в байт, кроме относительных вызовов bsr) найден во всех 411 32-битных
прошивках SH7055/SH7058 архива RomRaider; в 16-битных (68HC16) и в новых FA20DIT
(1.5–2 МБ) его нет — для них поведение ЭБУ не проверено (has_denso_lookup → False).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import numpy as np

from src.core.models import Scaling
from src.core.scaling import eval_expression_array


# Код функций из AZ1G301F: поиск по оси (0xBEB8C), чтение uint8/uint16 с интерполяцией
# (0xBEA04 / 0xBEA50), смешивание строк 3D-таблицы uint8/uint16 (0xBEB30 / 0xBEB5C)
_SIGNATURES = (
    "70ff4008f116f1058f0e20088d0f70fcf116f1058dfa2008f0117004f21670fcf211f023000b40094009000bf08de000000bf08d",
    "f28d310c6014f204600c405a8d06f22d6010600c405af12df121f21e000b0009",
    "f28d4000f204310c6015600d405a8d06f22d6011600d405af12df121f21e000b0009",
    "4f22230f031a313c301c6303f31cbf616023f48df4348d06f42c6133bf5a6023f03cf241f42e4f26000bf24c",
    "4f224000230f031a313c301c6303f31cbf706023f48df4348d06f42c6133bf696023f03cf241f42e4f26000bf24c",
)


def _pattern(code_hex: str) -> re.Pattern[bytes]:
    code = bytes.fromhex(code_hex)
    parts = []
    for i in range(0, len(code), 2):
        word = code[i : i + 2]
        # bsr (0xBxxx) — вызов по относительному смещению: оно зависит от расположения кода
        parts.append(b"[\xb0-\xbf]." if 0xB0 <= word[0] <= 0xBF else re.escape(word))
    return re.compile(b"".join(parts), re.DOTALL)


_PATTERNS = tuple(_pattern(code) for code in _SIGNATURES)


def has_denso_lookup(rom: bytes | bytearray) -> bool:
    """Есть ли в прошивке разобранный код чтения карт (значит, ЭБУ считает как в этом модуле)."""
    return all(pattern.search(rom) for pattern in _PATTERNS)


def axis_position(axis: Sequence[float] | np.ndarray, x: float) -> tuple[int, float]:
    """Точка оси и доля до следующей — как 0xBEB8C (ось по неубыванию)."""
    n = len(axis)
    if n == 0:
        raise ValueError("пустая ось")
    if not axis[n - 1] > x:
        return n - 1, 0.0
    for i in range(n - 2, -1, -1):
        if not axis[i] > x:
            return i, (x - axis[i]) / (axis[i + 1] - axis[i])
    return 0, 0.0


def interpolate(values: Sequence[float] | np.ndarray, index: int, fraction: float) -> float:
    """Значение между values[index] и values[index+1] — как 0xBEA50 (доля 0 — соседа не читаем)."""
    value = values[index]
    if fraction == 0:
        return float(value)
    return float(value + fraction * (values[index + 1] - value))


def strictly_monotonic(values: np.ndarray) -> bool:
    """Все значения конечны и строго возрастают или строго убывают."""
    if len(values) < 2 or not np.all(np.isfinite(values)):
        return False
    diffs = np.diff(values)
    return bool(np.all(diffs > 0) or np.all(diffs < 0))


def resample(
    values: np.ndarray | list[list[float]],
    src_x: Sequence[float],
    src_y: Sequence[float],
    dst_x: Sequence[float],
    dst_y: Sequence[float],
    scaling: Scaling | None = None,
) -> np.ndarray:
    """Что ЭБУ прочитает из карты values [rows=Y][cols=X] с осями src в точках осей dst.

    values и результат — физические значения; интерполяция — в сырых числах по формуле
    scaling (None — значения и есть сырые). Ось из одной точки — одно значение на всю ось.
    Немонотонная ось (статические подписи) сопоставляется по номеру точки — только если
    число точек совпадает, иначе ValueError. Убывающую ось ЭБУ не поддерживает; здесь
    она переворачивается.
    """
    grid = np.asarray(values, dtype=float)
    xs, ys = np.asarray(src_x, dtype=float), np.asarray(src_y, dtype=float)
    if len(xs) > 1 and np.all(np.diff(xs) < 0):
        xs, grid = xs[::-1], grid[:, ::-1]
    if len(ys) > 1 and np.all(np.diff(ys) < 0):
        ys, grid = ys[::-1], grid[::-1, :]
    raw = _to_raw(grid, scaling)
    x_pos = _positions(xs, np.asarray(dst_x, dtype=float))
    y_pos = _positions(ys, np.asarray(dst_y, dtype=float))

    result = np.empty((len(y_pos), len(x_pos)))
    for r, (iy, fy) in enumerate(y_pos):
        for c, (ix, fx) in enumerate(x_pos):
            low = interpolate(raw[iy], ix, fx)
            if fy == 0:
                result[r, c] = low
            else:
                high = interpolate(raw[iy + 1], ix, fx)
                result[r, c] = low + fy * (high - low)
    return _to_physical(result, scaling)


def _positions(src: np.ndarray, dst: np.ndarray) -> list[tuple[int, float]]:
    if len(src) == 1:
        return [(0, 0.0)] * len(dst)
    if np.all(np.isfinite(src)) and np.all(np.diff(src) >= 0):
        return [axis_position(src, float(x)) for x in dst]
    if len(src) == len(dst):
        return [(i, 0.0) for i in range(len(dst))]
    raise ValueError("ось немонотонна, а число точек различается")


def _to_raw(values: np.ndarray, scaling: Scaling | None) -> np.ndarray:
    if scaling is None or scaling.to_byte.strip() == "x":
        return values
    with np.errstate(divide="ignore", invalid="ignore"):
        return eval_expression_array(scaling.to_byte, values)


def _to_physical(values: np.ndarray, scaling: Scaling | None) -> np.ndarray:
    if scaling is None or scaling.expression.strip() == "x":
        return values
    with np.errstate(divide="ignore", invalid="ignore"):
        return eval_expression_array(scaling.expression, values)
