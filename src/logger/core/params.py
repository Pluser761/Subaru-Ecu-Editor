"""Значения параметров: сырые байты → число → единицы по формуле logger def.

- сырые байты читаются big-endian (ЭБУ Renesas SH, RAM как есть): без storagetype —
  беззнаковое целое длины адреса, `int8/int16/int32` — со знаком, `float` — IEEE-754;
  у адреса с `bit` значение — 0/1;
- формулы считает core.scaling (безопасный разбор, без eval); у вычисляемых параметров
  в формуле — другие параметры: `P12` — в его первых единицах, `[P21:ms]` — в мс.
"""

from __future__ import annotations

import math
import re
import struct
from collections.abc import Mapping

from src.core.scaling import eval_names
from src.logger.core.defs import REF_PATTERN, Address, Conversion, LoggerParam

_SIGNED = {"int8": (1, ">b"), "int16": (2, ">h"), "int32": (4, ">i")}


def decode_raw(data: bytes, conversion: Conversion, bit: int | None = None) -> float:
    if bit is not None:
        return float((data[-1] >> bit) & 1) if data else math.nan
    storage = conversion.storagetype.lower()
    if storage == "float":
        return float(struct.unpack(">f", data[-4:].rjust(4, b"\x00"))[0])
    if storage in _SIGNED:
        size, fmt = _SIGNED[storage]
        return float(struct.unpack(fmt, data[-size:].rjust(size, b"\x00"))[0])
    return float(int.from_bytes(data, "big"))


def byte_addresses(addresses: list[Address]) -> list[int]:
    """Все адреса байт параметра подряд (длина 4 — четыре адреса)."""
    return [a.address + i for a in addresses for i in range(a.length)]


def convert_all(param: LoggerParam, data: bytes, bit: int | None = None) -> dict[str, float]:
    """Значение параметра во всех его единицах: {единицы: значение}. bit — из адреса
    параметра для этого ЭБУ (у extended адреса свои у каждого ID ЭБУ)."""
    out = {}
    for conversion in param.conversions or [Conversion("raw", "x", "0")]:
        raw = decode_raw(data, conversion, bit)
        out[conversion.units] = _eval(conversion.expr, {"x": raw})
    return out


def calculate(param: LoggerParam, values: Mapping[str, Mapping[str, float]], units: str | None = None) -> float:
    """Вычисляемый параметр по значениям зависимостей: values[id][единицы]."""
    conversion = param.conversion(units)
    names: dict[str, float] = {}

    def ref(match: re.Match[str]) -> str:
        param_id, ref_units = match.group(1), match.group(2)
        name = f"_{param_id}_{len(names)}"
        names[name] = values.get(param_id, {}).get(ref_units, math.nan)
        return name

    expr = REF_PATTERN.sub(ref, conversion.expr)
    for param_id in param.depends:
        dep = values.get(param_id, {})
        names[param_id] = next(iter(dep.values()), math.nan)
    if any(math.isnan(v) for v in names.values()):
        return math.nan
    return _eval(expr, names)


def decimals(conversion: Conversion) -> int:
    """Число знаков из формата RomRaider («0.00» → 2, «0» → 0)."""
    fmt = conversion.format
    return len(fmt.split(".", 1)[1]) if "." in fmt else 0


def _eval(expr: str, names: Mapping[str, float]) -> float:
    try:
        return eval_names(expr, names)
    except (ValueError, SyntaxError, OverflowError):
        return math.nan
