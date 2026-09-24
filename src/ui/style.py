"""Общий вид графиков: фон, цветовая шкала значений (pyqtgraph), значок подсказки."""

from __future__ import annotations

import pyqtgraph as pg
from PyQt6.QtWidgets import QLabel

from src.ui.value_colors import NON_FINITE_RGB, VALUE_STOPS

BACKGROUND = "#f7f7f5"

# Шкала синий (мин) → красный (макс), общая с таблицей (value_colors)
VALUE_COLORMAP = pg.ColorMap(pos=[p for p, _ in VALUE_STOPS], color=[c for _, c in VALUE_STOPS])

# Ячейки с inf/nan (деление на ноль в scaling) — серые
NON_FINITE_RGBA = tuple(c / 255 for c in NON_FINITE_RGB) + (1.0,)


def hint_icon(text: str) -> QLabel:
    """Значок ⓘ с подсказкой при наведении."""
    label = QLabel("ⓘ")
    label.setToolTip(text)
    label.setStyleSheet("color: #888; font-size: 15px;")
    return label
