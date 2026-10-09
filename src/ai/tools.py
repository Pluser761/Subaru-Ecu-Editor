"""Операции, которые MCP-сервер даёт ИИ: прошивка, карты, предложение правки и её применение.

Без Qt и без MCP: функции принимают сессию и возвращают JSON-совместимые словари. Значения —
в единицах definitions (как в ROM и в ecu_defs.xml), не в экранных. Тексты для ИИ — на
английском и не переводятся: это не интерфейс пользователя.
"""

from __future__ import annotations

import difflib
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.core.models import CalibrationTable
from src.core.parser import EcuParser
from src.core.rom_io import write_axis_value, write_table_cell
from src.core.session import RomSession

# Знаков после точки в ответах: больше, чем на экране, чтобы было видно квантование
_DIGITS = 4
# ИИ возвращает значения из read_map (округлённые до _DIGITS): такая разница — не правка.
# Иначе кодирование сдвинуло бы ячейку на целый шаг типа (см. rom_io.encode_value)
_SAME = 1.5 * 10**-_DIGITS


class ToolError(ValueError):
    """Ошибка в запросе ИИ: текст уходит ему в ответ."""


def _round(value: float) -> float | None:
    return round(value, _DIGITS) if math.isfinite(value) else None


def find_map(parser: EcuParser, name: str) -> str:
    """id карты по имени: точно, без пробелов по краям или без учёта регистра."""
    maps = parser.list_maps()
    ids = {m.id for m in maps}
    if name in ids:
        return name
    wanted = name.strip().lower()
    for m in maps:
        if m.name.strip().lower() == wanted:
            return m.id
    names = [m.name.strip() for m in maps]
    close = difflib.get_close_matches(name.strip(), names, n=5, cutoff=0.5)
    hint = f" Did you mean: {', '.join(close)}?" if close else " Use list_maps to find names."
    raise ToolError(f"Map not found: {name!r}.{hint}")


def rom_info(session: RomSession) -> dict[str, Any]:
    parser = session.parser
    definition = parser.definition
    if definition is None:
        return {"open": False}
    romid = definition.romid
    return {
        "open": True,
        "file": parser.file_path,
        "xmlid": romid.xmlid,
        "car": " ".join(p for p in (romid.market, romid.make, romid.model, romid.submodel, romid.year) if p),
        "ecu_id": romid.ecuid,
        "memory_model": romid.memmodel,
        "maps": len(parser.list_maps()),
        "unsaved_changes": session.dirty,
        "changed_maps": sorted(m.strip() for m in session.changed),
        "save_blocked": session.save_blocker,
        "units": "definitions units (as in ecu_defs.xml: psi, °F, mph, g/rev, rpm…)",
    }


def list_maps(session: RomSession, category: str | None = None, query: str | None = None) -> list[dict[str, Any]]:
    """Карты прошивки; category — точное имя категории, query — подстрока имени (без регистра)."""
    result = []
    needle = (query or "").strip().lower()
    for m in session.parser.list_maps():
        if category and m.category != category:
            continue
        if needle and needle not in m.name.lower():
            continue
        result.append({
            "name": m.name.strip(),
            "category": m.category,
            "shape": f"{m.rows}x{m.cols}",
            "unit": m.unit,
            "userlevel": m.userlevel,
        })
    return result


def _axis(table: CalibrationTable, axis: str) -> dict[str, Any] | None:
    values = table.x_axis if axis == "x" else table.y_axis
    labels = table.x_labels if axis == "x" else table.y_labels
    if not values and not labels:
        return None
    return {
        "title": table.x_title if axis == "x" else table.y_title,
        "values": [_round(v) for v in values],
        "labels": labels,  # статическая ось: подписи вместо чисел, не редактируется
        "editable": labels is None,
        "shared_with": [n.strip() for n in (table.x_shared_with if axis == "x" else table.y_shared_with)],
    }


def describe_table(table: CalibrationTable) -> dict[str, Any]:
    return {
        "name": table.name.strip(),
        "category": table.category,
        "unit": table.unit,
        "formula": table.formula,
        "description": table.description,
        "storage": table.storage,
        "shape": f"{len(table.values)}x{len(table.values[0]) if table.values else 0}",
        "layout": "values[row][col]; row follows y_axis, col follows x_axis",
        "x_axis": _axis(table, "x"),
        "y_axis": _axis(table, "y"),
        "values": [[_round(v) for v in row] for row in table.values],
        "step": table.fine_increment,
    }


def read_map(session: RomSession, name: str, include_checkpoint: bool = False) -> dict[str, Any]:
    map_id = find_map(session.parser, name)
    result = describe_table(session.load_table(map_id))
    result["changed_since_checkpoint"] = map_id in session.changed
    if include_checkpoint:
        cp = session.checkpoint(map_id)
        result["checkpoint"] = {
            "values": [[_round(v) for v in row] for row in cp.values],
            "x_axis": [_round(v) for v in cp.x_axis],
            "y_axis": [_round(v) for v in cp.y_axis],
        }
    return result


def read_reference_map(parser: EcuParser, name: str) -> dict[str, Any]:
    """Карта из другой (справочной) прошивки — только чтение."""
    result = describe_table(parser.load_table(find_map(parser, name)))
    result["rom"] = {"file": parser.file_path, "xmlid": parser.xmlid}
    return result


# --- Предложение правки ------------------------------------------------------------------


@dataclass(frozen=True)
class CellChange:
    row: int
    col: int
    old: float
    new: float  # что ляжет в ROM (после квантования типа)
    requested: float  # что просил ИИ


@dataclass(frozen=True)
class AxisPointChange:
    axis: str  # "x" | "y"
    index: int
    old: float
    new: float
    requested: float


@dataclass
class EditProposal:
    """Правка карты от ИИ: что было и что станет; пишется только после подтверждения."""

    map_id: str
    reason: str
    table: CalibrationTable  # карта до правки (для показа)
    parser: EcuParser  # образ, для которого посчитана правка
    cells: list[CellChange] = field(default_factory=list)
    axes: list[AxisPointChange] = field(default_factory=list)
    # Другие таблицы на изменяемой общей оси: (имя, их ось)
    shared: list[tuple[str, str]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.map_id.strip()

    @property
    def empty(self) -> bool:
        return not self.cells and not self.axes


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"{what}: expected a number, got {value!r}")
    if not math.isfinite(value):
        raise ToolError(f"{what}: value must be finite")
    return float(value)


def plan_edit(
    session: RomSession,
    name: str,
    reason: str,
    *,
    values: list[list[float]] | None = None,
    cells: list[dict[str, Any]] | None = None,
    x_axis: list[float] | None = None,
    y_axis: list[float] | None = None,
) -> EditProposal:
    """Проверить запрос правки и посчитать, что реально ляжет в ROM (запись на копии образа):
    пользователь подтверждает итоговые значения. Совпадающие с текущими отбрасываются."""
    parser = session.parser
    map_id = find_map(parser, name)
    table = session.load_table(map_id)
    rows, cols = len(table.values), len(table.values[0]) if table.values else 0
    proposal = EditProposal(map_id=map_id, reason=reason.strip(), table=table, parser=parser)

    targets: dict[tuple[int, int], float] = {}
    if values is not None:
        if len(values) != rows or any(len(r) != cols for r in values):
            raise ToolError(f"values must be {rows}x{cols} (rows follow y_axis, columns follow x_axis)")
        for r, row in enumerate(values):
            for c, v in enumerate(row):
                targets[(r, c)] = _number(v, f"values[{r}][{c}]")
    for item in cells or []:
        try:
            r, c = int(item["row"]), int(item["col"])
        except (KeyError, TypeError, ValueError):
            raise ToolError(f"cell must be {{row, col, value}}, got {item!r}") from None
        if not (0 <= r < rows and 0 <= c < cols):
            raise ToolError(f"cell ({r}, {c}) is outside the {rows}x{cols} map")
        targets[(r, c)] = _number(item.get("value"), f"cell ({r}, {c})")
    rom = bytearray(parser.rom_bytes)
    table_def = parser.get_table_def(map_id)
    for (r, c), requested in sorted(targets.items()):
        old = table.values[r][c]
        if abs(requested - old) < _SAME:
            continue
        stored = write_table_cell(rom, table_def, r, c, requested)
        if stored != old:
            proposal.cells.append(CellChange(r, c, old, stored, requested))

    for axis, new_axis in (("x", x_axis), ("y", y_axis)):
        if new_axis is None:
            continue
        current = table.x_axis if axis == "x" else table.y_axis
        labels = table.x_labels if axis == "x" else table.y_labels
        if labels is not None or not current:
            raise ToolError(f"{axis.upper()} axis is static or absent and cannot be edited")
        if len(new_axis) != len(current):
            raise ToolError(f"{axis}_axis must have {len(current)} points")
        changed = False
        axis_def = table_def.x_axis if axis == "x" else table_def.y_axis
        for i, v in enumerate(new_axis):
            requested = _number(v, f"{axis}_axis[{i}]")
            if abs(requested - current[i]) < _SAME:
                continue
            stored = write_axis_value(rom, axis_def, i, requested)
            if stored != current[i]:
                proposal.axes.append(AxisPointChange(axis, i, current[i], stored, requested))
                changed = True
        if changed:
            proposal.shared += parser.shared_axis_users(map_id, axis)
    if proposal.empty:
        raise ToolError(
            "Nothing to change: after quantization to the ROM data type the values equal the current ones"
        )
    return proposal


def apply_edit(session: RomSession, proposal: EditProposal) -> dict[str, Any]:
    """Записать подтверждённую правку (значения, которые видел пользователь). Пока ждали ответа,
    ROM могли сменить или карту — поправить вручную: тогда не пишем, ИИ перечитает карту."""
    if session.parser is not proposal.parser:
        raise ToolError("Another ROM was opened while the edit waited for confirmation; nothing was written")
    current = session.load_table(proposal.map_id)
    if current.values != proposal.table.values or current.x_axis != proposal.table.x_axis or current.y_axis != proposal.table.y_axis:
        raise ToolError("The map was changed while the edit waited for confirmation; read it again and re-propose")
    axes = []
    for point in proposal.axes:
        stored = session.write_axis(proposal.map_id, point.axis, point.index, point.new).stored
        axes.append({"axis": point.axis, "index": point.index, "requested": _round(point.requested), "stored": _round(stored)})
    for cell in proposal.cells:
        session.write_cell(proposal.map_id, cell.row, cell.col, cell.new)
    # Где ROM хранит не то, что просили (шаг типа данных, граница диапазона) — самые большие
    off = sorted(
        (c for c in proposal.cells if round(c.new, _DIGITS) != round(c.requested, _DIGITS)),
        key=lambda c: -abs(c.new - c.requested),
    )
    return {
        "applied": True,
        "map": proposal.name,
        "cells_written": len(proposal.cells),
        "axis_points_written": len(axes),
        "axis_points": axes,
        "quantized_cells": len(off),
        "max_quantization_error": _round(max((abs(c.new - c.requested) for c in off), default=0.0)),
        "largest_quantization": [
            {"row": c.row, "col": c.col, "requested": _round(c.requested), "stored": _round(c.new)} for c in off[:10]
        ],
        "shared_axis_tables_affected": sorted({n.strip() for n, _ in proposal.shared}),
        "note": "Written to the ROM image in memory; the user saves the file manually.",
    }


def describe_proposal(proposal: EditProposal) -> dict[str, Any]:
    """Кратко о предложении — в ответ ИИ, если пользователь отказал."""
    return {"map": proposal.name, "cells": len(proposal.cells), "axis_points": len(proposal.axes)}


def reference_path(value: str, base: str | None) -> Path:
    """Путь к справочной прошивке: абсолютный или относительно папки открытого ROM."""
    path = Path(value).expanduser()
    if not path.is_absolute() and base:
        path = Path(base).parent / path
    if not path.is_file():
        raise ToolError(f"ROM file not found: {path}")
    return path
