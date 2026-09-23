from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (
    QMainWindow,
    QStackedWidget,
    QLabel,
    QStatusBar,
    QMessageBox,
    QFileDialog,
)

from src.ui.editor_view import EditorView
from src.ui.widgets.status_line import StatusLine


class MainWindow(QMainWindow):
    """Оболочка приложения: меню, разделы, статусбар."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Subaru ECU Editor")
        self.setGeometry(100, 100, 1100, 700)

        self.editor_view = EditorView()

        tools_placeholder = QLabel("Tools — скоро (интерполятор и др.)")
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
        self.set_status("Готово")

        self._build_menu()

        # Колбэки, которые вешает presenter
        self.on_open_rom: Callable[[], None] | None = None
        self.on_save_rom: Callable[[], None] | None = None
        self.on_show_editor: Callable[[], None] | None = None
        self.on_show_tools: Callable[[], None] | None = None

    def _build_menu(self) -> None:
        menu = self.menuBar()
        assert menu is not None

        file_menu = menu.addMenu("&File")
        assert file_menu is not None
        open_action = QAction("&Open ROM…", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self._emit_open)
        file_menu.addAction(open_action)

        save_action = QAction("&Save ROM", self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self._emit_save)
        file_menu.addAction(save_action)

        file_menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        view_menu = menu.addMenu("&View")
        assert view_menu is not None
        editor_action = QAction("&Editor", self)
        editor_action.triggered.connect(self._emit_show_editor)
        view_menu.addAction(editor_action)

        tools_action = QAction("&Tools", self)
        tools_action.triggered.connect(self._emit_show_tools)
        view_menu.addAction(tools_action)

    def show_editor(self) -> None:
        self._stack.setCurrentIndex(0)
        self.set_status("Editor")

    def show_tools(self) -> None:
        self._stack.setCurrentIndex(1)
        self.set_status("Tools")

    def set_status(self, text: str) -> None:
        self._status_line.show_info(text)

    def set_error(self, text: str) -> None:
        self._status_line.show_error(text)

    def clear_status(self) -> None:
        self._status_line.clear_message()

    def set_rom_path(self, path: str | None) -> None:
        title = "Subaru ECU Editor"
        if path:
            title = f"{title} — {path}"
        self.setWindowTitle(title)

    def ask_open_rom_path(self) -> str | None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open ROM",
            "",
            "ROM files (*.bin *.hex);;All files (*)",
        )
        return path or None

    def show_info(self, title: str, text: str) -> None:
        QMessageBox.information(self, title, text)

    def _emit_open(self) -> None:
        if self.on_open_rom:
            self.on_open_rom()

    def _emit_save(self) -> None:
        if self.on_save_rom:
            self.on_save_rom()

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
