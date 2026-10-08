"""Единицы на экране: пересчёт из единиц definitions (RomRaider «standard»: psi, °F, MPH)
в метрические (bar, °C, km/h) и обратно.

ROM, сессия и checkpoint всегда в единицах definitions; пересчёт — только на границе с
окнами (презентер). Единица таблицы — последняя часть в скобках («Boost Error (psi)» → psi)
или вся строка («psi relative sea level»), поэтому «Target Boost (psia) Compensation (%)» —
проценты и не пересчитывается. Для нелинейных формул пересчёт встраивается в саму формулу
таблицы (display_scaling), чтобы расчёт ЭБУ в сырых числах и квантование оставались точными.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from src.core.models import AxisDef, CalibrationTable, Checkpoint, Scaling, TableDef
from src.core.rom_io import is_static_axis
from src.core.scaling import eval_expression

PSI_TO_BAR = 0.0689475729
_X = re.compile(r"\bx\b")


class UnitSystem(Enum):
    DEFINITIONS = "definitions"  # как в описаниях: psi, °F, MPH
    METRIC = "metric"  # bar, °C, km/h


@dataclass(frozen=True)
class Conversion:
    source: str  # единица в definitions («psi relative sea level»)
    target: str  # на экране («bar relative sea level»)
    to_display: str  # выражение от x: definitions → экран
    to_definition: str  # обратное
    scale: float  # для разностей (шаги клавишами): производная to_display
    decimals: int  # знаков после точки на экране

    def display(self, value: float) -> float:
        return eval_expression(self.to_display, value)

    def definition(self, value: float) -> float:
        return eval_expression(self.to_definition, value)

    def relabel(self, text: str) -> str:
        """Подпись с единицей definitions → с единицей экрана («Coolant Temperature, Degrees F»)."""
        return text.replace(self.source, self.target) if self.source in text else text


def unit_token(units: str) -> str:
    """Единица из подписи: последняя часть в скобках, иначе вся строка."""
    match = re.search(r"\(([^()]*)\)\s*$", units)
    return (match.group(1) if match else units).strip()


def conversion_for(units: str, system: UnitSystem) -> Conversion | None:
    """Как показывать величину с подписью units в системе system; None — как есть."""
    if system is UnitSystem.DEFINITIONS:
        return None
    token = unit_token(units)
    lower = token.lower()
    if lower.startswith("psi"):
        return Conversion(token, "bar" + token[3:], f"x*{PSI_TO_BAR}", f"x/{PSI_TO_BAR}", PSI_TO_BAR, 3)
    if lower in ("degrees f", "°f", "deg f", "f"):
        return Conversion(token, "°C", "(x-32)/1.8", "x*1.8+32", 1 / 1.8, 2)
    if lower == "mph":
        return Conversion(token, "km/h", "x*1.609344", "x/1.609344", 1.609344, 2)
    return None


def display_scaling(scaling: Scaling | None, conversion: Conversion | None) -> Scaling | None:
    """Формула таблицы, дающая сразу экранные единицы: expression → to_display(expression),
    to_byte → to_byte(to_definition(x)); шаги и подпись — в экранных единицах."""
    if scaling is None or conversion is None:
        return scaling
    return scaling.model_copy(
        update={
            "units": conversion.relabel(scaling.units),
            "expression": _X.sub(f"({scaling.expression})", conversion.to_display),
            "to_byte": _X.sub(f"({conversion.to_definition})", scaling.to_byte),
            "fine_increment": _scaled(scaling.fine_increment, conversion),
            "coarse_increment": _scaled(scaling.coarse_increment, conversion),
        }
    )


def _scaled(step: float | None, conversion: Conversion) -> float | None:
    return None if step is None else abs(step * conversion.scale)


@dataclass(frozen=True)
class TableUnits:
    """Пересчёт одной таблицы: значения, ось X, ось Y (None — как в definitions)."""

    value: Conversion | None = None
    x: Conversion | None = None
    y: Conversion | None = None

    @classmethod
    def for_table(cls, table: TableDef, system: UnitSystem) -> TableUnits:
        def axis(ax: AxisDef | None) -> Conversion | None:
            if ax is None or is_static_axis(ax) or ax.scaling is None:
                return None  # статические подписи («1st», «-40») не пересчитываются
            return conversion_for(ax.scaling.units, system)

        value = conversion_for(table.scaling.units, system) if table.scaling else None
        return cls(value, axis(table.x_axis), axis(table.y_axis))

    @property
    def identity(self) -> bool:
        return self.value is None and self.x is None and self.y is None

    def axis(self, name: str) -> Conversion | None:
        return self.x if name == "x" else self.y

    # --- definitions → экран ---

    def table(self, table: CalibrationTable) -> CalibrationTable:
        if self.identity:
            return table
        update: dict[str, object] = {}
        notes = []
        if self.value is not None:
            conv = self.value
            update.update(
                values=[[conv.display(v) for v in row] for row in table.values],
                unit=conv.relabel(table.unit),
                scaling=display_scaling(table.scaling, conv),
                fine_increment=_scaled(table.fine_increment, conv),
                coarse_increment=_scaled(table.coarse_increment, conv),
                decimals=conv.decimals,
            )
            notes.append(f"значения в {conv.target} (в definitions — {conv.source})")
        for name, axis_conv in (("x", self.x), ("y", self.y)):
            if axis_conv is None:
                continue
            increments = getattr(table, f"{name}_increments")
            update.update({
                f"{name}_axis": [axis_conv.display(v) for v in getattr(table, f"{name}_axis")],
                f"{name}_title": axis_conv.relabel(getattr(table, f"{name}_title")),
                f"{name}_increments": (
                    None if increments is None else (_scaled(increments[0], axis_conv), _scaled(increments[1], axis_conv))
                ),
                f"{name}_decimals": axis_conv.decimals,
            })
            notes.append(f"ось {name.upper()} в {axis_conv.target} (в definitions — {axis_conv.source})")
        update["units_note"] = "; ".join(notes)
        return table.model_copy(update=update)

    def checkpoint(self, checkpoint: Checkpoint) -> Checkpoint:
        return self._map_checkpoint(checkpoint, display=True)

    def value_display(self, value: float) -> float:
        return value if self.value is None else self.value.display(value)

    def axis_display(self, name: str, value: float) -> float:
        conv = self.axis(name)
        return value if conv is None else conv.display(value)

    # --- экран → definitions ---

    def checkpoint_definition(self, checkpoint: Checkpoint) -> Checkpoint:
        return self._map_checkpoint(checkpoint, display=False)

    def value_definition(self, value: float) -> float:
        return value if self.value is None else self.value.definition(value)

    def axis_definition(self, name: str, value: float) -> float:
        conv = self.axis(name)
        return value if conv is None else conv.definition(value)

    def _map_checkpoint(self, checkpoint: Checkpoint, *, display: bool) -> Checkpoint:
        if self.identity:
            return checkpoint

        def apply(conv: Conversion | None, value: float) -> float:
            if conv is None:
                return value
            return conv.display(value) if display else conv.definition(value)

        return Checkpoint(
            values=[[apply(self.value, v) for v in row] for row in checkpoint.values],
            x_axis=[apply(self.x, v) for v in checkpoint.x_axis],
            y_axis=[apply(self.y, v) for v in checkpoint.y_axis],
        )
