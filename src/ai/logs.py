"""Логи для ИИ: чтение CSV (RomRaider, Car Scanner), сводка, выборка строк и раскладка по сетке карты.

Форматы:
- широкий: первая колонка — время (мс у RomRaider или «чч:мм:сс.ммм»), дальше параметры;
  разделитель «;» (тогда десятичная запятая) или «,»; пустые ячейки (разреженные логи
  Car Scanner) заполняются последним известным значением;
- длинный Car Scanner: «SECONDS;PID;VALUE;UNITS» — разворачивается в широкий так же.
Без Qt; тексты для ИИ — на английском.
"""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from src.ai.tools import ToolError

TIME = "Time (s)"
_OPS = {
    "<": np.less,
    "<=": np.less_equal,
    ">": np.greater,
    ">=": np.greater_equal,
    "==": np.equal,
    "!=": np.not_equal,
}
_STATS = ("mean", "min", "max", "count")


@dataclass(frozen=True)
class LogData:
    path: Path
    columns: list[str]  # первая — TIME, секунды от начала
    data: np.ndarray  # строки × колонки, nan — значения нет

    def column(self, name: str) -> np.ndarray:
        return self.data[:, self.columns.index(find_column(self, name))]


_cache: dict[Path, tuple[float, LogData]] = {}


def load_log(path: str | Path) -> LogData:
    """Прочитать лог (с кэшем по времени изменения файла)."""
    file = Path(path).expanduser()
    if not file.is_file():
        raise ToolError(f"Log file not found: {file}")
    mtime = file.stat().st_mtime
    cached = _cache.get(file)
    if cached and cached[0] == mtime:
        return cached[1]
    log = _parse(file)
    _cache[file] = (mtime, log)
    return log


def _parse(file: Path) -> LogData:
    text = file.read_text(encoding="utf-8-sig", errors="replace")
    first = text.split("\n", 1)[0]
    delimiter = ";" if first.count(";") > first.count(",") else ","
    rows = list(csv.reader(text.splitlines(), delimiter=delimiter))
    if not rows or len(rows) < 2:
        raise ToolError(f"Log is empty: {file}")
    header = [h.strip() for h in rows[0]]
    comma = delimiter == ";"
    if [h.upper() for h in header[:3]] == ["SECONDS", "PID", "VALUE"]:
        columns, matrix = _pivot_long(rows[1:], comma)
    else:
        while header and not header[-1]:
            header.pop()  # «…;» в конце строки
        columns = [TIME, *header[1:]]
        matrix = [[_number(cell, comma) for cell in row[: len(header)]] for row in rows[1:] if row]
        times = [_time(row[0]) if row else math.nan for row in rows[1:] if row]
        for line, t in zip(matrix, times):
            line[0] = t
            line.extend([math.nan] * (len(header) - len(line)))
    data = np.array(matrix, dtype=float).reshape(-1, len(columns))
    data = _forward_fill(data)
    if len(data) and math.isfinite(data[0, 0]):
        data[:, 0] -= data[0, 0]
        # RomRaider пишет миллисекунды
        if "msec" in (header[0] if header else "").lower():
            data[:, 0] /= 1000.0
    return LogData(file, columns, data)


def _pivot_long(rows: list[list[str]], comma: bool) -> tuple[list[str], list[list[float]]]:
    columns = [TIME]
    index: dict[str, int] = {}
    state: list[float] = [math.nan]
    out: list[list[float]] = []
    last_time: float | None = None
    for row in rows:
        if len(row) < 3:
            continue
        t = _number(row[0], comma)
        name = row[1].strip() + (f" ({row[3].strip()})" if len(row) > 3 and row[3].strip() else "")
        if name not in index:
            index[name] = len(columns)
            columns.append(name)
            state.append(math.nan)
        if last_time is not None and t != last_time:
            out.append(list(state))
        state[0] = t
        state[index[name]] = _number(row[2], comma)
        last_time = t
    if last_time is not None:
        out.append(list(state))
    return columns, [line + [math.nan] * (len(columns) - len(line)) for line in out]


def _number(cell: str, comma: bool) -> float:
    cell = cell.strip().strip('"')
    if comma:
        cell = cell.replace(",", ".")
    try:
        return float(cell)
    except ValueError:
        return math.nan


_CLOCK = re.compile(r"^(\d+):(\d+):(\d+(?:[.,]\d+)?)$")


def _time(cell: str) -> float:
    cell = cell.strip().strip('"')
    match = _CLOCK.match(cell)
    if match:
        h, m, s = match.groups()
        return int(h) * 3600 + int(m) * 60 + float(s.replace(",", "."))
    return _number(cell, True)


def _forward_fill(data: np.ndarray) -> np.ndarray:
    filled = data.copy()
    for c in range(filled.shape[1]):
        col = filled[:, c]
        mask = np.isnan(col)
        if not mask.any() or mask.all():
            continue
        idx = np.where(~mask, np.arange(len(col)), 0)
        np.maximum.accumulate(idx, out=idx)
        filled[:, c] = col[idx]
        # До первого значения — nan, а не значение из строки 0
        first = int(np.argmax(~mask))
        filled[:first, c] = math.nan
    return filled


def find_column(log: LogData, name: str) -> str:
    """Колонка по имени: точно, без регистра или по уникальной подстроке."""
    if name in log.columns:
        return name
    lower = name.strip().lower()
    exact = [c for c in log.columns if c.lower() == lower]
    if exact:
        return exact[0]
    partial = [c for c in log.columns if lower in c.lower()]
    if len(partial) == 1:
        return partial[0]
    if partial:
        raise ToolError(f"Column {name!r} is ambiguous: {partial}")
    raise ToolError(f"Column {name!r} not found. Columns: {log.columns}")


def _mask(log: LogData, where: list[dict[str, Any]] | None) -> np.ndarray:
    mask = np.ones(len(log.data), dtype=bool)
    for cond in where or []:
        try:
            column, op, value = cond["column"], cond["op"], float(cond["value"])
        except (KeyError, TypeError, ValueError):
            raise ToolError(f"Condition must be {{column, op, value}}, got {cond!r}") from None
        if op not in _OPS:
            raise ToolError(f"Unknown op {op!r}; use one of {list(_OPS)}")
        values = log.column(column)
        with np.errstate(invalid="ignore"):
            mask &= _OPS[op](values, value)
    return mask


def _finite(values: np.ndarray) -> np.ndarray:
    return values[np.isfinite(values)]


def list_logs(directory: Path) -> list[dict[str, Any]]:
    if not directory.is_dir():
        raise ToolError(f"Directory not found: {directory}")
    files = sorted(p for p in directory.rglob("*") if p.suffix.lower() in (".csv", ".txt") and p.is_file())
    return [{"path": str(p), "size_kb": round(p.stat().st_size / 1024)} for p in files]


def summary(path: str | Path) -> dict[str, Any]:
    log = load_log(path)
    stats = []
    for i, name in enumerate(log.columns):
        values = _finite(log.data[:, i])
        if not len(values):
            stats.append({"column": name, "samples": 0})
            continue
        stats.append({
            "column": name,
            "samples": int(len(values)),
            "min": _r(values.min()),
            "max": _r(values.max()),
            "mean": _r(values.mean()),
        })
    duration = _finite(log.data[:, 0])
    return {
        "path": str(log.path),
        "rows": int(len(log.data)),
        "duration_s": _r(duration.max()) if len(duration) else None,
        "columns": stats,
    }


def rows(
    path: str | Path,
    columns: list[str],
    where: list[dict[str, Any]] | None = None,
    every: int = 1,
    limit: int = 200,
) -> dict[str, Any]:
    """Строки лога: выбранные колонки, фильтр, прореживание, ограничение числа строк."""
    log = load_log(path)
    names = [TIME, *(find_column(log, c) for c in columns if find_column(log, c) != TIME)]
    idx = [log.columns.index(n) for n in names]
    selected = np.flatnonzero(_mask(log, where))[:: max(1, every)]
    total = len(selected)
    selected = selected[: max(1, min(limit, 2000))]
    return {
        "columns": names,
        "matched_rows": int(total),
        "returned_rows": int(len(selected)),
        "rows": [[_r(log.data[i, j]) for j in idx] for i in selected],
    }


def to_map_grid(
    path: str | Path,
    value_column: str,
    x_column: str | None,
    x_axis: list[float],
    y_column: str | None,
    y_axis: list[float],
    where: list[dict[str, Any]] | None = None,
    stat: str = "mean",
    min_samples: int = 1,
) -> dict[str, Any]:
    """Раскладка значения лога по ячейкам карты: точка лога попадает в ближайшую точку осей
    (как «scatter → table» в RomRaider). Оси — значения осей карты в единицах лога."""
    if stat not in _STATS:
        raise ToolError(f"stat must be one of {_STATS}")
    log = load_log(path)
    mask = _mask(log, where)
    value = log.column(value_column)
    mask &= np.isfinite(value)
    cols = len(x_axis) or 1
    rows_n = len(y_axis) or 1
    col_idx = _nearest(log, x_column, x_axis, mask) if x_axis else np.zeros(len(value), dtype=int)
    row_idx = _nearest(log, y_column, y_axis, mask) if y_axis else np.zeros(len(value), dtype=int)
    mask &= (col_idx >= 0) & (row_idx >= 0)

    grid: list[list[float | None]] = []
    counts: list[list[int]] = []
    for r in range(rows_n):
        grid_row: list[float | None] = []
        count_row: list[int] = []
        for c in range(cols):
            cell = value[mask & (row_idx == r) & (col_idx == c)]
            count_row.append(int(len(cell)))
            if len(cell) < max(1, min_samples):
                grid_row.append(None)
            elif stat == "count":
                grid_row.append(float(len(cell)))
            else:
                grid_row.append(_r(getattr(np, stat)(cell)))
        grid.append(grid_row)
        counts.append(count_row)
    return {
        "value_column": find_column(log, value_column),
        "stat": stat,
        "x_column": find_column(log, x_column) if x_column and x_axis else None,
        "y_column": find_column(log, y_column) if y_column and y_axis else None,
        "x_axis": x_axis,
        "y_axis": y_axis,
        "samples_used": int(mask.sum()),
        "grid": grid,  # [row][col], None — мало точек
        "counts": counts,
    }


def _nearest(log: LogData, column: str | None, axis: list[float], mask: np.ndarray) -> np.ndarray:
    if not column:
        raise ToolError("Axis column is required when the map has this axis")
    values = log.column(column)
    points = np.asarray(axis, dtype=float)
    result = np.full(len(values), -1, dtype=int)
    ok = np.isfinite(values)
    result[ok] = np.abs(values[ok, None] - points[None, :]).argmin(axis=1)
    return result


def _r(value: float) -> float | None:
    return round(float(value), 4) if math.isfinite(value) else None
