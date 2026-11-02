"""Перенос карт: чистые функции плана и пересчёта оси — без прошивки."""

import numpy as np
import pytest

from src.core.transfer import (
    AxisMode,
    AxisPlan,
    ResultStatus,
    TransferCandidate,
    _select_transfers,
    resample_axis,
)


def _candidate(blocked: str | None = None) -> TransferCandidate:
    return TransferCandidate(
        name="Map",
        category="Maps",
        source_shape=(4, 4),
        target_shape=(4, 4),
        x=AxisPlan("X", AxisMode.COPY, 4, 4),
        y=AxisPlan("Y", AxisMode.COPY, 4, 4),
        blocked=blocked,
    )


def test_resample_axis_preserves_endpoints_and_spreads_points():
    src = np.array([0.0, 10.0, 20.0, 30.0])
    assert resample_axis(src, 4) == pytest.approx(src)  # тот же размер — как есть
    assert resample_axis(src, 2) == pytest.approx([0.0, 30.0])
    assert resample_axis(src, 3) == pytest.approx([0.0, 15.0, 30.0])


def test_resample_axis_single_point():
    assert resample_axis(np.array([7.0]), 5) == pytest.approx([7.0, 7.0, 7.0, 7.0, 7.0])


def test_select_transfers_partitions_blocked_and_unknown():
    ok = _candidate()
    blocked = _candidate(blocked="разный тип таблицы")
    selected, results = _select_transfers(["Map", "Blocked", "Missing"], {"Map": ok, "Blocked": blocked})
    assert selected == ["Map"]
    assert results["Blocked"].status is ResultStatus.SKIPPED
    assert results["Blocked"].notes == ["разный тип таблицы"]
    assert results["Missing"].status is ResultStatus.SKIPPED
    assert results["Missing"].notes  # есть причина пропуска
