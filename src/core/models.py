from __future__ import annotations

from pydantic import BaseModel, Field


class CalibrationTable(BaseModel):
    """Таблица калибровки: имя и матрица значений."""

    name: str
    values: list[list[float]]
    x_axis: list[float] = Field(default_factory=list)
    y_axis: list[float] = Field(default_factory=list)
    # Подписи статических осей (Static X/Y Axis) как есть: «1st», «Enable Above», «-40».
    # Такие оси не хранятся в ROM и не редактируются; None — ось числовая из ROM.
    x_labels: list[str] | None = None
    y_labels: list[str] | None = None
    # Подписи осей для UI: «Engine Load, g/rev»; пусто — оси нет
    x_title: str = ""
    y_title: str = ""
    unit: str = ""
    # Шаги изменения из scaling (fineincrement / coarseincrement), если заданы
    fine_increment: float | None = None
    coarse_increment: float | None = None

    @property
    def rows(self) -> int:
        return len(self.values)

    @property
    def cols(self) -> int:
        return len(self.values[0]) if self.values else 0


class MapInfo(BaseModel):
    """Элемент списка карт в редакторе."""

    id: str
    name: str
    category: str = "Maps"
    rows: int
    cols: int
    unit: str = ""
    table_type: str = "3D"


class Scaling(BaseModel):
    units: str = ""
    expression: str = "x"
    to_byte: str = "x"
    format: str = "0.00"
    fine_increment: float | None = None
    coarse_increment: float | None = None


class AxisDef(BaseModel):
    axis_type: str  # X Axis | Y Axis | Static X Axis | Static Y Axis
    name: str = ""
    storageaddress: int | None = None
    storagetype: str = "uint16"
    endian: str = "big"
    size: int | None = None
    scaling: Scaling | None = None
    static_data: list[str] = Field(default_factory=list)


class TableDef(BaseModel):
    name: str
    category: str = "Maps"
    table_type: str = "3D"  # 3D | 2D | 1D | Switch
    storageaddress: int | None = None
    storagetype: str = "uint8"
    endian: str = "big"
    sizex: int | None = None
    sizey: int | None = None
    scaling: Scaling | None = None
    x_axis: AxisDef | None = None
    y_axis: AxisDef | None = None
    description: str = ""


class RomId(BaseModel):
    xmlid: str
    base: str | None = None
    internalidaddress: int | None = None
    internalidstring: str | None = None
    ecuid: str | None = None
    filesize: int | None = None
    market: str = ""
    make: str = ""
    model: str = ""
    submodel: str = ""
    year: str = ""
    memmodel: str = ""


class RomDefinition(BaseModel):
    romid: RomId
    tables: dict[str, TableDef] = Field(default_factory=dict)
