"""Подтверждение правки от ИИ: карта с новыми значениями, изменённые ячейки подсвечены,
в подсказке — «было → станет». Только показ и ответ; данные готовит презентер (в экранных единицах)."""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.i18n import tr
from src.ui.map.cells import format_cell_value

_UP = QColor("#ffd8c8")
_DOWN = QColor("#cfe0ff")
_AXIS_CHANGED = QColor("#fff0a8")


@dataclass
class EditPreview:
    """Правка в экранных единицах: матрицы до/после [строка][столбец], оси до/после."""

    name: str
    reason: str
    unit: str
    old: list[list[float]]
    new: list[list[float]]
    decimals: int = 2
    x_old: list[float] = field(default_factory=list)
    x_new: list[float] = field(default_factory=list)
    y_old: list[float] = field(default_factory=list)
    y_new: list[float] = field(default_factory=list)
    x_labels: list[str] | None = None
    y_labels: list[str] | None = None
    x_title: str = ""
    y_title: str = ""
    x_decimals: int = 2
    y_decimals: int = 2
    shared: list[str] = field(default_factory=list)  # другие таблицы на изменяемой оси


class AiEditDialog(QDialog):
    """Немодальное окно: можно сверить с открытой картой. accept — применить, reject — отказ."""

    def __init__(self, preview: EditPreview, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._preview = preview
        self.setWindowTitle(tr("ИИ предлагает правку — {name}", name=preview.name))
        self.resize(820, 560)

        title = QLabel(f"<b>{escape(preview.name)}</b>")
        title.setStyleSheet("font-size: 15px;")
        reason = QLabel(escape(preview.reason) if preview.reason else tr("ИИ не указал причину."))
        reason.setWordWrap(True)
        reason.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        reason.setStyleSheet("background: #f2f6ff; border: 1px solid #c9d6f0; border-radius: 8px; padding: 8px;")

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(reason)
        layout.addWidget(QLabel(self._summary()))
        if preview.shared:
            warning = QLabel(
                tr(
                    "⚠ Ось общая: изменится и у других таблиц ({n}): {names}. Их значения не пересчитываются.",
                    n=len(preview.shared),
                    names=", ".join(preview.shared),
                )
            )
            warning.setWordWrap(True)
            warning.setStyleSheet(
                "background: #fff4e5; border: 1px solid #f0c897; border-radius: 8px; padding: 8px; color: #7a4100;"
            )
            layout.addWidget(warning)

        self._delta = QCheckBox(tr("Показывать изменение (Δ) вместо новых значений"))
        self._delta.toggled.connect(self._fill)
        layout.addWidget(self._delta)

        self._table = QTableWidget()
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)  # type: ignore[union-attr]
        layout.addWidget(self._table, stretch=1)
        if preview.x_title or preview.y_title:
            axes = QLabel(tr("Столбцы: {x} · строки: {y}", x=preview.x_title or "—", y=preview.y_title or "—"))
            axes.setStyleSheet("color: #666;")
            layout.addWidget(axes)

        buttons = QDialogButtonBox()
        apply = buttons.addButton(tr("Применить"), QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(tr("Отклонить"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if apply is not None:
            apply.setDefault(False)  # Enter не применяет случайно
        self._fill()

    def _changes(self) -> list[float]:
        p = self._preview
        return [n - o for old_row, new_row in zip(p.old, p.new) for o, n in zip(old_row, new_row) if n != o]

    def _summary(self) -> str:
        p = self._preview
        deltas = self._changes()
        axis_points = sum(n != o for o, n in zip(p.x_old, p.x_new)) + sum(n != o for o, n in zip(p.y_old, p.y_new))
        parts = [tr("ячеек изменено: {n}", n=len(deltas))]
        if deltas:
            parts.append(
                tr(
                    "изменение от {low} до {high} {unit}",
                    low=format_cell_value(min(deltas), p.decimals),
                    high=format_cell_value(max(deltas), p.decimals),
                    unit=p.unit,
                ).strip()
            )
        if axis_points:
            parts.append(tr("точек осей изменено: {n}", n=axis_points))
        return " · ".join(parts)

    def _fill(self) -> None:
        p = self._preview
        delta = self._delta.isChecked()
        rows, cols = len(p.new), len(p.new[0]) if p.new else 0
        self._table.setRowCount(rows)
        self._table.setColumnCount(cols)
        self._set_headers(Qt.Orientation.Horizontal, cols, p.x_old, p.x_new, p.x_labels, p.x_decimals)
        self._set_headers(Qt.Orientation.Vertical, rows, p.y_old, p.y_new, p.y_labels, p.y_decimals)
        for r in range(rows):
            for c in range(cols):
                old, new = p.old[r][c], p.new[r][c]
                shown = new - old if delta else new
                text = format_cell_value(shown, p.decimals)
                if delta and new != old and shown > 0:
                    text = "+" + text
                item = QTableWidgetItem("" if delta and new == old else text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if new != old:
                    item.setBackground(_UP if new > old else _DOWN)
                    item.setToolTip(
                        tr(
                            "было {old} → станет {new}",
                            old=format_cell_value(old, p.decimals),
                            new=format_cell_value(new, p.decimals),
                        )
                    )
                self._table.setItem(r, c, item)

    def _set_headers(
        self,
        orientation: Qt.Orientation,
        count: int,
        old: list[float],
        new: list[float],
        labels: list[str] | None,
        decimals: int,
    ) -> None:
        for i in range(count):
            if labels is not None and i < len(labels):
                item = QTableWidgetItem(labels[i])
            elif i < len(new):
                item = QTableWidgetItem(format_cell_value(new[i], decimals))
                if i < len(old) and new[i] != old[i]:
                    item.setBackground(_AXIS_CHANGED)
                    item.setToolTip(
                        tr(
                            "было {old} → станет {new}",
                            old=format_cell_value(old[i], decimals),
                            new=format_cell_value(new[i], decimals),
                        )
                    )
            else:
                item = QTableWidgetItem("")
            if orientation == Qt.Orientation.Horizontal:
                self._table.setHorizontalHeaderItem(i, item)
            else:
                self._table.setVerticalHeaderItem(i, item)
