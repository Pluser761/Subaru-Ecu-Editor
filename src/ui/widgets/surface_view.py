from __future__ import annotations

import math

import numpy as np
import pyqtgraph as pg
import pyqtgraph.opengl as gl
from OpenGL import GL
from PyQt6.QtCore import QEvent, QObject, QPointF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFontMetrics, QMouseEvent, QPainter, QResizeEvent, QTransform, QVector3D
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from src.i18n import tr
from src.ui.style import BACKGROUND, NON_FINITE_RGBA, VALUE_COLORMAP, hint_icon

# Размер сцены: оси X/Y нормируются в [0, SIZE_XY], значения — в [0, SIZE_Z]
SIZE_XY = 10.0
SIZE_Z = 5.0
# Радиус сферы, которая должна целиком влезать в кадр (сцена + подписи)
SCENE_RADIUS = 7.5

MESH_LINE_RGBA = (0.15, 0.15, 0.15, 0.75)
MESH_LINE_WIDTH = 2.0
FLOOR_GRID_RGBA = (0, 0, 0, 90)
FLOOR_GRID_WIDTH = 2.0
LABEL_RGBA = (50, 50, 50, 255)
# Выделение: белая точка с тёмной обводкой — читается на любом цвете шкалы
# и не перекрашивает сам меш
SELECTED_FILL_RGBA = (1.0, 1.0, 1.0, 1.0)
SELECTED_OUTLINE_RGBA = (0.1, 0.1, 0.1, 0.9)
SELECTED_FILL_SIZE = 7
SELECTED_OUTLINE_SIZE = 11
SELECTED_STEM_RGBA = (0.1, 0.1, 0.1, 0.45)
SELECTED_STEM_WIDTH = 1.5
# Клик считается выбором точки, если мышь сдвинулась меньше порога (иначе — вращение)
CLICK_MAX_MOVE_PX = 4
# Максимальное расстояние от курсора до узла на экране, чтобы узел выбрался
PICK_RADIUS_PX = 16
# Узлы в пределах этого зазора считаются перекрывающимися на экране
PICK_OVERLAP_PX = 3

# Размер подписей осей в сцене: единиц сцены на пиксель шрифта.
# При вписанной камере и окне ~700×500 шрифт 16 px выглядит примерно как 16 px на экране
AXIS_LABEL_WORLD_PER_PX = 0.03
# Зазор между названием оси и диапазоном (две строки в одной точке), в пикселях шрифта
AXIS_LABEL_LINE_GAP_PX = 2

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


def _axis_caption(name: str, title: str, values: list[float], labels: list[str] | None) -> tuple[str, str]:
    """(название, диапазон) — рисуются двумя строками: название над точкой, диапазон под ней."""
    if labels:
        first, last = labels[0], labels[-1]
    elif values:
        first, last = f"{values[0]:.2f}", f"{values[-1]:.2f}"
    else:
        return "", ""
    return title or name, f"{first} … {last}"


class AxisTextItem(gl.GLTextItem):
    """Подпись оси, лежащая в сцене вдоль направления оси.

    Базовая линия текста всегда совпадает с осью; плоскость текста только поворачивается
    вокруг этой оси, разворачиваясь к камере. Размер задан в единицах сцены, поэтому
    подпись ведёт себя как объект сцены (масштабируется колесом вместе с сеткой).
    Читаемость: направление чтения выбирается слева направо, текст не зеркалится.
    """

    def __init__(self, direction: tuple[float, float, float], **kwds: object) -> None:
        super().__init__(**kwds)
        self._direction = np.asarray(direction, dtype=float)
        self._direction /= np.linalg.norm(self._direction)

    def paint(self) -> None:
        if len(self.text) < 1:
            return
        view = self.view()
        if view is None:
            return
        self.setupGLState()

        anchor = np.asarray(self.pos, dtype=float)
        camera = view.cameraPosition()
        to_camera = np.array([camera.x(), camera.y(), camera.z()]) - anchor
        axis = self._direction
        # Нормаль плоскости текста — направление на камеру без составляющей вдоль оси:
        # плоскость может только вращаться вокруг оси
        normal = to_camera - to_camera.dot(axis) * axis
        norm = np.linalg.norm(normal)
        if norm < 1e-9:
            return  # смотрим точно вдоль оси — подпись видна ребром
        normal /= norm
        up = np.cross(normal, axis)

        project = self.compute_projection()

        def screen(point: np.ndarray) -> QPointF:
            return project.map(QVector3D(*(float(c) for c in point))).toPointF()

        origin = screen(anchor)
        scale = AXIS_LABEL_WORLD_PER_PX
        along = screen(anchor + axis * scale) - origin
        down = screen(anchor - up * scale) - origin
        # Читаем слева направо: если ось на экране смотрит влево — разворачиваем текст
        # вокруг нормали (и ось, и «верх» меняют знак)
        if along.x() < 0:
            along, down = -along, -down
        det = along.x() * down.y() - along.y() * down.x()
        if abs(det) < 1e-6:
            return
        # Без зеркалирования: «низ» текста должен быть по часовой стрелке от направления чтения
        if det < 0:
            down = -down

        painter = QPainter(view)
        painter.setPen(self.color)
        painter.setFont(self.font)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        # Шрифтовые пиксели → экран: локально аффинная проекция плоскости текста
        painter.setTransform(
            QTransform(along.x(), along.y(), down.x(), down.y(), origin.x(), origin.y())
        )
        painter.drawText(self._aligned_offset(), self.text)
        painter.end()

    def _aligned_offset(self) -> QPointF:
        """Позиция базовой линии текста относительно точки привязки в системе текста.

        По вертикали — по ascent/descent шрифта, а не по tightBoundingRect: две строки
        в одной точке (AlignBottom над ней, AlignTop под ней) не налезают друг на друга.
        """
        metrics = QFontMetrics(self.font)
        width = metrics.horizontalAdvance(self.text)
        dx = 0.0
        if self.alignment & Qt.AlignmentFlag.AlignRight:
            dx = width
        elif self.alignment & Qt.AlignmentFlag.AlignHCenter:
            dx = width / 2.0
        gap = AXIS_LABEL_LINE_GAP_PX / 2.0
        if self.alignment & Qt.AlignmentFlag.AlignTop:
            baseline = metrics.ascent() + gap
        elif self.alignment & Qt.AlignmentFlag.AlignBottom:
            baseline = -metrics.descent() - gap
        else:
            baseline = (metrics.ascent() - metrics.descent()) / 2.0
        return QPointF(-dx, baseline)


class SurfaceView(QWidget):
    """3D-вид 3D-таблицы: меш с цветом по значению и подсветкой выделения.

    2D-таблицы (векторы) рисует CurveView, этот вид для них пустой.
    """

    # row, col, additive (Ctrl/Cmd+клик — добавить к выделению)
    point_clicked = pyqtSignal(int, int, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Координаты узлов в сцене [row, col, xyz] — для выбора точки кликом
        self._node_pos: np.ndarray | None = None
        self._press_pos: QPointF | None = None

        self._view = gl.GLViewWidget()
        self._view.setBackgroundColor(BACKGROUND)
        self._view.installEventFilter(self)
        # Фокус по клику — чтобы клавиши шага работали и из 3D-вида
        self._view.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

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
        # Выделение: маркер в узле (обводка + заливка) и тонкая «ножка» до пола
        self._selected_stems = gl.GLLinePlotItem(
            mode="lines",
            width=SELECTED_STEM_WIDTH,
            color=SELECTED_STEM_RGBA,
            glOptions=_OVERLAY_GL_OPTIONS,
        )
        self._selected_outline = gl.GLScatterPlotItem(
            size=SELECTED_OUTLINE_SIZE, color=SELECTED_OUTLINE_RGBA, glOptions=_OVERLAY_GL_OPTIONS
        )
        self._selected = gl.GLScatterPlotItem(
            size=SELECTED_FILL_SIZE, color=SELECTED_FILL_RGBA, glOptions=_OVERLAY_GL_OPTIONS
        )
        self._selected_stems.setDepthValue(10)
        self._selected_outline.setDepthValue(11)
        self._selected.setDepthValue(12)
        # Подписи привязаны к точке центром (по умолчанию — левым нижним углом).
        # GLTextItem однострочный, поэтому у осей две подписи в одной точке:
        # название — над ней, диапазон — под ней. Подписи осей X/Y лежат вдоль своего
        # ребра сетки и вращаются только вокруг него (AxisTextItem), подписи Z — экранные.
        center = Qt.AlignmentFlag.AlignCenter
        above = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom
        below = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
        label_alignment = {
            "x_title": above,
            "x_range": below,
            "y_title": above,
            "y_range": below,
            "z_min": center,
            "z_max": center,
        }
        label_direction = {
            "x_title": (1.0, 0.0, 0.0),
            "x_range": (1.0, 0.0, 0.0),
            "y_title": (0.0, 1.0, 0.0),
            "y_range": (0.0, 1.0, 0.0),
        }
        self._labels: dict[str, gl.GLTextItem] = {
            key: (
                AxisTextItem(label_direction[key], color=LABEL_RGBA, alignment=alignment)
                if key in label_direction
                else gl.GLTextItem(color=LABEL_RGBA, alignment=alignment)
            )
            for key, alignment in label_alignment.items()
        }
        for item in self._items():
            self._view.addItem(item)

        reset_btn = QPushButton(tr("Сбросить вид"))
        reset_btn.clicked.connect(self.reset_camera)
        # Подсказка — значком с тултипом: длинная строка задавала бы минимальную ширину окна
        self._hint = hint_icon(
            tr("Клик — выбрать точку, Ctrl/⌘+клик — добавить\nПеретаскивание — вращение, колесо — масштаб, СКМ — сдвиг")
        )

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

    @property
    def gl_widget(self) -> QWidget:
        """Виджет OpenGL — в фокусе при работе с 3D (для перехвата клавиш)."""
        return self._view

    def _items(self) -> tuple[gl.GLGraphicsItem, ...]:
        return (
            self._surface,
            self._selected_stems,
            self._selected_outline,
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
        elif obj == self._view and isinstance(event, QMouseEvent):
            self._handle_mouse(event)
        # Не поглощаем событие — вращение/масштаб GLViewWidget работают как раньше
        return super().eventFilter(obj, event)

    def _handle_mouse(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if event.type() == QEvent.Type.MouseButtonPress:
            self._press_pos = event.position()
            return
        if event.type() != QEvent.Type.MouseButtonRelease or self._press_pos is None:
            return
        moved = (event.position() - self._press_pos).manhattanLength()
        self._press_pos = None
        if moved > CLICK_MAX_MOVE_PX:
            return
        hit = self.pick_node(event.position())
        if hit is None:
            return
        mods = event.modifiers()
        additive = bool(mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier))
        self.point_clicked.emit(hit[0], hit[1], additive)

    def pick_node(self, pos: QPointF) -> tuple[int, int] | None:
        """Ближайший к курсору узел (row, col) в пределах PICK_RADIUS_PX или None."""
        if self._node_pos is None:
            return None
        viewport = self._view.getViewport()
        _, _, w, h = viewport
        mvp = self._view.projectionMatrix(viewport, viewport) * self._view.viewMatrix()
        # QMatrix4x4.data() — column-major
        m = np.array(mvp.data(), dtype=float).reshape(4, 4).T

        rows, cols, _ = self._node_pos.shape
        pts = np.concatenate([self._node_pos.reshape(-1, 3), np.ones((rows * cols, 1))], axis=1)
        clip = pts @ m.T
        in_front = clip[:, 3] > 1e-9
        wdiv = np.where(in_front, clip[:, 3], 1.0)
        sx = (clip[:, 0] / wdiv + 1.0) * 0.5 * w
        sy = (1.0 - clip[:, 1] / wdiv) * 0.5 * h
        dist = np.hypot(sx - pos.x(), sy - pos.y())
        dist[~in_front] = np.inf

        nearest = float(dist.min())
        if nearest > PICK_RADIUS_PX:
            return None
        # Ближайший к курсору; если узлы на экране почти совпали (передняя и задняя
        # сторона меша) — берём тот, что ближе к камере
        candidates = np.flatnonzero(dist <= nearest + PICK_OVERLAP_PX)
        best = candidates[np.argmin(clip[candidates, 3])]
        return int(best // cols), int(best % cols)

    def clear(self) -> None:
        self._node_pos = None
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
        x_title: str = "",
        y_title: str = "",
    ) -> None:
        """values[row][col]: row — ось Y, col — ось X (как в таблице карты).

        x_labels / y_labels — подписи статических осей для надписей сцены.
        """
        z = np.asarray(values, dtype=float)
        if z.ndim != 2 or z.shape[0] < 2 or z.shape[1] < 2:
            self.clear()
            return

        rows, cols = z.shape
        xs = normalize_axis(x_axis, cols) * SIZE_XY
        ys = normalize_axis(y_axis, rows) * SIZE_XY

        finite = np.isfinite(z)
        if finite.any():
            z_min, z_max = float(z[finite].min()), float(z[finite].max())
        else:
            z_min = z_max = 0.0
        span = z_max - z_min
        zn = (z - z_min) / span if span > 0 else np.full_like(z, 0.5)
        zn[~finite] = 0.0
        zs = zn * SIZE_Z

        grid_x, grid_y = np.meshgrid(xs, ys)
        self._node_pos = np.stack([grid_x, grid_y, zs], axis=-1)

        colors = VALUE_COLORMAP.map(zn.ravel(), mode="float").reshape(rows, cols, 4)
        colors[~finite] = NON_FINITE_RGBA
        cells = [(r, c) for r, c in (selected or ()) if r < rows and c < cols]

        self._show_surface(xs, ys, zs, colors)
        self._show_selection(xs, ys, zs, cells)
        self._set_labels(x_axis, y_axis, x_labels, y_labels, x_title, y_title, z_min, z_max)

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

    def _show_selection(
        self, xs: np.ndarray, ys: np.ndarray, zs: np.ndarray, cells: list[tuple[int, int]]
    ) -> None:
        markers = (self._selected_stems, self._selected_outline, self._selected)
        if not cells:
            for item in markers:
                item.setVisible(False)
            return
        pos = np.array([(xs[c], ys[r], zs[r, c]) for r, c in cells])
        floor = pos.copy()
        floor[:, 2] = 0.0
        stems = np.empty((len(pos) * 2, 3))
        stems[0::2] = floor
        stems[1::2] = pos
        self._selected_outline.setData(pos=pos, color=SELECTED_OUTLINE_RGBA)
        self._selected.setData(pos=pos, color=SELECTED_FILL_RGBA)
        self._selected_stems.setData(pos=stems)
        for item in markers:
            item.setVisible(True)

    def _set_labels(
        self,
        x_axis: list[float],
        y_axis: list[float],
        x_labels: list[str] | None,
        y_labels: list[str] | None,
        x_title: str,
        y_title: str,
        z_min: float,
        z_max: float,
    ) -> None:
        off = SIZE_XY * 0.06
        # Подписи осей — посередине рёбер и дальше от сетки, чтобы X и Y не слипались
        axis_off = SIZE_XY * 0.14
        x_name, x_range = _axis_caption("X", x_title, x_axis, x_labels)
        y_name, y_range = _axis_caption("Y", y_title, y_axis, y_labels)
        x_pos = (SIZE_XY / 2, -axis_off, 0.0)
        y_pos = (-axis_off, SIZE_XY / 2, 0.0)
        placements: dict[str, tuple[tuple[float, float, float], str]] = {
            "x_title": (x_pos, x_name),
            "x_range": (x_pos, x_range),
            "y_title": (y_pos, y_name),
            "y_range": (y_pos, y_range),
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
