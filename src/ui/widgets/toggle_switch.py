"""Тумблер в стиле iOS: дорожка со скруглением и бегунок с плавным переездом."""

from __future__ import annotations

from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt, pyqtProperty  # type: ignore[attr-defined]
from PyQt6.QtGui import QColor, QPainter, QPaintEvent
from PyQt6.QtWidgets import QAbstractButton, QWidget

ON_COLOR = QColor("#34c759")
OFF_COLOR = QColor("#c7c7cc")
DISABLED_COLOR = QColor("#e5e5ea")
KNOB_COLOR = QColor("#ffffff")
TRACK_SIZE = QSize(44, 26)
KNOB_MARGIN = 3
ANIMATION_MS = 140


class ToggleSwitch(QAbstractButton):
    """Флажок с видом тумблера: checked — включено. Сигналы как у QAbstractButton (toggled)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(TRACK_SIZE)
        # Положение бегунка 0…1 — анимируется между выкл. и вкл.
        self._position = 0.0
        self._animation = QPropertyAnimation(self, b"position", self)
        self._animation.setDuration(ANIMATION_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.toggled.connect(self._animate)

    def sizeHint(self) -> QSize:
        return TRACK_SIZE

    def set_checked_silently(self, checked: bool) -> None:
        """Показать состояние без сигнала toggled и без анимации (загрузка, откат)."""
        self.blockSignals(True)
        self.setChecked(checked)
        self.blockSignals(False)
        self._animation.stop()
        self._position = 1.0 if checked else 0.0
        self.update()

    def _get_position(self) -> float:
        return self._position

    def _set_position(self, value: float) -> None:
        self._position = value
        self.update()

    position = pyqtProperty(float, fget=_get_position, fset=_set_position)

    def _animate(self, checked: bool) -> None:
        self._animation.stop()
        self._animation.setStartValue(self._position)
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()

    def paintEvent(self, e: QPaintEvent | None) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        radius = rect.height() / 2
        if not self.isEnabled():
            track = DISABLED_COLOR
        else:
            # Цвет дорожки плавно переходит вместе с бегунком
            t = self._position
            track = QColor(
                round(OFF_COLOR.red() + (ON_COLOR.red() - OFF_COLOR.red()) * t),
                round(OFF_COLOR.green() + (ON_COLOR.green() - OFF_COLOR.green()) * t),
                round(OFF_COLOR.blue() + (ON_COLOR.blue() - OFF_COLOR.blue()) * t),
            )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(rect, radius, radius)

        knob = rect.height() - 2 * KNOB_MARGIN
        x = KNOB_MARGIN + (rect.width() - knob - 2 * KNOB_MARGIN) * self._position
        painter.setBrush(QColor(0, 0, 0, 40))
        painter.drawEllipse(QRectF(x, KNOB_MARGIN + 1, knob, knob))  # мягкая тень
        painter.setBrush(KNOB_COLOR)
        painter.drawEllipse(QRectF(x, KNOB_MARGIN, knob, knob))
        painter.end()
