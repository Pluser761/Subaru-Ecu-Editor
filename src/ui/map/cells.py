"""Общее для ячеек, заголовков и поля константы: формат чисел, ввод, цвета checkpoint,
текст подсказки об изменении."""

from __future__ import annotations

import math

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QColor, QPainter, QPen, QValidator

from src.core.checkpoint import EPSILON
from src.core.matrix_clipboard import parse_number

STATIC_AXIS_MESSAGE = "Статическая ось задана в definition и не редактируется"
NON_NUMERIC_CLIPBOARD_MESSAGE = "Буфер обмена содержит нечисловые данные"
CLIPBOARD_UNAVAILABLE_MESSAGE = "Буфер обмена недоступен"

# Рамка относительно checkpoint: выросло — красная, уменьшилось — синяя
CHECKPOINT_UP_COLOR = QColor("#d93025")
CHECKPOINT_DOWN_COLOR = QColor("#1a66d9")
CHECKPOINT_BORDER_WIDTH = 2


def format_cell_value(value: float) -> str:
    """Всегда два знака после точки."""
    return f"{value:.2f}"


def parse_cell_value(text: str) -> float:
    """Принимает и точку, и запятую как десятичный разделитель."""
    return parse_number(text)


def checkpoint_color(diff: int) -> QColor:
    return CHECKPOINT_UP_COLOR if diff > 0 else CHECKPOINT_DOWN_COLOR


def draw_checkpoint_border(painter: QPainter, rect: QRect, diff: int) -> None:
    """Рамка изменения относительно checkpoint — целиком внутри rect, чтобы соседние
    ячейки не перекрывали друг друга."""
    painter.save()
    # После стандартной отрисовки заголовка у painter бывает пустой clip — задаём свой
    painter.setClipRect(rect)
    pen = QPen(checkpoint_color(diff), CHECKPOINT_BORDER_WIDTH)
    pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    half = CHECKPOINT_BORDER_WIDTH // 2
    painter.drawRect(rect.adjusted(half, half, -half - 1, -half - 1))
    painter.restore()


def change_tooltip(value: float, base: float, note: str = "") -> str:
    """Текст подсказки об изменении относительно checkpoint (два знака, как в ячейках).

    note — пояснение к значению checkpoint (например, что оно интерполировано).
    """
    lines = [f"Checkpoint: {format_cell_value(base)}" + (f" ({note})" if note else "")]
    delta = value - base
    if math.isfinite(delta):
        change = f"Изменение: {delta:+.2f}"
        if math.isfinite(base) and abs(base) >= EPSILON:
            change += f" ({delta / abs(base) * 100:+.1f}%)"
        lines.append(change)
    return "\n".join(lines)


class DecimalInputValidator(QValidator):
    """Цифры, один разделитель (точка или запятая) и минус в начале."""

    def validate(self, text: str | None, pos: int) -> tuple[QValidator.State, str, int]:
        text = text or ""
        body = text[1:] if text.startswith("-") else text

        for ch in body:
            if not (ch.isdigit() or ch in ".,"):
                return QValidator.State.Invalid, text, pos

        if body.count(".") + body.count(",") > 1:
            return QValidator.State.Invalid, text, pos

        # «», «-», «.», «-,» — ещё набирают число
        if not any(ch.isdigit() for ch in body):
            return QValidator.State.Intermediate, text, pos

        return QValidator.State.Acceptable, text, pos
