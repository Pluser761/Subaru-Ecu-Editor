"""Перенос карт из донорской прошивки B в открытую прошивку A.

Правила (см. README, «Перенос карт»):
- размерность таблиц всегда остаётся как в A;
- размер по оси совпадает — ось и значения B переносятся как есть;
- размер отличается — ось A переписывается: первая/последняя точки из B, промежуточные
  пропорционально по индексам оси B; значения — билинейная интерполяция из B;
- статические оси (заданы в definition) переносятся как есть, по порядку;
- если переписанная ось A общая с другими таблицами, их значения пересчитываются
  со старой оси на новую; общую ось двух выбранных таблиц задаёт первая;
- переносить можно только между прошивками с одной корневой базой и процессором.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

import numpy as np

from src.core.models import AxisDef, TableDef
from src.core.parser import EcuParser
from src.core.rom_io import (
    is_static_axis,
    is_writable_axis,
    read_axis_values,
    table_dimensions,
    write_axis_values,
    write_table_values,
)
from src.core.xml_reader import EDITABLE_TABLE_TYPES


# --- Совместимость -------------------------------------------------------------


def check_compatibility(source: EcuParser, target: EcuParser) -> str | None:
    """None, если B можно переносить в A; иначе текст причины."""
    repo = target.defs_repo or source.defs_repo
    if repo is None or source.xmlid is None or target.xmlid is None:
        return "Прошивки не опознаны"
    src_base, dst_base = repo.root_base(source.xmlid), repo.root_base(target.xmlid)
    src_cpu, dst_cpu = repo.memmodel(source.xmlid), repo.memmodel(target.xmlid)
    if src_base != dst_base or src_cpu != dst_cpu:
        return (
            f"Прошивки несовместимы — B: {src_base or '?'} / {src_cpu or '?'}, "
            f"A: {dst_base or '?'} / {dst_cpu or '?'}"
        )
    return None


# --- План ------------------------------------------------------------------------


class AxisMode(Enum):
    NONE = "none"  # оси нет (размер 1)
    COPY = "copy"  # размер совпадает — как есть
    INTERPOLATE = "interpolate"  # размер отличается — ось A переписывается, значения интерполируются
    STATIC = "static"  # статическая ось — по порядку


@dataclass(frozen=True)
class AxisPlan:
    label: str
    mode: AxisMode
    source_count: int
    target_count: int


@dataclass(frozen=True)
class TransferCandidate:
    name: str
    category: str
    source_shape: tuple[int, int]
    target_shape: tuple[int, int]
    x: AxisPlan
    y: AxisPlan
    blocked: str | None = None

    @property
    def action(self) -> str:
        if self.blocked:
            return self.blocked
        interpolated = [a.label for a in (self.x, self.y) if a.mode is AxisMode.INTERPOLATE]
        if interpolated:
            return "интерполяция " + ", ".join(interpolated)
        return "как есть"


@dataclass
class TransferPlan:
    source_id: str
    target_id: str
    candidates: list[TransferCandidate]
    only_source: list[str]
    only_target: list[str]


def _transferable(tables: dict[str, TableDef]) -> dict[str, TableDef]:
    return {
        name: t
        for name, t in tables.items()
        if t.storageaddress is not None and t.table_type in EDITABLE_TABLE_TYPES
    }


def _strictly_monotonic(values: np.ndarray) -> bool:
    if len(values) < 2 or not np.all(np.isfinite(values)):
        return False
    diffs = np.diff(values)
    return bool(np.all(diffs > 0) or np.all(diffs < 0))


def _plan_axis(
    label: str,
    source: EcuParser,
    source_axis: AxisDef | None,
    target_axis: AxisDef | None,
    n_source: int,
    n_target: int,
) -> tuple[AxisPlan, str | None]:
    def plan(mode: AxisMode) -> AxisPlan:
        return AxisPlan(label, mode, n_source, n_target)

    if n_source == 1 and n_target == 1:
        return plan(AxisMode.NONE), None
    if (n_source == 1) != (n_target == 1):
        return plan(AxisMode.NONE), f"ось {label}: разная размерность ({n_source} и {n_target})"
    if is_static_axis(target_axis) or is_static_axis(source_axis) or target_axis is None:
        if n_source != n_target:
            return plan(AxisMode.STATIC), f"статическая ось {label}: {n_source} и {n_target} точек"
        return plan(AxisMode.STATIC), None
    if not is_writable_axis(target_axis):
        return plan(AxisMode.NONE), f"ось {label} в A не хранится в ROM"
    if source_axis is None or source_axis.storageaddress is None:
        return plan(AxisMode.NONE), f"у B нет оси {label}"
    if n_source == n_target:
        return plan(AxisMode.COPY), None
    source_values = np.array(read_axis_values(source.rom_bytes, source_axis, n_source))
    if not _strictly_monotonic(source_values):
        return plan(AxisMode.INTERPOLATE), f"ось {label} в B немонотонна"
    return plan(AxisMode.INTERPOLATE), None


def plan_transfer(source: EcuParser, target: EcuParser) -> TransferPlan:
    if source.definition is None or target.definition is None:
        raise ValueError("Прошивки не загружены")
    src_tables = _transferable(source.definition.tables)
    dst_tables = _transferable(target.definition.tables)

    candidates: list[TransferCandidate] = []
    for name in sorted(set(src_tables) & set(dst_tables)):
        s, t = src_tables[name], dst_tables[name]
        s_rows, s_cols = table_dimensions(s)
        t_rows, t_cols = table_dimensions(t)
        x, x_err = _plan_axis("X", source, s.x_axis, t.x_axis, s_cols, t_cols)
        y, y_err = _plan_axis("Y", source, s.y_axis, t.y_axis, s_rows, t_rows)
        blocked = x_err or y_err
        if s.table_type != t.table_type:
            blocked = f"разный тип таблицы ({s.table_type} и {t.table_type})"
        candidates.append(
            TransferCandidate(
                name=name,
                category=t.category,
                source_shape=(s_rows, s_cols),
                target_shape=(t_rows, t_cols),
                x=x,
                y=y,
                blocked=blocked,
            )
        )
    candidates.sort(key=lambda c: (c.category, c.name))
    return TransferPlan(
        source_id=source.xmlid or "",
        target_id=target.xmlid or "",
        candidates=candidates,
        only_source=sorted(set(src_tables) - set(dst_tables)),
        only_target=sorted(set(dst_tables) - set(src_tables)),
    )


# --- Интерполяция ------------------------------------------------------------------


def resample_axis(source_axis: np.ndarray, count: int) -> np.ndarray:
    """Ось из count точек: первая/последняя — как у донора, промежуточные — пропорционально
    по индексам оси донора (сохраняет распределение точек донора)."""
    src = np.asarray(source_axis, dtype=float)
    if count == len(src):
        return src.copy()
    positions = np.linspace(0.0, len(src) - 1, count)
    return np.interp(positions, np.arange(len(src)), src)


def _interp_1d(xp: np.ndarray, fp: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Линейная интерполяция по значениям оси; вне диапазона — крайние значения.
    Если ось немонотонна (статические подписи и т.п.), сопоставляем по индексу."""
    if len(xp) == 1:
        return np.full(len(x), fp[0])
    if _strictly_monotonic(xp):
        if xp[0] > xp[-1]:
            xp, fp = xp[::-1], fp[::-1]
        return np.interp(x, xp, fp)
    if len(xp) == len(x):
        return fp.copy()
    raise ValueError("ось немонотонна, а число точек различается")


def resample_grid(
    values: np.ndarray,
    src_x: np.ndarray,
    src_y: np.ndarray,
    dst_x: np.ndarray,
    dst_y: np.ndarray,
) -> np.ndarray:
    """Билинейная интерполяция матрицы [rows=Y, cols=X] с осей src на оси dst.
    В узлах, совпадающих с исходными, значения точные."""
    grid = np.asarray(values, dtype=float)
    along_x = np.array([_interp_1d(src_x, row, dst_x) for row in grid])
    return np.array([_interp_1d(src_y, col, dst_y) for col in along_x.T]).T


def _index_coords(n: int) -> np.ndarray:
    return np.arange(n, dtype=float)


# --- Применение ----------------------------------------------------------------------


class ResultStatus(Enum):
    COPIED = "перенесена как есть"
    INTERPOLATED = "интерполирована"
    RECALCULATED = "пересчитана на новую общую ось"
    SKIPPED = "пропущена"


@dataclass
class AxisChange:
    label: str
    before: tuple[float, float] | None
    after: tuple[float, float] | None


@dataclass
class TableResult:
    name: str
    status: ResultStatus
    source_shape: tuple[int, int] | None = None
    target_shape: tuple[int, int] | None = None
    axes: list[AxisChange] = field(default_factory=list)
    # Макс. |A после переноса − B| в точках донора (ужатие + квантование типа A)
    max_deviation: float | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class TransferReport:
    source_id: str
    target_id: str
    source_path: str
    target_path: str
    created: datetime
    results: list[TableResult]
    # Значения изменённых таблиц A до переноса — для рамок checkpoint
    before_values: dict[str, list[list[float]]]

    def count(self, status: ResultStatus) -> int:
        return sum(1 for r in self.results if r.status is status)


@dataclass
class _Grid:
    values: np.ndarray
    x: np.ndarray
    y: np.ndarray


def _read_grid(parser: EcuParser, name: str) -> _Grid:
    table = parser.load_table(name)
    return _Grid(np.array(table.values, dtype=float), np.array(table.x_axis), np.array(table.y_axis))


def _span(axis: np.ndarray) -> tuple[float, float] | None:
    return (float(axis[0]), float(axis[-1])) if len(axis) > 1 else None


def apply_transfer(
    source: EcuParser,
    target: EcuParser,
    names: list[str],
    plan: TransferPlan | None = None,
) -> TransferReport:
    """Перенести выбранные таблицы B в rom_bytes A."""
    reason = check_compatibility(source, target)
    if reason:
        raise ValueError(reason)
    plan = plan or plan_transfer(source, target)
    by_name = {c.name: c for c in plan.candidates}
    target_tables = target.definition.tables if target.definition else {}

    results: dict[str, TableResult] = {}
    selected: list[str] = []
    for name in names:
        candidate = by_name.get(name)
        if candidate is None:
            results[name] = TableResult(name, ResultStatus.SKIPPED, notes=["нет в обеих прошивках"])
        elif candidate.blocked:
            results[name] = TableResult(
                name,
                ResultStatus.SKIPPED,
                candidate.source_shape,
                candidate.target_shape,
                notes=[candidate.blocked],
            )
        else:
            selected.append(name)

    source_grids = {name: _read_grid(source, name) for name in selected}
    users = target.axis_users()
    before: dict[str, _Grid] = {}

    def snapshot(name: str) -> None:
        if name not in before:
            before[name] = _read_grid(target, name)

    for name in selected:
        snapshot(name)

    # 1. Желаемые оси A. Общую ось задаёт первая выбранная таблица.
    desired: dict[int, tuple[AxisDef, np.ndarray, str]] = {}
    notes: dict[str, list[str]] = defaultdict(list)
    for name in selected:
        candidate = by_name[name]
        t_def = target_tables[name]
        grid = source_grids[name]
        for axis_plan, axis_def, src_axis in (
            (candidate.x, t_def.x_axis, grid.x),
            (candidate.y, t_def.y_axis, grid.y),
        ):
            if axis_plan.mode not in (AxisMode.COPY, AxisMode.INTERPOLATE):
                continue
            assert axis_def is not None and axis_def.storageaddress is not None
            want = resample_axis(src_axis, axis_plan.target_count)
            address = axis_def.storageaddress
            if address in desired:
                _, current_want, owner = desired[address]
                if not np.allclose(current_want, want):
                    notes[name].append(
                        f"ось {axis_plan.label} общая с «{owner}» — значения интерполированы на её ось"
                    )
                continue
            desired[address] = (axis_def, want, name)

    # 2. Пишем оси; запоминаем, какие реально изменились (для пересчёта соседей)
    changed_axes: set[int] = set()
    for address, (axis_def, want, _owner) in desired.items():
        for sibling in users.get(address, []):
            snapshot(sibling)
        current = np.array(read_axis_values(target.rom_bytes, axis_def, len(want)))
        stored = write_axis_values(target.rom_bytes, axis_def, want)
        if not np.array_equal(stored, current):
            changed_axes.add(address)

    recalculated = [
        sibling
        for address in changed_axes
        for sibling in users.get(address, [])
        if sibling not in source_grids
    ]
    recalculated = list(dict.fromkeys(recalculated))  # без повторов, порядок сохраняем

    # 3. Значения: выбранные — из B, соседи по общей оси — из своих старых значений
    for name in selected + recalculated:
        t_def = target_tables[name]
        rows, cols = table_dimensions(t_def)
        after_axes = _read_grid(target, name)
        is_selected = name in source_grids
        src = source_grids[name] if is_selected else before[name]
        try:
            src_x = src.x if len(src.x) > 1 else _index_coords(len(src.x))
            src_y = src.y if len(src.y) > 1 else _index_coords(len(src.y))
            intended = resample_grid(src.values, src_x, src_y, after_axes.x, after_axes.y)
            stored = write_table_values(target.rom_bytes, t_def, intended.reshape(rows, cols))
        except ValueError as exc:
            results[name] = TableResult(name, ResultStatus.SKIPPED, notes=[str(exc)])
            continue

        old = before[name]
        axes = [
            AxisChange("X", _span(old.x), _span(after_axes.x)),
            AxisChange("Y", _span(old.y), _span(after_axes.y)),
        ]
        if is_selected:
            candidate = by_name[name]
            status = (
                ResultStatus.INTERPOLATED
                if AxisMode.INTERPOLATE in (candidate.x.mode, candidate.y.mode) or notes[name]
                else ResultStatus.COPIED
            )
            # Насколько карта A воспроизводит B в точках донора
            back = resample_grid(stored, after_axes.x, after_axes.y, src_x, src_y)
            finite = np.isfinite(back) & np.isfinite(src.values)
            deviation = float(np.max(np.abs(back - src.values)[finite])) if finite.any() else None
            results[name] = TableResult(
                name,
                status,
                candidate.source_shape,
                candidate.target_shape,
                axes,
                deviation,
                notes[name],
            )
        else:
            extra = []
            for change in axes:
                if change.before and change.after and (
                    min(change.after) < min(change.before) or max(change.after) > max(change.before)
                ):
                    extra.append(f"ось {change.label} шире старой — за её краями продлены крайние значения")
            results[name] = TableResult(
                name,
                ResultStatus.RECALCULATED,
                (rows, cols),
                (rows, cols),
                axes,
                None,
                ["общая ось переписана при переносе"] + extra,
            )

    ordered = [results[n] for n in names if n in results] + [
        results[n] for n in recalculated if n in results and n not in names
    ]
    changed = {r.name for r in ordered if r.status is not ResultStatus.SKIPPED}
    return TransferReport(
        source_id=plan.source_id,
        target_id=plan.target_id,
        source_path=source.file_path or "",
        target_path=target.file_path or "",
        created=datetime.now(),
        results=ordered,
        before_values={
            name: np.round(before[name].values, 2).tolist() for name in changed if name in before
        },
    )


# --- Отчёт -------------------------------------------------------------------------


def report_path(target_path: str, created: datetime) -> Path:
    """Рядом с файлом A: <имя A>.transfer-<дата_время>.md"""
    path = Path(target_path)
    return path.with_name(f"{path.stem}.transfer-{created:%Y-%m-%d_%H-%M-%S}.md")


def _md(text: str) -> str:
    return text.replace("|", "\\|")


def _fmt_span(span: tuple[float, float] | None) -> str:
    return "—" if span is None else f"{span[0]:.2f} … {span[1]:.2f}"


def _fmt_axis(change: AxisChange) -> str:
    if change.before is None and change.after is None:
        return "—"
    if change.before == change.after:
        return _fmt_span(change.after)
    return f"{_fmt_span(change.before)} → {_fmt_span(change.after)}"


def _fmt_shape(shape: tuple[int, int] | None) -> str:
    return "—" if shape is None else f"{shape[0]}×{shape[1]}"


def render_report(report: TransferReport) -> str:
    lines = [
        f"# Перенос карт {report.source_id} → {report.target_id}",
        "",
        f"- Дата: {report.created:%Y-%m-%d %H:%M:%S}",
        f"- Цель (A): **{report.target_id}** — `{report.target_path}`",
        f"- Донор (B): **{report.source_id}** — `{report.source_path}`",
        "- Метод: размерность A сохраняется. Если размер по оси совпадает — ось и значения B "
        "переносятся как есть; если отличается — ось A = весь диапазон B, промежуточные точки "
        "пропорционально по индексам оси B, значения — билинейная интерполяция.",
        "- Изменения сделаны в образе ROM в памяти; файл A не перезаписан.",
        "",
        "## Итог",
        "",
        "| Результат | Таблиц |",
        "|---|---:|",
    ]
    for status in ResultStatus:
        lines.append(f"| {status.value} | {report.count(status)} |")

    transferred = [
        r for r in report.results if r.status in (ResultStatus.COPIED, ResultStatus.INTERPOLATED)
    ]
    if transferred:
        lines += [
            "",
            "## Перенесённые таблицы",
            "",
            "Отклонение — максимальная разница между картой A после переноса и картой B "
            "в точках донора (интерполяция + квантование типа A).",
            "",
            "| Таблица | B → A | Результат | Ось X | Ось Y | Макс. отклонение | Примечания |",
            "|---|---|---|---|---|---:|---|",
        ]
        for r in transferred:
            deviation = "—" if r.max_deviation is None else f"{r.max_deviation:.3f}"
            x, y = (r.axes + [AxisChange("X", None, None), AxisChange("Y", None, None)])[:2]
            lines.append(
                f"| {_md(r.name)} | {_fmt_shape(r.source_shape)} → {_fmt_shape(r.target_shape)} "
                f"| {r.status.value} | {_fmt_axis(x)} | {_fmt_axis(y)} | {deviation} "
                f"| {_md('; '.join(r.notes))} |"
            )

    recalculated = [r for r in report.results if r.status is ResultStatus.RECALCULATED]
    if recalculated:
        lines += [
            "",
            "## Пересчитаны из-за общих осей",
            "",
            "Эти таблицы не выбирались, но делят ось с перенесёнными. Их значения "
            "интерполированы со старой оси на новую, чтобы не съехать.",
            "",
            "| Таблица | Ось X | Ось Y | Примечания |",
            "|---|---|---|---|",
        ]
        for r in recalculated:
            x, y = r.axes[:2]
            lines.append(f"| {_md(r.name)} | {_fmt_axis(x)} | {_fmt_axis(y)} | {_md('; '.join(r.notes))} |")

    skipped = [r for r in report.results if r.status is ResultStatus.SKIPPED]
    if skipped:
        lines += ["", "## Пропущено", "", "| Таблица | Причина |", "|---|---|"]
        for r in skipped:
            lines.append(f"| {_md(r.name)} | {_md('; '.join(r.notes))} |")

    return "\n".join(lines) + "\n"


def save_report(report: TransferReport) -> Path:
    path = report_path(report.target_path, report.created)
    path.write_text(render_report(report), encoding="utf-8")
    return path
