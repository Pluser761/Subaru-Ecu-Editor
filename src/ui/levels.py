"""Уровни карт из RomRaider definitions (userlevel): названия и цвета — общие для дерева
карт и окна карты."""

from __future__ import annotations

from PyQt6.QtGui import QColor

from src.ui.value_colors import cell_background

# Фон по уровню: шкала синий (1) → красный (5), бледнее, чем в ячейках
LEVEL_COLOR_STRENGTH = 0.28
LEVELS = {
    1: "основные таблицы",
    2: "дополнительные",
    3: "продвинутые",
    4: "экспертные",
    5: "служебные, отладка",
}


def level_color(level: int) -> QColor:
    return cell_background((level - 1) / (len(LEVELS) - 1), LEVEL_COLOR_STRENGTH)
