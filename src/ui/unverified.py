"""Значок «не проверена» — для карт из дополнений definitions (не из RomRaider ecu_defs.xml)."""

from __future__ import annotations

from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import QApplication, QStyle

from src.i18n import tr

ICON_SIZE = 14


def unverified_icon() -> QIcon:
    style = QApplication.style()
    assert style is not None
    return style.standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)


def unverified_pixmap(size: int = ICON_SIZE) -> QPixmap:
    return unverified_icon().pixmap(size, size)


def unverified_tooltip(source: str) -> str:
    return tr(
        "Не проверена: таблица из дополнения definitions ({file}), а не из RomRaider ecu_defs.xml. "
        "Адрес и смысл могут быть неточными",
        file=source,
    )
