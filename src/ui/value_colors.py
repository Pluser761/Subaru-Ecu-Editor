"""Цветовая шкала значений: одна на таблицу, 3D-меш и 2D-график."""

from __future__ import annotations

import math

from PyQt6.QtGui import QColor

# Классическая «тюнинговая» шкала: синий (мин) → голубой → зелёный → жёлтый → красный (макс)
VALUE_STOPS: list[tuple[float, tuple[int, int, int]]] = [
    (0.0, (40, 80, 220)),
    (0.25, (40, 190, 220)),
    (0.5, (60, 200, 80)),
    (0.75, (240, 210, 40)),
    (1.0, (220, 50, 40)),
]

# Ячейки с inf/nan (деление на ноль в scaling)
NON_FINITE_RGB = (153, 153, 153)


def value_rgb(t: float) -> tuple[int, int, int]:
    """Цвет шкалы в точке t ∈ [0, 1] (линейно между опорными цветами)."""
    if not math.isfinite(t):
        return NON_FINITE_RGB
    t = min(max(t, 0.0), 1.0)
    for (p0, c0), (p1, c1) in zip(VALUE_STOPS, VALUE_STOPS[1:]):
        if t <= p1:
            k = (t - p0) / (p1 - p0)
            return (
                round(c0[0] + (c1[0] - c0[0]) * k),
                round(c0[1] + (c1[1] - c0[1]) * k),
                round(c0[2] + (c1[2] - c0[2]) * k),
            )
    return VALUE_STOPS[-1][1]


def cell_background(t: float, strength: float) -> QColor:
    """Фон ячейки: цвет шкалы, разбавленный белым (strength — доля цвета), чтобы текст читался."""
    r, g, b = value_rgb(t)
    return QColor(
        round(255 + (r - 255) * strength),
        round(255 + (g - 255) * strength),
        round(255 + (b - 255) * strength),
    )
