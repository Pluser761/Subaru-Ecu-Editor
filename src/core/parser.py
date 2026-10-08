from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from src.core.checksum import TABLE_NAME as CHECKSUM_TABLE
from src.core.ecu_lookup import has_denso_lookup
from src.core.models import AxisDef, CalibrationTable, MapInfo, RomDefinition, SwitchInfo, TableDef
from src.core.rom_io import (
    axis_title,
    is_writable_axis,
    read_axis_labels,
    read_axis_values,
    read_table_matrix,
    table_dimensions,
    write_axis_value,
    write_table_cell,
)
from src.core.xml_reader import EDITABLE_TABLE_TYPES, EcuDefsRepository


# «(P0030) FRONT O2 SENSOR RANGE/PERF» → код и название
_DTC_CODE = re.compile(r"^\((P[0-9A-F]{4})\)\s*(.*)$")


class EcuParser:
    """ROM-образ + resolved definition."""

    def __init__(
        self,
        file_path: str | None = None,
        *,
        rom_bytes: bytes | None = None,
        definition: RomDefinition | None = None,
        defs_repo: EcuDefsRepository | None = None,
    ) -> None:
        self.file_path: str | None = file_path
        # Изменяемая копия образа: правки из редактора пишутся сюда
        self.rom_bytes: bytearray = bytearray(rom_bytes or b"")
        self.definition: RomDefinition | None = definition
        self.defs_repo = defs_repo
        self._axis_users: dict[int, list[str]] | None = None
        # Проверяется по коду прошивки один раз: код при правках карт не меняется
        self._ecu_interpolation: bool | None = None

    @classmethod
    def open_rom(
        cls,
        rom_path: str | Path,
        defs_path: str | Path,
        *,
        defs_repo: EcuDefsRepository | None = None,
    ) -> EcuParser:
        path = Path(rom_path)
        data = path.read_bytes()
        repo = defs_repo or EcuDefsRepository.load(defs_path)
        definition = repo.identify(data)
        if definition is None:
            raise LookupError(
                "Не удалось определить ECU definition для этой прошивки "
                "(нет совпадения internalid / filesize)"
            )
        return cls(
            file_path=str(path),
            rom_bytes=data,
            definition=definition,
            defs_repo=repo,
        )

    @property
    def xmlid(self) -> str | None:
        if self.definition is None:
            return None
        return self.definition.romid.xmlid

    def list_maps(self) -> list[MapInfo]:
        if self.definition is None:
            return []
        maps: list[MapInfo] = []
        for name, table in self.definition.tables.items():
            if table.table_type not in EDITABLE_TABLE_TYPES:
                continue
            if table.storageaddress is None:
                continue
            rows, cols = table_dimensions(table)
            unit = table.scaling.units if table.scaling else ""
            maps.append(
                MapInfo(
                    id=name,
                    name=name,
                    category=table.category or "Maps",
                    rows=rows,
                    cols=cols,
                    unit=unit,
                    table_type=table.table_type,
                    userlevel=table.userlevel,
                )
            )
        maps.sort(key=lambda m: (m.category, m.name))
        return maps

    @property
    def ecu_interpolation(self) -> bool:
        """В прошивке тот же код чтения карт, что разобран в core.ecu_lookup (SH7055/SH7058)."""
        if self._ecu_interpolation is None:
            self._ecu_interpolation = has_denso_lookup(self.rom_bytes)
        return self._ecu_interpolation

    # --- Переключатели (Switch): ошибки DTC и исправления ---

    def list_switches(self) -> list[SwitchInfo]:
        """Переключатели этой прошивки (с адресом и вариантами on/off), кроме таблицы
        контрольных сумм; по категории и имени."""
        if self.definition is None:
            return []
        switches = [
            self.switch_info(name)
            for name, table in self.definition.tables.items()
            if table.table_type == "Switch"
            and table.name != CHECKSUM_TABLE
            and table.storageaddress is not None
            and {"on", "off"} <= set(table.states)
        ]
        return sorted(switches, key=lambda s: (s.category, s.code or s.name))

    def switch_info(self, switch_id: str) -> SwitchInfo:
        table = self.get_table_def(switch_id)
        raw = self._switch_bytes(table).hex()
        state = next((name for name, data in table.states.items() if data == raw), None)
        match = _DTC_CODE.match(table.name)
        return SwitchInfo(
            id=switch_id,
            name=match.group(2).strip() if match else table.name.strip(),
            category=table.category,
            description=table.description.strip(),
            code=match.group(1) if match else "",
            state=state,
            raw=raw,
        )

    def write_switch(self, switch_id: str, state: str) -> None:
        """Записать байты состояния state («on» / «off») переключателя."""
        table = self.get_table_def(switch_id)
        if table.table_type != "Switch" or state not in table.states or table.storageaddress is None:
            raise ValueError(f"{switch_id!r}: нет состояния {state!r}")
        data = bytes.fromhex(table.states[state])
        address = table.storageaddress
        self.rom_bytes[address : address + len(data)] = data

    def _switch_bytes(self, table: TableDef) -> bytes:
        size = len(bytes.fromhex(next(iter(table.states.values()))))
        assert table.storageaddress is not None
        return bytes(self.rom_bytes[table.storageaddress : table.storageaddress + size])

    def checksum_table(self) -> tuple[int, int] | None:
        """(адрес, размер в байтах) таблицы контрольных сумм из definitions или None."""
        table = self.definition.tables.get(CHECKSUM_TABLE) if self.definition else None
        if table is None or table.storageaddress is None or not table.sizey:
            return None
        return table.storageaddress, table.sizey

    def axis_users(self) -> dict[int, list[str]]:
        """Адрес оси в ROM → редактируемые таблицы, которые её используют."""
        if self._axis_users is None:
            users: dict[int, list[str]] = defaultdict(list)
            tables = self.definition.tables if self.definition else {}
            for name, t in tables.items():
                if t.table_type not in EDITABLE_TABLE_TYPES or t.storageaddress is None:
                    continue
                for axis in (t.x_axis, t.y_axis):
                    if is_writable_axis(axis):
                        assert axis is not None and axis.storageaddress is not None
                        users[axis.storageaddress].append(name)
            self._axis_users = dict(users)
        return self._axis_users

    def shared_axis_tables(self, map_id: str, axis: str) -> list[str]:
        """Другие таблицы на той же оси в ROM (axis: "x" или "y"); пусто — ось не общая."""
        return [name for name, _ in self.shared_axis_users(map_id, axis)]

    def shared_axis_users(self, map_id: str, axis: str) -> list[tuple[str, str]]:
        """Другие таблицы на той же оси в ROM и какая это ось у них: [(имя, "x" | "y")].
        Одна ось бывает X у одних таблиц и Y у других (ECT: Y у 2D, X у 3D)."""
        table = self.get_table_def(map_id)
        axis_def = table.x_axis if axis == "x" else table.y_axis
        if not is_writable_axis(axis_def):
            return []
        assert axis_def is not None and axis_def.storageaddress is not None
        address = axis_def.storageaddress
        users: list[tuple[str, str]] = []
        for name in self.axis_users().get(address, []):
            if name == map_id:
                continue
            other = self.get_table_def(name)
            kind = "x" if other.x_axis is not None and other.x_axis.storageaddress == address else "y"
            users.append((name, kind))
        return users

    def get_table_def(self, map_id: str) -> TableDef:
        if self.definition is None:
            raise KeyError("ROM definition is not loaded")
        table = self.definition.tables.get(map_id)
        if table is None:
            raise KeyError(f"Unknown map: {map_id}")
        return table

    def write_cell(self, map_id: str, row: int, col: int, value: float) -> float:
        """Записать значение ячейки в rom_bytes; вернуть реально сохранённое (после квантования)."""
        table = self.get_table_def(map_id)
        return write_table_cell(self.rom_bytes, table, row, col, value)

    def write_axis(self, map_id: str, axis: str, index: int, value: float) -> float:
        """axis: "x" или "y". Вернуть реально сохранённое значение."""
        table = self.get_table_def(map_id)
        axis_def = table.x_axis if axis == "x" else table.y_axis
        return write_axis_value(self.rom_bytes, axis_def, index, value)

    def read_cell(self, map_id: str, row: int, col: int) -> float:
        return read_table_matrix(self.rom_bytes, self.get_table_def(map_id))[row][col]

    def read_axis(self, map_id: str, axis: str, index: int) -> float:
        table = self.get_table_def(map_id)
        rows, cols = table_dimensions(table)
        if axis == "x":
            return read_axis_values(self.rom_bytes, table.x_axis, cols)[index]
        return read_axis_values(self.rom_bytes, table.y_axis, rows)[index]

    def load_table(self, map_id: str) -> CalibrationTable:
        table = self.get_table_def(map_id)
        if not self.rom_bytes:
            raise RuntimeError("ROM bytes are not loaded")

        rows, cols = table_dimensions(table)
        values = read_table_matrix(self.rom_bytes, table)
        x_axis = read_axis_values(self.rom_bytes, table.x_axis, cols)
        y_axis = read_axis_values(self.rom_bytes, table.y_axis, rows)
        scaling = table.scaling
        unit = scaling.units if scaling else ""
        return CalibrationTable(
            name=table.name,
            values=values,
            x_axis=x_axis,
            y_axis=y_axis,
            x_labels=read_axis_labels(table.x_axis, cols),
            y_labels=read_axis_labels(table.y_axis, rows),
            x_title=axis_title(table.x_axis) if cols > 1 else "",
            y_title=axis_title(table.y_axis) if rows > 1 else "",
            x_shared_with=self.shared_axis_tables(map_id, "x") if cols > 1 else [],
            y_shared_with=self.shared_axis_tables(map_id, "y") if rows > 1 else [],
            unit=unit,
            fine_increment=scaling.fine_increment if scaling else None,
            coarse_increment=scaling.coarse_increment if scaling else None,
            x_increments=_axis_increments(table.x_axis),
            y_increments=_axis_increments(table.y_axis),
            scaling=scaling,
            ecu_interpolation=self.ecu_interpolation,
            description=table.description.strip(),
            formula=scaling.expression if scaling else "",
            category=table.category,
            userlevel=table.userlevel,
            storage=f"{table.storagetype} · 0x{table.storageaddress:X}" if table.storageaddress is not None else "",
        )


def _axis_increments(axis: AxisDef | None) -> tuple[float | None, float | None] | None:
    if not is_writable_axis(axis) or axis is None or axis.scaling is None:
        return None
    return axis.scaling.fine_increment, axis.scaling.coarse_increment
