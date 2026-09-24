"""Сессия открытого ROM: образ, checkpoint всех карт, изменённые карты — и правила записи.

Без Qt: презентер вызывает сценарии («записать ячейку», «поправить ось», «перенести карты»)
и показывает результат. Все checkpoint живут здесь, окна карт только рисуют их копию.
"""

from __future__ import annotations

from dataclasses import dataclass

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

    def transfer(self, donor: EcuParser, names: list[str], plan: TransferPlan) -> TransferReport:
        """Перенос карт из донора. Checkpoint перенесённых — состояние до переноса, если у карты
        ещё не было своего (своя точка отсчёта пользователя важнее)."""
        report = apply_transfer(donor, self.parser, names, plan)
        for name, before in report.before.items():
            self._checkpoints.setdefault(name, before)
        if report.before:
            self.dirty = True
        return report


def _snapshot(table: CalibrationTable) -> Checkpoint:
    return Checkpoint(values=table.values, x_axis=table.x_axis, y_axis=table.y_axis)
