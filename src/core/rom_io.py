from __future__ import annotations

import struct

from src.core.models import AxisDef, Scaling, TableDef
from src.core.scaling import eval_expression


_TYPE_FMT = {
    ("uint8", "big"): ("B", 1),
    ("uint8", "little"): ("B", 1),
    ("int8", "big"): ("b", 1),
    ("int8", "little"): ("b", 1),
    ("uint16", "big"): (">H", 2),
    ("uint16", "little"): ("<H", 2),
    ("int16", "big"): (">h", 2),
    ("int16", "little"): ("<h", 2),
    ("uint32", "big"): (">I", 4),
    ("uint32", "little"): ("<I", 4),
    # Особенность RomRaider: для float атрибут endian инвертирован. В ecu_defs.xml
    # все float на SH7055/SH7058 (big-endian CPU) помечены endian="little",
    # и RomRaider читает их как big-endian. Повторяем, иначе оси 32-битных ROM — мусор.
    ("float", "big"): ("<f", 4),
    ("float", "little"): (">f", 4),
}


def storage_size(storagetype: str, endian: str = "big") -> int:
    key = (storagetype.lower(), endian.lower())
    if key not in _TYPE_FMT:
        # fallback uint8
        return 1
    return _TYPE_FMT[key][1]


def read_raw_values(
    rom: bytes,
    address: int,
    count: int,
    storagetype: str,
    endian: str,
) -> list[float]:
    st = storagetype.lower()
    en = endian.lower()
    fmt, size = _TYPE_FMT.get((st, en), ("B", 1))
    # uint8/int8 ignore endian in struct module single-byte
    if size == 1:
        fmt = "B" if st.startswith("u") else "b"

    out: list[float] = []
    for i in range(count):
        start = address + i * size
        end = start + size
        if end > len(rom):
            raise ValueError(
                f"ROM truncated: need {end} bytes for value at 0x{address:X}"
            )
        chunk = rom[start:end]
        if size == 1:
            (raw,) = struct.unpack(fmt, chunk)
        else:
            (raw,) = struct.unpack(fmt, chunk)
        out.append(float(raw))
    return out


def apply_scaling(raw_values: list[float], scaling: Scaling | None) -> list[float]:
    if scaling is None:
        return raw_values
    expr = scaling.expression or "x"
    return [eval_expression(expr, v) for v in raw_values]


def table_dimensions(table: TableDef) -> tuple[int, int]:
    """(rows, cols) для UI: rows=Y, cols=X."""
    t = table.table_type
    if t == "3D":
        cols = table.sizex or 1
        rows = table.sizey or 1
        return rows, cols
    if t == "2D":
        # обычно вектор по Y
        if table.sizey and table.sizey > 1:
            return table.sizey, 1
        if table.sizex and table.sizex > 1:
            return 1, table.sizex
        return table.sizey or 1, table.sizex or 1
    return 1, 1


def is_static_axis(axis: AxisDef | None) -> bool:
    return axis is not None and (axis.axis_type.startswith("Static") or bool(axis.static_data))


def read_axis_labels(axis: AxisDef | None, expected_len: int) -> list[str] | None:
    """Подписи статической оси как в definition; None для оси из ROM.

    У вектора (2D-таблица) второй оси нет — заголовок пустой и не редактируется.
    """
    if axis is None and expected_len == 1:
        return [""]
    if axis is None or not is_static_axis(axis):
        return None
    labels = [item.strip() for item in axis.static_data[:expected_len]]
    labels += [str(i) for i in range(len(labels), expected_len)]
    return labels


def read_axis_values(rom: bytes, axis: AxisDef | None, expected_len: int) -> list[float]:
    if axis is None:
        return [float(i) for i in range(expected_len)]

    if is_static_axis(axis):
        # Статические данные уже в физических единицах — scaling не применяем.
        # Нечисловые подписи («1st», «Enable Above») → индекс.
        values: list[float] = []
        for i, item in enumerate(axis.static_data[:expected_len]):
            try:
                values.append(float(item.strip().replace(",", ".")))
            except ValueError:
                values.append(float(i))
        values += [float(i) for i in range(len(values), expected_len)]
        return values

    if axis.storageaddress is None:
        return [float(i) for i in range(expected_len)]

    count = axis.size or expected_len
    raw = read_raw_values(
        rom,
        axis.storageaddress,
        count,
        axis.storagetype,
        axis.endian,
    )
    scaled = apply_scaling(raw, axis.scaling)
    if len(scaled) >= expected_len:
        return scaled[:expected_len]
    return scaled + [float(i) for i in range(len(scaled), expected_len)]


def read_table_matrix(rom: bytes, table: TableDef) -> list[list[float]]:
    if table.storageaddress is None:
        raise ValueError(f"Table '{table.name}' has no storageaddress")

    rows, cols = table_dimensions(table)
    count = rows * cols
    raw = read_raw_values(
        rom,
        table.storageaddress,
        count,
        table.storagetype,
        table.endian,
    )
    scaled = apply_scaling(raw, table.scaling)

    # RomRaider: values stored row-major by Y then X
    matrix: list[list[float]] = []
    idx = 0
    for _r in range(rows):
        row: list[float] = []
        for _c in range(cols):
            row.append(scaled[idx])
            idx += 1
        matrix.append(row)
    return matrix
