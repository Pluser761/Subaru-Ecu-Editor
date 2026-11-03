"""Окно логгера: подключение, выбор параметров, запись и отметки; справа — вкладки
«Дашборд» (плитка на каждый выбранный параметр) и «График» (график на каждый выбранный).

Только виджеты и сигналы — логика в src/logger/presenter.py. Отдельное окно ОС.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import pyqtgraph as pg
from PyQt6.QtCore import QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.i18n import tr
from src.logger.ui.dashboard import Dashboard
from src.logger.ui.profile_picker import ProfilePicker

DEFAULT_SIZE = (1100, 720)
GRAPH_SECONDS = 30.0
PLOT_HEIGHT = 130  # высота одного графика: больше графиков — вкладка прокручивается
_ROLE_ID = int(Qt.ItemDataRole.UserRole)
MARK_COLOR = "#d03030"
# Цвета графиков: тёмные, различимые на белом
GRAPH_COLORS = ("#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf")
UNITS_COLUMN_WIDTH = 72
# Ctrl на macOS Qt сам показывает и понимает как ⌘
_PICK_KEY = QKeySequence("Ctrl+P")


@dataclass
class ParamRow:
    """Параметр в дереве выбора."""

    id: str
    name: str
    units: list[str]
    unit: str  # выбранные единицы
    checked: bool = False
    enabled: bool = True  # поддерживается подключённым ЭБУ
    tooltip: str = ""


@dataclass
class LiveRow:
    id: str
    name: str
    unit: str


@dataclass
class DtcRow:
    """Код неисправности в таблице «Ошибки»."""

    code: str
    name: str
    current: bool  # активна сейчас
    stored: bool  # сохранена в памяти ЭБУ


@dataclass
class _Graph:
    plot: pg.PlotItem
    curve: pg.PlotDataItem
    times: deque[float] = field(default_factory=deque)
    values: deque[float] = field(default_factory=deque)


class LoggerWindow(QWidget):
    connect_requested = pyqtSignal(str)  # id порта
    disconnect_requested = pyqtSignal()
    refresh_ports_requested = pyqtSignal()
    selection_changed = pyqtSignal()
    record_toggled = pyqtSignal(bool)
    mark_requested = pyqtSignal()
    pick_profile_requested = pyqtSignal()  # быстрый выбор (Cmd/Ctrl+P)
    profile_chosen = pyqtSignal(str)  # путь выбранного в быстром выборе
    open_profile_requested = pyqtSignal()
    save_profile_requested = pyqtSignal()
    choose_folder_requested = pyqtSignal()
    open_logs_requested = pyqtSignal()
    dtc_read_requested = pyqtSignal()
    dtc_clear_requested = pyqtSignal()
    closed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(tr("Логгер"))
        self.resize(*DEFAULT_SIZE)
        self._connected = False
        self._live_ids: list[str] = []
        self._graphs: dict[str, _Graph] = {}
        self._mark_times: list[float] = []
        self._filling = False  # программное заполнение дерева — не слать selection_changed
        self._recording = False
        self._dtc_read = False  # коды прочитаны — сброс разрешён
        self._dtc_busy = False

        # --- Верхняя панель: порт, подключение, статус ---
        self._ports = QComboBox()
        self._ports.setMinimumWidth(220)
        refresh = QPushButton("⟳")
        refresh.setToolTip(tr("Обновить список адаптеров"))
        refresh.setFixedWidth(32)
        refresh.clicked.connect(self.refresh_ports_requested)
        self._connect_btn = QPushButton(tr("Подключить"))
        self._connect_btn.clicked.connect(self._toggle_connect)
        self._status = QLabel(tr("Не подключено"))
        self._status.setStyleSheet("color: #555;")
        self._rate = QLabel("")
        self._rate.setStyleSheet("color: #555;")
        top = QHBoxLayout()
        top.addWidget(QLabel(tr("Адаптер:")))
        top.addWidget(self._ports)
        top.addWidget(refresh)
        top.addWidget(self._connect_btn)
        top.addSpacing(12)
        top.addWidget(self._status, 1)
        top.addWidget(self._rate)

        # --- Панель записи ---
        self._record_btn = QPushButton(tr("● Запись"))
        self._record_btn.setCheckable(True)
        self._record_btn.setEnabled(False)
        self._record_btn.toggled.connect(self.record_toggled)
        self._mark_btn = QPushButton(tr("Отметка (пробел)"))
        self._mark_btn.setToolTip(tr("Отметить событие (хлопок и т.п.): в логе — 1 в колонке Mark"))
        self._mark_btn.setEnabled(False)
        self._mark_btn.clicked.connect(self.mark_requested)
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, self._mark_shortcut)
        self._mark_flash = QLabel("")
        self._mark_flash.setStyleSheet(f"color: {MARK_COLOR}; font-weight: 600;")
        folder_btn = QPushButton(tr("Папка логов…"))
        folder_btn.clicked.connect(self.choose_folder_requested)
        logs_btn = QPushButton(tr("Журнал адаптера…"))
        logs_btn.clicked.connect(self.open_logs_requested)
        self._folder = QLabel("")
        self._folder.setStyleSheet("color: #555;")
        self._folder.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        record_row = QHBoxLayout()
        record_row.addWidget(self._record_btn)
        record_row.addWidget(self._mark_btn)
        record_row.addWidget(self._mark_flash)
        record_row.addSpacing(12)
        record_row.addWidget(folder_btn)
        record_row.addWidget(logs_btn)
        record_row.addWidget(self._folder, 1)

        # --- Слева: выбор параметров ---
        self._filter = QLineEdit()
        self._filter.setPlaceholderText(tr("Поиск параметра"))
        self._filter.textChanged.connect(self._apply_filter)
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels([tr("Параметр"), tr("Ед.")])
        tree_header = self._tree.header()
        assert tree_header is not None
        # Имя тянется, единицы — узкая колонка (длинные вроде «estimated AFR» обрезаются,
        # полностью — в подсказке и в меню двойного щелчка)
        tree_header.setStretchLastSection(False)
        tree_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        tree_header.resizeSection(1, UNITS_COLUMN_WIDTH)
        self._tree.itemChanged.connect(self._on_item_changed)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._profile_btn = QPushButton(tr("Профили ▾"))
        self._profile_btn.setToolTip(tr("Быстрый выбор профиля ({key})", key=_PICK_KEY.toString(
            QKeySequence.SequenceFormat.NativeText)))
        self._profile_btn.clicked.connect(self.pick_profile_requested)
        QShortcut(_PICK_KEY, self, self.pick_profile_requested.emit)
        self._picker = ProfilePicker(self)
        self._picker.picked.connect(self.profile_chosen)
        profile_open = QPushButton(tr("Открыть файл…"))
        profile_open.setToolTip(tr("Открыть профиль RomRaider из любой папки"))
        profile_open.clicked.connect(self.open_profile_requested)
        profile_save = QPushButton(tr("Сохранить профиль…"))
        profile_save.clicked.connect(self.save_profile_requested)
        profile_row = QHBoxLayout()
        profile_row.addWidget(self._profile_btn, 1)
        profile_row.addWidget(profile_open)
        profile_row.addWidget(profile_save)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self._filter)
        left_layout.addWidget(self._tree, 1)
        left_layout.addLayout(profile_row)
        hint = QLabel(tr("Двойной щелчок по единицам — сменить их."))
        hint.setStyleSheet("color: #777;")
        hint.setWordWrap(True)
        left_layout.addWidget(hint)

        # --- Справа: вкладки «Дашборд» и «График» — оба показывают всё, что отмечено слева ---
        self.dashboard = Dashboard()
        self._plots = pg.GraphicsLayoutWidget()
        self._plots.setBackground("w")
        plots_scroll = QScrollArea()
        plots_scroll.setWidgetResizable(True)
        plots_scroll.setWidget(self._plots)

        # --- Справа: вкладка «Ошибки» — чтение и сброс DTC ---
        self._dtc_tree = QTreeWidget()
        self._dtc_tree.setHeaderLabels([tr("Код"), tr("Описание"), tr("Сейчас"), tr("Сохранена")])
        self._dtc_tree.setRootIsDecorated(False)
        dtc_header = self._dtc_tree.header()
        assert dtc_header is not None
        dtc_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        dtc_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        dtc_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        dtc_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        dtc_header_item = self._dtc_tree.headerItem()
        if dtc_header_item is not None:
            dtc_header_item.setToolTip(2, tr("Неисправность активна в этом запуске"))
            dtc_header_item.setToolTip(3, tr("Неисправность записана в памяти ЭБУ"))
        self._dtc_read_btn = QPushButton(tr("Обновить"))
        self._dtc_read_btn.clicked.connect(self.dtc_read_requested)
        self._dtc_clear_btn = QPushButton(tr("Сбросить ошибки"))
        self._dtc_clear_btn.clicked.connect(self.dtc_clear_requested)
        self._dtc_status = QLabel("")
        self._dtc_status.setWordWrap(True)
        dtc_hint = QLabel(tr("Сброс стирает и обученные значения (топливные коррекции, ХХ), "
                              "и мониторы готовности."))
        dtc_hint.setStyleSheet("color: #777;")
        dtc_hint.setWordWrap(True)
        dtc_buttons = QHBoxLayout()
        dtc_buttons.addWidget(self._dtc_read_btn)
        dtc_buttons.addWidget(self._dtc_clear_btn)
        dtc_buttons.addStretch(1)
        dtc_tab = QWidget()
        dtc_layout = QVBoxLayout(dtc_tab)
        dtc_layout.addWidget(self._dtc_tree, 1)
        dtc_layout.addWidget(self._dtc_status)
        dtc_layout.addLayout(dtc_buttons)
        dtc_layout.addWidget(dtc_hint)

        self._tabs = QTabWidget()
        self._tabs.addTab(self.dashboard, tr("Дашборд"))
        self._tabs.addTab(plots_scroll, tr("График"))
        self._tabs.addTab(dtc_tab, tr("Ошибки"))
        right = self._tabs

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([420, 680])

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(record_row)
        layout.addWidget(split, 1)

        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(lambda: self._mark_flash.setText(""))
        self._update_dtc_buttons()

    # --- Подключение ---

    def set_ports(self, ports: list[tuple[str, str]], current: str | None = None) -> None:
        """[(id порта, подпись)]; эмулятор — тоже порт."""
        self._ports.clear()
        for port_id, label in ports:
            self._ports.addItem(label, port_id)
        index = self._ports.findData(current) if current else -1
        self._ports.setCurrentIndex(index if index >= 0 else 0)

    def current_port(self) -> str | None:
        data = self._ports.currentData()
        return str(data) if data is not None else None

    def set_connected(self, connected: bool, status: str) -> None:
        self._connected = connected
        self._connect_btn.setText(tr("Отключить") if connected else tr("Подключить"))
        self._ports.setEnabled(not connected)
        self._record_btn.setEnabled(connected)
        self._mark_btn.setEnabled(connected)
        if not connected:
            self._rate.setText("")
            self.set_recording(None)
            # Коды другого ЭБУ/сессии не показываем; сброс снова потребует чтения
            self._dtc_read = False
            self._dtc_busy = False
            self._dtc_tree.clear()
            self._dtc_status.setText("")
        self._update_dtc_buttons()
        self._status.setStyleSheet("color: #555;")
        self._status.setText(status)

    def set_error(self, text: str) -> None:
        self._status.setStyleSheet("color: #c03030;")
        self._status.setText(text)

    def set_rate(self, rate: float) -> None:
        self._rate.setText(tr("{rate} опр/с", rate=f"{rate:.1f}") if rate > 0 else "")

    def _toggle_connect(self) -> None:
        if self._connected:
            self.disconnect_requested.emit()
        else:
            port = self.current_port()
            if port:
                self.connect_requested.emit(port)

    # --- Запись и отметки ---

    def set_log_folder(self, folder: str) -> None:
        self._folder.setText(folder)
        self._folder.setToolTip(folder)

    def set_recording(self, path: str | None) -> None:
        self._recording = path is not None
        self._record_btn.blockSignals(True)
        self._record_btn.setChecked(path is not None)
        self._record_btn.blockSignals(False)
        self._record_btn.setText(tr("■ Стоп") if path else tr("● Запись"))
        self._record_btn.setStyleSheet(f"color: {MARK_COLOR};" if path else "")
        self._tree.setEnabled(path is None)  # пока пишется лог, колонки не меняются
        self._update_dtc_buttons()  # во время записи сброс недоступен
        if path:
            self._folder.setText(path)
            self._folder.setToolTip(path)

    def flash_mark(self, time_s: float) -> None:
        self._mark_flash.setText(tr("● отметка"))
        self._flash_timer.start(800)
        self._mark_times.append(time_s)
        for graph in self._graphs.values():
            graph.plot.addItem(pg.InfiniteLine(time_s, angle=90, pen=pg.mkPen(MARK_COLOR, width=1)))

    def _mark_shortcut(self) -> None:
        if self._connected:
            self.mark_requested.emit()

    # --- Выбор параметров ---

    def set_parameters(self, groups: list[tuple[str, list[ParamRow]]]) -> None:
        self._filling = True
        self._tree.clear()
        for title, rows in groups:
            group = QTreeWidgetItem([f"{title} ({len(rows)})"])
            group.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self._tree.addTopLevelItem(group)
            for row in rows:
                item = QTreeWidgetItem([row.name, row.unit])
                item.setToolTip(1, row.unit)
                item.setData(0, _ROLE_ID, row.id)
                item.setData(1, _ROLE_ID, row.units)
                flags = Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable
                if row.enabled:
                    flags |= Qt.ItemFlag.ItemIsEnabled
                item.setFlags(flags)
                item.setCheckState(0, Qt.CheckState.Checked if row.checked else Qt.CheckState.Unchecked)
                item.setToolTip(0, row.tooltip)
                group.addChild(item)
            group.setExpanded(len(rows) < 80)
        self._filling = False
        self._apply_filter(self._filter.text())

    def selected(self) -> list[tuple[str, str]]:
        """Отмеченные параметры: [(id, единицы)] в порядке дерева."""
        out = []
        for item in self._param_items():
            if item.checkState(0) == Qt.CheckState.Checked:
                out.append((str(item.data(0, _ROLE_ID)), item.text(1)))
        return out

    def set_checked(self, units_by_id: dict[str, str | None]) -> None:
        """Отметить ровно эти параметры (профиль); единицы — если заданы и есть у параметра."""
        self._filling = True
        for item in self._param_items():
            pid = str(item.data(0, _ROLE_ID))
            checked = pid in units_by_id and bool(item.flags() & Qt.ItemFlag.ItemIsEnabled)
            item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            units = units_by_id.get(pid)
            if checked and units and units in (item.data(1, _ROLE_ID) or []):
                item.setText(1, units)
        self._filling = False
        self.selection_changed.emit()

    def _param_items(self) -> list[QTreeWidgetItem]:
        items = []
        for i in range(self._tree.topLevelItemCount()):
            group = self._tree.topLevelItem(i)
            if group is not None:
                items += [group.child(j) for j in range(group.childCount()) if group.child(j) is not None]
        return [item for item in items if item is not None]

    def _apply_filter(self, text: str) -> None:
        key = text.strip().lower()
        for item in self._param_items():
            item.setHidden(bool(key) and key not in item.text(0).lower() and key != str(item.data(0, _ROLE_ID)).lower())

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if not self._filling and column == 0 and item.parent() is not None:
            self.selection_changed.emit()

    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        units: list[str] = item.data(1, _ROLE_ID) or []
        if column != 1 or len(units) < 2 or not self._tree.isEnabled():
            return
        menu = QMenu(self)
        for unit in units:
            action = menu.addAction(unit)
            assert action is not None
            action.setCheckable(True)
            action.setChecked(unit == item.text(1))
        rect = self._tree.visualItemRect(item)
        viewport = self._tree.viewport()
        assert viewport is not None
        chosen = menu.exec(viewport.mapToGlobal(QPoint(rect.right() - 60, rect.bottom())))
        if chosen is not None and chosen.text() != item.text(1):
            item.setText(1, chosen.text())
            if item.checkState(0) == Qt.CheckState.Checked:
                self.selection_changed.emit()

    # --- Живые значения ---

    def set_live_rows(self, rows: list[LiveRow]) -> None:
        """Выбранные параметры: плитки на дашборде и графики (прежние данные графиков теряются)."""
        self._live_ids = [row.id for row in rows]
        self.dashboard.set_tiles([(row.id, row.name, row.unit) for row in rows])
        self._set_graphs([(row.id, f"{row.name}, {row.unit}") for row in rows])

    def set_row_state(self, pid: str, tooltip: str, slow: bool, unsupported: bool) -> None:
        tile = self.dashboard.tile(pid)
        if tile is not None:
            tile.set_state(tooltip, slow, unsupported)

    def update_values(self, texts: dict[str, tuple[str, str, str]]) -> None:
        """id → (значение, мин, макс) уже отформатированные."""
        for pid, (value, low, high) in texts.items():
            tile = self.dashboard.tile(pid)
            if tile is not None:
                tile.set_values(value, low, high)

    # --- Ошибки (DTC) ---

    def set_dtc_rows(self, rows: list[DtcRow]) -> None:
        """Показать прочитанные коды; пустой список — ошибок нет (чтение удалось)."""
        self._dtc_tree.clear()
        for row in rows:
            item = QTreeWidgetItem([
                row.code, row.name,
                "✓" if row.current else "",
                "✓" if row.stored else "",
            ])
            item.setToolTip(1, row.name)
            self._dtc_tree.addTopLevelItem(item)
        self._dtc_read = True
        self.set_dtc_status(tr("Кодов: {n}", n=len(rows)) if rows else tr("Ошибок нет"))
        self._update_dtc_buttons()

    def set_dtc_status(self, text: str) -> None:
        self._dtc_status.setStyleSheet("color: #555;")
        self._dtc_status.setText(text)

    def forget_dtc(self) -> None:
        """Содержимое памяти ошибок могло измениться — данные больше не достоверны."""
        self._dtc_read = False
        self._dtc_tree.clear()
        self._update_dtc_buttons()

    def set_dtc_error(self, text: str) -> None:
        self._dtc_status.setStyleSheet("color: #c03030;")
        self._dtc_status.setText(text)

    def set_dtc_busy(self, busy: bool, text: str = "") -> None:
        """На время чтения/сброса кнопки заблокированы; text — что происходит."""
        self._dtc_busy = busy
        if text:
            self.set_dtc_status(text)
        self._update_dtc_buttons()

    def _update_dtc_buttons(self) -> None:
        self._dtc_read_btn.setEnabled(self._connected and not self._dtc_busy)
        self._dtc_clear_btn.setEnabled(self._connected and not self._dtc_busy
                                       and not self._recording and self._dtc_read)
        if self._recording:
            tip = tr("Пока пишется лог, сброс недоступен — остановите запись")
        elif not self._dtc_read:
            tip = tr("Сначала прочитайте коды")
        else:
            tip = tr("Сброс стирает и обученные значения (топливные коррекции, ХХ), "
                     "и мониторы готовности.")
        self._dtc_clear_btn.setToolTip(tip)

    def ask_clear_dtc(self) -> bool:
        """Подтверждение сброса: он стирает и обученные значения, и мониторы готовности."""
        answer = QMessageBox.question(
            self,
            tr("Сброс ошибок"),
            tr("Сбросить память ошибок (reset ЭБУ)?\n\nВместе с кодами сбросятся обученные значения "
               "(топливные коррекции, ХХ) и мониторы готовности. После сброса коды будут прочитаны заново."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    # --- Графики ---

    def _set_graphs(self, graphs: list[tuple[str, str]]) -> None:
        self._plots.setMinimumHeight(PLOT_HEIGHT * len(graphs))
        self._plots.clear()
        self._graphs.clear()
        first: pg.PlotItem | None = None
        for i, (pid, title) in enumerate(graphs):
            plot = self._plots.addPlot(row=i, col=0)
            color = GRAPH_COLORS[i % len(GRAPH_COLORS)]
            plot.setTitle(title, color=color, size="9pt", justify="left")
            plot.showGrid(x=True, y=True, alpha=0.3)
            plot.setMouseEnabled(x=False, y=False)
            plot.hideButtons()
            if first is None:
                first = plot
            else:
                plot.setXLink(first)
            if i < len(graphs) - 1:
                plot.getAxis("bottom").setStyle(showValues=False)
            curve = plot.plot(pen=pg.mkPen(color, width=2))
            self._graphs[pid] = _Graph(plot, curve)

    def add_points(self, time_s: float, values: dict[str, float]) -> None:
        for pid, graph in self._graphs.items():
            value = values.get(pid)
            if value is None or value != value:  # nan — пропуск
                continue
            graph.times.append(time_s)
            graph.values.append(value)
            while graph.times and graph.times[0] < time_s - GRAPH_SECONDS:
                graph.times.popleft()
                graph.values.popleft()

    def redraw_graphs(self, now_s: float) -> None:
        if self._tabs.currentIndex() != 1:
            return  # вкладка графиков скрыта — точки копятся, рисовать незачем
        for graph in self._graphs.values():
            graph.curve.setData(list(graph.times), list(graph.values))
            graph.plot.setXRange(max(0.0, now_s - GRAPH_SECONDS), max(GRAPH_SECONDS, now_s), padding=0)

    # --- Диалоги ---

    def show_profile_picker(self, profiles: list[tuple[str, str, str]]) -> None:
        """Быстрый выбор: [(имя, путь, подсказка)]; окошко — под кнопкой «Профили»."""
        at = self._profile_btn.mapToGlobal(QPoint(0, self._profile_btn.height()))
        self._picker.show_profiles(profiles, at)

    def ask_profile_path(self, save: bool, folder: str) -> str | None:
        caption = tr("Сохранить профиль логгера") if save else tr("Открыть профиль логгера")
        filters = tr("Профиль RomRaider (*.xml)")
        if save:
            path, _ = QFileDialog.getSaveFileName(self, caption, folder, filters)
        else:
            path, _ = QFileDialog.getOpenFileName(self, caption, folder, filters)
        return path or None

    def ask_folder(self, current: str) -> str | None:
        path = QFileDialog.getExistingDirectory(self, tr("Папка для логов"), current)
        return path or None

    def show_message(self, title: str, text: str) -> None:
        QMessageBox.information(self, title, text)

    def closeEvent(self, event: QCloseEvent | None) -> None:
        self.closed.emit()
        super().closeEvent(event)
