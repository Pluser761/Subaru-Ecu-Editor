"""Ячейка таблицы карты: фон по значению, рамка checkpoint, подсказка, редактор ввода."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import QAbstractItemModel, QEvent, QModelIndex, Qt
from PyQt6.QtGui import QHelpEvent, QPainter
from PyQt6.QtWidgets import QAbstractItemView, QLineEdit, QStyledItemDelegate, QStyleOptionViewItem, QToolTip, QWidget

from src.ui.map.cells import DecimalInputValidator, draw_checkpoint_border, parse_cell_value
from src.ui.value_colors import cell_background

if TYPE_CHECKING:
    from src.ui.map.model import MapTableModel

# Насыщенность фона ячеек (доля цвета шкалы, остальное — белый): светлее, чем в 3D, чтобы
# чёрный текст читался на любом цвете
CELL_COLOR_STRENGTH = 0.45


class CellDelegate(QStyledItemDelegate):
    def __init__(self, model: MapTableModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model = model

    def paint(self, painter: QPainter | None, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        if painter is None:
            return
        row, col = index.row(), index.column()
        # Фон — здесь, а не в initStyleOption: та вызывается и при каждом пересчёте высоты
        # строк (sizeHint). Выделение стиль рисует поверх заливки; inf — серый
        painter.fillRect(option.rect, cell_background(self._model.value_position(row, col), CELL_COLOR_STRENGTH))
        super().paint(painter, option, index)
        diff = self._model.diff(row, col)
        if diff:
            draw_checkpoint_border(painter, option.rect, diff)

    def helpEvent(
        self,
        event: QHelpEvent | None,
        view: QAbstractItemView | None,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> bool:
        """Подсказка ячейки: значение checkpoint и изменение; без изменения — ничего."""
        if event is None or event.type() != QEvent.Type.ToolTip:
            return super().helpEvent(event, view, option, index)
        text = self._model.tooltip(index.row(), index.column())
        if text:
            QToolTip.showText(event.globalPos(), text, view)
        else:
            QToolTip.hideText()
        return True

    def createEditor(self, parent: QWidget | None, option: QStyleOptionViewItem, index: QModelIndex) -> QWidget:
        editor = QLineEdit(parent)
        editor.setValidator(DecimalInputValidator(editor))
        editor.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return editor

    def setEditorData(self, editor: QWidget | None, index: QModelIndex) -> None:
        if isinstance(editor, QLineEdit):
            editor.setText(str(index.data(Qt.ItemDataRole.EditRole) or ""))
            editor.selectAll()

    def setModelData(self, editor: QWidget | None, model: QAbstractItemModel | None, index: QModelIndex) -> None:
        if not isinstance(editor, QLineEdit) or model is None:
            return
        try:
            value = parse_cell_value(editor.text().strip())
        except ValueError:
            return
        model.setData(index, value, Qt.ItemDataRole.EditRole)
