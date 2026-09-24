from __future__ import annotations

from enum import Enum

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QKeyEvent
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QTableWidgetSelectionRange,
    QVBoxLayout,
    QWidget,
)

from src.core.models import CalibrationTable, Checkpoint
from src.ui.key_bindings import Action, KeyBindings, is_fine_modifier
from src.ui.widgets.constant_editor import ConstantEditor
from src.ui.widgets.step_controls import StepControls
from src.ui.widgets.table_editor import TableEditor
from src.ui.widgets.vertical_label import VerticalLabel

# Нет ограничения высоты виджета (QWIDGETSIZE_MAX)
_NO_MAX_HEIGHT = 16777215

# Размер окна по умолчанию: по содержимому, но не больше этого
MAX_WINDOW_WIDTH = 1400
MAX_WINDOW_HEIGHT = 900
CONSTANT_WINDOW_SIZE = (460, 300)


class _Mode(Enum):
    """Как показана карта."""

    TABLE = "table"  # как в ROM: строки — ось Y, столбцы — ось X
    ROW = "row"  # 2D-вектор, хранящийся столбцом (N×1), показан одной строкой (1×N)
    CONSTANT = "constant"  # 1×1 — поле ввода вместо таблицы


def dimension_label(rows: int, cols: int) -> str:
    """3D / 2D / 1D — по форме таблицы, как она показывается (а не по table_type)."""
    if rows >= 2 and cols >= 2:
        return "3D"
    if rows * cols >= 2:
        return "2D"
    return "1D"


def describe_shape(rows: int, cols: int, unit: str) -> str:
    """«3D · 18 × 13 · psi», «2D · 16 · RPM», «1D · RPM»."""
    dim = dimension_label(rows, cols)
    parts = [dim]
    if dim == "3D":
        parts.append(f"{rows} × {cols}")
    elif dim == "2D":
        parts.append(str(max(rows, cols)))
    if unit:
        parts.append(unit)
    return " · ".join(parts)


class MapWindow(QWidget):
    """Отдельное окно одной карты: таблица (или поле константы), шаги, checkpoint.

    Сигналы правок и методы update_* / checkpoint — в координатах карты в ROM
    (row — ось Y, col — ось X), даже если карта показана транспонированной (_Mode.ROW)
    или полем ввода (_Mode.CONSTANT). graph_data() отдаёт то, что на экране.
    """

    # map_id + правка в координатах ROM
    cell_value_changed = pyqtSignal(str, int, int, float)  # map_id, row, col, value
    x_axis_changed = pyqtSignal(str, int, float)  # map_id, col, value
    y_axis_changed = pyqtSignal(str, int, float)  # map_id, row, value
    # Окно стало активным — график переключается на эту карту
    activated = pyqtSignal(str)
    # Окно закрыто; checkpoint карты — чтобы он пережил закрытие окна
    closed = pyqtSignal(str, object)  # map_id, Checkpoint
    # Значения/оси/выделение на экране изменились — перерисовать график
    display_changed = pyqtSignal(str)
    operation_info = pyqtSignal(str)
    operation_error = pyqtSignal(str)
    matrix_copied = pyqtSignal(int, int)
    matrix_pasted = pyqtSignal(int, int)
    # Нажали Set checkpoint — изменения карты сброшены
    checkpoint_set = pyqtSignal(str)

    def __init__(
        self,
        table: CalibrationTable,
        checkpoint: Checkpoint | None,
        key_bindings: KeyBindings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._key_bindings = key_bindings
        self._map_id = table.name

        self._title = QLabel()
        self._title.setStyleSheet("font-size: 14px; font-weight: 600;")
        self._meta = QLabel()
        self._meta.setStyleSheet("color: #666;")
        # Ось, которую используют и другие таблицы: её правка меняет ось и у них
        self._shared_axes = QLabel()
        self._shared_axes.setStyleSheet("color: #b36b00;")
        self._shared_axes.setVisible(False)

        self._table = TableEditor()
        self._constant = ConstantEditor()
        self._steps = StepControls()
        self._table_name = ""
        self._table_unit = ""
        self._mode = _Mode.TABLE
        # Шаги осей на экране (после транспонирования) и шаги значений, пока выделена ось
        self._axis_steps: dict[Qt.Orientation, tuple[float | None, float | None] | None] = {}
        self._value_steps: tuple[float, float] | None = None

        self._table.axis_selection_changed.connect(self._on_axis_selection_changed)
        self._table.cell_value_changed.connect(self._on_table_cell_changed)
        self._table.x_axis_changed.connect(lambda i, v: self._on_table_axis_changed(Qt.Orientation.Horizontal, i, v))
        self._table.y_axis_changed.connect(lambda i, v: self._on_table_axis_changed(Qt.Orientation.Vertical, i, v))
        self._constant.value_changed.connect(lambda v: self.cell_value_changed.emit(self._map_id, 0, 0, v))
        self._table.operation_info.connect(self.operation_info.emit)
        self._table.operation_error.connect(self.operation_error.emit)
        self._table.matrix_copied.connect(self.matrix_copied.emit)
        self._table.matrix_pasted.connect(self.matrix_pasted.emit)

        # Клавиши шага работают, когда в фокусе таблица, её заголовки
        # (после клика по заголовку фокус у него) или поле константы
        for widget in (
            self._table,
            self._table.horizontalHeader(),
            self._table.verticalHeader(),
            self._constant.input,
        ):
            if widget is not None:
                widget.installEventFilter(self)

        # Заголовки осей: X — над столбцами, Y — вертикально слева от строк
        self._x_title = QLabel()
        self._x_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._y_title = VerticalLabel()
        for title in (self._x_title, self._y_title):
            title.setStyleSheet("color: #444; font-weight: 600;")
        self._axis_titles = ("", "")

        # Правки/вставки шлют сигнал на каждую ячейку — схлопываем в одну перерисовку графика
        self._display_timer = QTimer(self)
        self._display_timer.setSingleShot(True)
        self._display_timer.setInterval(0)
        self._display_timer.timeout.connect(lambda: self.display_changed.emit(self._map_id))
        self._table.cell_value_changed.connect(self._schedule_display)
        self._table.x_axis_changed.connect(self._schedule_display)
        self._table.y_axis_changed.connect(self._schedule_display)
        self._table.itemSelectionChanged.connect(self._schedule_display)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.addWidget(self._title)
        layout.addWidget(self._meta)
        layout.addWidget(self._shared_axes)
        checkpoint_btn = QPushButton("Set checkpoint")
        checkpoint_btn.setToolTip(
            "Запомнить текущие значения как точку отсчёта. Изменённые после этого ячейки "
            "обводятся: красным — больше, синим — меньше"
        )
        checkpoint_btn.clicked.connect(self._set_checkpoint)
        edit_row = QHBoxLayout()
        edit_row.addWidget(self._steps, stretch=1)
        edit_row.addWidget(checkpoint_btn)
        layout.addLayout(edit_row)

        self._grid_frame = QFrame()
        self._grid_frame.setFrameShape(QFrame.Shape.StyledPanel)
        grid_frame_layout = QVBoxLayout(self._grid_frame)
        grid_frame_layout.setContentsMargins(0, 0, 0, 0)
        grid_frame_layout.addWidget(self._table)

        table_frame = QWidget()
        self._table_layout = QGridLayout(table_frame)
        self._table_layout.setContentsMargins(0, 0, 0, 0)
        self._table_layout.setSpacing(2)
        self._table_layout.addWidget(self._x_title, 0, 1)
        self._table_layout.addWidget(self._y_title, 1, 0)
        self._table_layout.addWidget(self._grid_frame, 1, 1)
        self._table_layout.setColumnStretch(1, 1)

        # Таблица или поле ввода константы
        self._pages = QStackedWidget()
        self._pages.addWidget(table_frame)
        self._pages.addWidget(self._constant)
        layout.addWidget(self._pages, stretch=1)

        self.show_table(table, checkpoint)
        self.resize(*self._default_size())

    @property
    def map_id(self) -> str:
        return self._map_id

    @property
    def is_constant(self) -> bool:
        return self._mode is _Mode.CONSTANT

    # --- Данные карты (координаты ROM) ---

    def show_table(self, table: CalibrationTable, checkpoint: Checkpoint | None = None) -> None:
        """Показать карту (в т.ч. перечитанную после переноса) с сохранённым checkpoint."""
        self._map_id = table.name
        self.setWindowTitle(table.name.strip())
        self._title.setText(table.name)
        self._table_name = table.name
        self._table_unit = table.unit
        self._meta.setText(describe_shape(table.rows, table.cols, table.unit))
        self._set_shared_axes(table.x_shared_with, table.y_shared_with)
        self._value_steps = None
        self._steps.set_axis_mode(False)
        self._steps.set_increments(table.fine_increment, table.coarse_increment)

        if table.rows == 1 and table.cols == 1:
            self._mode = _Mode.CONSTANT
            self._table.set_table(None)
            self._set_axis_titles("", "")
            cp = checkpoint.values[0][0] if checkpoint and checkpoint.values and checkpoint.values[0] else None
            self._constant.set_constant(table.values[0][0], table.unit or "Значение", cp)
            self._pages.setCurrentWidget(self._constant)
        else:
            self._mode = _Mode.ROW if table.cols == 1 else _Mode.TABLE
            shown = _transpose_table(table) if self._mode is _Mode.ROW else table
            self._axis_steps = {
                Qt.Orientation.Horizontal: shown.x_increments,
                Qt.Orientation.Vertical: shown.y_increments,
            }
            self._table.set_table(shown)
            self._set_axis_titles(shown.x_title, shown.y_title)
            if checkpoint is not None:
                self._table.restore_checkpoint(
                    _transpose_checkpoint(checkpoint) if self._mode is _Mode.ROW else checkpoint
                )
            self._pages.setCurrentIndex(0)
        self._set_compact_table(self._is_single_row())
        self._schedule_display()

    def update_cell(self, row: int, col: int, value: float) -> None:
        """Показать значение, реально сохранённое в ROM (без повторной записи)."""
        if self._mode is _Mode.CONSTANT:
            self._constant.set_display(value)
            return
        if self._mode is _Mode.ROW:
            row, col = col, row
        self._table.set_cell_display(row, col, value)
        self._schedule_display()

    def update_axis(self, orientation: Qt.Orientation, index: int, value: float) -> None:
        """Показать значение оси карты (Horizontal — X, Vertical — Y в ROM)."""
        if self._mode is _Mode.CONSTANT:
            return
        self._table.set_axis_display(self._display_orientation(orientation), index, value)
        self._schedule_display()

    def checkpoint(self) -> Checkpoint:
        """Checkpoint карты (значения и оси) в координатах ROM."""
        if self._mode is _Mode.CONSTANT:
            return Checkpoint(values=[[self._constant.checkpoint_value()]])
        checkpoint = self._table.checkpoint()
        return _transpose_checkpoint(checkpoint) if self._mode is _Mode.ROW else checkpoint

    # --- График ---

    def graph_data(self) -> dict[str, object] | None:
        """Аргументы GraphWindow.set_data — то, что на экране; None — графика нет."""
        if self._mode is _Mode.CONSTANT or self._table.rowCount() == 0:
            return None
        return {
            "name": self._table_name,
            "values": self._table.get_values(),
            "x_axis": self._table.get_x_axis(),
            "y_axis": self._table.get_y_axis(),
            "selected": self._table.selected_cells(),
            "x_labels": self._table.axis_labels(Qt.Orientation.Horizontal),
            "y_labels": self._table.axis_labels(Qt.Orientation.Vertical),
            "x_title": self._axis_titles[0],
            "y_title": self._axis_titles[1],
            "unit": self._table_unit,
        }

    def select_cell(self, row: int, col: int, additive: bool) -> None:
        """Выделить ячейку (экранные координаты) — клик по точке графика."""
        if self._mode is _Mode.CONSTANT:
            return
        if additive:
            item = self._table.item(row, col)
            selected = item is not None and item.isSelected()
            self._table.setRangeSelected(QTableWidgetSelectionRange(row, col, row, col), not selected)
        else:
            self._table.setCurrentCell(row, col)
        self._table.scrollToItem(self._table.item(row, col))

    # --- Клавиши шага ---

    def handle_step_key(self, event: QKeyEvent) -> bool:
        """Клавиши «увеличить»/«уменьшить»: шаг выделенных ячеек, точек оси или константы.
        True — клавиша обработана (в т.ч. если менять нечего)."""
        action = self._key_bindings.action_for(event)
        if action is None:
            return False
        step = self._steps.fine_step if is_fine_modifier(event) else self._steps.coarse_step
        delta = step if action is Action.INCREASE else -step
        if self._mode is _Mode.CONSTANT:
            if self._constant.adjust(delta):
                self.operation_info.emit(f"{delta:+.2f}")
            return True
        if self._table.rowCount() == 0:
            return False
        if self._table.has_axis_selection():
            count = self._table.adjust_axis_selection(delta)
            if count:
                self.operation_info.emit(f"{delta:+.2f} → точек оси: {count}")
            return True
        count = self._table.adjust_selected(delta)
        if count:
            self.operation_info.emit(f"{delta:+.2f} → ячеек: {count}")
        return True

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:
        if obj is self._table and event is not None and event.type() == QEvent.Type.Resize:
            if self._is_single_row():
                # Горизонтальная прокрутка могла появиться или пропасть — подгоняем высоту
                QTimer.singleShot(0, lambda: self._set_compact_table(self._is_single_row()))
            return super().eventFilter(obj, event)
        if isinstance(event, QKeyEvent) and event.type() == QEvent.Type.KeyPress:
            if self.handle_step_key(event):
                return True
        return super().eventFilter(obj, event)

    # --- Окно ---

    def changeEvent(self, event: QEvent | None) -> None:
        super().changeEvent(event)
        if event is not None and event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self.activated.emit(self._map_id)

    def closeEvent(self, event: QCloseEvent | None) -> None:
        self.closed.emit(self._map_id, self.checkpoint())
        super().closeEvent(event)

    def _default_size(self) -> tuple[int, int]:
        if self._mode is _Mode.CONSTANT:
            return CONSTANT_WINDOW_SIZE
        header = self._table.horizontalHeader()
        v_header = self._table.verticalHeader()
        column = header.minimumSectionSize() if header is not None else 60
        width = (
            (v_header.sizeHint().width() if v_header is not None else 0)
            + self._table.columnCount() * (column + 8)
            + 80
        )
        if self._is_single_row():
            height = 240
        else:
            row_height = self._table.rowHeight(0) if self._table.rowCount() else 24
            height = self._table.rowCount() * row_height + 175
        return min(max(width, 520), MAX_WINDOW_WIDTH), min(height, MAX_WINDOW_HEIGHT)

    # --- Внутреннее ---

    def _schedule_display(self, *_args: object) -> None:
        self._display_timer.start()

    def _display_orientation(self, orientation: Qt.Orientation) -> Qt.Orientation:
        """Ось карты → заголовок таблицы на экране (в _Mode.ROW оси меняются местами)."""
        if self._mode is not _Mode.ROW:
            return orientation
        if orientation == Qt.Orientation.Horizontal:
            return Qt.Orientation.Vertical
        return Qt.Orientation.Horizontal

    def _on_axis_selection_changed(self) -> None:
        """Выделены точки оси — поля шага показывают шаги этой оси; снято — шаги значений."""
        orientation = self._table.axis_selection_orientation()
        if orientation is None:
            if self._value_steps is not None:
                self._steps.set_increments(*self._value_steps)
                self._value_steps = None
            self._steps.set_axis_mode(False)
            return
        if self._value_steps is None:
            # Запоминаем как есть, с ручными правками пользователя
            self._value_steps = (self._steps.fine_step, self._steps.coarse_step)
        fine, coarse = self._axis_steps.get(orientation) or (None, None)
        self._steps.set_increments(fine, coarse)
        self._steps.set_axis_mode(True)

    def _on_table_cell_changed(self, row: int, col: int, value: float) -> None:
        if self._mode is _Mode.ROW:
            row, col = col, row
        self.cell_value_changed.emit(self._map_id, row, col, value)

    def _on_table_axis_changed(self, shown: Qt.Orientation, index: int, value: float) -> None:
        # Та же перестановка осей работает в обе стороны
        if self._display_orientation(shown) == Qt.Orientation.Horizontal:
            self.x_axis_changed.emit(self._map_id, index, value)
        else:
            self.y_axis_changed.emit(self._map_id, index, value)

    def _is_single_row(self) -> bool:
        return self._mode is not _Mode.CONSTANT and self._table.rowCount() == 1

    def _set_compact_table(self, compact: bool) -> None:
        """Одна строка на экране: рамка таблицы по высоте содержимого, ниже — пусто."""
        self._table_layout.setRowStretch(1, 0 if compact else 1)
        self._table_layout.setRowStretch(2, 1 if compact else 0)
        if not compact:
            self._grid_frame.setMaximumHeight(_NO_MAX_HEIGHT)
            return
        header = self._table.horizontalHeader()
        scroll = self._table.horizontalScrollBar()
        height = (
            (header.sizeHint().height() if header is not None else 0)
            + sum(self._table.rowHeight(r) for r in range(self._table.rowCount()))
            + 2 * self._table.frameWidth()
            + 2 * self._grid_frame.frameWidth()
            + (scroll.sizeHint().height() if scroll is not None and scroll.isVisible() else 0)
        )
        self._grid_frame.setMaximumHeight(height)

    def _set_axis_titles(self, x_title: str, y_title: str) -> None:
        self._x_title.setText(x_title)
        self._x_title.setVisible(bool(x_title))
        self._y_title.setText(y_title)
        self._y_title.setVisible(bool(y_title))
        self._axis_titles = (x_title, y_title)

    def _set_shared_axes(self, x_shared: list[str], y_shared: list[str]) -> None:
        parts: list[str] = []
        tips: list[str] = []
        for axis, names in (("X", x_shared), ("Y", y_shared)):
            if names:
                parts.append(f"ось {axis} — ещё {len(names)} {_tables_word(len(names))}")
                tips.append(f"Ось {axis} общая с:\n" + "\n".join(f"  • {n}" for n in names))
        if parts:
            self._shared_axes.setText("⚠ Общая " + "; ".join(parts))
            self._shared_axes.setToolTip(
                "\n\n".join(tips)
                + "\n\nПравка общей оси меняет её и в этих таблицах; их значения не пересчитываются."
            )
        else:
            self._shared_axes.setText("")
            self._shared_axes.setToolTip("")
        self._shared_axes.setVisible(bool(parts))

    def _set_checkpoint(self) -> None:
        if self._mode is _Mode.CONSTANT:
            self._constant.set_checkpoint()
        elif self._table.rowCount() > 0:
            self._table.set_checkpoint()
        else:
            return
        self.checkpoint_set.emit(self._map_id)
        self.operation_info.emit("Checkpoint установлен: подсветка изменений сброшена")


def _tables_word(n: int) -> str:
    """Согласование: 1 таблица, 2 таблицы, 5 таблиц."""
    if n % 10 == 1 and n % 100 != 11:
        return "таблица"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "таблицы"
    return "таблиц"


def _transpose(values: list[list[float]]) -> list[list[float]]:
    return [list(row) for row in zip(*values, strict=True)] if values else []


def _transpose_checkpoint(checkpoint: Checkpoint) -> Checkpoint:
    """Перестановка осей работает в обе стороны: ROM ↔ экран."""
    return Checkpoint(
        values=_transpose(checkpoint.values), x_axis=checkpoint.y_axis, y_axis=checkpoint.x_axis
    )


def _transpose_table(table: CalibrationTable) -> CalibrationTable:
    """Та же карта с переставленными осями: N×1 → 1×N."""
    return table.model_copy(
        update={
            "values": _transpose(table.values),
            "x_axis": table.y_axis,
            "y_axis": table.x_axis,
            "x_labels": table.y_labels,
            "y_labels": table.x_labels,
            "x_title": table.y_title,
            "y_title": table.x_title,
            "x_shared_with": table.y_shared_with,
            "y_shared_with": table.x_shared_with,
            "x_increments": table.y_increments,
            "y_increments": table.x_increments,
        }
    )
