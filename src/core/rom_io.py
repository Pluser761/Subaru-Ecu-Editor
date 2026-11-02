from __future__ import annotations

import struct
from functools import lru_cache

import numpy as np

from src.i18n import tr
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
    """Физическое значение → сырое для записи одной ячейки/точки оси (со «сдвигом»)."""
    (raw,) = encode_values(np.array([value]), scaling, storagetype, np.array([current_raw]), nudge=True)
    return float(raw)


def encode_values(
    values: np.ndarray,
    scaling: Scaling | None,
    storagetype: str,
    current_raw: np.ndarray,
    *,
    nudge: bool = False,
) -> np.ndarray:
    """Физические значения → сырые для записи в ROM.

    Целые до 16 бит — ближайшее представимое по expression. nudge=True (правка одной ячейки)
    добавляет «сдвиг от застревания»: если ближайшим оказалось текущее сырое, а значение на
    экране отличается, берётся соседнее в нужную сторону — иначе мелкий шаг на грубой шкале
    (например, AFR в uint8) не двигался бы. float / uint32 — обратная формула to_byte.
    Нечисловые значения (inf/nan) оставляют текущее сырое значение.
    """
    st = storagetype.lower()
    v = np.asarray(values, dtype=float).ravel()
    out = np.asarray(current_raw, dtype=float).ravel().copy()
    ok = np.isfinite(v)
    if not ok.any():
        return out

    expression = scaling.expression if scaling and scaling.expression else "x"
    to_byte = scaling.to_byte if scaling and scaling.to_byte else "x"

    if _uses_lookup(st):
        raws, phys = _raw_lookup(expression, st)
        finite = np.isfinite(phys)
        if nudge and not finite.any():
            raise ValueError(tr("Scaling «{expression}» не даёт конечных значений", expression=expression))
        out[ok] = _lookup_nearest(v[ok], raws, phys, finite, _INT_RANGES[st][0], out[ok], nudge)
        return out

    raw = eval_expression_array(to_byte, v)
    if nudge:
        bad = ~np.isfinite(raw[ok])
        if bad.any():
            value = float(v[ok][np.argmax(bad)])
            raise ValueError(tr("Значение {value} не переводится в байты (to_byte «{to_byte}»)", value=value, to_byte=to_byte))
    else:
        ok &= np.isfinite(raw)
    if st in _INT_RANGES:
        lo_i, hi_i = _INT_RANGES[st]
        raw = np.clip(np.round(raw), lo_i, hi_i)
    out[ok] = raw[ok]
    return out


def _lookup_nearest(
    target: np.ndarray,
    raws: np.ndarray,
    phys: np.ndarray,
    finite: np.ndarray,
    lo: int,
    current: np.ndarray,
    nudge: bool,
) -> np.ndarray:
    """Ближайшее сырое значение к каждому из target; nudge — сдвиг от застревания."""
    order = np.argsort(phys[finite], kind="stable")
    sorted_phys = phys[finite][order]
    sorted_raws = raws[finite][order]
    idx = np.searchsorted(sorted_phys, target)
    left = np.clip(idx - 1, 0, len(sorted_phys) - 1)
    right = np.clip(idx, 0, len(sorted_phys) - 1)
    closer_right = np.abs(sorted_phys[right] - target) < np.abs(sorted_phys[left] - target)
    best = np.where(closer_right, sorted_raws[right], sorted_raws[left])
    if not nudge:
        return best

    current_int = current.astype(int)
    current_phys = phys[current_int - lo]
    same_display = np.round(target, _DISPLAY_DECIMALS) == np.round(current_phys, _DISPLAY_DECIMALS)
    result = np.where(same_display, current_int, best)
    stuck = ~same_display & (best == current_int)
    if stuck.any():
        direction = np.where(target > current_phys, 1.0, -1.0)
        step = (phys[None, :] - current_phys[:, None]) * direction[:, None]
        ahead = finite[None, :] & (step > 0)
        nudge_idx = np.argmin(np.where(ahead, step, np.inf), axis=1)
        result = np.where(stuck & ahead.any(axis=1), raws[nudge_idx], result)
    return result


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
        raise ValueError(tr("Ось не хранится в ROM и не редактируется"))
    if index < 0 or (axis.size is not None and index >= axis.size):
        raise IndexError(f"Axis index {index} out of range")
    _, size = _struct_format(axis.storagetype, axis.endian)
    address = axis.storageaddress + index * size
    return _write_physical(rom, address, axis.storagetype, axis.endian, axis.scaling, value)


# --- Пакетная запись (перенос карт) --------------------------------------------


def _write_block(
    rom: bytearray,
    address: int,
    storagetype: str,
    endian: str,
    scaling: Scaling | None,
    values: np.ndarray,
) -> np.ndarray:
    """Записать подряд лежащие значения; вернуть реально сохранённые физические."""
    count = int(np.asarray(values).size)
    _, size = _struct_format(storagetype, endian)
    current = np.array(read_raw_values(rom, address, count, storagetype, endian))
    raws = encode_values(values, scaling, storagetype, current)
    for i in np.flatnonzero(raws != current):
        _write_raw(rom, address + int(i) * size, storagetype, endian, float(raws[i]))
    stored = read_raw_values(rom, address, count, storagetype, endian)
    return np.array(apply_scaling(stored, scaling))


def write_table_values(rom: bytearray, table: TableDef, values: np.ndarray) -> np.ndarray:
    """Записать всю матрицу таблицы [rows, cols]; вернуть сохранённую матрицу."""
    if table.storageaddress is None:
        raise ValueError(f"Table '{table.name}' has no storageaddress")
    rows, cols = table_dimensions(table)
    grid = np.asarray(values, dtype=float)
    if grid.shape != (rows, cols):
        raise ValueError(f"Shape {grid.shape} != table {rows}×{cols}")
    stored = _write_block(rom, table.storageaddress, table.storagetype, table.endian, table.scaling, grid)
    return stored.reshape(rows, cols)


def is_writable_axis(axis: AxisDef | None) -> bool:
    return axis is not None and not is_static_axis(axis) and axis.storageaddress is not None


def write_axis_values(rom: bytearray, axis: AxisDef | None, values: np.ndarray) -> np.ndarray:
    """Записать все точки оси; вернуть сохранённые значения."""
    if axis is None or not is_writable_axis(axis):
        raise ValueError(tr("Ось не хранится в ROM и не редактируется"))
    assert axis.storageaddress is not None
    return _write_block(rom, axis.storageaddress, axis.storagetype, axis.endian, axis.scaling, values)
