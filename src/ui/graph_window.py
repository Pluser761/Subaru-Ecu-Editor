from __future__ import annotations

from enum import Enum

from PyQt6.QtCore import QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QGuiApplication
from PyQt6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget

from src.ui.widgets.curve_view import CurveView
from src.ui.widgets.surface_view import SurfaceView


# Размер окна при первом показе (меньше окна карты), зазор до окна карты
DEFAULT_SIZE = (460, 440)
WINDOW_GAP = 8
TITLE_BAR_ALLOWANCE = 32


class GraphKind(Enum):
    NONE = "none"  # константа (1×1) — окно не нужно
    CURVE = "curve"  # 2D-таблица (вектор)
    MESH = "mesh"  # 3D-таблица


def graph_kind(rows: int, cols: int) -> GraphKind:
    """Вид графика по форме таблицы (а не по table_type: у 3D бывает одна строка)."""
    if rows >= 2 and cols >= 2:
        return GraphKind.MESH
    if rows * cols >= 2:
        return GraphKind.CURVE
    return GraphKind.NONE


class GraphWindow(QWidget):
    """Отдельное окно графика текущей карты: меш для 3D, кривая для 2D."""

    # Пользователь закрыл окно крестиком — редактор больше не показывает его сам
    closed_by_user = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("График")
        self.resize(*DEFAULT_SIZE)
        # Показ при выборе карты не должен уводить фокус из дерева или таблицы
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        # Уже показывали в этой сессии — дальше окно стоит там, куда его поставил пользователь
        self._placed = False

        # Имя показанной карты: при смене карты масштаб 2D-графика сбрасывается
        self._name = ""
        self.surface = SurfaceView()
        self.curve = CurveView()
        self._stack = QStackedWidget()
        self._stack.addWidget(self.surface)
        self._stack.addWidget(self.curve)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 8)
        layout.addWidget(self._stack)

    def set_data(
        self,
        name: str,
        values: list[list[float]],
        x_axis: list[float],
        y_axis: list[float],
        selected: set[tuple[int, int]],
        *,
        x_labels: list[str] | None,
        y_labels: list[str] | None,
        x_title: str,
        y_title: str,
        unit: str,
    ) -> GraphKind:
        """Нарисовать карту подходящим видом; вернуть вид (NONE — рисовать нечего)."""
        rows = len(values)
        cols = len(values[0]) if values else 0
        kind = graph_kind(rows, cols)
        self.setWindowTitle(f"{name.strip()} — график" if name.strip() else "График")
        if kind is GraphKind.MESH:
            self.curve.clear()
            self.surface.set_data(
                values, x_axis, y_axis, selected,
                x_labels=x_labels, y_labels=y_labels, x_title=x_title, y_title=y_title,
            )
            self._stack.setCurrentWidget(self.surface)
        elif kind is GraphKind.CURVE:
            self.surface.clear()
            self.curve.set_data(
                values, x_axis, y_axis, selected,
                x_labels=x_labels, y_labels=y_labels, x_title=x_title, y_title=y_title, unit=unit,
            )
            if name != self._name:
                self.curve.reset_view()
            self._stack.setCurrentWidget(self.curve)
        else:
            self.clear()
        self._name = name
        return kind

    def clear(self) -> None:
        self._name = ""
        self.setWindowTitle("График")
        self.surface.clear()
        self.curve.clear()

    def show_near(self, anchor: QWidget | None) -> None:
        """Показать. В первый раз за сессию — под окном карты anchor (не хватает места снизу —
        справа от него), целиком на экране; потом окно стоит там, куда его передвинули."""
        if not self._placed and anchor is not None:
            self.move(self._position_near(anchor))
            self._placed = True
        self.show()

    def _position_near(self, anchor: QWidget) -> QPoint:
        target = anchor.frameGeometry()
        screen = anchor.screen() or QGuiApplication.primaryScreen()
        # До первого показа frameGeometry без рамки ОС — берём с запасом на заголовок
        width, height = self.width(), self.height() + TITLE_BAR_ALLOWANCE
        pos = QPoint(target.left(), target.bottom() + WINDOW_GAP)
        if screen is None:
            return pos
        area = screen.availableGeometry()
        if pos.y() + height > area.bottom():
            pos = QPoint(target.right() + WINDOW_GAP, target.top())
        pos.setX(max(area.left(), min(pos.x(), area.right() - width)))
        pos.setY(max(area.top(), min(pos.y(), area.bottom() - height)))
        return pos

    def closeEvent(self, a0: QCloseEvent | None) -> None:
        self.closed_by_user.emit()
        super().closeEvent(a0)
