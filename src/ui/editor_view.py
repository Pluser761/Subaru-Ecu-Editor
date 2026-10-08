from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QGuiApplication, QKeyEvent
from PyQt6.QtWidgets import QHBoxLayout, QHeaderView, QLabel, QSizePolicy, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from src.core.models import CalibrationTable, Checkpoint, MapInfo, RomId
from src.ui.graph_window import GraphKind, GraphWindow
from src.ui.key_bindings import PHYSICAL_CONTROL, KeyBindings
from src.ui.map.cells import dimension_label
from src.ui.map.window import MapWindow
from src.ui.levels import LEVELS, level_color

# Новые окна карт — лесенкой от правого края главного окна
CASCADE_STEP = 28
CASCADE_MAX = 10
DIMENSION_COLOR = QColor("#888888")
# Отметка карты, изменённой относительно checkpoint
CHANGED_MARK = "●"
CHANGED_COLOR = QColor("#c05000")


class EditorView(QWidget):
    """Раздел Editor: дерево карт; каждая открытая карта — отдельное окно (MapWindow),
    одно окно графика следует за активной картой.

    Правки приходят от самих окон карт (map_opened — подключиться к новому окну);
    методы update_* / checkpoint — с map_id и в координатах карты в ROM.
    """

    # Открыть карту (клик или Enter в дереве)
    map_selected = pyqtSignal(str)
    # Окно графика закрыто пользователем — автопоказ выключен
    graph_closed = pyqtSignal()
    # Открыто окно карты — презентер подключается к его сигналам правок напрямую
    map_opened = pyqtSignal(object)  # MapWindow
    # Сообщения самого дерева («Карта не открыта»)
    operation_info = pyqtSignal(str)

    def __init__(self, key_bindings: KeyBindings | None = None, parent=None) -> None:
        super().__init__(parent)
        self._key_bindings = key_bindings or KeyBindings(self)

        self._map_tree = QTreeWidget()
        self._map_tree.setColumnCount(2)
        self._map_tree.setHeaderLabels(["Maps", ""])
        self._map_tree.setMinimumWidth(200)
        tree_header = self._map_tree.header()
        if tree_header is not None:
            tree_header.setStretchLastSection(False)
            tree_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            tree_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._map_tree.itemClicked.connect(self._on_tree_clicked)
        # Enter — открыть карту, Control+Enter — закрыть её окно
        self._map_tree.installEventFilter(self)
        # Листья дерева по map_id — для отметки изменённых карт
        self._leaves: dict[str, QTreeWidgetItem] = {}
        self._changed: set[str] = set()

        # Открытые окна карт; порядок активации — для выбора карты графика после закрытия
        self._windows: dict[str, MapWindow] = {}
        self._activation_order: list[str] = []
        self._current: str | None = None
        self._cascade = 0

        # График текущей карты — отдельное окно: меш для 3D, кривая для 2D, для констант скрыто.
        # Пока автопоказ включён, окно появляется, когда у текущей карты есть график
        self._graph = GraphWindow(self)
        self._graph_enabled = True
        self._graph.closed_by_user.connect(self._on_graph_closed)
        self._graph.surface.point_clicked.connect(self._on_graph_point_clicked)
        self._graph.curve.point_clicked.connect(self._on_graph_point_clicked)
        # Клавиши шага в окне графика меняют текущую карту
        for widget in (self._graph.surface.gl_widget, self._graph.curve.plot_widget):
            widget.installEventFilter(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        # Какая прошивка открыта: имя файла и код прошивки (xmlid) с описанием ЭБУ
        self._rom_file = QLabel()
        self._rom_file.setStyleSheet("font-weight: 600;")
        self._rom_file.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._rom_id = QLabel()
        self._rom_id.setStyleSheet("color: #666;")
        self._rom_id.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # Длинное имя файла не растягивает узкое главное окно — обрезается, полный путь в подсказке
        for label in (self._rom_file, self._rom_id):
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.set_rom_info(None, None)
        layout.addWidget(self._rom_file)
        layout.addWidget(self._rom_id)
        layout.addWidget(_legend())
        layout.addWidget(self._map_tree)

    @property
    def graph_window(self) -> GraphWindow:
        return self._graph

    def set_graph_enabled(self, enabled: bool) -> None:
        """Автопоказ окна графика: включён — окно открыто, пока у текущей карты есть график."""
        self._graph_enabled = enabled
        self._refresh_graph()

    def set_rom_info(self, path: str | None, romid: RomId | None) -> None:
        """Шапка над деревом: имя файла прошивки и её код (xmlid), рынок, модель, процессор."""
        if not path:
            self._rom_file.setText("ROM не открыт")
            self._rom_id.setText("File → Open ROM…")
            self._rom_file.setToolTip("")
            self._rom_id.setToolTip("")
            return
        self._rom_file.setText(Path(path).name)
        tooltip = path
        if romid is None:
            self._rom_id.setText("")
        else:
            car = " ".join(part for part in (romid.market, romid.make, romid.model, romid.submodel, romid.year) if part)
            self._rom_id.setText(" · ".join(part for part in (romid.xmlid, car, romid.memmodel) if part))
            if romid.ecuid:
                tooltip += f"\nECU ID: {romid.ecuid}"
        self._rom_file.setToolTip(tooltip)
        self._rom_id.setToolTip(tooltip)

    def set_maps(self, maps: list[MapInfo]) -> None:
        self._map_tree.clear()
        self._leaves = {}
        self._changed = set()
        categories: dict[str, QTreeWidgetItem] = {}
        for info in maps:
            if info.category not in categories:
                cat_item = QTreeWidgetItem([info.category])
                cat_item.setExpanded(True)
                self._map_tree.addTopLevelItem(cat_item)
                categories[info.category] = cat_item
            leaf = QTreeWidgetItem([info.name, dimension_label(info.rows, info.cols)])
            leaf.setData(0, int(Qt.ItemDataRole.UserRole), info.id)
            leaf.setForeground(1, DIMENSION_COLOR)
            leaf.setTextAlignment(1, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
            if info.userlevel in LEVELS:
                brush = QBrush(level_color(info.userlevel))
                leaf.setBackground(0, brush)
                leaf.setBackground(1, brush)
                leaf.setToolTip(0, f"Уровень {info.userlevel} — {LEVELS[info.userlevel]}")
            categories[info.category].addChild(leaf)
            self._leaves[info.id] = leaf

    def set_changed_maps(self, changed: set[str]) -> None:
        """Отметить в дереве карты, изменённые относительно своего checkpoint: жирное имя
        и ● перед размерностью; категория с такими картами — тоже жирная."""
        if changed == self._changed:
            return
        self._changed = set(changed)
        categories: set[int] = set()
        for map_id, leaf in self._leaves.items():
            is_changed = map_id in changed
            font = leaf.font(0)
            font.setBold(is_changed)
            leaf.setFont(0, font)
            dim = leaf.text(1).removeprefix(CHANGED_MARK).strip()
            leaf.setText(1, f"{CHANGED_MARK} {dim}" if is_changed else dim)
            leaf.setForeground(1, CHANGED_COLOR if is_changed else DIMENSION_COLOR)
            parent = leaf.parent()
            if is_changed and parent is not None:
                categories.add(id(parent))
        for i in range(self._map_tree.topLevelItemCount()):
            category = self._map_tree.topLevelItem(i)
            if category is not None:
                font = category.font(0)
                font.setBold(id(category) in categories)
                category.setFont(0, font)

    # --- Окна карт ---

    def has_map(self, map_id: str) -> bool:
        return map_id in self._windows

    def open_maps(self) -> list[str]:
        return list(self._windows)

    def open_map(self, table: CalibrationTable, checkpoint: Checkpoint | None) -> None:
        """Открыть окно карты поверх остальных, не забирая фокус у дерева."""
        window = MapWindow(table, checkpoint, self._key_bindings, self)
        map_id = window.map_id
        self._windows[map_id] = window
        window.activated.connect(self._set_current)
        window.display_changed.connect(self._on_display_changed)
        window.closed.connect(self._on_window_closed)
        self.map_opened.emit(window)
        window.move(self._next_window_position(window))
        self._show(window)
        self._set_current(map_id)

    def raise_map(self, map_id: str) -> None:
        """Поднять уже открытое окно карты поверх остальных, не забирая фокус."""
        window = self._windows.get(map_id)
        if window is None:
            return
        self._show(window)
        self._set_current(map_id)

    def close_map(self, map_id: str) -> bool:
        """Закрыть окно карты; False — оно не открыто."""
        window = self._windows.get(map_id)
        if window is None:
            return False
        window.close()
        return True

    def reload_map(self, table: CalibrationTable, checkpoint: Checkpoint | None) -> None:
        """Показать перечитанную карту в уже открытом окне (после переноса карт)."""
        window = self._windows.get(table.name)
        if window is not None:
            window.show_table(table, checkpoint)

    def close_all_maps(self) -> None:
        for window in list(self._windows.values()):
            window.close()

    def update_cell(self, map_id: str, row: int, col: int, value: float) -> None:
        """Показать значение, реально сохранённое в ROM (без повторной записи)."""
        window = self._windows.get(map_id)
        if window is not None:
            window.update_cell(row, col, value)

    def update_axis(self, map_id: str, orientation: Qt.Orientation, index: int, value: float) -> None:
        """Показать значение оси карты; orientation — ось в ROM (Horizontal — X, Vertical — Y)."""
        window = self._windows.get(map_id)
        if window is not None:
            window.update_axis(orientation, index, value)

    def checkpoint(self, map_id: str) -> Checkpoint | None:
        window = self._windows.get(map_id)
        return window.checkpoint() if window is not None else None

    # --- Внутреннее ---

    @staticmethod
    def _show(window: MapWindow) -> None:
        # Без активации: фокус остаётся в дереве (листать и открывать дальше), а macOS не
        # пишет в лог TSMSendMessageToUIServer, как при программной активации окна с полями ввода
        window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        window.show()
        window.raise_()

    def _next_window_position(self, window: QWidget) -> QPoint:
        """Лесенкой от правого края главного окна, но целиком на экране главного окна."""
        main = self.window()
        origin = main.frameGeometry().topRight() + QPoint(12, 0) if main is not None else QPoint(120, 80)
        offset = CASCADE_STEP * (self._cascade % CASCADE_MAX)
        self._cascade += 1
        pos = origin + QPoint(offset, offset)
        screen = (main.screen() if main is not None else None) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            size = window.frameGeometry().size()
            pos.setX(max(area.left(), min(pos.x(), area.right() - size.width())))
            pos.setY(max(area.top(), min(pos.y(), area.bottom() - size.height())))
        return pos

    def _set_current(self, map_id: str) -> None:
        if map_id not in self._windows:
            return
        if map_id in self._activation_order:
            self._activation_order.remove(map_id)
        self._activation_order.append(map_id)
        if self._current != map_id:
            self._current = map_id
            self._refresh_graph()

    def _on_window_closed(self, map_id: str) -> None:
        self._windows.pop(map_id, None)
        if map_id in self._activation_order:
            self._activation_order.remove(map_id)
        if self._current == map_id:
            self._current = self._activation_order[-1] if self._activation_order else None
            self._refresh_graph()
        if not self._windows:
            self._cascade = 0

    def _on_display_changed(self, map_id: str) -> None:
        if map_id == self._current:
            self._refresh_graph()

    def _refresh_graph(self) -> None:
        window = self._windows.get(self._current) if self._current is not None else None
        data = window.graph_data() if window is not None else None
        kind = GraphKind.NONE
        if data is not None:
            kind = self._graph.set_data(**data)  # type: ignore[arg-type]
        else:
            self._graph.clear()
        if self._graph_enabled and kind is not GraphKind.NONE:
            if not self._graph.isVisible():
                self._graph.show_near(window)
        else:
            self._graph.hide()

    def _on_graph_closed(self) -> None:
        self._graph_enabled = False
        self.graph_closed.emit()

    def _on_graph_point_clicked(self, row: int, col: int, additive: bool) -> None:
        window = self._windows.get(self._current) if self._current is not None else None
        if window is not None:
            window.select_cell(row, col, additive)

    def _on_tree_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        map_id = _map_id_of(item)
        if map_id:
            self.map_selected.emit(map_id)

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:
        if isinstance(event, QKeyEvent) and event.type() == QEvent.Type.KeyPress:
            if obj is self._map_tree:
                return self._handle_tree_key(event)
            # Окно графика: клавиши шага — текущей карте
            window = self._windows.get(self._current) if self._current is not None else None
            if window is not None and window.handle_step_key(event):
                return True
        return super().eventFilter(obj, event)

    def _handle_tree_key(self, event: QKeyEvent) -> bool:
        """Enter на карте — открыть её окно (фокус остаётся в дереве), Control+Enter — закрыть.
        Enter на категории — раскрыть/свернуть. Стрелки — стандартное поведение дерева."""
        if event.key() not in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return False
        item = self._map_tree.currentItem()
        if item is None:
            return False
        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        map_id = _map_id_of(item)
        if map_id is None:
            if modifiers == Qt.KeyboardModifier.NoModifier:
                item.setExpanded(not item.isExpanded())
                return True
            return False
        if modifiers == Qt.KeyboardModifier.NoModifier:
            self.map_selected.emit(map_id)
            return True
        if modifiers == PHYSICAL_CONTROL:
            if not self.close_map(map_id):
                self.operation_info.emit(f"Карта не открыта: {map_id.strip()}")
            return True
        return False


def _map_id_of(item: QTreeWidgetItem) -> str | None:
    """map_id листа дерева; None — это категория."""
    map_id = item.data(0, int(Qt.ItemDataRole.UserRole))
    return str(map_id) if map_id else None


def _legend() -> QWidget:
    """Строка над деревом: цвета уровней 1…5 и отметка изменённой карты (подсказки при наведении)."""
    legend = QWidget()
    row = QHBoxLayout(legend)
    row.setContentsMargins(2, 0, 2, 2)
    row.setSpacing(3)
    caption = QLabel("Уровень")
    caption.setStyleSheet("color: #666;")
    caption.setToolTip("Уровень таблицы из RomRaider definitions (userlevel): 1 — основные … 5 — служебные")
    row.addWidget(caption)
    for level, name in LEVELS.items():
        chip = QLabel(str(level))
        chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        chip.setFixedSize(22, 18)
        chip.setStyleSheet(
            f"background: {level_color(level).name()}; border: 1px solid #bbb; border-radius: 3px; color: #333;"
        )
        chip.setToolTip(f"Уровень {level} — {name}")
        row.addWidget(chip)
    row.addStretch(1)
    changed = QLabel(f"{CHANGED_MARK} изменена")
    changed.setStyleSheet(f"color: {CHANGED_COLOR.name()};")
    changed.setToolTip("Жирная карта с ● — значения или оси отличаются от её checkpoint")
    row.addWidget(changed)
    return legend
