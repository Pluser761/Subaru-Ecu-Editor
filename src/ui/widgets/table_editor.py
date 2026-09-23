from __future__ import annotations

import math

from PyQt6.QtCore import Qt, QModelIndex, QRect, QEvent, pyqtSignal
from PyQt6.QtGui import QColor, QKeySequence, QMouseEvent, QPainter, QPalette, QPen, QValidator
from PyQt6.QtWidgets import (
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QStyledItemDelegate,
    QLineEdit,
    QWidget,
    QStyleOptionViewItem,
    QApplication,
)

from src.core.matrix_clipboard import (
    format_matrix,
    format_axis,
    parse_matrix,
    parse_axis,
    parse_number,
    matrix_shape,
    validate_rectangular,
    validate_paste_size,
    validate_axis_paste,
)
from src.core.models import CalibrationTable


STATIC_AXIS_MESSAGE = "Статическая ось задана в definition и не редактируется"
NON_NUMERIC_CLIPBOARD_MESSAGE = "Буфер обмена содержит нечисловые данные"


# Рамка ячейки относительно checkpoint: выросло — красная, уменьшилось — синяя
CHECKPOINT_UP_COLOR = QColor("#d93025")
CHECKPOINT_DOWN_COLOR = QColor("#1a66d9")
CHECKPOINT_BORDER_WIDTH = 2
# Значения сравниваются с точностью отображения (2 знака)
CHECKPOINT_EPSILON = 0.005
# Выделение нейтральным цветом: системный синий сливается с синей рамкой checkpoint
SELECTION_BG_COLOR = QColor("#d5dbe4")
SELECTION_TEXT_COLOR = QColor("#000000")


def format_cell_value(value: float) -> str:
    """Всегда два знака после точки."""
    return f"{value:.2f}"


def parse_cell_value(text: str) -> float:
    """Принимает и точку, и запятую как десятичный разделитель."""
    return parse_number(text)


class DecimalInputValidator(QValidator):
    """Цифры, один разделитель (точка или запятая) и минус в начале."""

    def validate(self, text: str, pos: int) -> tuple[QValidator.State, str, int]:
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


class DecimalCellDelegate(QStyledItemDelegate):
    """Редактор ячейки: фильтр ввода + нормализация к xx.xx; рамка изменений от checkpoint."""

    def paint(
        self,
        painter: QPainter | None,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> None:
        super().paint(painter, option, index)
        table = self.parent()
        if painter is None or not isinstance(table, TableEditor):
            return
        diff = table.checkpoint_diff(index.row(), index.column())
        if diff == 0:
            return
        color = CHECKPOINT_UP_COLOR if diff > 0 else CHECKPOINT_DOWN_COLOR
        painter.save()
        pen = QPen(color, CHECKPOINT_BORDER_WIDTH)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # Рамка целиком внутри ячейки, чтобы соседние не перекрывали друг друга
        half = CHECKPOINT_BORDER_WIDTH // 2
        painter.drawRect(option.rect.adjusted(half, half, -half - 1, -half - 1))
        painter.restore()

    def createEditor(
        self,
        parent: QWidget,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> QWidget:
        editor = QLineEdit(parent)
        editor.setValidator(DecimalInputValidator(editor))
        editor.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return editor

    def setEditorData(self, editor: QWidget, index: QModelIndex) -> None:
        assert isinstance(editor, QLineEdit)
        editor.setText(index.data(Qt.ItemDataRole.DisplayRole) or "")
        editor.selectAll()

    def setModelData(self, editor: QWidget, model, index: QModelIndex) -> None:
        assert isinstance(editor, QLineEdit)
        raw = editor.text().strip()
        try:
            value = parse_cell_value(raw)
        except ValueError:
            return
        model.setData(index, format_cell_value(value), Qt.ItemDataRole.EditRole)


class EditableHeaderView(QHeaderView):
    """Заголовок с правкой по двойному клику и copy/paste оси."""

    label_edited = pyqtSignal(int, float)  # section, value
    axis_pasted = pyqtSignal(int)  # сколько значений вставлено

    def __init__(self, orientation: Qt.Orientation, parent: QWidget | None = None) -> None:
        super().__init__(orientation, parent)
        self.setSectionsClickable(True)
        self.setHighlightSections(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._editor: QLineEdit | None = None
        self._edit_section: int = -1
        self._context_section: int = 0

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        if event is not None:
            section = self.logicalIndexAt(event.pos())
            if section >= 0:
                self._context_section = section
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent | None) -> None:
        if event is None:
            return
        section = self.logicalIndexAt(event.pos())
        if section >= 0:
            self._start_edit(section)
        else:
            super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_axis()
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Paste):
            self.paste_axis()
            event.accept()
            return
        super().keyPressEvent(event)

    def copy_axis(self) -> None:
        table = self.parent()
        if not isinstance(table, TableEditor):
            return
        sections = self._sections_for_clipboard(table)
        if not sections:
            return
        labels = table.axis_labels(self.orientation())
        if labels is not None:
            # Статическая ось: копируем подписи как есть («1st», «Enable Above»)
            QApplication.clipboard().setText("\t".join(labels[i] for i in sections if 0 <= i < len(labels)))
            return
        if self.orientation() == Qt.Orientation.Horizontal:
            axis = table.get_x_axis()
        else:
            axis = table.get_y_axis()
        values = [axis[i] for i in sections if 0 <= i < len(axis)]
        QApplication.clipboard().setText(format_axis(values))

    def paste_axis(self) -> None:
        table = self.parent()
        if not isinstance(table, TableEditor):
            return
        if table.axis_labels(self.orientation()) is not None:
            table.operation_error.emit(STATIC_AXIS_MESSAGE)
            return
        text = QApplication.clipboard().text()
        try:
            values = parse_axis(text)
        except ValueError:
            table.operation_error.emit(NON_NUMERIC_CLIPBOARD_MESSAGE)
            return
        sections = self._sections_for_clipboard(table)
        if not sections:
            table.operation_error.emit("Нет выбранных заголовков для вставки")
            return

        error = validate_axis_paste(len(values), target_count=len(sections))
        if error:
            table.operation_error.emit(error)
            return

        written = 0
        model = self.model()
        for section, value in zip(sections, values, strict=True):
            if model is not None:
                model.setHeaderData(
                    section,
                    self.orientation(),
                    format_cell_value(value),
                    Qt.ItemDataRole.EditRole,
                )
            self.label_edited.emit(section, value)
            written += 1
        if written:
            self.axis_pasted.emit(written)
            self.viewport().update()
            table.operation_info.emit(f"Вставлено в ось: {written}")

    def _sections_for_clipboard(self, table: TableEditor) -> list[int]:
        """Выделенные столбцы/строки таблицы, иначе секция под курсором."""
        ranges = table.selectedRanges()
        sections: list[int] = []
        if self.orientation() == Qt.Orientation.Horizontal:
            for selection in ranges:
                sections.extend(range(selection.leftColumn(), selection.rightColumn() + 1))
        else:
            for selection in ranges:
                sections.extend(range(selection.topRow(), selection.bottomRow() + 1))
        seen: set[int] = set()
        ordered: list[int] = []
        for s in sections:
            if s not in seen:
                seen.add(s)
                ordered.append(s)
        if ordered:
            return ordered
        if 0 <= self._context_section < self.count():
            return [self._context_section]
        return []

    def _start_edit(self, section: int) -> None:
        self._cancel_edit()
        table = self.parent()
        if isinstance(table, TableEditor) and table.axis_labels(self.orientation()) is not None:
            table.operation_error.emit(STATIC_AXIS_MESSAGE)
            return
        model = self.model()
        if model is None:
            return

        orientation = self.orientation()
        current = model.headerData(section, orientation, Qt.ItemDataRole.DisplayRole)
        if orientation == Qt.Orientation.Horizontal:
            geo = QRect(
                self.sectionViewportPosition(section),
                0,
                self.sectionSize(section),
                self.height(),
            )
        else:
            geo = QRect(
                0,
                self.sectionViewportPosition(section),
                self.width(),
                self.sectionSize(section),
            )

        editor = QLineEdit(self.viewport())
        editor.setValidator(DecimalInputValidator(editor))
        editor.setAlignment(Qt.AlignmentFlag.AlignCenter)
        editor.setText(str(current or ""))
        editor.setGeometry(geo)
        editor.selectAll()
        editor.show()
        editor.setFocus(Qt.FocusReason.MouseFocusReason)

        editor.editingFinished.connect(self._commit_edit)
        editor.installEventFilter(self)

        self._editor = editor
        self._edit_section = section
        self._context_section = section

    def _commit_edit(self) -> None:
        if self._editor is None or self._edit_section < 0:
            return

        editor = self._editor
        section = self._edit_section
        raw = editor.text().strip()

        self._editor = None
        self._edit_section = -1
        editor.deleteLater()

        try:
            value = parse_cell_value(raw)
        except ValueError:
            self.viewport().update()
            return

        formatted = format_cell_value(value)
        model = self.model()
        if model is not None:
            model.setHeaderData(
                section,
                self.orientation(),
                formatted,
                Qt.ItemDataRole.EditRole,
            )
        self.label_edited.emit(section, value)
        self.viewport().update()

    def _cancel_edit(self) -> None:
        if self._editor is None:
            return
        editor = self._editor
        self._editor = None
        self._edit_section = -1
        editor.deleteLater()
        self.viewport().update()

    def eventFilter(self, obj, event) -> bool:  # type: ignore[no-untyped-def]
        if obj is self._editor and event is not None:
            if event.type() == QEvent.Type.KeyPress:
                key = event.key()  # type: ignore[attr-defined]
                if key == Qt.Key.Key_Escape:
                    self._cancel_edit()
                    return True
                if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self._commit_edit()
                    return True
            if event.type() == QEvent.Type.FocusOut:
                self._commit_edit()
                return False
        return super().eventFilter(obj, event)


class TableEditor(QTableWidget):
    """Редактируемая сетка значений калибровочной таблицы."""

    cell_value_changed = pyqtSignal(int, int, float)
    x_axis_changed = pyqtSignal(int, float)  # col, value
    y_axis_changed = pyqtSignal(int, float)  # row, value
    matrix_copied = pyqtSignal(int, int)  # rows, cols
    matrix_pasted = pyqtSignal(int, int)  # rows, cols written
    operation_error = pyqtSignal(str)
    operation_info = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAlternatingRowColors(True)
        self.setItemDelegate(DecimalCellDelegate(self))
        palette = self.palette()
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
            palette.setColor(group, QPalette.ColorRole.Highlight, SELECTION_BG_COLOR)
            palette.setColor(group, QPalette.ColorRole.HighlightedText, SELECTION_TEXT_COLOR)
        self.setPalette(palette)

        h_header = EditableHeaderView(Qt.Orientation.Horizontal, self)
        v_header = EditableHeaderView(Qt.Orientation.Vertical, self)
        self.setHorizontalHeader(h_header)
        self.setVerticalHeader(v_header)

        h_header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        v_header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        h_header.setToolTip("Двойной клик — изменить ось X. Ctrl+C/V — копировать/вставить ось")
        v_header.setToolTip("Двойной клик — изменить ось Y. Ctrl+C/V — копировать/вставить ось")

        h_header.label_edited.connect(self.x_axis_changed.emit)
        v_header.label_edited.connect(self.y_axis_changed.emit)

        self.itemChanged.connect(self._on_item_changed)
        self._updating = False
        # Статические оси: подписи и их числовые значения (для 3D) хранятся здесь,
        # а не разбираются из текста заголовка
        self._x_labels: list[str] | None = None
        self._y_labels: list[str] | None = None
        self._x_static: list[float] = []
        self._y_static: list[float] = []
        # Точка отсчёта для подсветки изменений (значения на момент Set checkpoint)
        self._checkpoint: list[list[float]] = []

    def event(self, event) -> bool:  # type: ignore[no-untyped-def]
        # Перехватываем до стандартного QAbstractItemView paste
        if event.type() == QEvent.Type.ShortcutOverride and (
            self._is_paste_event(event) or self._is_copy_event(event)
        ):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self._is_copy_event(event):
            self.copy_selection()
            event.accept()
            return
        if self._is_paste_event(event):
            self.paste_selection()
            event.accept()
            return
        super().keyPressEvent(event)

    @staticmethod
    def _is_copy_event(event) -> bool:  # type: ignore[no-untyped-def]
        if event.matches(QKeySequence.StandardKey.Copy):
            return True
        mods = event.modifiers()
        return event.key() == Qt.Key.Key_C and bool(
            mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)
        )

    @staticmethod
    def _is_paste_event(event) -> bool:  # type: ignore[no-untyped-def]
        if event.matches(QKeySequence.StandardKey.Paste):
            return True
        mods = event.modifiers()
        return event.key() == Qt.Key.Key_V and bool(
            mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)
        )

    def copy_selection(self) -> None:
        bounds = self._selection_bounds()
        if bounds is None:
            self.operation_error.emit("Нет выделения для копирования")
            return
        top, left, bottom, right = bounds
        matrix: list[list[float]] = []
        for r in range(top, bottom + 1):
            row: list[float] = []
            for c in range(left, right + 1):
                item = self.item(r, c)
                text = item.text() if item else "0"
                try:
                    row.append(parse_cell_value(text))
                except ValueError:
                    row.append(0.0)
            matrix.append(row)
        QApplication.clipboard().setText(format_matrix(matrix))
        self.matrix_copied.emit(len(matrix), len(matrix[0]) if matrix else 0)

    def paste_selection(self) -> None:
        text = QApplication.clipboard().text()
        try:
            matrix = parse_matrix(text)
        except ValueError:
            self.operation_error.emit(NON_NUMERIC_CLIPBOARD_MESSAGE)
            return

        shape_error = validate_rectangular(matrix)
        if shape_error:
            self.operation_error.emit(shape_error)
            return

        bounds = self._selection_bounds()
        if bounds is None:
            self.operation_error.emit("Нет активной ячейки для вставки")
            return
        top, left, bottom, right = bounds
        sel_rows = bottom - top + 1
        sel_cols = right - left + 1
        paste_rows, paste_cols = matrix_shape(matrix)

        size_error = validate_paste_size(
            paste_rows,
            paste_cols,
            sel_rows=sel_rows,
            sel_cols=sel_cols,
        )
        if size_error:
            self.operation_error.emit(size_error)
            return

        self._updating = True
        try:
            for dr, row_vals in enumerate(matrix):
                for dc, value in enumerate(row_vals):
                    self._set_cell_value(top + dr, left + dc, value)
        finally:
            self._updating = False

        for dr, row_vals in enumerate(matrix):
            for dc, value in enumerate(row_vals):
                self.cell_value_changed.emit(top + dr, left + dc, value)

        self.matrix_pasted.emit(paste_rows, paste_cols)

    def adjust_selected(self, delta: float) -> int:
        """Прибавить delta ко всем выделенным ячейкам; вернуть число изменённых."""
        cells = sorted(self.selected_cells())
        if not cells and self.currentRow() >= 0 and self.currentColumn() >= 0:
            cells = [(self.currentRow(), self.currentColumn())]

        changed: list[tuple[int, int, float]] = []
        self._updating = True
        try:
            for row, col in cells:
                item = self.item(row, col)
                if item is None:
                    continue
                try:
                    value = parse_cell_value(item.text())
                except ValueError:
                    continue
                if not math.isfinite(value):
                    continue
                # Округляем как при отображении, чтобы не копить ошибку float
                new_value = round(value + delta, 2)
                self._set_cell_value(row, col, new_value)
                changed.append((row, col, new_value))
        finally:
            self._updating = False

        for row, col, value in changed:
            self.cell_value_changed.emit(row, col, value)
        return len(changed)

    def _set_cell_value(self, row: int, col: int, value: float) -> None:
        item = self.item(row, col)
        text = format_cell_value(value)
        if item is None:
            item = QTableWidgetItem(text)
            item.setTextAlignment(
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            )
            self.setItem(row, col, item)
        else:
            item.setText(text)

    def _selection_bounds(self) -> tuple[int, int, int, int] | None:
        ranges = self.selectedRanges()
        if ranges:
            top = min(r.topRow() for r in ranges)
            left = min(r.leftColumn() for r in ranges)
            bottom = max(r.bottomRow() for r in ranges)
            right = max(r.rightColumn() for r in ranges)
            return top, left, bottom, right
        row, col = self.currentRow(), self.currentColumn()
        if row < 0 or col < 0:
            return None
        return row, col, row, col

    def set_table(self, table: CalibrationTable | None) -> None:
        self._updating = True
        try:
            self.clear()
            self._x_labels = self._y_labels = None
            self._x_static, self._y_static = [], []
            self._checkpoint = []
            if table is None:
                self.setRowCount(0)
                self.setColumnCount(0)
                return

            rows, cols = table.rows, table.cols
            self.setRowCount(rows)
            self.setColumnCount(cols)

            if table.x_labels is not None and len(table.x_labels) == cols:
                self._x_labels = list(table.x_labels)
                self._x_static = list(table.x_axis)
                x_labels = self._x_labels
            elif table.x_axis and len(table.x_axis) == cols:
                x_labels = [format_cell_value(v) for v in table.x_axis]
            else:
                x_labels = [format_cell_value(float(c)) for c in range(cols)]
            self.setHorizontalHeaderLabels(x_labels)

            if table.y_labels is not None and len(table.y_labels) == rows:
                self._y_labels = list(table.y_labels)
                self._y_static = list(table.y_axis)
                y_labels = self._y_labels
            elif table.y_axis and len(table.y_axis) == rows:
                y_labels = [format_cell_value(v) for v in table.y_axis]
            else:
                y_labels = [format_cell_value(float(r)) for r in range(rows)]
            self.setVerticalHeaderLabels(y_labels)

            for r in range(rows):
                for c in range(cols):
                    item = QTableWidgetItem(format_cell_value(table.values[r][c]))
                    item.setTextAlignment(
                        int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    )
                    self.setItem(r, c, item)
        finally:
            self._updating = False
        # Только что загруженная таблица — сама себе checkpoint
        self.set_checkpoint()

    def set_checkpoint(self) -> None:
        """Запомнить текущие значения как точку отсчёта; рамки изменений сбрасываются."""
        self.set_checkpoint_values(self.get_values())

    def checkpoint_values(self) -> list[list[float]]:
        return [list(row) for row in self._checkpoint]

    def set_checkpoint_values(self, values: list[list[float]]) -> None:
        """Восстановить сохранённый checkpoint (размер должен совпадать с таблицей)."""
        if len(values) != self.rowCount() or any(len(r) != self.columnCount() for r in values):
            values = self.get_values()
        self._checkpoint = [list(row) for row in values]
        viewport = self.viewport()
        if viewport is not None:
            viewport.update()

    def set_cell_display(self, row: int, col: int, value: float) -> None:
        """Показать значение без сигнала cell_value_changed (например, после квантования)."""
        self._updating = True
        try:
            self._set_cell_value(row, col, value)
        finally:
            self._updating = False

    def set_axis_display(self, orientation: Qt.Orientation, index: int, value: float) -> None:
        """Показать значение оси в заголовке без сигнала изменения."""
        model = self.model()
        if model is not None:
            model.setHeaderData(index, orientation, format_cell_value(value), Qt.ItemDataRole.EditRole)

    def checkpoint_diff(self, row: int, col: int) -> int:
        """+1 — значение выросло относительно checkpoint, -1 — уменьшилось, 0 — без изменений."""
        if row >= len(self._checkpoint) or col >= len(self._checkpoint[row]):
            return 0
        item = self.item(row, col)
        if item is None:
            return 0
        try:
            value = parse_cell_value(item.text())
        except ValueError:
            return 0
        base = self._checkpoint[row][col]
        if value == base:  # в т.ч. inf == inf
            return 0
        if not (math.isfinite(value) and math.isfinite(base)):
            return 1 if value > base else -1
        if abs(value - base) < CHECKPOINT_EPSILON:
            return 0
        return 1 if value > base else -1

    def get_values(self) -> list[list[float]]:
        values: list[list[float]] = []
        for r in range(self.rowCount()):
            row: list[float] = []
            for c in range(self.columnCount()):
                item = self.item(r, c)
                text = item.text() if item else "0"
                try:
                    row.append(parse_cell_value(text))
                except ValueError:
                    row.append(0.0)
            values.append(row)
        return values

    def selected_cells(self) -> set[tuple[int, int]]:
        return {(index.row(), index.column()) for index in self.selectedIndexes()}

    def axis_labels(self, orientation: Qt.Orientation) -> list[str] | None:
        """Подписи статической оси или None, если ось числовая и редактируемая."""
        if orientation == Qt.Orientation.Horizontal:
            return self._x_labels
        return self._y_labels

    def get_x_axis(self) -> list[float]:
        if self._x_labels is not None:
            return list(self._x_static)
        return self._header_values(Qt.Orientation.Horizontal, self.columnCount())

    def get_y_axis(self) -> list[float]:
        if self._y_labels is not None:
            return list(self._y_static)
        return self._header_values(Qt.Orientation.Vertical, self.rowCount())

    def _header_values(self, orientation: Qt.Orientation, count: int) -> list[float]:
        model = self.model()
        result: list[float] = []
        for i in range(count):
            text = ""
            if model is not None:
                text = str(model.headerData(i, orientation, Qt.ItemDataRole.DisplayRole) or "")
            try:
                result.append(parse_cell_value(text))
            except ValueError:
                result.append(0.0)
        return result

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating:
            return
        try:
            value = parse_cell_value(item.text())
        except ValueError:
            return

        formatted = format_cell_value(value)
        if item.text() != formatted:
            self._updating = True
            try:
                item.setText(formatted)
            finally:
                self._updating = False

        self.cell_value_changed.emit(item.row(), item.column(), value)
