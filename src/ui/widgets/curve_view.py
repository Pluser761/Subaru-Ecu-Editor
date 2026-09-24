from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from src.ui.style import BACKGROUND, NON_FINITE_RGBA, VALUE_COLORMAP, hint_icon

LINE_COLOR = (60, 60, 60)
LINE_WIDTH = 2
AXIS_COLOR = "#444"
GRID_ALPHA = 0.25
# Отступ от краёв, чтобы крайние точки не обрезались рамкой (доля диапазона)
VIEW_PADDING = 0.05
POINT_SIZE = 9
# Выделение — как в 3D: белая точка с тёмной обводкой поверх цвета значения
SELECTED_SIZE = 14
SELECTED_PEN = pg.mkPen((25, 25, 25), width=2)
SELECTED_BRUSH = pg.mkBrush(255, 255, 255)


class CurveView(QWidget):
    """График 2D-таблицы (вектора): значение по оси, точки с цветом по значению.

    По горизонтали — реальные значения оси, если она строго монотонна, иначе — индексы
    с подписями оси (статические оси: «1st», «Enable Above»). Ячейки с inf/nan рисуются
    серыми на уровне минимума и не соединяются линией.
    """

    # row, col, additive (Ctrl/Cmd+клик — добавить к выделению)
    point_clicked = pyqtSignal(int, int, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Ячейка таблицы для каждой точки графика
        self._cells: list[tuple[int, int]] = []
        # Точек на категориальной оси (подписи вместо чисел); 0 — ось числовая
        self._categories = 0

        self._plot = pg.PlotWidget(background=BACKGROUND)
        # Фокус по клику — чтобы клавиши шага работали и из графика
        self._plot.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        item = self._plot.getPlotItem()
        assert item is not None
        item.showGrid(x=True, y=True, alpha=GRID_ALPHA)
        item.setMenuEnabled(False)
        item.hideButtons()
        view_box = item.getViewBox()
        assert view_box is not None
        view_box.setDefaultPadding(VIEW_PADDING)
        for name in ("bottom", "left"):
            axis = item.getAxis(name)
            axis.setPen(AXIS_COLOR)
            axis.setTextPen(AXIS_COLOR)

        self._curve = pg.PlotDataItem(pen=pg.mkPen(LINE_COLOR, width=LINE_WIDTH), connect="finite")
        self._points = pg.ScatterPlotItem(size=POINT_SIZE, pen=pg.mkPen(LINE_COLOR, width=1))
        self._points.sigClicked.connect(self._on_points_clicked)
        self._selected = pg.ScatterPlotItem(size=SELECTED_SIZE, pen=SELECTED_PEN, brush=SELECTED_BRUSH)
        self._selected.setZValue(10)
        for graphics in (self._curve, self._points, self._selected):
            self._plot.addItem(graphics)

        reset_btn = QPushButton("Сбросить вид")
        reset_btn.clicked.connect(self.reset_view)
        hint = hint_icon("Клик — выбрать точку, Ctrl/⌘+клик — добавить\nКолесо — масштаб, перетаскивание — сдвиг")

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.addWidget(hint)
        toolbar.addStretch(1)
        toolbar.addWidget(reset_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addLayout(toolbar)
        layout.addWidget(self._plot, stretch=1)

    @property
    def plot_widget(self) -> QWidget:
        """Виджет графика — в фокусе при работе с ним (для перехвата клавиш)."""
        return self._plot

    def reset_view(self) -> None:
        self._plot.enableAutoRange()
        self._apply_category_range()

    def _apply_category_range(self) -> None:
        """Категориальная ось: у каждой точки слот шириной 1, чтобы крайние подписи
        делений помещались (pyqtgraph не рисует подпись, вылезающую за край оси)."""
        if self._categories:
            self._plot.setXRange(-0.5, self._categories - 0.5, padding=0)

    def clear(self) -> None:
        self._cells = []
        self._categories = 0
        self._curve.setData([], [])
        self._points.setData([])
        self._selected.setData([])

    def set_data(
        self,
        values: list[list[float]],
        x_axis: list[float],
        y_axis: list[float],
        selected: set[tuple[int, int]] | None = None,
        *,
        x_labels: list[str] | None = None,
        y_labels: list[str] | None = None,
        x_title: str = "",
        y_title: str = "",
        unit: str = "",
    ) -> None:
        """values[row][col] как в таблице карты; вектор — одна строка или один столбец."""
        z = np.asarray(values, dtype=float)
        if z.ndim != 2 or z.size < 2 or min(z.shape) != 1:
            self.clear()
            return
        rows, cols = z.shape
        if rows == 1:
            ys, axis, labels, title = z[0], x_axis, x_labels, x_title
            self._cells = [(0, c) for c in range(cols)]
        else:
            ys, axis, labels, title = z[:, 0], y_axis, y_labels, y_title
            self._cells = [(r, 0) for r in range(rows)]
        n = len(ys)

        xs, ticks = _positions(axis, labels, n)
        categories = n if ticks is not None else 0
        bottom = self._plot.getPlotItem().getAxis("bottom")
        bottom.setTicks(ticks)
        self._plot.setLabel("bottom", title)
        self._plot.setLabel("left", unit)

        finite = np.isfinite(ys)
        y_min = float(ys[finite].min()) if finite.any() else 0.0
        y_max = float(ys[finite].max()) if finite.any() else 0.0
        span = y_max - y_min
        norm = (ys - y_min) / span if span > 0 else np.full(n, 0.5)
        colors = VALUE_COLORMAP.map(np.where(finite, norm, 0.0), mode="byte")
        brushes = [
            pg.mkBrush(*c) if ok else pg.mkBrush(*(int(v * 255) for v in NON_FINITE_RGBA))
            for c, ok in zip(colors, finite, strict=True)
        ]
        plot_ys = np.where(finite, ys, y_min)

        self._curve.setData(xs, np.where(finite, ys, np.nan))
        self._points.setData(x=xs, y=plot_ys, brush=brushes, data=list(range(n)))

        index = {cell: i for i, cell in enumerate(self._cells)}
        chosen = [index[cell] for cell in (selected or ()) if cell in index]
        self._selected.setData(x=xs[chosen], y=plot_ys[chosen], data=chosen)

        # Диапазон по X — только при смене вида оси, чтобы правки не сбрасывали масштаб
        if categories != self._categories:
            self._categories = categories
            self.reset_view()

    def _on_points_clicked(self, _item: object, points: list, event: object) -> None:
        if not points or not self._cells:
            return
        i = int(points[0].data())
        mods = event.modifiers() if hasattr(event, "modifiers") else Qt.KeyboardModifier.NoModifier
        additive = bool(mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier))
        row, col = self._cells[i]
        self.point_clicked.emit(row, col, additive)


def _positions(
    axis: list[float], labels: list[str] | None, n: int
) -> tuple[np.ndarray, list[list[tuple[float, str]]] | None]:
    """Координаты точек по горизонтали и подписи делений (None — числовая ось, деления авто)."""
    if labels is None and len(axis) == n:
        a = np.asarray(axis, dtype=float)
        diffs = np.diff(a)
        if np.all(np.isfinite(a)) and (np.all(diffs > 0) or np.all(diffs < 0)):
            return a, None
    xs = np.arange(n, dtype=float)
    if labels is not None:
        names = [labels[i] if i < len(labels) else str(i) for i in range(n)]
    else:
        names = [f"{axis[i]:g}" if i < len(axis) else str(i) for i in range(n)]
    return xs, [[(float(i), name) for i, name in enumerate(names)]]
