"""Интерполяция калибровочных таблиц по осям (перенос карт, сравнение с checkpoint)."""

from __future__ import annotations

import numpy as np


def strictly_monotonic(values: np.ndarray) -> bool:
    """Все значения конечны и строго возрастают или строго убывают."""
    if len(values) < 2 or not np.all(np.isfinite(values)):
        return False
    diffs = np.diff(values)
    return bool(np.all(diffs > 0) or np.all(diffs < 0))


def _interp_1d(xp: np.ndarray, fp: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Линейная интерполяция по значениям оси; вне диапазона — крайние значения.
    Если ось немонотонна (статические подписи и т.п.), сопоставляем по индексу."""
    if len(xp) == 1:
        return np.full(len(x), fp[0])
    if strictly_monotonic(xp):
        if xp[0] > xp[-1]:
            xp, fp = xp[::-1], fp[::-1]
        return np.interp(x, xp, fp)
    if len(xp) == len(x):
        return fp.copy()
    raise ValueError("ось немонотонна, а число точек различается")


def resample_grid(
    values: np.ndarray,
    src_x: np.ndarray,
    src_y: np.ndarray,
    dst_x: np.ndarray,
    dst_y: np.ndarray,
) -> np.ndarray:
    """Билинейная интерполяция матрицы [rows=Y, cols=X] с осей src на оси dst.
    В узлах, совпадающих с исходными, значения точные."""
    grid = np.asarray(values, dtype=float)
    along_x = np.array([_interp_1d(src_x, row, dst_x) for row in grid])
    return np.array([_interp_1d(src_y, col, dst_y) for col in along_x.T]).T
