"""Подбор скейлинга ДМРВ по живому логу — без Qt (вкладка «MAF», как в RomRaider).

Точка — (напряжение ДМРВ, суммарная топливная коррекция %): A/F Correction #1 + A/F Learning #1.
Коррекции имеют смысл только в замкнутом контуре и без tip-in, поэтому фильтры повторяют
RomRaider: RPM, расход, ECT, IAT, скорость изменения напряжения, CL/OL == 8, tip-in == 0.
Положительная коррекция = ЭБУ добавляет топливо = ДМРВ занижает расход → скейлинг вверх.

Рабочий диапазон способа — малые и средние расходы (обычно 1.2–2.6 В): на WOT замкнутого
контура нет, а широкополосной лямбды у логгера нет.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

# ID параметров из logger def (как у RomRaider)
MAFV_ID = "P18"
MAF_ID = "P12"
AF_CORRECTION_ID = "P3"
AF_LEARNING_ID = "P4"
RPM_ID = "P8"
ECT_ID = "P2"
IAT_ID = "P11"
CL_OL_IDS = ("E3", "E33")
TIP_IN_IDS = ("E23", "E54")
# Значение CL/OL у RomRaider: замкнутый контур
CLOSED_LOOP = 8
# Без этих параметров точку не посчитать
REQUIRED_IDS = (MAFV_ID, AF_CORRECTION_ID, AF_LEARNING_ID)
# Что просим у опроса, когда включена запись MAF
COLLECT_IDS = REQUIRED_IDS + (MAF_ID, RPM_ID, ECT_ID, IAT_ID, *CL_OL_IDS, *TIP_IN_IDS)
# Имя таблицы в definitions; в части прошивок оно с хвостовым пробелом
MAF_TABLE = "MAF Sensor Scaling"
# Сколько точек держать: больше — уже не влияет на полином, но тормозит график
MAX_POINTS = 20000


@dataclass(frozen=True)
class MafFilters:
    """Порог фильтрации точки; значения по умолчанию — как в RomRaider."""

    rpm_min: float = 0.0
    rpm_max: float = 4500.0
    maf_min: float = 0.0
    maf_max: float = 100.0
    ect_min: float = 70.0
    iat_max: float = 100.0
    mafv_rate_max: float = 0.1  # В/с: точка берётся на установившемся режиме


@dataclass(frozen=True)
class MafPoint:
    """Валидная точка лога: напряжение ДМРВ и суммарная коррекция."""

    volts: float
    trim: float  # %


@dataclass(frozen=True)
class MafTable:
    """Таблица «MAF Sensor Scaling» открытой прошивки."""

    name: str
    axis: tuple[float, ...]  # ось Y — напряжение ДМРВ, В
    values: tuple[float, ...]  # расход, г/с


@dataclass(frozen=True)
class MafTrend:
    """Полином коррекции по напряжению: y = P((x - mean) / scale), y — проценты."""

    mean: float
    scale: float
    coeffs: tuple[float, ...]

    def percent(self, volts: Sequence[float] | float) -> np.ndarray:
        x = np.asarray(volts, dtype=float)
        return np.polyval(np.asarray(self.coeffs, dtype=float), (x - self.mean) / self.scale)


class MafCollector:
    """Фильтр точек по образцу MafUpdateHandler из RomRaider."""

    def __init__(self, filters: MafFilters | None = None, max_points: int = MAX_POINTS) -> None:
        self.filters = filters or MafFilters()
        self.points: deque[MafPoint] = deque(maxlen=max_points)
        self._last_volts: float | None = None
        self._last_ms: float | None = None

    def clear(self) -> None:
        self.points.clear()
        self._last_volts = None
        self._last_ms = None

    def accept(self, values: Mapping[str, float], time_ms: float) -> MafPoint | None:
        """Проверить точку опроса; валидную — запомнить и вернуть, иначе None."""
        volts = _value(values, MAFV_ID)
        correction = _value(values, AF_CORRECTION_ID)
        learning = _value(values, AF_LEARNING_ID)
        if volts is None or correction is None or learning is None:
            return None

        # Замкнутый контур: на WOT коррекции не обновляются, точка была бы нулевой
        closed_loop = _first(values, CL_OL_IDS)
        if closed_loop is not None and int(closed_loop) != CLOSED_LOOP:
            return None
        # Tip-in: переходный режим, коррекции ещё не устоялись
        tip_in = _first(values, TIP_IN_IDS)
        if tip_in is not None and tip_in != 0:
            return None

        f = self.filters
        rpm = _value(values, RPM_ID)
        if rpm is not None and not f.rpm_min <= rpm <= f.rpm_max:
            return None
        maf = _value(values, MAF_ID)
        if maf is not None and not f.maf_min <= maf <= f.maf_max:
            return None
        iat = _value(values, IAT_ID)
        if iat is not None and iat > f.iat_max:
            return None
        ect = _value(values, ECT_ID)
        if ect is not None and ect < f.ect_min:
            return None

        # Скорость изменения напряжения — только между соседними опросами с валидными
        # прочими параметрами (как в RomRaider); на первом опросе проверять не с чем
        rate_ok = True
        if self._last_volts is not None and self._last_ms is not None and time_ms > self._last_ms:
            rate = abs(volts - self._last_volts) / ((time_ms - self._last_ms) / 1000.0)
            rate_ok = rate <= f.mafv_rate_max
        self._last_volts, self._last_ms = volts, time_ms
        if not rate_ok:
            return None

        point = MafPoint(volts, learning + correction)
        self.points.append(point)
        return point


def fit_trend(points: Iterable[MafPoint], order: int) -> MafTrend:
    """Полиномиальная аппроксимация точек; ValueError — точек мало или они на одном напряжении."""
    xs = np.array([p.volts for p in points], dtype=float)
    ys = np.array([p.trim for p in points], dtype=float)
    if len(xs) < order + 1:
        raise ValueError("not enough points for the polynomial order")
    mean = float(xs.mean())
    scale = float((xs.max() - xs.min()) / 2.0)
    if scale <= 0:
        raise ValueError("all points have the same voltage")
    # Подгонка по нормированной оси: polyfit высоких степеней иначе плохо обусловлен
    coeffs = np.polyfit((xs - mean) / scale, ys, order)
    return MafTrend(mean, scale, tuple(float(c) for c in coeffs))


def trend_curve(trend: MafTrend, volts: Sequence[float]) -> tuple[list[float], list[float]]:
    """Кривая для графика: ([напряжение], [коррекция, %])."""
    xs = [float(v) for v in volts]
    return xs, [float(v) for v in trend.percent(xs)]


def scale_values(
    table: MafTable,
    low: float,
    high: float,
    trend: MafTrend,
) -> dict[int, float]:
    """Новые значения строк, чья ось в [low, high]: value * (1 + percent/100).

    Индекс — номер строки таблицы. Пустой словарь — в диапазон не попала ни одна строка.
    """
    if not low < high:
        raise ValueError("the range minimum must be less than the maximum")
    if len(table.axis) != len(table.values) or not table.axis:
        raise ValueError("the table axis and values have different lengths")
    out: dict[int, float] = {}
    for i, (volts, value, percent) in enumerate(
        zip(table.axis, table.values, trend.percent(table.axis))
    ):
        if low <= volts <= high and math.isfinite(percent):
            updated = value * (1.0 + float(percent) / 100.0)
            if math.isfinite(updated) and updated > 0:
                out[i] = updated
    return out


def _value(values: Mapping[str, float], pid: str) -> float | None:
    """Значение параметра; нет или NaN (ЭБУ не отдал) — None."""
    value = values.get(pid)
    if value is None or math.isnan(value):
        return None
    return value


def _first(values: Mapping[str, float], ids: Sequence[str]) -> float | None:
    """Первое доступное значение из группы (E3/E33, E23/E54): у ЭБУ бывает только одно."""
    for pid in ids:
        value = _value(values, pid)
        if value is not None:
            return value
    return None
