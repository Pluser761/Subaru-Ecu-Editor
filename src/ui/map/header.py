"""Заголовок оси таблицы карты: правка по двойному клику, copy/paste оси,
выделение точек оси (Ctrl+клик), рамки изменений относительно checkpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import QEvent, QObject, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QKeyEvent, QKeySequence, QMouseEvent, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QHeaderView, QLineEdit

from src.core.matrix_clipboard import format_axis, parse_axis, validate_axis_paste
from src.ui.key_bindings import PHYSICAL_CONTROL
from src.ui.map.cells import (
    CLIPBOARD_UNAVAILABLE_MESSAGE,
    NON_NUMERIC_CLIPBOARD_MESSAGE,
    STATIC_AXIS_MESSAGE,
    DecimalInputValidator,
    draw_checkpoint_border,
    parse_cell_value,
)

if TYPE_CHECKING:
    from src.ui.map.view import MapTableView

# Выделенные точки оси — янтарная заливка, отлична от выделения ячеек
AXIS_SELECTION_COLOR = QColor(255, 190, 40, 110)
AXIS_SELECTION_BORDER = QColor("#c98a00")


class AxisHeaderView(QHeaderView):
    """Клик — выделить ячейки столбца/строки (стандартно). Ctrl+клик (физический Control) —
    выделить саму точку оси: клавиши шага тогда меняют значения оси.

    Правки оси идут в модель (setHeaderData); о них сообщает модель (axis_edited).
    """

    # Ctrl+клик выделил/снял точку оси
    axis_selection_changed = pyqtSignal()

    def __init__(self, orientation: Qt.Orientation, table: MapTableView) -> None:
        super().__init__(orientation, table)
        self._table = table
        self.setSectionsClickable(True)
        self.setHighlightSections(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._editor: QLineEdit | None = None
        self._edit_section = -1
        self._context_section = 0
        # Выделенные точки оси (секции)
        self._axis_selected: list[int] = []

    # --- Выделение точек оси ---

    @property
    def axis_selection(self) -> list[int]:
        return sorted(self._axis_selected)

    def clear_axis_selection(self) -> None:
        if self._axis_selected:
            self._axis_selected = []
            self.update_viewport()
            self.axis_selection_changed.emit()

    def _toggle_axis_point(self, section: int) -> None:
        if self._table.map_model.is_static(self.orientation()):
            self._table.operation_error.emit(STATIC_AXIS_MESSAGE)
            return
        # Точки оси и ячейки не выделяются одновременно: шаг меняет что-то одно
        self._table.clearSelection()
        self._table.clear_axis_selection(except_header=self)
        if section in self._axis_selected:
            self._axis_selected.remove(section)
        else:
            self._axis_selected.append(section)
        self.update_viewport()
        self.axis_selection_changed.emit()

    # --- События ---

    def mousePressEvent(self, e: QMouseEvent | None) -> None:
        if e is not None:
            section = self.logicalIndexAt(e.pos())
            if section >= 0:
                self._context_section = section
            # На macOS Qt превращает Control+клик в правый клик — принимаем обе кнопки
            if (
                section >= 0
                and e.modifiers() & PHYSICAL_CONTROL
                and e.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton)
            ):
                self._toggle_axis_point(section)
                e.accept()
                return
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e: QMouseEvent | None) -> None:
        if e is None:
            return
        section = self.logicalIndexAt(e.pos())
        if section >= 0:
            self._start_edit(section)
        else:
            super().mouseDoubleClickEvent(e)

    def keyPressEvent(self, e: QKeyEvent | None) -> None:
        if e is not None and e.matches(QKeySequence.StandardKey.Copy):
            self.copy_axis()
            e.accept()
            return
        if e is not None and e.matches(QKeySequence.StandardKey.Paste):
            self.paste_axis()
            e.accept()
            return
        super().keyPressEvent(e)

    def paintSection(self, painter: QPainter | None, rect: QRect, logicalIndex: int) -> None:
        super().paintSection(painter, rect, logicalIndex)
        if painter is None:
            return
        if logicalIndex in self._axis_selected:
            painter.save()
            # После стандартной отрисовки у painter остаётся пустой clip — без этого не видно
            painter.setClipRect(rect)
            painter.fillRect(rect, AXIS_SELECTION_COLOR)
            painter.setPen(QPen(AXIS_SELECTION_BORDER, 1))
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
            painter.restore()
        diff = self._table.map_model.axis_diff(self.orientation(), logicalIndex)
        if diff:
            draw_checkpoint_border(painter, rect, diff)

    def update_viewport(self) -> None:
        viewport = self.viewport()
        if viewport is not None:
            viewport.update()

    # --- Буфер обмена ---

    def copy_axis(self) -> None:
        sections = self._sections_for_clipboard()
        if not sections:
            return
        clipboard = QApplication.clipboard()
        if clipboard is None:
            self._table.operation_error.emit(CLIPBOARD_UNAVAILABLE_MESSAGE)
            return
        model = self._table.map_model
        labels = model.labels(self.orientation())
        if labels is not None:
            # Статическая ось: копируем подписи как есть («1st», «Enable Above»)
            clipboard.setText("\t".join(labels[i] for i in sections if 0 <= i < len(labels)))
            return
        axis = model.axis(self.orientation())
        decimals = model.axis_decimals(self.orientation())
        clipboard.setText(format_axis([axis[i] for i in sections if 0 <= i < len(axis)], decimals))

    def paste_axis(self) -> None:
        model = self._table.map_model
        if model.is_static(self.orientation()):
            self._table.operation_error.emit(STATIC_AXIS_MESSAGE)
            return
        clipboard = QApplication.clipboard()
        if clipboard is None:
            self._table.operation_error.emit(CLIPBOARD_UNAVAILABLE_MESSAGE)
            return
        try:
            values = parse_axis(clipboard.text())
        except ValueError:
            self._table.operation_error.emit(NON_NUMERIC_CLIPBOARD_MESSAGE)
            return
        sections = self._sections_for_clipboard()
        if not sections:
            self._table.operation_error.emit("Нет выбранных заголовков для вставки")
            return
        error = validate_axis_paste(len(values), target_count=len(sections))
        if error:
            self._table.operation_error.emit(error)
            return
        for section, value in zip(sections, values, strict=True):
            model.setHeaderData(section, self.orientation(), value, Qt.ItemDataRole.EditRole)
        self._table.operation_info.emit(f"Вставлено в ось: {len(sections)}")

    def _sections_for_clipboard(self) -> list[int]:
        """Выделенные точки оси, иначе столбцы/строки выделенных ячеек, иначе секция под курсором."""
        if self._axis_selected:
            return self.axis_selection
        horizontal = self.orientation() == Qt.Orientation.Horizontal
        sections = sorted({col if horizontal else row for row, col in self._table.selected_cells()})
        if sections:
            return sections
        if 0 <= self._context_section < self.count():
            return [self._context_section]
        return []

    # --- Правка по двойному клику ---

    def _start_edit(self, section: int) -> None:
        self._cancel_edit()
        if self._table.map_model.is_static(self.orientation()):
            self._table.operation_error.emit(STATIC_AXIS_MESSAGE)
            return
        if self.orientation() == Qt.Orientation.Horizontal:
            geo = QRect(self.sectionViewportPosition(section), 0, self.sectionSize(section), self.height())
        else:
            geo = QRect(0, self.sectionViewportPosition(section), self.width(), self.sectionSize(section))

        editor = QLineEdit(self.viewport())
        editor.setValidator(DecimalInputValidator(editor))
        editor.setAlignment(Qt.AlignmentFlag.AlignCenter)
        editor.setText(str(self._table.map_model.headerData(section, self.orientation()) or ""))
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
        editor, section = self._editor, self._edit_section
        self._editor = None
        self._edit_section = -1
        text = editor.text().strip()
        editor.deleteLater()
        try:
            value = parse_cell_value(text)
        except ValueError:
            self.update_viewport()
            return
        self._table.map_model.setHeaderData(section, self.orientation(), value, Qt.ItemDataRole.EditRole)
        self.update_viewport()

    def _cancel_edit(self) -> None:
        if self._editor is None:
            return
        editor = self._editor
        self._editor = None
        self._edit_section = -1
        editor.deleteLater()
        self.update_viewport()

    def eventFilter(self, a0: QObject | None, a1: QEvent | None) -> bool:
        if a0 is self._editor and a1 is not None:
            if isinstance(a1, QKeyEvent) and a1.type() == QEvent.Type.KeyPress:
                if a1.key() == Qt.Key.Key_Escape:
                    self._cancel_edit()
                    return True
                if a1.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self._commit_edit()
                    return True
            if a1.type() == QEvent.Type.FocusOut:
                self._commit_edit()
                return False
        return super().eventFilter(a0, a1)
