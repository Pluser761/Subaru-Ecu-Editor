"""Заметки о прошивке (контекст для ИИ): просмотр и правка Markdown-текста. Только показ и ответ;
где и как хранится — решает презентер."""

from __future__ import annotations

from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout, QWidget

from src.i18n import tr


class NotesDialog(QDialog):
    def __init__(self, title: str, text: str, location: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Заметки о прошивке — {title}", title=title))
        self.resize(640, 520)

        hint = QLabel(
            tr(
                "Контекст для ИИ между разговорами: машина, топливо, моды, цели, что уже меняли и почему. "
                "ИИ читает эти заметки и дописывает в них сам; здесь их можно поправить."
            )
        )
        hint.setWordWrap(True)

        self._edit = QPlainTextEdit()
        self._edit.setPlainText(text)
        self._edit.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self._edit.setPlaceholderText(
            tr("Например: STI 2008, 98 октан, даунпайп и интейк, цель — 1.3 bar без детонации.")
        )

        where = QLabel(tr("Файл: {path}", path=location))
        where.setWordWrap(True)
        where.setStyleSheet("color: gray;")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(self._edit, 1)
        layout.addWidget(where)
        layout.addWidget(buttons)

    def text(self) -> str:
        return self._edit.toPlainText()
