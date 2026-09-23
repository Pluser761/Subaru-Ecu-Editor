from __future__ import annotations

import math
import struct
from functools import lru_cache

import numpy as np

from src.core.models import AxisDef, Scaling, TableDef
from src.core.scaling import eval_expression, eval_expression_array


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


def _struct_format(storagetype: str, endian: str) -> tuple[str, int]:
    st = storagetype.lower()
    fmt, size = _TYPE_FMT.get((st, endian.lower()), ("B", 1))
    # uint8/int8 ignore endian in struct module single-byte
    if size == 1:
        fmt = "b" if st == "int8" else "B"
    return fmt, size


def read_raw_values(
    rom: bytes | bytearray,
    address: int,
    count: int,
    storagetype: str,
    endian: str,
) -> list[float]:
    fmt, size = _struct_format(storagetype, endian)

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


def axis_title(axis: AxisDef | None) -> str:
    """Название оси с единицами: «Engine Speed, RPM». Единицы не дублируются, если уже в имени."""
    if axis is None:
        return ""
    name = axis.name.strip()
    units = axis.scaling.units.strip() if axis.scaling else ""
    if units and units.lower() not in name.lower():
        return f"{name}, {units}" if name else units
    return name


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


def read_axis_values(rom: bytes | bytearray, axis: AxisDef | None, expected_len: int) -> list[float]:
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


def read_table_matrix(rom: bytes | bytearray, table: TableDef) -> list[list[float]]:
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


# --- Запись -----------------------------------------------------------------

# Диапазоны сырых целых значений по storagetype
_INT_RANGES: dict[str, tuple[int, int]] = {
    "uint8": (0, 0xFF),
    "int8": (-0x80, 0x7F),
    "uint16": (0, 0xFFFF),
    "int16": (-0x8000, 0x7FFF),
    "uint32": (0, 0xFFFF_FFFF),
}
# Для типов не шире 16 бит кодируем перебором всех сырых значений (≤ 65536)
_LOOKUP_MAX_BITS = 16
# Совпадение с точностью отображения: «то же значение» — ничего не меняем
_DISPLAY_DECIMALS = 2


@lru_cache(maxsize=256)
def _raw_lookup(expression: str, storagetype: str) -> tuple[np.ndarray, np.ndarray]:
    """Все сырые значения типа и их физические значения по expression."""
    lo, hi = _INT_RANGES[storagetype]
    raws = np.arange(lo, hi + 1, dtype=np.int64)
    phys = eval_expression_array(expression, raws)
    return raws, phys


def _uses_lookup(storagetype: str) -> bool:
    st = storagetype.lower()
    if st not in _INT_RANGES:
        return False
    lo, hi = _INT_RANGES[st]
    return (hi - lo + 1) <= (1 << _LOOKUP_MAX_BITS)


def encode_value(
    value: float,
    scaling: Scaling | None,
    storagetype: str,
    current_raw: float,
) -> float:
    """Физическое значение → сырое для записи в ROM.

    Целые до 16 бит: берём сырое значение, чей пересчёт ближе всего к value.
    Если ближайшим оказалось текущее, а value отличается от текущего на экране,
    сдвигаемся на соседнее представимое значение в нужную сторону — иначе мелкий
    шаг на грубой шкале (например, AFR в uint8) «застревал» бы на месте.
    float / uint32: обратная формула to_byte из definition.
    """
    st = storagetype.lower()
    expression = scaling.expression if scaling and scaling.expression else "x"
    to_byte = scaling.to_byte if scaling and scaling.to_byte else "x"

    if _uses_lookup(st):
        raws, phys = _raw_lookup(expression, st)
        lo = _INT_RANGES[st][0]
        current = int(current_raw)
        current_phys = float(phys[current - lo])
        if round(value, _DISPLAY_DECIMALS) == round(current_phys, _DISPLAY_DECIMALS):
            return float(current)
        finite = np.isfinite(phys)
        if not finite.any():
            raise ValueError(f"Scaling «{expression}» не даёт конечных значений")
        distance = np.where(finite, np.abs(phys - value), np.inf)
        best = int(raws[int(np.argmin(distance))])
        if best == current:
            direction = 1.0 if value > current_phys else -1.0
            step = (phys - current_phys) * direction
            ahead = finite & (step > 0)
            if not ahead.any():
                return float(current)  # уже на границе диапазона типа
            best = int(raws[int(np.argmin(np.where(ahead, step, np.inf)))])
        return float(best)

    raw = eval_expression(to_byte, value)
    if not math.isfinite(raw):
        raise ValueError(f"Значение {value} не переводится в байты (to_byte «{to_byte}»)")
    if st in _INT_RANGES:
        lo, hi = _INT_RANGES[st]
        return float(min(max(round(raw), lo), hi))
    return raw


def _write_raw(rom: bytearray, address: int, storagetype: str, endian: str, raw: float) -> None:
    fmt, size = _struct_format(storagetype, endian)
    if address + size > len(rom):
        raise ValueError(f"ROM truncated: need {address + size} bytes for value at 0x{address:X}")
    packed = struct.pack(fmt, raw if fmt.endswith("f") else int(raw))
    rom[address : address + size] = packed


def _write_physical(
    rom: bytearray,
    address: int,
    storagetype: str,
    endian: str,
    scaling: Scaling | None,
    value: float,
) -> float:
    """Записать одно значение; вернуть то, что реально легло в ROM (после квантования)."""
    (current_raw,) = read_raw_values(rom, address, 1, storagetype, endian)
    raw = encode_value(value, scaling, storagetype, current_raw)
    if raw != current_raw:
        _write_raw(rom, address, storagetype, endian, raw)
    (stored_raw,) = read_raw_values(rom, address, 1, storagetype, endian)
    return apply_scaling([stored_raw], scaling)[0]


def write_table_cell(rom: bytearray, table: TableDef, row: int, col: int, value: float) -> float:
    if table.storageaddress is None:
        raise ValueError(f"Table '{table.name}' has no storageaddress")
    rows, cols = table_dimensions(table)
    if not (0 <= row < rows and 0 <= col < cols):
        raise IndexError(f"Cell ({row}, {col}) outside {rows}×{cols}")
    _, size = _struct_format(table.storagetype, table.endian)
    # Тот же порядок, что при чтении: row-major, Y затем X
    address = table.storageaddress + (row * cols + col) * size
    return _write_physical(rom, address, table.storagetype, table.endian, table.scaling, value)


def write_axis_value(rom: bytearray, axis: AxisDef | None, index: int, value: float) -> float:
    if axis is None or is_static_axis(axis) or axis.storageaddress is None:
        raise ValueError("Ось не хранится в ROM и не редактируется")
    if index < 0 or (axis.size is not None and index >= axis.size):
        raise IndexError(f"Axis index {index} out of range")
    _, size = _struct_format(axis.storagetype, axis.endian)
    address = axis.storageaddress + index * size
    return _write_physical(rom, address, axis.storagetype, axis.endian, axis.scaling, value)
