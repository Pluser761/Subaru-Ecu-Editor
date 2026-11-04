"""Запись лога в CSV как у RomRaider: «Time (msec);Engine Speed (rpm);…».

Разделитель и десятичный знак — как в логах RomRaider на русской системе («;» и «,»),
их уже читают src/ai/logs.py и анализ в редакторе. Последняя колонка — отметки
(«Mark»: 1 в строке, где нажата отметка).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from src.logger.core.session import AflTable, FlkcTable, Sample

MARK_COLUMN = "Mark"


class CsvLogWriter:
    def __init__(
        self,
        out: TextIO,
        columns: Sequence[tuple[str, str, str, int]],
        separator: str = ";",
        decimal: str = ",",
    ) -> None:
        """columns: [(id параметра, имя, единицы, знаков после запятой)]."""
        self._out = out
        self._columns = list(columns)
        self._sep = separator
        self._decimal = decimal
        header = ["Time (msec)"] + [f"{name} ({units})" for _pid, name, units, _d in self._columns] + [MARK_COLUMN]
        out.write(separator.join(header) + "\n")

    @classmethod
    def open(cls, path: str | Path, columns: Sequence[tuple[str, str, str, int]], **kwargs: str) -> CsvLogWriter:
        return cls(Path(path).open("w", encoding="utf-8", newline=""), columns, **kwargs)

    def write(self, sample: Sample) -> None:
        cells = [str(round(sample.time_ms))]
        for pid, _name, _units, digits in self._columns:
            cells.append(self._number(sample.values.get(pid, math.nan), digits))
        cells.append(str(sample.mark))
        self._out.write(self._sep.join(cells) + "\n")

    def flush(self) -> None:
        self._out.flush()

    def close(self) -> None:
        self._out.close()

    def _number(self, value: float, digits: int) -> str:
        if math.isnan(value) or math.isinf(value):
            return ""
        return f"{value:.{digits}f}".replace(".", self._decimal)


_AFL_ZONES = ("A", "B", "C", "D")


def _learning_number(value: float, digits: int = 2) -> str:
    if math.isnan(value) or math.isinf(value):
        return ""
    return f"{value:.{digits}f}".replace(".", ",")


def _axis_number(value: float) -> str:
    """Значение оси без лишних нулей: 3200, 5.6, 1.64 (десятичный знак «,»)."""
    if math.isnan(value) or math.isinf(value):
        return ""
    return f"{value:g}".replace(".", ",")


def _axis_labels(axis: Sequence[float]) -> list[str]:
    """Подписи диапазонов по границам: «0 - a», «a - b», …, «z - ». Пустая ось — пустой список."""
    if not axis:
        return []
    labels = [f"0 - {_axis_number(axis[0])}"]
    for i in range(1, len(axis)):
        labels.append(f"{_axis_number(axis[i - 1])} - {_axis_number(axis[i])}")
    labels.append(f"{_axis_number(axis[-1])} - ")
    return labels


def write_learning_csv(
    path: str | Path,
    ecu_id: str,
    afl: AflTable,
    afl_ranges: Sequence[float],
    flkc: FlkcTable,
) -> None:
    """Одноразовая выгрузка таблиц обучения (A/F Learning #1 + FLKC) в CSV («;» и «,»)."""
    num = _learning_number
    lines: list[str] = [f"ECU;{ecu_id}", ""]

    # --- A/F Learning #1 ---
    lines.append("A/F Learning #1")
    lines.append("Zone;Airflow (g/s);Value (%)")
    zone_labels = _axis_labels(afl_ranges) or [""] * len(_AFL_ZONES)
    for i, zone in enumerate(_AFL_ZONES):
        air = zone_labels[i] if i < len(zone_labels) else ""
        lines.append(f"{zone};{air};{num(afl.stored.get(zone, math.nan))}")
    lines.append(f"Current;;{num(afl.current)}")
    lines.append(f"Range;;{afl.range_index or ''}")
    lines.append("")

    # --- FLKC ---
    rows = len(flkc.cells)
    cols = len(flkc.cells[0]) if rows else 0
    rpm_labels = _axis_labels(flkc.rpm_axis) or [""] * rows
    load_labels = _axis_labels(flkc.load_axis) or [""] * cols
    lines.append("Fine Learning Knock Correction (deg)")
    lines.append("Load \\ RPM;" + ";".join(rpm_labels[:rows]))
    for row in range(rows):
        label = load_labels[row] if row < len(load_labels) else ""
        cells = ";".join(num(flkc.cells[row][column]) for column in range(cols))
        lines.append(f"{label};{cells}")
    lines.append("")

    Path(path).write_text("\n".join(lines), encoding="utf-8", newline="")
