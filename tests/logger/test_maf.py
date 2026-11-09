"""Вкладка MAF: фильтры точек, полином и множители строк таблицы (без Qt)."""

import math

import pytest

from src.logger.core.maf import (
    MafCollector,
    MafFilters,
    MafPoint,
    MafTable,
    MafTrend,
    fit_trend,
    scale_values,
)


def _values(**overrides: float) -> dict[str, float]:
    """Опрос с валидной точкой: замкнутый контур, установившийся режим."""
    values = {
        "P18": 2.0,
        "P12": 40.0,
        "P3": 2.0,
        "P4": 3.0,
        "P8": 2000.0,
        "P2": 90.0,
        "P11": 30.0,
        "E3": 8.0,
        "E23": 0.0,
    }
    values.update(overrides)
    return values


def test_point_sums_trims() -> None:
    collector = MafCollector()
    point = collector.accept(_values(), 1000.0)
    assert point == MafPoint(2.0, 5.0)  # A/F Learning #1 + A/F Correction #1
    assert list(collector.points) == [point]


def test_trim_params_required() -> None:
    collector = MafCollector()
    for pid in ("P18", "P3", "P4"):
        values = _values()
        del values[pid]
        assert collector.accept(values, 1000.0) is None
    assert not collector.points


def test_nan_is_missing() -> None:
    assert MafCollector().accept(_values(P18=math.nan), 1000.0) is None
    assert MafCollector().accept(_values(P3=math.nan), 1000.0) is None


def test_closed_loop_only() -> None:
    assert MafCollector().accept(_values(E3=4.0), 1000.0) is None
    # У 32-битных ЭБУ вместо E3 бывает E33; NaN считается отсутствующим параметром
    assert MafCollector().accept(_values(E33=8.0), 1000.0) is not None
    assert MafCollector().accept(_values(E3=math.nan, E33=8.0), 1000.0) is not None
    # Нет ни CL/OL, ни tip-in — фильтры не применяются (как в RomRaider)
    assert MafCollector().accept(_values(E3=math.nan), 1000.0) is not None


def test_tip_in_rejected() -> None:
    assert MafCollector().accept(_values(E23=2.0), 1000.0) is None
    assert MafCollector().accept(_values(E23=math.nan, E54=0.0), 1000.0) is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {"P8": 5000.0},
        {"P12": 150.0},
        {"P2": 50.0},
        {"P11": 120.0},
    ],
)
def test_ranges_reject(overrides: dict[str, float]) -> None:
    assert MafCollector().accept(_values(**overrides), 1000.0) is None


def test_slow_rate_only() -> None:
    collector = MafCollector(MafFilters(mafv_rate_max=0.1))
    assert collector.accept(_values(P18=2.0), 1000.0) is not None
    # +0.3 В за 0.1 с = 3 В/с — слишком быстро
    assert collector.accept(_values(P18=2.3), 1100.0) is None
    # Опорная точка для скорости — последний опрос, даже отклонённый по скорости
    assert collector.accept(_values(P18=2.05), 4000.0) is not None
    assert len(collector.points) == 2


def test_clear() -> None:
    collector = MafCollector()
    collector.accept(_values(), 1000.0)
    collector.clear()
    assert not collector.points
    assert collector.accept(_values(P18=3.0), 1000.0) is not None  # скорость снова не с чем сравнивать


def test_fit_line() -> None:
    points = [MafPoint(1.5, 1.0), MafPoint(2.0, 3.0), MafPoint(2.5, 5.0), MafPoint(3.0, 7.0)]
    trend = fit_trend(points, 3)
    assert trend.percent(2.0) == pytest.approx(3.0, abs=1e-9)
    assert trend.percent(2.75) == pytest.approx(6.0, abs=1e-9)


def test_fit_needs_points() -> None:
    with pytest.raises(ValueError):
        fit_trend([MafPoint(2.0, 1.0), MafPoint(2.5, 2.0), MafPoint(3.0, 3.0)], 3)


def test_fit_same_volts() -> None:
    points = [MafPoint(2.0, float(i)) for i in range(10)]
    with pytest.raises(ValueError):
        fit_trend(points, 3)


def test_scale_values_in_range() -> None:
    trend = MafTrend(mean=2.0, scale=1.0, coeffs=(10.0,))  # всюду +10 %
    table = MafTable("MAF Sensor Scaling", (1.0, 1.5, 2.0, 2.5), (10.0, 20.0, 30.0, 40.0))
    scaled = scale_values(table, 1.5, 2.0, trend)
    assert sorted(scaled) == [1, 2]
    assert scaled[1] == pytest.approx(22.0)
    assert scaled[2] == pytest.approx(33.0)


def test_scale_values_empty_range() -> None:
    trend = MafTrend(mean=2.0, scale=1.0, coeffs=(10.0,))
    table = MafTable("MAF Sensor Scaling", (1.0, 2.0), (10.0, 20.0))
    assert scale_values(table, 3.0, 4.0, trend) == {}


def test_scale_values_bad_range() -> None:
    trend = MafTrend(mean=2.0, scale=1.0, coeffs=(10.0,))
    table = MafTable("MAF Sensor Scaling", (1.0, 2.0), (10.0, 20.0))
    with pytest.raises(ValueError):
        scale_values(table, 2.0, 2.0, trend)
