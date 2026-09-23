from __future__ import annotations

from pathlib import Path

from src.core.models import CalibrationTable, MapInfo, RomDefinition, TableDef
from src.core.rom_io import (
    read_axis_labels,
    read_axis_values,
    read_table_matrix,
    table_dimensions,
    write_axis_value,
    write_table_cell,
)
from src.core.xml_reader import EDITABLE_TABLE_TYPES, EcuDefsRepository


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
                )
            )
        maps.sort(key=lambda m: (m.category, m.name))
        return maps

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
            unit=unit,
            fine_increment=scaling.fine_increment if scaling else None,
            coarse_increment=scaling.coarse_increment if scaling else None,
        )
