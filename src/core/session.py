"""Сессия открытого ROM: образ, checkpoint всех карт, изменённые карты — и правила записи.

Без Qt: презентер вызывает сценарии («записать ячейку», «поправить ось», «перенести карты»)
и показывает результат. Все checkpoint живут здесь, окна карт только рисуют их копию.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from src.core import checksum
from src.core.checkpoint import table_differs
from src.core.models import CalibrationTable, Checkpoint
from src.core.parser import EcuParser
from src.core.transfer import TransferPlan, TransferReport, apply_transfer


@dataclass(frozen=True)
class AxisWrite:
    """Результат правки оси: что легло в ROM и какие ещё таблицы на этой оси
    (у них это может быть другая ось: X вместо Y)."""

    stored: float
    shared: list[tuple[str, str]]  # (map_id, "x" | "y")


class RomSession:
    def __init__(self, parser: EcuParser) -> None:
        self.parser = parser
        # Правки есть, файл ещё не сохранён
        self.dirty = False
        # Checkpoint карт, которые уже открывали или меняли; живут, пока открыт ROM
        self._checkpoints: dict[str, Checkpoint] = {}
        self._changed: set[str] = set()
        # Почему сохранять нельзя (None — можно): суммы оригинала проверяются при открытии.
        # Сходятся — алгоритм для этого ЭБУ подходит; нет таблицы или не сходятся — не знаем как
        self.save_blocker = _save_blocker(parser)

    # --- Сохранение ---

    def save(self, path: str | Path) -> int:
        """Пересчитать контрольные суммы и записать образ в path (через временный файл);
        вернуть число исправленных сумм. ValueError — сохранять нельзя (save_blocker)."""
        if self.save_blocker:
            raise ValueError(self.save_blocker)
        table = self.parser.checksum_table()
        assert table is not None
        fixed = checksum.fix(self.parser.rom_bytes, *table)
        target = Path(path)
        fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(self.parser.rom_bytes)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self.parser.file_path = str(target)
        self.dirty = False
        return fixed

    # --- Карты и checkpoint ---

    def load_table(self, map_id: str) -> CalibrationTable:
        return self.parser.load_table(map_id)

    def checkpoint(self, map_id: str) -> Checkpoint:
        """Checkpoint карты; при первом обращении — её текущее состояние."""
        if map_id not in self._checkpoints:
            self._checkpoints[map_id] = _snapshot(self.load_table(map_id))
        return self._checkpoints[map_id].model_copy(deep=True)

    def set_checkpoint(self, map_id: str, checkpoint: Checkpoint | None = None) -> None:
        """Новая точка отсчёта (по умолчанию — текущее состояние карты); отметка изменения снимается."""
        self._checkpoints[map_id] = checkpoint or _snapshot(self.load_table(map_id))
        self._changed.discard(map_id)

    @property
    def changed(self) -> set[str]:
        """Карты, отличающиеся от своего checkpoint (по последнему refresh_changed)."""
        return set(self._changed)

    def refresh_changed(self, map_ids: set[str]) -> set[str]:
        """Перепроверить карты; вернуть актуальное множество изменённых."""
        for map_id in map_ids:
            checkpoint = self._checkpoints.get(map_id)
            if checkpoint is None:
                self._changed.discard(map_id)
                continue
            table = self.load_table(map_id)
            if table_differs(table.values, table.x_axis, table.y_axis, checkpoint):
                self._changed.add(map_id)
            else:
                self._changed.discard(map_id)
        return self.changed

    # --- Сценарии записи ---

    def write_cell(self, map_id: str, row: int, col: int, value: float) -> float:
        """Записать ячейку; вернуть реально сохранённое значение (после квантования)."""
        self.checkpoint(map_id)
        stored = self.parser.write_cell(map_id, row, col, value)
        self.dirty = True
        return stored

    def write_axis(self, map_id: str, axis: str, index: int, value: float) -> AxisWrite:
        """Записать точку оси. Общая ось меняется и у других таблиц — их checkpoint снимается
        до записи, иначе при первом открытии они примут новую ось за исходную."""
        shared = self.parser.shared_axis_users(map_id, axis)
        for name in (map_id, *(name for name, _ in shared)):
            self.checkpoint(name)
        stored = self.parser.write_axis(map_id, axis, index, value)
        self.dirty = True
        return AxisWrite(stored=stored, shared=shared)

    def write_switch(self, switch_id: str, state: str) -> None:
        """Включить/выключить переключатель (ошибку DTC и т.п.)."""
        self.parser.write_switch(switch_id, state)
        self.dirty = True

    def transfer(self, donor: EcuParser, names: list[str], plan: TransferPlan) -> TransferReport:
        """Перенос карт из донора. Checkpoint перенесённых — состояние до переноса, если у карты
        ещё не было своего (своя точка отсчёта пользователя важнее)."""
        report = apply_transfer(donor, self.parser, names, plan)
        for name, before in report.before.items():
            self._checkpoints.setdefault(name, before)
        if report.before:
            self.dirty = True
        return report


def _save_blocker(parser: EcuParser) -> str | None:
    if not parser.rom_bytes:
        return "ROM не открыт"
    table = parser.checksum_table()
    if table is None:
        return "Для этой прошивки неизвестна таблица контрольных сумм — сохранять небезопасно"
    try:
        valid = checksum.verify(parser.rom_bytes, *table)
    except ValueError as exc:
        return f"Таблица контрольных сумм не распознана: {exc}"
    if not valid:
        return (
            "Контрольные суммы исходного файла не сходятся — алгоритм для этого ЭБУ не подходит "
            "(или файл уже был изменён без пересчёта); сохранять небезопасно"
        )
    return None


def _snapshot(table: CalibrationTable) -> Checkpoint:
    return Checkpoint(values=table.values, x_axis=table.x_axis, y_axis=table.y_axis)
