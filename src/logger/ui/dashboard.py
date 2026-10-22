"""Дашборд логгера: плитка на каждый выбранный параметр — имя, значение крупно, единицы,
мин/макс. Плитки раскладываются сеткой по ширине окна."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QResizeEvent
from PyQt6.QtWidgets import QFrame, QGridLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

TILE_WIDTH = 210  # примерная ширина плитки: по ней — число колонок
TILE_STYLE = "QFrame#tile {{ background: {bg}; border: 1px solid {border}; border-radius: 6px; }}"
NORMAL = ("#ffffff", "#cfcfcf")
SLOW = ("#fff4e5", "#e0a040")  # читается по K-line — обновляется реже
UNSUPPORTED = ("#f0f0f0", "#cfcfcf")  # ЭБУ не отдаёт


class Tile(QFrame):
    def __init__(self, name: str, unit: str) -> None:
        super().__init__()
        self.setObjectName("tile")
        self._name = QLabel(name)
        self._name.setStyleSheet("color: #444;")
        self._name.setWordWrap(True)
        self._value = QLabel("—")
        self._value.setStyleSheet("font-size: 26px; font-weight: 600; color: #111;")
        self._value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._unit = QLabel(unit)
        self._unit.setStyleSheet("color: #666;")
        self._unit.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._range = QLabel("")
        self._range.setStyleSheet("color: #888; font-size: 11px;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(0)
        layout.addWidget(self._name)
        layout.addWidget(self._value)
        layout.addWidget(self._unit)
        layout.addWidget(self._range)
        self.set_state("", False, False)

    def set_values(self, value: str, low: str, high: str) -> None:
        if self._value.text() != value:
            self._value.setText(value)
        text = f"{low} … {high}"
        if self._range.text() != text:
            self._range.setText(text)

    def set_state(self, tooltip: str, slow: bool, unsupported: bool) -> None:
        bg, border = UNSUPPORTED if unsupported else SLOW if slow else NORMAL
        self.setStyleSheet(TILE_STYLE.format(bg=bg, border=border))
        self.setToolTip(tooltip)
        self._value.setStyleSheet(
            f"font-size: 26px; font-weight: 600; color: {'#999' if unsupported else '#111'};"
        )


class Dashboard(QScrollArea):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._inner = QWidget()
        self._grid = QGridLayout(self._inner)
        self._grid.setSpacing(8)
        self._grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(self._inner)
        self._tiles: dict[str, Tile] = {}
        self._order: list[str] = []
        self._columns = 0

    def set_tiles(self, tiles: list[tuple[str, str, str]]) -> None:
        """[(id, имя, единицы)] — прежние плитки удаляются."""
        for tile in self._tiles.values():
            tile.deleteLater()
        self._tiles = {pid: Tile(name, unit) for pid, name, unit in tiles}
        self._order = [pid for pid, _n, _u in tiles]
        self._columns = 0
        self._relayout()

    def tile(self, pid: str) -> Tile | None:
        return self._tiles.get(pid)

    def resizeEvent(self, event: QResizeEvent | None) -> None:
        super().resizeEvent(event)
        self._relayout()

    def _relayout(self) -> None:
        columns = max(1, self.viewport().width() // TILE_WIDTH) if self.viewport() else 1  # type: ignore[union-attr]
        if columns == self._columns and self._grid.count() == len(self._order):
            return
        self._columns = columns
        for pid in self._order:
            self._grid.removeWidget(self._tiles[pid])
        for i, pid in enumerate(self._order):
            self._grid.addWidget(self._tiles[pid], i // columns, i % columns)
        for c in range(columns):
            self._grid.setColumnStretch(c, 1)
