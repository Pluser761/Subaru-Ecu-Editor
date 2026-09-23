from __future__ import annotations

from PyQt6.QtWidgets import QLabel, QSizePolicy


class StatusLine(QLabel):
    """Нижняя строка состояния: обычные сообщения и ошибки."""

    _INFO_STYLE = "color: #333; padding: 2px 6px;"
    _ERROR_STYLE = (
        "color: #8b0000; background: #ffe8e8; padding: 2px 6px; font-weight: 600;"
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumHeight(22)
        self.clear_message()

    def show_info(self, text: str) -> None:
        self.setStyleSheet(self._INFO_STYLE)
        self.setText(text)

    def show_error(self, text: str) -> None:
        self.setStyleSheet(self._ERROR_STYLE)
        self.setText(text)

    def clear_message(self) -> None:
        self.setStyleSheet(self._INFO_STYLE)
        self.setText("")
