"""Модель таблицы карты: числа, оси, checkpoint. Источник правды для таблицы и графика —
значения хранятся числами, а не текстом ячеек."""

from __future__ import annotations

import math

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, pyqtSignal

from src.core.checkpoint import baseline, diff_sign, epsilon_for
from src.core.models import CalibrationTable, Checkpoint, Scaling
from src.ui.map.cells import change_tooltip, format_cell_value, parse_cell_value

_ALIGN_RIGHT = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)


class MapTableModel(QAbstractTableModel):
    """Значения карты [row][col] (row — ось Y, col — ось X, как на экране) и её оси.

    setData / setHeaderData — правка пользователем: сохраняют значение и сообщают о нём
    (cell_edited / axis_edited). set_value / set_axis_value — показать значение без сигнала
    правки (то, что реально легло в ROM после квантования).
    """

    cell_edited = pyqtSignal(int, int, float)  # row, col, value
    axis_edited = pyqtSignal(Qt.Orientation, int, float)  # ориентация заголовка, index, value

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._values: list[list[float]] = []
        self._axes: dict[Qt.Orientation, list[float]] = {Qt.Orientation.Horizontal: [], Qt.Orientation.Vertical: []}
        # Подписи статических осей («1st», «Enable Above»); None — ось числовая, редактируемая
        self._labels: dict[Qt.Orientation, list[str] | None] = {
            Qt.Orientation.Horizontal: None,
            Qt.Orientation.Vertical: None,
        }
        self._checkpoint: Checkpoint | None = None
        # Знаков после точки на экране: значения и оси (bar — 3)
        self._decimals = 2
        self._axis_decimals = {Qt.Orientation.Horizontal: 2, Qt.Orientation.Vertical: 2}
        # Как сравнивать с checkpoint после сдвига осей: формула значений и расчёт ЭБУ
        self._scaling: Scaling | None = None
        self._ecu_interpolation = False
        # Кэши: checkpoint в точках текущих осей и (min, max) для цвета; None — пересчитать
        self._baseline: list[list[float]] | None = None
        self._range: tuple[float, float] | None = None

    # --- Загрузка ---

    def load(self, table: CalibrationTable | None) -> None:
        """Показать карту; её значения становятся checkpoint."""
        self.beginResetModel()
        if table is None:
            self._values = []
            self._axes = {Qt.Orientation.Horizontal: [], Qt.Orientation.Vertical: []}
            self._labels = {Qt.Orientation.Horizontal: None, Qt.Orientation.Vertical: None}
        else:
            rows, cols = table.rows, table.cols
            self._values = [list(row) for row in table.values]
            self._axes = {
                Qt.Orientation.Horizontal: _axis_or_index(table.x_axis, cols),
                Qt.Orientation.Vertical: _axis_or_index(table.y_axis, rows),
            }
            self._labels = {
                Qt.Orientation.Horizontal: _labels_if_static(table.x_labels, cols),
                Qt.Orientation.Vertical: _labels_if_static(table.y_labels, rows),
            }
        self._scaling = table.scaling if table is not None else None
        self._decimals = table.decimals if table is not None else 2
        self._axis_decimals = {
            Qt.Orientation.Horizontal: table.x_decimals if table is not None else 2,
            Qt.Orientation.Vertical: table.y_decimals if table is not None else 2,
        }
        self._ecu_interpolation = table.ecu_interpolation if table is not None else False
        self._checkpoint = None
        self._invalidate()
        self.endResetModel()
        self.set_checkpoint()

    # --- QAbstractTableModel ---

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._values)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() or not self._values else len(self._values[0])

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid():
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return format_cell_value(self._values[index.row()][index.column()], self._decimals)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return _ALIGN_RIGHT
        return None

    def setData(self, index: QModelIndex, value: object, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid() or role != Qt.ItemDataRole.EditRole:
            return False
        number = _to_float(value)
        if number is None:
            return False
        self.set_value(index.row(), index.column(), number)
        self.cell_edited.emit(index.row(), index.column(), number)
        return True

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return None
        labels = self._labels[orientation]
        if labels is not None:
            return labels[section] if 0 <= section < len(labels) else ""
        axis = self._axes[orientation]
        return format_cell_value(axis[section], self._axis_decimals[orientation]) if 0 <= section < len(axis) else ""

    def setHeaderData(
        self, section: int, orientation: Qt.Orientation, value: object, role: int = Qt.ItemDataRole.EditRole
    ) -> bool:
        if role != Qt.ItemDataRole.EditRole or self.is_static(orientation):
            return False
        number = _to_float(value)
        if number is None or not 0 <= section < len(self._axes[orientation]):
            return False
        self.set_axis_value(orientation, section, number)
        self.axis_edited.emit(orientation, section, number)
        return True

    # --- Значения ---

    def values(self) -> list[list[float]]:
        return [list(row) for row in self._values]

    def value(self, row: int, col: int) -> float:
        return self._values[row][col]

    def set_value(self, row: int, col: int, value: float) -> None:
        """Показать значение без сигнала правки."""
        self._values[row][col] = value
        self._range = None
        index = self.index(row, col)
        self.dataChanged.emit(index, index)

    def axis(self, orientation: Qt.Orientation) -> list[float]:
        """Значения оси (у статической — её числа из definition, для графика)."""
        return list(self._axes[orientation])

    def set_axis_value(self, orientation: Qt.Orientation, index: int, value: float) -> None:
        """Показать значение точки оси без сигнала правки."""
        self._axes[orientation][index] = value
        self._baseline = None
        self.headerDataChanged.emit(orientation, index, index)

    @property
    def decimals(self) -> int:
        return self._decimals

    def axis_decimals(self, orientation: Qt.Orientation) -> int:
        return self._axis_decimals[orientation]

    def labels(self, orientation: Qt.Orientation) -> list[str] | None:
        """Подписи статической оси или None, если ось числовая и редактируемая."""
        labels = self._labels[orientation]
        return list(labels) if labels is not None else None

    def is_static(self, orientation: Qt.Orientation) -> bool:
        return self._labels[orientation] is not None

    # --- Checkpoint ---

    def checkpoint(self) -> Checkpoint:
        if self._checkpoint is None:
            return Checkpoint(values=[])
        return self._checkpoint.model_copy(deep=True)

    def set_checkpoint(self) -> None:
        """Текущие значения и оси — точка отсчёта; изменений больше нет."""
        self.restore_checkpoint(
            Checkpoint(
                values=self.values(),
                x_axis=self.axis(Qt.Orientation.Horizontal),
                y_axis=self.axis(Qt.Orientation.Vertical),
            )
        )

    def restore_checkpoint(self, checkpoint: Checkpoint) -> None:
        """Восстановить сохранённый checkpoint. Часть, не совпавшая по размеру с таблицей,
        берётся из текущих значений — без изменений."""
        rows, cols = self.rowCount(), self.columnCount()
        values = checkpoint.values
        if len(values) != rows or any(len(r) != cols for r in values):
            values = self.values()
        x_axis = checkpoint.x_axis if len(checkpoint.x_axis) == cols else self.axis(Qt.Orientation.Horizontal)
        y_axis = checkpoint.y_axis if len(checkpoint.y_axis) == rows else self.axis(Qt.Orientation.Vertical)
        self._checkpoint = Checkpoint(values=[list(r) for r in values], x_axis=list(x_axis), y_axis=list(y_axis))
        self._baseline = None
        self._refresh_all()

    def diff(self, row: int, col: int) -> int:
        """+1 — ячейка выросла относительно checkpoint (с учётом сдвига осей), -1 — уменьшилась."""
        base = self._checkpoint_baseline()
        if row >= len(base) or col >= len(base[row]):
            return 0
        return diff_sign(self._values[row][col], base[row][col], epsilon_for(self._decimals))

    def axis_diff(self, orientation: Qt.Orientation, index: int) -> int:
        """То же для точки оси; у статических осей изменений не бывает."""
        if self._checkpoint is None or self.is_static(orientation):
            return 0
        horizontal = orientation == Qt.Orientation.Horizontal
        base_axis = self._checkpoint.x_axis if horizontal else self._checkpoint.y_axis
        if not 0 <= index < len(base_axis):
            return 0
        return diff_sign(self._axes[orientation][index], base_axis[index], epsilon_for(self._axis_decimals[orientation]))

    def tooltip(self, row: int, col: int) -> str:
        """«Checkpoint: … / Изменение: …» для изменённой ячейки, иначе пустая строка."""
        if self._checkpoint is None or self.diff(row, col) == 0:
            return ""
        note = ""
        if self.axis_diff(Qt.Orientation.Horizontal, col) or self.axis_diff(Qt.Orientation.Vertical, row):
            was = format_cell_value(self._checkpoint.values[row][col], self._decimals)
            note = f"по осям checkpoint; в ячейке было {was}"
        return change_tooltip(self._values[row][col], self._checkpoint_baseline()[row][col], note, self._decimals)

    # --- Цвет ---

    def value_position(self, row: int, col: int) -> float:
        """Место значения на шкале цвета: 0 — минимум таблицы, 1 — максимум, nan — inf/nan.
        Если все значения равны — середина шкалы."""
        value = self._values[row][col]
        if not math.isfinite(value):
            return math.nan
        lo, hi = self._value_range()
        return (value - lo) / (hi - lo) if hi > lo else 0.5

    # --- Внутреннее ---

    def _checkpoint_baseline(self) -> list[list[float]]:
        if self._checkpoint is None:
            return []
        if self._baseline is None:
            self._baseline = baseline(
                self._checkpoint,
                self.axis(Qt.Orientation.Horizontal),
                self.axis(Qt.Orientation.Vertical),
                self._scaling,
                ecu=self._ecu_interpolation,
            )
        return self._baseline

    def _value_range(self) -> tuple[float, float]:
        if self._range is None:
            finite = [v for row in self._values for v in row if math.isfinite(v)]
            self._range = (min(finite), max(finite)) if finite else (0.0, 0.0)
        return self._range

    def _invalidate(self) -> None:
        self._baseline = None
        self._range = None

    def _refresh_all(self) -> None:
        """Перерисовать всё: рамки checkpoint и цвет зависят от всей таблицы."""
        if self.rowCount() and self.columnCount():
            self.dataChanged.emit(self.index(0, 0), self.index(self.rowCount() - 1, self.columnCount() - 1))
        for orientation, count in ((Qt.Orientation.Horizontal, self.columnCount()), (Qt.Orientation.Vertical, self.rowCount())):
            if count:
                self.headerDataChanged.emit(orientation, 0, count - 1)


def _to_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return parse_cell_value(str(value))
    except ValueError:
        return None


def _axis_or_index(axis: list[float], count: int) -> list[float]:
    return list(axis) if len(axis) == count else [float(i) for i in range(count)]


def _labels_if_static(labels: list[str] | None, count: int) -> list[str] | None:
    return list(labels) if labels is not None and len(labels) == count else None
