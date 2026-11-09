"""Окно «MAF-скейлинг»: подбор скейлинга ДМРВ по живому логу, как в RomRaider.

Отдельное окно ОС (Cmd/Ctrl+M), чтобы держать его рядом с дашбордом логгера. Слева — фильтры,
интерполяция и запись в таблицу, справа — точки (напряжение ДМРВ × суммарная коррекция) и
подогнанная кривая. Только виджеты и сигналы: сбор точек, полином и множители — в
logger/core/maf.py, запись в прошивку — LoggerPresenter и MainPresenter.
"""

from __future__ import annotations

from collections.abc import Sequence

import pyqtgraph as pg
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.i18n import tr
from src.logger.core.maf import MafFilters

POINT_COLOR = "#5566bb"
CURVE_COLOR = "#d03030"
ORDERS = tuple(range(3, 21))
DEFAULT_ORDER = 10
VOLT_MAX = 5.5


def _spin(value: float, low: float, high: float, step: float, suffix: str, decimals: int = 2) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(low, high)
    box.setDecimals(decimals)
    box.setSingleStep(step)
    box.setValue(value)
    box.setSuffix(suffix)
    box.setMinimumWidth(84)
    return box


class MafWindow(QWidget):
    """Окно «MAF-скейлинг»: фильтры, полином и запись новых значений в открытую прошивку."""

    record_toggled = pyqtSignal(bool)
    interpolate_requested = pyqtSignal(int)  # степень полинома
    apply_requested = pyqtSignal(float, float)  # диапазон напряжения, В
    reset_requested = pyqtSignal()
    closed = pyqtSignal()  # окно закрыли: презентер останавливает запись точек

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.setWindowTitle(tr("MAF-скейлинг"))
        self.setMinimumSize(760, 520)
        self.resize(1000, 620)
        self._build()

    def closeEvent(self, event: QCloseEvent | None) -> None:
        self.closed.emit()
        super().closeEvent(event)

    # --- Сборка ---

    def _build(self) -> None:
        self._rpm_min = _spin(0, 0, 9000, 100, tr(" об/мин"), 0)
        self._rpm_max = _spin(4500, 0, 9000, 100, tr(" об/мин"), 0)
        self._maf_min = _spin(0, 0, 500, 10, tr(" г/с"), 1)
        self._maf_max = _spin(100, 0, 500, 10, tr(" г/с"), 1)
        self._ect_min = _spin(70, -40, 150, 5, tr(" °C"), 0)
        self._iat_max = _spin(100, -40, 150, 5, tr(" °C"), 0)
        self._rate_max = _spin(0.1, 0, 10, 0.1, tr(" В/с"))
        self._apply_min = _spin(1.20, 0, VOLT_MAX, 0.01, tr(" В"))
        self._apply_max = _spin(2.60, 0, VOLT_MAX, 0.01, tr(" В"))
        self._record = QCheckBox(tr("Записывать точки"))
        self._record.setToolTip(tr(
            "Брать точки из живого лога: только замкнутый контур, без tip-in, "
            "на установившемся режиме — как в RomRaider"
        ))
        self._record.toggled.connect(self.record_toggled)

        # Подписи слева, поля справа — так вся панель помещается в окно без сжатия полей
        filters = QGroupBox(tr("Фильтр данных"))
        grid = QGridLayout(filters)
        grid.setVerticalSpacing(4)
        self._add_range(grid, 0, tr("RPM"), self._rpm_min, self._rpm_max)
        self._add_range(grid, 1, tr("Расход воздуха"), self._maf_min, self._maf_max)
        grid.addWidget(QLabel(tr("ECT ≥")), 2, 0)
        grid.addWidget(self._ect_min, 2, 1, 1, 3)
        grid.addWidget(QLabel(tr("IAT ≤")), 3, 0)
        grid.addWidget(self._iat_max, 3, 1, 1, 3)
        grid.addWidget(QLabel(tr("Δ ДМРВ ≤")), 4, 0)
        grid.addWidget(self._rate_max, 4, 1, 1, 3)
        grid.addWidget(self._record, 5, 0, 1, 4)

        self._order = QComboBox()
        self._order.addItems([str(order) for order in ORDERS])
        self._order.setCurrentText(str(DEFAULT_ORDER))
        interpolate = QPushButton(tr("Интерполировать"))
        interpolate.setToolTip(tr("Подогнать полином по накопленным точкам"))
        interpolate.clicked.connect(
            lambda: self.interpolate_requested.emit(int(self._order.currentText()))
        )
        interp_box = QGroupBox(tr("Интерполяция"))
        interp_grid = QGridLayout(interp_box)
        interp_grid.addWidget(QLabel(tr("Степень полинома")), 0, 0)
        interp_grid.addWidget(self._order, 0, 1)
        interp_grid.addWidget(interpolate, 1, 0, 1, 2)

        update = QPushButton(tr("Обновить MAF"))
        update.setToolTip(tr("Умножить строки «MAF Sensor Scaling» на (1 + коррекция/100)"))
        update.clicked.connect(
            lambda: self.apply_requested.emit(self._apply_min.value(), self._apply_max.value())
        )
        reset = QPushButton(tr("Сбросить точки"))
        reset.clicked.connect(self.reset_requested)
        update_box = QGroupBox(tr("Обновить таблицу"))
        update_grid = QGridLayout(update_box)
        self._add_range(update_grid, 0, tr("Напряжение ДМРВ"), self._apply_min, self._apply_max)
        update_grid.addWidget(update, 1, 0, 1, 2)
        update_grid.addWidget(reset, 1, 2, 1, 2)

        hint = QLabel(tr(
            "Точки — напряжение ДМРВ и суммарная коррекция (A/F Correction + A/F Learning). "
            "Она ненулевая только в замкнутом контуре, поэтому таблица правится в малых и "
            "средних расходах (обычно 1.2–2.6 В); на полном газу нужна широкополосная лямбда. "
            "Данные и подключение — в окне логгера (Ctrl+L)."
        ))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #777; font-size: 11px;")

        self._info = QLabel("")
        self._info.setWordWrap(True)

        left = QWidget()
        left.setMaximumWidth(360)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(filters)
        left_layout.addWidget(interp_box)
        left_layout.addWidget(update_box)
        left_layout.addWidget(self._info)
        left_layout.addWidget(hint)
        left_layout.addStretch(1)

        self._plot = pg.PlotWidget(background="w")
        self._plot.setLabel("bottom", tr("ДМРВ, В"))
        self._plot.setLabel("left", tr("Коррекция, %"))
        self._plot.showGrid(x=True, y=True, alpha=0.3)
        self._plot.addItem(pg.InfiniteLine(0, angle=0, pen=pg.mkPen("#bbbbbb", width=1)))
        self._scatter = pg.ScatterPlotItem(size=4, brush=pg.mkBrush(POINT_COLOR), pen=None)
        self._plot.addItem(self._scatter)
        self._curve = self._plot.plot(pen=pg.mkPen(CURVE_COLOR, width=2))
        self._plot.setMouseEnabled(x=True, y=True)

        layout = QHBoxLayout(self)
        layout.addWidget(left)
        layout.addWidget(self._plot, 1)

    @staticmethod
    def _add_range(grid: QGridLayout, row: int, title: str, low: QDoubleSpinBox, high: QDoubleSpinBox) -> None:
        grid.addWidget(QLabel(title), row, 0)
        grid.addWidget(low, row, 1)
        grid.addWidget(QLabel("–"), row, 2)
        grid.addWidget(high, row, 3)

    # --- Для презентера ---

    def filters(self) -> MafFilters:
        """Текущие пороги фильтра: меняются на лету, точки пишутся по ним."""
        return MafFilters(
            rpm_min=self._rpm_min.value(),
            rpm_max=self._rpm_max.value(),
            maf_min=self._maf_min.value(),
            maf_max=self._maf_max.value(),
            ect_min=self._ect_min.value(),
            iat_max=self._iat_max.value(),
            mafv_rate_max=self._rate_max.value(),
        )

    def set_recording(self, on: bool) -> None:
        self._record.blockSignals(True)
        self._record.setChecked(on)
        self._record.blockSignals(False)

    def set_points(self, xs: Sequence[float], ys: Sequence[float]) -> None:
        self._scatter.setData(x=list(xs), y=list(ys))

    def set_curve(self, xs: Sequence[float] | None, ys: Sequence[float] | None) -> None:
        if xs is None or ys is None:
            self._curve.setData([], [])
        else:
            self._curve.setData(list(xs), list(ys))

    def clear(self) -> None:
        self._scatter.setData([], [])
        self.set_curve(None, None)
        self.set_info("")

    def set_info(self, text: str, error: bool = False) -> None:
        self._info.setStyleSheet("color: #c03030;" if error else "color: #555;")
        self._info.setText(text)
