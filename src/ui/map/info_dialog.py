"""Справка о карте: описание из definitions и где/как она лежит в ROM (кнопка ⓘ, F1)."""

from __future__ import annotations

from html import escape

from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QTextBrowser, QVBoxLayout, QWidget

from src.i18n import tr
from src.core.models import CalibrationTable
from src.ui.levels import LEVELS, level_color
from src.ui.map.cells import describe_shape, format_cell_value


class MapInfoDialog(QDialog):
    """Немодальное окно: можно держать открытым рядом с таблицей."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.resize(560, 460)
        self._text = QTextBrowser()
        self._text.setOpenExternalLinks(False)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout = QVBoxLayout(self)
        layout.addWidget(self._text)
        layout.addWidget(buttons)

    def show_table(self, table: CalibrationTable) -> None:
        """table — в координатах ROM (строки — ось Y), как её отдаёт парсер."""
        self.setWindowTitle(tr("{name} — описание", name=table.name.strip()))
        self._text.setHtml(describe_table(table))


def describe_table(table: CalibrationTable) -> str:
    rows = [(tr("Размерность"), describe_shape(table.rows, table.cols, table.unit))]
    if table.formula and table.formula.strip() != "x":
        rows.append((
            tr("Формула"),
            tr("{formula} (x — сырое число в ROM, единицы definitions)", formula=f"<code>{escape(table.formula)}</code>"),
        ))
    if table.units_note:
        rows.append((tr("Единицы"), escape(table.units_note)))
    rows += _axis_rows("X", table.x_title, table.x_axis, table.x_labels, table.x_shared_with, table.cols)
    rows += _axis_rows("Y", table.y_title, table.y_axis, table.y_labels, table.y_shared_with, table.rows)
    if table.storage:
        rows.append((tr("В ROM"), escape(table.storage)))
    rows.append((
        tr("Чтение ЭБУ"),
        tr("по коду ЭБУ Denso SH7055/SH7058 (проверено по прошивке)")
        if table.ecu_interpolation
        else tr("не проверено для этого ЭБУ"),
    ))

    header = [escape(table.category)] if table.category else []
    if table.userlevel in LEVELS:
        color = level_color(table.userlevel).name()
        header.append(
            f'<span style="background:{color}; padding:0 4px;">'
            + tr("уровень {n} — {name}", n=table.userlevel, name=escape(tr(LEVELS[table.userlevel])))
            + "</span>"
        )
    description = escape(table.description) if table.description else "<i>" + tr("В definitions описания нет.") + "</i>"
    details = "".join(
        f'<tr><td style="color:#666; padding-right:12px; vertical-align:top;">{name}</td><td>{value}</td></tr>'
        for name, value in rows
    )
    return (
        f"<h3>{escape(table.name.strip())}</h3>"
        f'<p style="color:#555;">{" · ".join(header)}</p>'
        f"<p>{description}</p>"
        f"<table>{details}</table>"
    )


def _axis_rows(
    name: str, title: str, values: list[float], labels: list[str] | None, shared: list[str], count: int
) -> list[tuple[str, str]]:
    if count < 2:
        return []
    caption = escape(title) if title else "—"
    if labels is not None:
        text = tr("{caption}; подписи: {labels}", caption=caption, labels=escape(", ".join(labels)))
    else:
        text = tr("{caption}; {first} … {last}, точек: {count}", caption=caption, first=f"{values[0]:g}", last=f"{values[-1]:g}", count=count)
    if shared:
        listed = ", ".join(escape(n.strip()) for n in shared[:6]) + (" …" if len(shared) > 6 else "")
        text += "<br><span style='color:#b36b00;'>" + tr("общая ещё с {n}: {listed}", n=len(shared), listed=listed) + "</span>"
    return [(tr("Ось {name}", name=name), text)]
