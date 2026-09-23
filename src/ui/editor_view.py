from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QTreeWidget,
    QTreeWidgetItem,
    QLabel,
    QSplitter,
    QFrame,
)

from src.core.models import CalibrationTable, MapInfo
from src.ui.widgets.surface_view import SurfaceView
from src.ui.widgets.table_editor import TableEditor


class EditorView(QWidget):
    """Раздел Editor: дерево карт + таблица + 3D-вид + краткая информация."""

    map_selected = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self._map_tree = QTreeWidget()
        self._map_tree.setHeaderLabel("Maps")
        self._map_tree.setMinimumWidth(200)
        self._map_tree.itemClicked.connect(self._on_tree_clicked)

        self._title = QLabel("Select a map")
        self._title.setStyleSheet("font-size: 14px; font-weight: 600;")

        self._meta = QLabel("")
        self._meta.setStyleSheet("color: #666;")

        self._table = TableEditor()
        self._surface = SurfaceView()

        # Правки/вставки шлют сигнал на каждую ячейку — схлопываем в одну перерисовку
        self._surface_timer = QTimer(self)
        self._surface_timer.setSingleShot(True)
        self._surface_timer.setInterval(0)
        self._surface_timer.timeout.connect(self._refresh_surface)
        self._table.cell_value_changed.connect(self._schedule_surface)
        self._table.x_axis_changed.connect(self._schedule_surface)
        self._table.y_axis_changed.connect(self._schedule_surface)
        self._table.itemSelectionChanged.connect(self._schedule_surface)

        right = QVBoxLayout()
        right.setContentsMargins(8, 0, 0, 0)
        right.addWidget(self._title)
        right.addWidget(self._meta)

        table_frame = QFrame()
        table_frame.setFrameShape(QFrame.Shape.StyledPanel)
        table_layout = QVBoxLayout(table_frame)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.addWidget(self._table)

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

    def show_table(self, table: CalibrationTable) -> None:
        self._title.setText(table.name)
        unit = f" · {table.unit}" if table.unit else ""
        self._meta.setText(f"{table.rows} × {table.cols}{unit}")
        self._table.set_table(table)
        self._refresh_surface()

    def clear_table(self) -> None:
        self._title.setText("Select a map")
        self._meta.setText("")
        self._table.set_table(None)
        self._surface.clear()

    def _on_tree_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        map_id = item.data(0, int(Qt.ItemDataRole.UserRole))
        if map_id:
            self.map_selected.emit(str(map_id))

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
        )
