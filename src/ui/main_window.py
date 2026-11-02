from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QCloseEvent, QDesktopServices, QKeySequence
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
from src.ui.notes_dialog import NotesDialog
from src.ui.settings_dialog import SettingsDialog
from src.core.transfer import TransferPlan
from src.ui.transfer_dialog import TransferDialog, TransferReportDialog
from src.ui.widgets.status_line import StatusLine
from src.version import APP_NAME, version


class MainWindow(QMainWindow):
    """Оболочка приложения: меню, разделы, статусбар."""

    # События меню: окно лишь сообщает о действии, что делать решает presenter
    open_rom_requested = pyqtSignal()
    save_rom_requested = pyqtSignal()
    save_rom_as_requested = pyqtSignal()
    transfer_maps_requested = pyqtSignal()
    show_switches_requested = pyqtSignal()
    revert_all_requested = pyqtSignal()
    unit_system_requested = pyqtSignal(UnitSystem)
    language_requested = pyqtSignal(Language)
    ai_server_toggled = pyqtSignal(bool)
    ai_help_requested = pyqtSignal()
    ai_notes_requested = pyqtSignal()
    open_defs_folder_requested = pyqtSignal()
    open_logger_requested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(_app_title())
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

        # Синхронные хуки закрытия (нужен результат, а не событие): presenter задаёт их,
        # closeEvent вызывает напрямую
        self.on_close_requested: Callable[[], bool] | None = None
        self.on_close: Callable[[], None] | None = None

    def _build_menu(self) -> None:
        menu = self.menuBar()
        assert menu is not None

        file_menu = menu.addMenu(tr("&Файл"))
        assert file_menu is not None
        open_action = QAction(tr("&Открыть ROM…"), self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.open_rom_requested)
        file_menu.addAction(open_action)

        save_action = QAction(tr("&Сохранить ROM"), self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self.save_rom_requested)
        file_menu.addAction(save_action)

        save_as_action = QAction(tr("Сохранить ROM &как…"), self)
        save_as_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        save_as_action.triggered.connect(self.save_rom_as_requested)
        file_menu.addAction(save_as_action)

        file_menu.addSeparator()
        defs_action = QAction(tr("Папка definitions…"), self)
        defs_action.setToolTip(tr(
            "Свои дополнения definitions (RomRaider или EcuFlash, например SubaruDefs); "
            "подхватываются при следующем запуске"
        ))
        defs_action.triggered.connect(self.open_defs_folder_requested)
        file_menu.addAction(defs_action)
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
        editor_action.triggered.connect(self.show_editor)
        view_menu.addAction(editor_action)

        tools_action = QAction(tr("&Инструменты"), self)
        tools_action.triggered.connect(self.show_tools)
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
            action.triggered.connect(lambda _checked, s=system: self.unit_system_requested.emit(s))
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
            action.triggered.connect(lambda _checked, value=lang: self.language_requested.emit(value))
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
        transfer_action.triggered.connect(self.transfer_maps_requested)
        maps_menu.addAction(transfer_action)

        revert_action = QAction(tr("Откатить все карты к checkpoint…"), self)
        revert_action.setToolTip(tr("Вернуть в прошивке значения и оси всех изменённых карт к их checkpoint"))
        revert_action.triggered.connect(self.revert_all_requested)
        maps_menu.addAction(revert_action)

        switches_action = QAction(tr("Ошибки и переключатели…"), self)
        switches_action.setShortcut(QKeySequence("Ctrl+E"))
        switches_action.triggered.connect(self.show_switches_requested)
        maps_menu.addAction(switches_action)

        logger_menu = menu.addMenu(tr("&Логгер"))
        assert logger_menu is not None
        logger_action = QAction(tr("Открыть логгер…"), self)
        logger_action.setShortcut(QKeySequence("Ctrl+L"))
        logger_action.triggered.connect(self.open_logger_requested)
        logger_menu.addAction(logger_action)

        ai_menu = menu.addMenu(tr("&ИИ"))
        assert ai_menu is not None
        self.ai_server_action = QAction(tr("MCP-сервер для ИИ"), self)
        self.ai_server_action.setCheckable(True)
        self.ai_server_action.toggled.connect(self.ai_server_toggled)
        ai_menu.addAction(self.ai_server_action)
        ai_help_action = QAction(tr("Как подключить Claude…"), self)
        ai_help_action.triggered.connect(self.ai_help_requested)
        ai_menu.addAction(ai_help_action)
        ai_menu.addSeparator()
        ai_notes_action = QAction(tr("Заметки о прошивке…"), self)
        ai_notes_action.triggered.connect(self.ai_notes_requested)
        ai_menu.addAction(ai_notes_action)

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
        title = _app_title()
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

    def set_unit_system_checked(self, system: UnitSystem) -> None:
        self._unit_actions[system].setChecked(True)

    def ask_revert(self, names: list[str]) -> bool:
        """Подтвердить откат карт к checkpoint: правки после checkpoint пропадут."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(tr("Откат к checkpoint"))
        if len(names) == 1:
            box.setText(tr("Откатить карту «{name}» к checkpoint?", name=names[0].strip()))
        else:
            box.setText(tr("Откатить к checkpoint изменённые карты: {n}?", n=len(names)))
            box.setDetailedText("\n".join(name.strip() for name in names))
        box.setInformativeText(
            tr(
                "Значения и оси вернутся к checkpoint, правки после него пропадут. "
                "Общие оси откатятся и у других карт на них."
            )
        )
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Yes

    def ask_restart_for_language(self) -> bool:
        """Язык меняется после перезапуска: перезапустить сейчас?"""
        answer = QMessageBox.question(
            self,
            tr("Язык интерфейса"),
            tr("Язык интерфейса изменится после перезапуска. Перезапустить сейчас?"),
        )
        return answer == QMessageBox.StandardButton.Yes

    def set_ai_server_checked(self, enabled: bool) -> None:
        """Отметка в меню без сигнала (сервер не запустился, восстановление из настроек)."""
        self.ai_server_action.blockSignals(True)
        self.ai_server_action.setChecked(enabled)
        self.ai_server_action.blockSignals(False)

    def open_folder(self, folder: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def ask_rom_notes(self, title: str, text: str, location: str) -> str | None:
        """Окно заметок о прошивке; None — закрыли без сохранения."""
        dialog = NotesDialog(title, text, location, self)
        if dialog.exec() != NotesDialog.DialogCode.Accepted:
            return None
        return dialog.text()

    def ask_overwrite_notes(self) -> bool:
        answer = QMessageBox.question(
            self,
            tr("Заметки о прошивке"),
            tr("Пока окно было открыто, заметки изменились (их дописал ИИ). Заменить их вашим текстом?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

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


def _app_title() -> str:
    return f"{APP_NAME} {version()}"
