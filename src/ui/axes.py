"""Сопоставление оси карты (core.models.Axis) с ориентацией заголовка Qt.

X — столбцы (Horizontal), Y — строки (Vertical). Единственное место такого перевода:
остальной UI пользуется им вместо локальных `if orientation == Qt.Orientation.Horizontal`.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt

from src.core.models import Axis


def axis_orientation(axis: Axis) -> Qt.Orientation:
    """Ось карты → ориентация заголовка: X → Horizontal, Y → Vertical."""
    return Qt.Orientation.Horizontal if axis is Axis.X else Qt.Orientation.Vertical


def orientation_axis(orientation: Qt.Orientation) -> Axis:
    """Обратно: Horizontal → X, Vertical → Y."""
    return Axis.X if orientation == Qt.Orientation.Horizontal else Axis.Y
