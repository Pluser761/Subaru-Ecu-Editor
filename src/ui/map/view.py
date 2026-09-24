"""Таблица карты: QTableView поверх MapTableModel — выделение, шаг клавишами, буфер обмена."""

from __future__ import annotations

import math

from PyQt6.QtCore import QEvent, QItemSelection, QItemSelectionModel, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QKeyEvent, QKeySequence, QPalette
from PyQt6.QtWidgets import QApplication, QHeaderView, QTableView, QWidget

from src.core.matrix_clipboard import format_matrix, matrix_shape, parse_matrix, validate_paste_size, validate_rectangular
from src.core.models import CalibrationTable, Checkpoint
from src.ui.map.cells import CLIPBOARD_UNAVAILABLE_MESSAGE, NON_NUMERIC_CLIPBOARD_MESSAGE, format_cell_value
from src.ui.map.delegate import CellDelegate
from src.ui.map.header import AxisHeaderView
from src.ui.map.model import MapTableModel

# Выделение нейтральным цветом: системный синий сливается с синей рамкой checkpoint
SELECTION_BG_COLOR = QColor("#d5dbe4")
SELECTION_TEXT_COLOR = QColor("#000000")
# Запас к ширине текста в столбце и к высоте строки: отступы ячейки и рамка checkpoint
COLUMN_TEXT_PADDING = 10
ROW_PADDING = 8


class MapTableView(QTableView):
    """Сетка значений карты в экранных координатах (row — строка на экране).

    Правки пользователя (ввод, шаг, вставка — ячеек и осей) приходят сигналами
    cell_value_changed / x_axis_changed / y_axis_changed; set_cell_display / set_axis_display
    показывают значение, реально легшее в ROM, без сигнала.
    """

    cell_value_changed = pyqtSignal(int, int, float)  # row, col, value
    x_axis_changed = pyqtSignal(int, float)  # col, value
    y_axis_changed = pyqtSignal(int, float)  # row, value
    matrix_copied = pyqtSignal(int, int)  # rows, cols
    matrix_pasted = pyqtSignal(int, int)  # rows, cols
    # Выделение точек оси (Ctrl+клик по заголовку) появилось, сменилось или снято
    axis_selection_changed = pyqtSignal()
    # Выделение ячеек изменилось (для графика)
    selection_changed = pyqtSignal()
    operation_error = pyqtSignal(str)
    operation_info = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model = MapTableModel(self)
        self.setModel(self._model)
        self.setItemDelegate(CellDelegate(self._model, self))
        # Чередование строк не нужно: фон ячеек окрашен по значению
        self.setAlternatingRowColors(False)
        palette = self.palette()
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
            palette.setColor(group, QPalette.ColorRole.Highlight, SELECTION_BG_COLOR)
            palette.setColor(group, QPalette.ColorRole.HighlightedText, SELECTION_TEXT_COLOR)
        self.setPalette(palette)

        self._h_header = AxisHeaderView(Qt.Orientation.Horizontal, self)
        self._v_header = AxisHeaderView(Qt.Orientation.Vertical, self)
        self.setHorizontalHeader(self._h_header)
        self.setVerticalHeader(self._v_header)
        self._h_header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        # Строки одной высоты: не ResizeToContents — тот на каждую правку ячейки пересчитывает
        # высоту всех строк, опрашивая модель о каждой ячейке (шаг по таблице — секунда)
        self._v_header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self._v_header.setDefaultSectionSize(self.fontMetrics().height() + ROW_PADDING)
        tooltip = (
            "Двойной клик — изменить ось. Ctrl+клик — выделить точку оси (шаг меняет ось). "
            "Ctrl+C/V — копировать/вставить ось"
        )
        for header in self._headers():
            header.setToolTip(tooltip)
            header.axis_selection_changed.connect(self.axis_selection_changed.emit)

        self._model.cell_edited.connect(self.cell_value_changed.emit)
        self._model.axis_edited.connect(self._on_axis_edited)
        # Цвет и рамки зависят от всей таблицы (min/max, сдвиг осей) — перерисовываем целиком
        self._model.dataChanged.connect(self._repaint)
        self._model.headerDataChanged.connect(self._repaint)
        selection = self.selectionModel()
        if selection is not None:
            selection.selectionChanged.connect(self._on_selection_changed)

    @property
    def map_model(self) -> MapTableModel:
        return self._model

    # --- Данные ---

    def set_table(self, table: CalibrationTable | None) -> None:
        """Показать карту (в экранных координатах); её значения становятся checkpoint."""
        self.clear_axis_selection()
        self._model.load(table)
        self._fit_min_column_width()

    def rowCount(self) -> int:
        return self._model.rowCount()

    def columnCount(self) -> int:
        return self._model.columnCount()

    def values(self) -> list[list[float]]:
        return self._model.values()

    def x_axis(self) -> list[float]:
        return self._model.axis(Qt.Orientation.Horizontal)

    def y_axis(self) -> list[float]:
        return self._model.axis(Qt.Orientation.Vertical)

    def axis_labels(self, orientation: Qt.Orientation) -> list[str] | None:
        return self._model.labels(orientation)

    def set_cell_display(self, row: int, col: int, value: float) -> None:
        self._model.set_value(row, col, value)

    def set_axis_display(self, orientation: Qt.Orientation, index: int, value: float) -> None:
        self._model.set_axis_value(orientation, index, value)

    def checkpoint(self) -> Checkpoint:
        return self._model.checkpoint()

    def set_checkpoint(self) -> None:
        self._model.set_checkpoint()

    def restore_checkpoint(self, checkpoint: Checkpoint) -> None:
        self._model.restore_checkpoint(checkpoint)

    # --- Выделение ---

    def selected_cells(self) -> set[tuple[int, int]]:
        return {(index.row(), index.column()) for index in self.selectedIndexes()}

    def select_cell(self, row: int, col: int, additive: bool) -> None:
        """Выделить ячейку (клик по точке графика); additive — добавить/снять в выделении."""
        index = self._model.index(row, col)
        selection = self.selectionModel()
        if additive and selection is not None:
            selection.select(
                QItemSelection(index, index),
                QItemSelectionModel.SelectionFlag.Toggle,
            )
        else:
            self.setCurrentIndex(index)
        self.scrollTo(index)

    def clear_axis_selection(self, except_header: AxisHeaderView | None = None) -> None:
        for header in self._headers():
            if header is not except_header:
                header.clear_axis_selection()

    def axis_selection_orientation(self) -> Qt.Orientation | None:
        """Заголовок, в котором выделены точки оси, или None."""
        for header in self._headers():
            if header.axis_selection:
                return header.orientation()
        return None

    def has_axis_selection(self) -> bool:
        return self.axis_selection_orientation() is not None

    # --- Шаг клавишами ---

    def adjust_selected(self, delta: float) -> int:
        """Прибавить delta к выделенным ячейкам (или текущей); вернуть число изменённых."""
        cells = sorted(self.selected_cells())
        current = self.currentIndex()
        if not cells and current.isValid():
            cells = [(current.row(), current.column())]
        changed = 0
        for row, col in cells:
            value = self._model.value(row, col)
            if not math.isfinite(value):
                continue
            # Округляем как при отображении, чтобы не копить ошибку float
            self._model.setData(self._model.index(row, col), round(value + delta, 2))
            changed += 1
        return changed

    def adjust_axis_selection(self, delta: float) -> int:
        """Прибавить delta к выделенным точкам оси; вернуть число изменённых."""
        for header in self._headers():
            sections = header.axis_selection
            if not sections:
                continue
            axis = self._model.axis(header.orientation())
            changed = 0
            for section in sections:
                if 0 <= section < len(axis) and math.isfinite(axis[section]):
                    self._model.setHeaderData(section, header.orientation(), round(axis[section] + delta, 2))
                    changed += 1
            return changed
        return 0

    # --- Буфер обмена ---

    def event(self, e: QEvent | None) -> bool:
        # Перехватываем до стандартного копирования/вставки QAbstractItemView
        if isinstance(e, QKeyEvent) and e.type() == QEvent.Type.ShortcutOverride and (_is_copy(e) or _is_paste(e)):
            e.accept()
            return True
        return super().event(e)

    def keyPressEvent(self, e: QKeyEvent | None) -> None:
        if e is not None and _is_copy(e):
            self.copy_selection()
            e.accept()
            return
        if e is not None and _is_paste(e):
            self.paste_selection()
            e.accept()
            return
        super().keyPressEvent(e)

    def copy_selection(self) -> None:
        bounds = self._selection_bounds()
        if bounds is None:
            self.operation_error.emit("Нет выделения для копирования")
            return
        top, left, bottom, right = bounds
        matrix = [[self._model.value(r, c) for c in range(left, right + 1)] for r in range(top, bottom + 1)]
        clipboard = QApplication.clipboard()
        if clipboard is None:
            self.operation_error.emit(CLIPBOARD_UNAVAILABLE_MESSAGE)
            return
        clipboard.setText(format_matrix(matrix))
        self.matrix_copied.emit(len(matrix), len(matrix[0]) if matrix else 0)

    def paste_selection(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is None:
            self.operation_error.emit(CLIPBOARD_UNAVAILABLE_MESSAGE)
            return
        try:
            matrix = parse_matrix(clipboard.text())
        except ValueError:
            self.operation_error.emit(NON_NUMERIC_CLIPBOARD_MESSAGE)
            return
        bounds = self._selection_bounds()
        if bounds is None:
            self.operation_error.emit("Нет активной ячейки для вставки")
            return
        top, left, bottom, right = bounds
        paste_rows, paste_cols = matrix_shape(matrix)
        error = validate_rectangular(matrix) or validate_paste_size(
            paste_rows, paste_cols, sel_rows=bottom - top + 1, sel_cols=right - left + 1
        )
        if error:
            self.operation_error.emit(error)
            return
        for dr, row_values in enumerate(matrix):
            for dc, value in enumerate(row_values):
                self._model.setData(self._model.index(top + dr, left + dc), value)
        self.matrix_pasted.emit(paste_rows, paste_cols)

    # --- Внутреннее ---

    def _headers(self) -> tuple[AxisHeaderView, AxisHeaderView]:
        return self._h_header, self._v_header

    def _on_axis_edited(self, orientation: Qt.Orientation, index: int, value: float) -> None:
        if orientation == Qt.Orientation.Horizontal:
            self.x_axis_changed.emit(index, value)
        else:
            self.y_axis_changed.emit(index, value)

    def _on_selection_changed(self, *_args: object) -> None:
        # Выделили ячейки — выделение точек оси снимается
        if self.selectedIndexes():
            self.clear_axis_selection()
        self.selection_changed.emit()

    def _repaint(self, *_args: object) -> None:
        viewport = self.viewport()
        if viewport is not None:
            viewport.update()

    def _selection_bounds(self) -> tuple[int, int, int, int] | None:
        cells = self.selected_cells()
        if not cells:
            current = self.currentIndex()
            if not current.isValid():
                return None
            cells = {(current.row(), current.column())}
        rows = [r for r, _ in cells]
        cols = [c for _, c in cells]
        return min(rows), min(cols), max(rows), max(cols)

    def _fit_min_column_width(self) -> None:
        """Столбцы растягиваются по ширине, но не уже самого длинного значения или подписи:
        иначе текст обрезается («1700...»); если места мало — появляется прокрутка."""
        cell_metrics = self.fontMetrics()
        # Заголовок текущего столбца рисуется жирным — меряем жирным шрифтом
        bold = self._h_header.font()
        bold.setBold(True)
        header_metrics = QFontMetrics(bold)
        widths = [
            header_metrics.horizontalAdvance(str(self._model.headerData(c, Qt.Orientation.Horizontal)))
            for c in range(self.columnCount())
        ]
        widths += [
            cell_metrics.horizontalAdvance(format_cell_value(v)) for row in self._model.values() for v in row
        ]
        if widths:
            self._h_header.setMinimumSectionSize(max(widths) + COLUMN_TEXT_PADDING)


def _is_copy(event: QKeyEvent) -> bool:
    return event.matches(QKeySequence.StandardKey.Copy) or (
        event.key() == Qt.Key.Key_C
        and bool(event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier))
    )


def _is_paste(event: QKeyEvent) -> bool:
    return event.matches(QKeySequence.StandardKey.Paste) or (
        event.key() == Qt.Key.Key_V
        and bool(event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier))
    )
