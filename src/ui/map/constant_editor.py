from __future__ import annotations

import math

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from src.core.checkpoint import diff_sign
from src.ui.map.cells import (
    CHECKPOINT_BORDER_WIDTH,
    CHECKPOINT_DOWN_COLOR,
    CHECKPOINT_UP_COLOR,
    DecimalInputValidator,
    change_tooltip,
    format_cell_value,
    parse_cell_value,
)

INPUT_WIDTH = 180


class ConstantEditor(QWidget):
    """Редактор константы (таблица 1×1): подпись и поле ввода в белой рамке по центру.

    Рамка поля относительно checkpoint — как у ячеек таблицы: красная — больше, синяя — меньше.
    """

    # Введённое значение (до квантования; реально сохранённое вернётся через set_display)
    value_changed = pyqtSignal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._value = 0.0
        self._checkpoint = 0.0

        self._caption = QLabel()
        self._caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._caption.setStyleSheet("color: #444; font-weight: 600;")

        self._input = QLineEdit()
        self._input.setValidator(DecimalInputValidator(self._input))
        self._input.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._input.setFixedWidth(INPUT_WIDTH)
        self._input.editingFinished.connect(self._commit)

        # Белый бокс как у таблицы: та же рамка StyledPanel и фон ячеек (Base)
        box = QFrame()
        box.setFrameShape(QFrame.Shape.StyledPanel)
        box.setAutoFillBackground(True)
        palette = box.palette()
        palette.setColor(QPalette.ColorRole.Window, palette.color(QPalette.ColorRole.Base))
        box.setPalette(palette)
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(24, 16, 24, 20)
        box_layout.setSpacing(8)
        box_layout.addWidget(self._caption)
        box_layout.addWidget(self._input, alignment=Qt.AlignmentFlag.AlignHCenter)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(box)
        row.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(2)

    @property
    def input(self) -> QLineEdit:
        """Поле ввода — в фокусе при правке (для перехвата клавиш шага)."""
        return self._input

    def set_constant(self, value: float, caption: str, checkpoint: float | None = None) -> None:
        self._caption.setText(caption)
        self._checkpoint = value if checkpoint is None else checkpoint
        self.set_display(value)

    def set_display(self, value: float) -> None:
        """Показать значение без сигнала value_changed (например, после квантования)."""
        self._value = value
        self._input.setText(format_cell_value(value))
        self._update_border()

    def value(self) -> float:
        return self._value

    def checkpoint_value(self) -> float:
        return self._checkpoint

    def set_checkpoint(self) -> None:
        self._checkpoint = self._value
        self._update_border()

    def adjust(self, delta: float) -> bool:
        """Прибавить delta (клавиши шага); False — значение не число."""
        if not math.isfinite(self._value):
            return False
        # Округляем как при отображении, чтобы не копить ошибку float
        self._apply(round(self._value + delta, 2))
        return True

    def _commit(self) -> None:
        try:
            value = parse_cell_value(self._input.text())
        except ValueError:
            self.set_display(self._value)
            return
        if format_cell_value(value) == format_cell_value(self._value):
            self.set_display(self._value)
            return
        self._apply(value)

    def _apply(self, value: float) -> None:
        self.set_display(value)
        self.value_changed.emit(value)

    def _update_border(self) -> None:
        diff = diff_sign(self._value, self._checkpoint)
        if diff == 0:
            self._input.setStyleSheet("")
            self._input.setToolTip("")
            return
        color = CHECKPOINT_UP_COLOR if diff > 0 else CHECKPOINT_DOWN_COLOR
        self._input.setStyleSheet(
            f"QLineEdit {{ border: {CHECKPOINT_BORDER_WIDTH}px solid {color.name()}; padding: 2px; }}"
        )
        self._input.setToolTip(change_tooltip(self._value, self._checkpoint))
