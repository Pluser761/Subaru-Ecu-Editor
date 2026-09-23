from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QPainter, QPaintEvent
from PyQt6.QtWidgets import QSizePolicy, QWidget


class VerticalLabel(QWidget):
    """Текст, повёрнутый на 90° (читается снизу вверх) — заголовок оси строк."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = text
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:
        self._text = text
        self.setToolTip(text)
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        metrics = self.fontMetrics()
        return QSize(metrics.height() + 6, metrics.horizontalAdvance(self._text) + 6)

    def minimumSizeHint(self) -> QSize:
        return QSize(self.fontMetrics().height() + 6, 0)

    def paintEvent(self, event: QPaintEvent | None) -> None:
        painter = QPainter(self)
        painter.setPen(self.palette().windowText().color())
        painter.translate(0, self.height())
        painter.rotate(-90)
        # После поворота ширина и высота области меняются местами
        painter.drawText(0, 0, self.height(), self.width(), Qt.AlignmentFlag.AlignCenter, self._text)
        painter.end()
