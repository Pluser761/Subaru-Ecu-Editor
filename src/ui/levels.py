"""Уровни карт из RomRaider definitions (userlevel): названия и цвета — общие для дерева
карт и окна карты."""

from __future__ import annotations

from PyQt6.QtGui import QColor

from src.i18n import N_
from src.ui.value_colors import cell_background

# Фон по уровню: шкала синий (1) → красный (5), бледнее, чем в ячейках
LEVEL_COLOR_STRENGTH = 0.28
# Названия переводятся при показе: tr(LEVELS[n])
LEVELS = {
    1: N_("основные таблицы"),
    2: N_("дополнительные"),
    3: N_("продвинутые"),
    4: N_("экспертные"),
    5: N_("служебные, отладка"),
}


def level_color(level: int) -> QColor:
    return cell_background((level - 1) / (len(LEVELS) - 1), LEVEL_COLOR_STRENGTH)
