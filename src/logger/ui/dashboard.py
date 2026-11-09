"""Дашборд логгера: плитка на каждый выбранный параметр — имя, значение крупно, единицы,
мин/макс. Плитки фиксированной ширины раскладываются сеткой по ширине окна; число колонок
подбирается под ширину, а окно подгоняется под сетку (см. LoggerWindow._fit_window)."""

from __future__ import annotations

import math

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QResizeEvent
from PyQt6.QtWidgets import QFrame, QGridLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

TILE_WIDTH = 200  # ширина плитки: по ней — число колонок и подгонка окна
TILE_HEIGHT = 108  # оценка, если плиток ещё нет
TILE_GAP = 8
MAX_COLS = 4  # больше колонок — окно шире, чем нужно для чтения
TILE_STYLE = "QFrame#tile {{ background: {bg}; border: 1px solid {border}; border-radius: 6px; }}"
NORMAL = ("#ffffff", "#cfcfcf")
SLOW = ("#fff4e5", "#e0a040")  # читается по K-line — обновляется реже
UNSUPPORTED = ("#f0f0f0", "#cfcfcf")  # ЭБУ не отдаёт


class Tile(QFrame):
    def __init__(self, name: str, unit: str) -> None:
        super().__init__()
        self.setObjectName("tile")
        self.setFixedWidth(TILE_WIDTH)
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
        self._grid.setSpacing(TILE_GAP)
        self._grid.setContentsMargins(0, 0, 0, 0)
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

    def grid_size(self, count: int) -> tuple[int, int, int]:
        """Размер сетки под count плиток: (ширина, высота, колонки).

        Число колонок — как у почти квадратной сетки, но не больше MAX_COLS; высота —
        по самой высокой плитке (имена бывают в две строки)."""
        if count <= 0:
            return 2 * TILE_WIDTH + TILE_GAP, TILE_HEIGHT, 2
        columns = min(MAX_COLS, max(1, math.ceil(math.sqrt(count))))
        rows = math.ceil(count / columns)
        tile_height = max((tile.sizeHint().height() for tile in self._tiles.values()), default=TILE_HEIGHT)
        width = columns * TILE_WIDTH + (columns - 1) * TILE_GAP
        height = rows * tile_height + (rows - 1) * TILE_GAP
        return width, height, columns

    def resizeEvent(self, event: QResizeEvent | None) -> None:
        super().resizeEvent(event)
        self._relayout()

    def _relayout(self) -> None:
        viewport = self.viewport()
        width = viewport.width() if viewport is not None else 0
        columns = max(1, (width + TILE_GAP) // (TILE_WIDTH + TILE_GAP))
        if columns == self._columns and self._grid.count() == len(self._order):
            return
        self._columns = columns
        for pid in self._order:
            self._grid.removeWidget(self._tiles[pid])
        for i, pid in enumerate(self._order):
            self._grid.addWidget(self._tiles[pid], i // columns, i % columns)
        # Плитки фиксированной ширины: свободное место уходит в колонку-распорку справа,
        # иначе они растягиваются на всё окно (и оно кажется слишком широким)
        for c in range(self._grid.columnCount()):
            self._grid.setColumnStretch(c, 0)
        self._grid.setColumnStretch(columns, 1)
