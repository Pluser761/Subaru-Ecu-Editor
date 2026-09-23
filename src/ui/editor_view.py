from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QTreeWidget,
    QTreeWidgetItem,
    QLabel,
    QSplitter,
    QFrame,
    QGridLayout,
    QPushButton,
    QTableWidgetSelectionRange,
)

from src.core.models import CalibrationTable, MapInfo
from src.ui.key_bindings import Action, KeyBindings, is_fine_modifier
from src.ui.widgets.step_controls import StepControls
from src.ui.widgets.surface_view import SurfaceView
from src.ui.widgets.table_editor import TableEditor
from src.ui.widgets.vertical_label import VerticalLabel


class EditorView(QWidget):
    """Раздел Editor: дерево карт + таблица + 3D-вид + краткая информация."""

    map_selected = pyqtSignal(str)

    def __init__(self, key_bindings: KeyBindings | None = None, parent=None) -> None:
        super().__init__(parent)
        self._key_bindings = key_bindings or KeyBindings(self)

        self._map_tree = QTreeWidget()
        self._map_tree.setHeaderLabel("Maps")
        self._map_tree.setMinimumWidth(200)
        self._map_tree.itemClicked.connect(self._on_tree_clicked)
        # →/← на карте: открыть её / вернуться к категории и свернуть
        self._map_tree.installEventFilter(self)

        self._title = QLabel("Select a map")
        self._title.setStyleSheet("font-size: 14px; font-weight: 600;")

        self._meta = QLabel("")
        self._meta.setStyleSheet("color: #666;")

        # Ось, которую используют и другие таблицы: её правка меняет ось и у них
        self._shared_axes = QLabel("")
        self._shared_axes.setStyleSheet("color: #b36b00;")
        self._shared_axes.setVisible(False)

        self._table = TableEditor()
        self._surface = SurfaceView()
        self._steps = StepControls()

        # Клавиши шага работают, когда в фокусе таблица, её заголовки
        # (после клика по заголовку фокус у него) или 3D-вид
        for widget in (
            self._table,
            self._table.horizontalHeader(),
            self._table.verticalHeader(),
            self._surface.gl_widget,
        ):
            if widget is not None:
                widget.installEventFilter(self)

        # Заголовки осей: X — над столбцами, Y — вертикально слева от строк
        self._x_title = QLabel()
        self._x_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._y_title = VerticalLabel()
        for title in (self._x_title, self._y_title):
            title.setStyleSheet("color: #444; font-weight: 600;")
        self._surface_titles = ("", "")

        # Правки/вставки шлют сигнал на каждую ячейку — схлопываем в одну перерисовку
        self._surface_timer = QTimer(self)
        self._surface_timer.setSingleShot(True)
        self._surface_timer.setInterval(0)
        self._surface_timer.timeout.connect(self._refresh_surface)
        self._table.cell_value_changed.connect(self._schedule_surface)
        self._table.x_axis_changed.connect(self._schedule_surface)
        self._table.y_axis_changed.connect(self._schedule_surface)
        self._table.itemSelectionChanged.connect(self._schedule_surface)
        self._surface.point_clicked.connect(self._on_surface_point_clicked)

        right = QVBoxLayout()
        right.setContentsMargins(8, 0, 0, 0)
        right.addWidget(self._title)
        right.addWidget(self._meta)
        right.addWidget(self._shared_axes)
        checkpoint_btn = QPushButton("Set checkpoint")
        checkpoint_btn.setToolTip(
            "Запомнить текущие значения как точку отсчёта. Изменённые после этого ячейки "
            "обводятся: красным — больше, синим — меньше"
        )
        checkpoint_btn.clicked.connect(self._set_checkpoint)
        edit_row = QHBoxLayout()
        edit_row.addWidget(self._steps, stretch=1)
        edit_row.addWidget(checkpoint_btn)
        right.addLayout(edit_row)

        grid_frame = QFrame()
        grid_frame.setFrameShape(QFrame.Shape.StyledPanel)
        grid_frame_layout = QVBoxLayout(grid_frame)
        grid_frame_layout.setContentsMargins(0, 0, 0, 0)
        grid_frame_layout.addWidget(self._table)

        table_frame = QWidget()
        table_layout = QGridLayout(table_frame)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(2)
        table_layout.addWidget(self._x_title, 0, 1)
        table_layout.addWidget(self._y_title, 1, 0)
        table_layout.addWidget(grid_frame, 1, 1)
        table_layout.setRowStretch(1, 1)
        table_layout.setColumnStretch(1, 1)

        content_splitter = QSplitter(Qt.Orientation.Vertical)
        content_splitter.addWidget(table_frame)
        content_splitter.addWidget(self._surface)
        content_splitter.setStretchFactor(0, 1)
        content_splitter.setStretchFactor(1, 1)
        content_splitter.setSizes([300, 300])
        right.addWidget(content_splitter, stretch=1)

        right_widget = QWidget()
        right_widget.setLayout(right)

        splitter = QSplitter()
        splitter.addWidget(self._map_tree)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 580])

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(splitter)

    @property
    def table_editor(self) -> TableEditor:
        return self._table

    def set_maps(self, maps: list[MapInfo]) -> None:
        self._map_tree.clear()
        categories: dict[str, QTreeWidgetItem] = {}
        for info in maps:
            if info.category not in categories:
                cat_item = QTreeWidgetItem([info.category])
                cat_item.setExpanded(True)
                self._map_tree.addTopLevelItem(cat_item)
                categories[info.category] = cat_item
            leaf = QTreeWidgetItem([info.name])
            leaf.setData(0, int(Qt.ItemDataRole.UserRole), info.id)
            categories[info.category].addChild(leaf)

    def update_cell(self, row: int, col: int, value: float) -> None:
        """Показать значение, реально сохранённое в ROM (без повторной записи)."""
        self._table.set_cell_display(row, col, value)
        self._schedule_surface()

    def update_axis(self, orientation: Qt.Orientation, index: int, value: float) -> None:
        self._table.set_axis_display(orientation, index, value)
        self._schedule_surface()

    def checkpoint_values(self) -> list[list[float]]:
        return self._table.checkpoint_values()

    def show_table(self, table: CalibrationTable, checkpoint: list[list[float]] | None = None) -> None:
        self._title.setText(table.name)
        unit = f" · {table.unit}" if table.unit else ""
        self._meta.setText(f"{table.rows} × {table.cols}{unit}")
        self._table.set_table(table)
        self._set_axis_titles(table.x_title, table.y_title)
        self._set_shared_axes(table.x_shared_with, table.y_shared_with)
        if checkpoint is not None:
            self._table.set_checkpoint_values(checkpoint)
        self._steps.set_increments(table.fine_increment, table.coarse_increment)
        self._refresh_surface()

    def clear_table(self) -> None:
        self._title.setText("Select a map")
        self._meta.setText("")
        self._table.set_table(None)
        self._set_axis_titles("", "")
        self._set_shared_axes([], [])
        self._surface.clear()

    def _on_tree_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        map_id = item.data(0, int(Qt.ItemDataRole.UserRole))
        if map_id:
            self.map_selected.emit(str(map_id))

    def _set_axis_titles(self, x_title: str, y_title: str) -> None:
        self._x_title.setText(x_title)
        self._x_title.setVisible(bool(x_title))
        self._y_title.setText(y_title)
        self._y_title.setVisible(bool(y_title))
        self._surface_titles = (x_title, y_title)

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

    def _schedule_surface(self, *_args: object) -> None:
        self._surface_timer.start()

    def _refresh_surface(self) -> None:
        if self._table.rowCount() == 0 or self._table.columnCount() == 0:
            self._surface.clear()
            return
        self._surface.set_data(
            self._table.get_values(),
            self._table.get_x_axis(),
            self._table.get_y_axis(),
            self._table.selected_cells(),
            x_labels=self._table.axis_labels(Qt.Orientation.Horizontal),
            y_labels=self._table.axis_labels(Qt.Orientation.Vertical),
            x_title=self._surface_titles[0],
            y_title=self._surface_titles[1],
        )

    @property
    def step_controls(self) -> StepControls:
        return self._steps

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:
        if obj is self._map_tree:
            if isinstance(event, QKeyEvent) and event.type() == QEvent.Type.KeyPress:
                return self._handle_tree_key(event)
            return super().eventFilter(obj, event)
        if isinstance(event, QKeyEvent) and event.type() == QEvent.Type.KeyPress:
            action = self._key_bindings.action_for(event)
            if action is not None and self._table.rowCount() > 0:
                step = self._steps.fine_step if is_fine_modifier(event) else self._steps.coarse_step
                delta = step if action is Action.INCREASE else -step
                count = self._table.adjust_selected(delta)
                if count:
                    self._table.operation_info.emit(f"{delta:+.2f} → ячеек: {count}")
                return True
        return super().eventFilter(obj, event)

    def _handle_tree_key(self, event: QKeyEvent) -> bool:
        """→ на карте открывает её, ← на карте сворачивает её категорию.

        На категориях работает стандартное поведение QTreeWidget: → раскрывает, ← сворачивает.
        """
        if event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier:
            return False
        item = self._map_tree.currentItem()
        category = item.parent() if item is not None else None
        if item is None or category is None:
            return False
        if event.key() == Qt.Key.Key_Right:
            self._on_tree_clicked(item, 0)
            return True
        if event.key() == Qt.Key.Key_Left:
            self._map_tree.setCurrentItem(category)
            category.setExpanded(False)
            return True
        return False

    def _set_checkpoint(self) -> None:
        if self._table.rowCount() == 0:
            return
        self._table.set_checkpoint()
        self._table.operation_info.emit("Checkpoint установлен: подсветка изменений сброшена")

    def _on_surface_point_clicked(self, row: int, col: int, additive: bool) -> None:
        if additive:
            item = self._table.item(row, col)
            selected = item is not None and item.isSelected()
            self._table.setRangeSelected(QTableWidgetSelectionRange(row, col, row, col), not selected)
        else:
            self._table.setCurrentCell(row, col)
        self._table.scrollToItem(self._table.item(row, col))


def _tables_word(n: int) -> str:
    """Согласование: 1 таблица, 2 таблицы, 5 таблиц."""
    if n % 10 == 1 and n % 100 != 11:
        return "таблица"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "таблицы"
    return "таблиц"
