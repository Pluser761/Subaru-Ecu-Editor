from __future__ import annotations

from PyQt6.QtWidgets import QDoubleSpinBox, QHBoxLayout, QLabel, QWidget

from src.i18n import tr

DEFAULT_FINE_STEP = 0.1
DEFAULT_COARSE_STEP = 1.0
# Ячейки хранятся с двумя знаками — шаг мельче 0.01 ничего бы не менял
MIN_STEP = 0.01


class StepControls(QWidget):
    """Поля мелкого и крупного шага изменения ячеек.

    При открытии карты подставляются fineincrement / coarseincrement из definition;
    пользователь может поменять их вручную до открытия следующей карты.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._fine = self._make_spin(DEFAULT_FINE_STEP)
        self._coarse = self._make_spin(DEFAULT_COARSE_STEP)
        self._fine.setToolTip(tr("Применяется с зажатым Control"))
        self._coarse.setToolTip(tr("Применяется без модификаторов"))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._caption = QLabel()
        self.set_axis_mode(False)
        layout.addWidget(self._caption)
        layout.addWidget(self._fine)
        layout.addWidget(QLabel(tr("крупный")))
        layout.addWidget(self._coarse)
        layout.addStretch(1)

    @staticmethod
    def _make_spin(value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(2)
        spin.setRange(MIN_STEP, 100000.0)
        spin.setValue(value)
        spin.setKeyboardTracking(False)
        return spin

    @property
    def fine_step(self) -> float:
        return self._fine.value()

    @property
    def coarse_step(self) -> float:
        return self._coarse.value()

    def set_axis_mode(self, axis: bool) -> None:
        """Подпись: шаги значений таблицы или шаги выделенной оси."""
        self._caption.setText(tr("Шаг оси: мелкий (Ctrl)") if axis else tr("Шаг: мелкий (Ctrl)"))

    def set_decimals(self, decimals: int) -> None:
        """Точность полей шага — как у значений на экране (bar — 3 знака)."""
        for spin in (self._fine, self._coarse):
            spin.setDecimals(decimals)
            spin.setMinimum(10.0 ** -decimals)

    def set_increments(self, fine: float | None, coarse: float | None) -> None:
        """Шаги из definition таблицы; если не заданы — остаются текущие."""
        if fine is not None:
            self._fine.setValue(max(fine, self._fine.minimum()))
        if coarse is not None:
            self._coarse.setValue(max(coarse, self._coarse.minimum()))
