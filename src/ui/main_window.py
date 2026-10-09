from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QActionGroup, QCloseEvent, QKeySequence
from PyQt6.QtWidgets import (
    QMainWindow,
    QStackedWidget,
    QLabel,
    QStatusBar,
    QMessageBox,
    QFileDialog,
)

from src.core.units import UnitSystem
from src.i18n import Language, tr
from src.i18n import language as current_language
from src.ui.editor_view import EditorView
from src.ui.key_bindings import KeyBindings
from src.ui.settings_dialog import SettingsDialog
from src.core.transfer import TransferPlan
from src.ui.transfer_dialog import TransferDialog, TransferReportDialog
from src.ui.widgets.status_line import StatusLine


class MainWindow(QMainWindow):
    """Оболочка приложения: меню, разделы, статусбар."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Subaru ECU Editor")
        # Главное окно — только дерево карт; карты и график открываются отдельными окнами
        self.setGeometry(100, 100, 380, 720)

        # Настройки живут в памяти процесса (без сохранения между запусками)
        self.key_bindings = KeyBindings(self)
        self.editor_view = EditorView(self.key_bindings)

        tools_placeholder = QLabel(tr("Инструменты — скоро (интерполятор и др.)"))
        tools_placeholder.setStyleSheet("color: #888; padding: 24px; font-size: 13px;")

        self._stack = QStackedWidget()
        self._stack.addWidget(self.editor_view)  # 0
        self._stack.addWidget(tools_placeholder)  # 1
        self.setCentralWidget(self._stack)

        self._status_bar = QStatusBar()
        self._status_bar.setSizeGripEnabled(True)
        self._status_line = StatusLine()
        self._status_bar.addWidget(self._status_line, 1)
        self.setStatusBar(self._status_bar)
        self.set_status(tr("Готово"))

        self._build_menu()

        # Колбэки, которые вешает presenter
        self.on_open_rom: Callable[[], None] | None = None
        # Сохранение возвращает, удалось ли (результат окну не нужен)
        self.on_save_rom: Callable[[], object] | None = None
        self.on_save_rom_as: Callable[[], object] | None = None
        self.on_show_editor: Callable[[], None] | None = None
        self.on_show_tools: Callable[[], None] | None = None
        self.on_transfer_maps: Callable[[], None] | None = None
        self.on_show_switches: Callable[[], None] | None = None
        self.on_unit_system: Callable[[UnitSystem], None] | None = None
        self.on_language: Callable[[Language], None] | None = None
        # Меню «ИИ»: включить/выключить MCP-сервер, как подключиться
        self.on_ai_server: Callable[[bool], None] | None = None
        self.on_ai_help: Callable[[], None] | None = None
        # Можно ли закрыть окно (несохранённые правки); False — отменить закрытие
        self.on_close_requested: Callable[[], bool] | None = None
        # Перед закрытием окна (сохранение настроек)
        self.on_close: Callable[[], None] | None = None

    def _build_menu(self) -> None:
        menu = self.menuBar()
        assert menu is not None

        file_menu = menu.addMenu(tr("&Файл"))
        assert file_menu is not None
        open_action = QAction(tr("&Открыть ROM…"), self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self._emit_open)
        file_menu.addAction(open_action)

        save_action = QAction(tr("&Сохранить ROM"), self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self._emit_save)
        file_menu.addAction(save_action)

        save_as_action = QAction(tr("Сохранить ROM &как…"), self)
        save_as_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        save_as_action.triggered.connect(self._emit_save_as)
        file_menu.addAction(save_as_action)

        file_menu.addSeparator()
        settings_action = QAction(tr("Настройки…"), self)
        # У Preferences нет стандартной клавиши на Windows — тогда Ctrl+,
        if QKeySequence.keyBindings(QKeySequence.StandardKey.Preferences):
            settings_action.setShortcut(QKeySequence.StandardKey.Preferences)
        else:
            settings_action.setShortcut(QKeySequence("Ctrl+,"))
        # На macOS пункт переедет в меню приложения, как принято (⌘,)
        settings_action.setMenuRole(QAction.MenuRole.PreferencesRole)
        settings_action.triggered.connect(self.show_settings)
        file_menu.addAction(settings_action)

        file_menu.addSeparator()
        quit_action = QAction(tr("&Выход"), self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        view_menu = menu.addMenu(tr("&Вид"))
        assert view_menu is not None
        editor_action = QAction(tr("&Редактор"), self)
        editor_action.triggered.connect(self._emit_show_editor)
        view_menu.addAction(editor_action)

        tools_action = QAction(tr("&Инструменты"), self)
        tools_action.triggered.connect(self._emit_show_tools)
        view_menu.addAction(tools_action)

        view_menu.addSeparator()
        # Единицы на экране: как в описаниях или метрические — для всех величин сразу
        units_menu = view_menu.addMenu(tr("Единицы"))
        assert units_menu is not None
        self._unit_actions: dict[UnitSystem, QAction] = {}
        group = QActionGroup(self)
        for system, title in (
            (UnitSystem.DEFINITIONS, tr("Как в описаниях (psi, °F, mph)")),
            (UnitSystem.METRIC, tr("Метрические (bar, °C, km/h)")),
        ):
            action = QAction(title, self)
            action.setCheckable(True)
            action.triggered.connect(lambda _checked, s=system: self._emit_unit_system(s))
            group.addAction(action)
            units_menu.addAction(action)
            self._unit_actions[system] = action
        self._unit_actions[UnitSystem.DEFINITIONS].setChecked(True)

        # Язык интерфейса — на родном языке каждого пункта, чтобы найти и в незнакомом
        language_menu = view_menu.addMenu("Язык / Language")
        assert language_menu is not None
        self._language_actions: dict[Language, QAction] = {}
        language_group = QActionGroup(self)
        for lang, title in ((Language.RU, "Русский"), (Language.EN, "English")):
            action = QAction(title, self)
            action.setCheckable(True)
            action.setChecked(lang is current_language())
            action.triggered.connect(lambda _checked, value=lang: self._emit_language(value))
            language_group.addAction(action)
            language_menu.addAction(action)
            self._language_actions[lang] = action

        view_menu.addSeparator()
        # Окно графика: включено — показывается само для карт с графиком (2D/3D)
        self.graph_action = QAction(tr("Окно &графика"), self)
        self.graph_action.setCheckable(True)
        self.graph_action.setChecked(True)
        self.graph_action.setShortcut(QKeySequence("Ctrl+G"))
        self.graph_action.toggled.connect(self.editor_view.set_graph_enabled)
        self.editor_view.graph_closed.connect(lambda: self.graph_action.setChecked(False))
        view_menu.addAction(self.graph_action)

        maps_menu = menu.addMenu(tr("&Карты"))
        assert maps_menu is not None
        transfer_action = QAction(tr("Перенести карты из другой прошивки…"), self)
        transfer_action.triggered.connect(self._emit_transfer_maps)
        maps_menu.addAction(transfer_action)

        switches_action = QAction(tr("Ошибки и переключатели…"), self)
        switches_action.setShortcut(QKeySequence("Ctrl+E"))
        switches_action.triggered.connect(self._emit_show_switches)
        maps_menu.addAction(switches_action)

        ai_menu = menu.addMenu(tr("&ИИ"))
        assert ai_menu is not None
        self.ai_server_action = QAction(tr("MCP-сервер для ИИ"), self)
        self.ai_server_action.setCheckable(True)
        self.ai_server_action.toggled.connect(self._emit_ai_server)
        ai_menu.addAction(self.ai_server_action)
        ai_help_action = QAction(tr("Как подключить Claude…"), self)
        ai_help_action.triggered.connect(lambda: self.on_ai_help and self.on_ai_help())
        ai_menu.addAction(ai_help_action)

    def closeEvent(self, event: QCloseEvent | None) -> None:
        if self.on_close_requested is not None and not self.on_close_requested():
            if event is not None:
                event.ignore()
            return
        if self.on_close:
            self.on_close()
        # hide, а не close: закрытие окна графика пользователем выключает его автопоказ
        self.editor_view.graph_window.hide()
        self.editor_view.close_all_maps()
        super().closeEvent(event)

    def show_settings(self) -> None:
        SettingsDialog(self.key_bindings, self).exec()

    def show_editor(self) -> None:
        self._stack.setCurrentIndex(0)
        self.set_status(tr("Редактор"))

    def show_tools(self) -> None:
        self._stack.setCurrentIndex(1)
        self.set_status(tr("Инструменты"))

    def set_status(self, text: str) -> None:
        self._status_line.show_info(text)

    def set_error(self, text: str) -> None:
        self._status_line.show_error(text)

    def clear_status(self) -> None:
        self._status_line.clear_message()

    def set_rom_path(self, path: str | None, modified: bool = False) -> None:
        """Заголовок окна: открытый файл; [*] — отметка несохранённых правок (Qt)."""
        title = "Subaru ECU Editor"
        if path:
            title = f"{title} — {path}[*]"
        self.setWindowTitle(title)
        self.setWindowModified(modified)

    def ask_save_rom_path(self, suggested: str) -> str | None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("Сохранить ROM"),
            suggested,
            tr("Файлы ROM (*.bin *.hex);;Все файлы (*)"),
        )
        return path or None

    def ask_unsaved_changes(self) -> str:
        """«save» / «discard» / «cancel» — что делать с несохранёнными правками ROM."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(tr("Несохранённые правки"))
        box.setText(tr("В прошивке есть несохранённые правки."))
        box.setInformativeText(tr("Сохранить их перед продолжением?"))
        box.setStandardButtons(
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Save)
        answer = box.exec()
        if answer == QMessageBox.StandardButton.Save:
            return "save"
        if answer == QMessageBox.StandardButton.Discard:
            return "discard"
        return "cancel"

    def ask_open_rom_path(self) -> str | None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("Открыть ROM"),
            "",
            tr("Файлы ROM (*.bin *.hex);;Все файлы (*)"),
        )
        return path or None

    def ask_donor_rom_path(self) -> str | None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("Донорская прошивка (B)"),
            "",
            tr("Файлы ROM (*.bin *.hex);;Все файлы (*)"),
        )
        return path or None

    def ask_transfer_selection(self, plan: TransferPlan) -> list[str] | None:
        dialog = TransferDialog(plan, self)
        if dialog.exec() != TransferDialog.DialogCode.Accepted:
            return None
        return dialog.selected_names()

    def show_transfer_report(self, markdown: str, path: Path | None) -> None:
        TransferReportDialog(markdown, path, self).exec()

    def show_info(self, title: str, text: str) -> None:
        QMessageBox.information(self, title, text)

    def _emit_open(self) -> None:
        if self.on_open_rom:
            self.on_open_rom()

    def _emit_transfer_maps(self) -> None:
        if self.on_transfer_maps:
            self.on_transfer_maps()

    def _emit_save(self) -> None:
        if self.on_save_rom:
            self.on_save_rom()

    def set_unit_system_checked(self, system: UnitSystem) -> None:
        self._unit_actions[system].setChecked(True)

    def ask_restart_for_language(self) -> bool:
        """Язык меняется после перезапуска: перезапустить сейчас?"""
        answer = QMessageBox.question(
            self,
            tr("Язык интерфейса"),
            tr("Язык интерфейса изменится после перезапуска. Перезапустить сейчас?"),
        )
        return answer == QMessageBox.StandardButton.Yes

    def _emit_ai_server(self, enabled: bool) -> None:
        if self.on_ai_server:
            self.on_ai_server(enabled)

    def set_ai_server_checked(self, enabled: bool) -> None:
        """Отметка в меню без сигнала (сервер не запустился, восстановление из настроек)."""
        self.ai_server_action.blockSignals(True)
        self.ai_server_action.setChecked(enabled)
        self.ai_server_action.blockSignals(False)

    def show_ai_help(self, url: str, running: bool) -> None:
        state = tr("Сервер запущен.") if running else tr("Сервер выключен: включите «ИИ → MCP-сервер для ИИ».")
        box = QMessageBox(self)
        box.setWindowTitle(tr("Подключение ИИ"))
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.setText(
            tr(
                "<p>Редактор работает как MCP-сервер: ИИ видит открытую прошивку, карты и логи "
                "и может предлагать правки. Каждую правку вы подтверждаете; файл сохраняете сами.</p>"
                "<p>Адрес: <code>{url}</code></p>"
                "<p><b>Claude Code</b> — в терминале:<br><code>claude mcp add --transport http subaru-ecu {url}</code></p>"
                "<p><b>Claude Desktop</b> и другие клиенты — добавьте MCP-сервер с транспортом "
                "Streamable HTTP по этому адресу.</p><p>{state}</p>",
                url=url,
                state=state,
            )
        )
        box.exec()

    def _emit_language(self, language: Language) -> None:
        if self.on_language:
            self.on_language(language)

    def _emit_unit_system(self, system: UnitSystem) -> None:
        if self.on_unit_system:
            self.on_unit_system(system)

    def _emit_show_switches(self) -> None:
        if self.on_show_switches:
            self.on_show_switches()

    def _emit_save_as(self) -> None:
        if self.on_save_rom_as:
            self.on_save_rom_as()

    def _emit_show_editor(self) -> None:
        if self.on_show_editor:
            self.on_show_editor()
        else:
            self.show_editor()

    def _emit_show_tools(self) -> None:
        if self.on_show_tools:
            self.on_show_tools()
        else:
            self.show_tools()
