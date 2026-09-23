from __future__ import annotations

import math

import numpy as np
import pyqtgraph as pg
import pyqtgraph.opengl as gl
from OpenGL import GL
from PyQt6.QtCore import QEvent, QObject, QSize
from PyQt6.QtGui import QResizeEvent
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

# Размер сцены: оси X/Y нормируются в [0, SIZE_XY], значения — в [0, SIZE_Z]
SIZE_XY = 10.0
SIZE_Z = 5.0
# Радиус сферы, которая должна целиком влезать в кадр (сцена + подписи)
SCENE_RADIUS = 7.5

BACKGROUND = "#f7f7f5"
MESH_LINE_RGBA = (0.15, 0.15, 0.15, 0.75)
MESH_LINE_WIDTH = 2.0
FLOOR_GRID_RGBA = (0, 0, 0, 90)
FLOOR_GRID_WIDTH = 2.0
LABEL_RGBA = (50, 50, 50, 255)
SELECTED_RGBA = (1.0, 0.45, 0.0, 1.0)
SELECTED_MARKER_SIZE = 14
# Ячейки с inf/nan (деление на ноль в scaling) — серые и лежат на уровне минимума
NON_FINITE_RGBA = (0.6, 0.6, 0.6, 1.0)

# Классическая «тюнинговая» шкала: синий (мин) → красный (макс)
_CMAP = pg.ColorMap(
    pos=[0.0, 0.25, 0.5, 0.75, 1.0],
    color=[
        (40, 80, 220),
        (40, 190, 220),
        (60, 200, 80),
        (240, 210, 40),
        (220, 50, 40),
    ],
)

# Подсветка выделения рисуется поверх меша, без теста глубины
_OVERLAY_GL_OPTIONS = {
    GL.GL_DEPTH_TEST: False,
    GL.GL_BLEND: True,
    GL.GL_CULL_FACE: False,
    "glBlendFunc": (GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA),
}


def normalize_axis(axis: list[float], count: int) -> np.ndarray:
    """Координаты [0, 1] по реальным значениям оси; иначе — равномерно по индексу."""
    if count < 2:
        return np.zeros(count)
    if len(axis) == count:
        a = np.asarray(axis, dtype=float)
        diffs = np.diff(a)
        if np.all(np.isfinite(a)) and (np.all(diffs > 0) or np.all(diffs < 0)):
            return (a - a[0]) / (a[-1] - a[0])
    return np.linspace(0.0, 1.0, count)


def _axis_caption(name: str, values: list[float], labels: list[str] | None) -> str:
    if labels:
        first, last = labels[0], labels[-1]
    elif values:
        first, last = f"{values[0]:.2f}", f"{values[-1]:.2f}"
    else:
        return ""
    return f"{name} {first} … {last}"


class SurfaceView(QWidget):
    """3D-вид калибровочной таблицы: меш с цветом по значению и подсветкой выделения."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._view = gl.GLViewWidget()
        self._view.setBackgroundColor(BACKGROUND)
        self._view.installEventFilter(self)

        grid = gl.GLGridItem(color=FLOOR_GRID_RGBA)
        grid.setSize(SIZE_XY, SIZE_XY)
        grid.setSpacing(SIZE_XY / 10, SIZE_XY / 10)
        grid.translate(SIZE_XY / 2, SIZE_XY / 2, 0)
        grid.lineplot.setData(width=FLOOR_GRID_WIDTH)
        self._view.addItem(grid)

        self._surface = gl.GLSurfacePlotItem(
            showGrid=True,
            lineColor=MESH_LINE_RGBA,
            lineWidth=MESH_LINE_WIDTH,
            smooth=False,
            computeNormals=False,
        )
        self._line = gl.GLLinePlotItem(width=3, antialias=True)
        self._points = gl.GLScatterPlotItem(size=8, glOptions="opaque")
        # Выделение: маркер в узле + вертикальная «ножка» до пола
        self._selected_stems = gl.GLLinePlotItem(
            mode="lines", width=2, color=SELECTED_RGBA, glOptions=_OVERLAY_GL_OPTIONS
        )
        self._selected = gl.GLScatterPlotItem(
            size=SELECTED_MARKER_SIZE, color=SELECTED_RGBA, glOptions=_OVERLAY_GL_OPTIONS
        )
        self._selected_stems.setDepthValue(10)
        self._selected.setDepthValue(11)
        self._labels = {key: gl.GLTextItem(color=LABEL_RGBA) for key in ("x", "y", "z_min", "z_max")}
        for item in self._items():
            self._view.addItem(item)

        reset_btn = QPushButton("Сбросить вид")
        reset_btn.clicked.connect(self.reset_camera)
        self._hint = QLabel("ЛКМ — вращение, колесо — масштаб, СКМ — сдвиг")
        self._hint.setStyleSheet("color: #888;")

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.addWidget(self._hint)
        toolbar.addStretch(1)
        toolbar.addWidget(reset_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addLayout(toolbar)
        layout.addWidget(self._view, stretch=1)

        self.reset_camera()
        self.clear()

    def _items(self) -> tuple[gl.GLGraphicsItem, ...]:
        return (
            self._surface,
            self._line,
            self._points,
            self._selected_stems,
            self._selected,
            *self._labels.values(),
        )

    def _fit_distance(self, size: QSize | None = None) -> float:
        # fov в pyqtgraph задаёт горизонтальный угол, по вертикали видно меньше
        size = size or self._view.size()
        w, h = max(size.width(), 1), max(size.height(), 1)
        half_fov = math.radians(self._view.opts["fov"]) / 2
        return SCENE_RADIUS * max(1.0, w / h) / math.tan(half_fov)

    def reset_camera(self) -> None:
        self._view.opts["center"] = pg.Vector(SIZE_XY / 2, SIZE_XY / 2, SIZE_Z / 2)
        self._view.setCameraPosition(distance=self._fit_distance(), elevation=28, azimuth=-125)

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:
        if obj == self._view and isinstance(event, QResizeEvent):
            if event.oldSize().isValid():
                # Сохраняем пользовательский зум относительно «вписанного» расстояния
                ratio = self._fit_distance(event.size()) / self._fit_distance(event.oldSize())
                self._view.opts["distance"] *= ratio
            else:
                self._view.opts["distance"] = self._fit_distance(event.size())
        return super().eventFilter(obj, event)

    def clear(self) -> None:
        for item in self._items():
            item.setVisible(False)

    def set_data(
        self,
        values: list[list[float]],
        x_axis: list[float],
        y_axis: list[float],
        selected: set[tuple[int, int]] | None = None,
        *,
        x_labels: list[str] | None = None,
        y_labels: list[str] | None = None,
    ) -> None:
        """values[row][col]: row — ось Y, col — ось X (как в TableEditor).

        x_labels / y_labels — подписи статических осей для надписей сцены.
        """
        z = np.asarray(values, dtype=float)
        if z.ndim != 2 or z.size < 2:
            self.clear()
            return

        rows, cols = z.shape
        # Вектор (2D-таблица) кладём посередине сетки по «пустой» оси
        xs = normalize_axis(x_axis, cols) * SIZE_XY if cols > 1 else np.full(1, SIZE_XY / 2)
        ys = normalize_axis(y_axis, rows) * SIZE_XY if rows > 1 else np.full(1, SIZE_XY / 2)

        finite = np.isfinite(z)
        if finite.any():
            z_min, z_max = float(z[finite].min()), float(z[finite].max())
        else:
            z_min = z_max = 0.0
        span = z_max - z_min
        zn = (z - z_min) / span if span > 0 else np.full_like(z, 0.5)
        zn[~finite] = 0.0
        zs = zn * SIZE_Z

        colors = _CMAP.map(zn.ravel(), mode="float").reshape(rows, cols, 4)
        colors[~finite] = NON_FINITE_RGBA
        cells = [(r, c) for r, c in (selected or ()) if r < rows and c < cols]
        # Узлы выделенных ячеек подкрашиваем и на самом меше
        for r, c in cells:
            colors[r, c] = SELECTED_RGBA

        if rows >= 2 and cols >= 2:
            self._show_surface(xs, ys, zs, colors)
        else:
            self._show_line(xs, ys, zs, colors)

        self._show_selection(xs, ys, zs, cells)
        self._set_labels(x_axis, y_axis, x_labels, y_labels, z_min, z_max, rows, cols)

    def _show_surface(self, xs: np.ndarray, ys: np.ndarray, zs: np.ndarray, colors: np.ndarray) -> None:
        # GLSurfacePlotItem ждёт z[ix, iy] — транспонируем из [row, col];
        # цвета — плоский массив вершин в том же порядке (ix * ny + iy)
        self._surface.setData(
            x=xs,
            y=ys,
            z=zs.T,
            colors=colors.transpose(1, 0, 2).reshape(-1, 4),
        )
        self._surface.setVisible(True)
        self._line.setVisible(False)
        self._points.setVisible(False)

    def _show_line(self, xs: np.ndarray, ys: np.ndarray, zs: np.ndarray, colors: np.ndarray) -> None:
        # 2D-таблица (вектор): рисуем кривую вдоль той оси, по которой есть длина
        rows, cols = zs.shape
        pos = np.array([(xs[c], ys[r], zs[r, c]) for r in range(rows) for c in range(cols)])
        flat_colors = colors.reshape(-1, 4)
        self._line.setData(pos=pos, color=flat_colors)
        self._points.setData(pos=pos, color=flat_colors)
        self._line.setVisible(True)
        self._points.setVisible(True)
        self._surface.setVisible(False)

    def _show_selection(
        self, xs: np.ndarray, ys: np.ndarray, zs: np.ndarray, cells: list[tuple[int, int]]
    ) -> None:
        if not cells:
            self._selected.setVisible(False)
            self._selected_stems.setVisible(False)
            return
        pos = np.array([(xs[c], ys[r], zs[r, c]) for r, c in cells])
        floor = pos.copy()
        floor[:, 2] = 0.0
        stems = np.empty((len(pos) * 2, 3))
        stems[0::2] = floor
        stems[1::2] = pos
        self._selected.setData(pos=pos, color=SELECTED_RGBA)
        self._selected_stems.setData(pos=stems)
        self._selected.setVisible(True)
        self._selected_stems.setVisible(True)

    def _set_labels(
        self,
        x_axis: list[float],
        y_axis: list[float],
        x_labels: list[str] | None,
        y_labels: list[str] | None,
        z_min: float,
        z_max: float,
        rows: int,
        cols: int,
    ) -> None:
        off = SIZE_XY * 0.06
        x_caption = _axis_caption("X", x_axis, x_labels) if cols > 1 else ""
        y_caption = _axis_caption("Y", y_axis, y_labels) if rows > 1 else ""
        # Одна подпись на ось посередине ребра — в углах подписи X и Y слипаются
        placements: dict[str, tuple[tuple[float, float, float], str]] = {
            "x": ((SIZE_XY / 2, -off, 0), x_caption),
            "y": ((-off, SIZE_XY / 2, 0), y_caption),
            "z_min": ((SIZE_XY + off, SIZE_XY + off, 0), f"{z_min:.2f}"),
            "z_max": ((SIZE_XY + off, SIZE_XY + off, SIZE_Z), f"{z_max:.2f}"),
        }
        for key, item in self._labels.items():
            pos, text = placements[key]
            if not text:
                item.setVisible(False)
                continue
            item.setData(pos=pos, text=text)
            item.setVisible(True)
