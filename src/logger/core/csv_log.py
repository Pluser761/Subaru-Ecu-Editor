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

from src.logger.core.session import Sample

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
