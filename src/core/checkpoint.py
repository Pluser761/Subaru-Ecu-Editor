"""Сравнение карты с её checkpoint: что изменилось и насколько.

Checkpoint — значения и оси карты на момент открытия, «Set checkpoint» или до переноса.
Всё сравнивается с точностью отображения (2 знака). Если ось сдвинули, ячейку сравнивают
не со старым числом в той же клетке, а с тем, что ЭБУ прочитал бы из старой карты в новой
точке оси (core.ecu_lookup: сырые числа, крайние значения, ступеньки на повторах точек).
Для прошивок, где код ЭБУ не подтверждён, — линейная интерполяция физических значений.
"""

from __future__ import annotations

import math

import numpy as np

from src.core import ecu_lookup
from src.core.interpolation import resample_grid
from src.core.models import Checkpoint, Scaling

# Значения считаются равными с точностью отображения (2 знака)
EPSILON = 0.005


def diff_sign(value: float, base: float, epsilon: float = EPSILON) -> int:
    """+1 — выросло относительно base, -1 — уменьшилось, 0 — без изменений (с точностью
    epsilon — половина последнего показанного знака; по умолчанию 2 знака)."""
    if value == base:  # в т.ч. inf == inf
        return 0
    if not (math.isfinite(value) and math.isfinite(base)):
        return 1 if value > base else -1
    if abs(value - base) < epsilon:
        return 0
    return 1 if value > base else -1


def epsilon_for(decimals: int) -> float:
    """Половина последнего показанного знака: 2 знака → 0.005, 3 → 0.0005."""
    return 0.5 * 10.0 ** -decimals


def same_values(a: list[float], b: list[float]) -> bool:
    return len(a) == len(b) and all(diff_sign(x, y) == 0 for x, y in zip(a, b, strict=True))


def baseline(
    checkpoint: Checkpoint,
    x_axis: list[float],
    y_axis: list[float],
    scaling: Scaling | None = None,
    *,
    ecu: bool = False,
) -> list[list[float]]:
    """Значения checkpoint в точках текущих осей (row — Y, col — X).

    ecu — считать как ЭБУ (core.ecu_lookup, интерполяция сырых чисел по формуле scaling);
    иначе — линейная интерполяция физических значений.

    Оси не менялись — это просто значения checkpoint. Если оси checkpoint не заданы
    (константа) или по ним нельзя интерполировать (немонотонная ось другого размера) —
    тоже значения как есть.
    """
    raw = checkpoint.values
    cp_x, cp_y = checkpoint.x_axis, checkpoint.y_axis
    if not raw or not cp_x or not cp_y or (same_values(cp_x, x_axis) and same_values(cp_y, y_axis)):
        return raw
    try:
        if ecu:
            grid = ecu_lookup.resample(raw, cp_x, cp_y, x_axis, y_axis, scaling)
        else:
            grid = resample_grid(np.array(raw), np.array(cp_x), np.array(cp_y), np.array(x_axis), np.array(y_axis))
    except ValueError:
        return raw
    # inf в соседней ячейке даёт nan даже в точном узле — там берём значение как есть
    grid = np.where(np.isnan(grid), np.array(raw, dtype=float), grid)
    return [[float(v) for v in row] for row in grid]


def table_differs(
    values: list[list[float]], x_axis: list[float], y_axis: list[float], checkpoint: Checkpoint
) -> bool:
    """Отличаются ли значения или оси карты от checkpoint (оси — если они в checkpoint есть)."""
    if len(values) != len(checkpoint.values):
        return True
    if any(not same_values(row, base) for row, base in zip(values, checkpoint.values, strict=True)):
        return True
    if checkpoint.x_axis and not same_values(x_axis, checkpoint.x_axis):
        return True
    return bool(checkpoint.y_axis) and not same_values(y_axis, checkpoint.y_axis)
