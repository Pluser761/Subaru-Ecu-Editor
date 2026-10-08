from __future__ import annotations

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtGui import QFocusEvent, QKeyEvent
from PyQt6.QtWidgets import QPushButton, QWidget

from src.i18n import N_, tr
from src.ui.key_bindings import MODIFIER_KEYS, key_name

CAPTURE_PROMPT = N_("Нажмите клавишу… (Esc — отмена)")


class KeyCaptureButton(QPushButton):
    """Кнопка назначения клавиши: клик → ждём нажатие → запоминаем клавишу.

    Модификаторы не записываются (Control выбирает шаг), Esc отменяет,
    потеря фокуса тоже отменяет.
    """

    key_captured = pyqtSignal(int)

    def __init__(self, key: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._key = key
        self._capturing = False
        self.setCheckable(True)
        self.setMinimumWidth(220)
        self.clicked.connect(self._start_capture)
        self._refresh()

    @property
    def key(self) -> int:
        return self._key

    def set_key(self, key: int) -> None:
        self._key = key
        self._refresh()

    def _refresh(self) -> None:
        self.setChecked(self._capturing)
        self.setText(tr(CAPTURE_PROMPT) if self._capturing else key_name(self._key))

    def _start_capture(self) -> None:
        self._capturing = True
        self._refresh()
        self.setFocus()
        # Забираем клавиатуру, чтобы нажатие не ушло в диалог (Enter/Esc/Tab)
        self.grabKeyboard()

    def _stop_capture(self) -> None:
        if not self._capturing:
            return
        self._capturing = False
        self.releaseKeyboard()
        self._refresh()

    def event(self, e: QEvent | None) -> bool:
        # Tab/Backtab обрабатываются до keyPressEvent (переход фокуса) — ловим раньше
        if self._capturing and isinstance(e, QKeyEvent) and e.type() == QEvent.Type.KeyPress:
            self._on_capture_key(e)
            return True
        return super().event(e)

    def _on_capture_key(self, e: QKeyEvent) -> None:
        key = e.key()
        if key in MODIFIER_KEYS:
            return  # ждём основную клавишу
        if key == Qt.Key.Key_Escape.value:
            self._stop_capture()
            return
        self._key = key
        self._stop_capture()
        self.key_captured.emit(key)

    def focusOutEvent(self, e: QFocusEvent | None) -> None:
        self._stop_capture()
        super().focusOutEvent(e)
