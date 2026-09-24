import math

import pytest

from src.core.checkpoint import baseline, diff_sign, same_values, table_differs
from src.core.models import Checkpoint

# Idle Speed Target: 104 °F → 1500, 122 °F → 1300, 140 °F → 1050 (строки — ось Y)
IDLE = Checkpoint(values=[[1500.0], [1300.0], [1050.0]], x_axis=[0.0], y_axis=[104.0, 122.0, 140.0])


@pytest.mark.parametrize(
    ("value", "base", "sign"),
    [(1.0, 1.0, 0), (1.004, 1.0, 0), (1.006, 1.0, 1), (0.99, 1.0, -1), (math.inf, math.inf, 0), (math.inf, 5.0, 1)],
)
def test_diff_sign(value, base, sign):
    assert diff_sign(value, base) == sign


def test_baseline_unchanged_axes_is_checkpoint():
    assert baseline(IDLE, [0.0], [104.0, 122.0, 140.0]) == IDLE.values


def test_baseline_interpolates_moved_axis_point():
    # Точку 122 сдвинули в 113 — посередине между 104 (1500) и 122 (1300): «было» 1400
    got = baseline(IDLE, [0.0], [104.0, 113.0, 140.0])
    assert [row[0] for row in got] == pytest.approx([1500.0, 1400.0, 1050.0])


def test_baseline_clamps_beyond_checkpoint_range():
    got = baseline(IDLE, [0.0], [104.0, 122.0, 200.0])
    assert got[2][0] == pytest.approx(1050.0)


def test_baseline_without_axes_is_values():
    constant = Checkpoint(values=[[3.0]])
    assert baseline(constant, [0.0], [0.0]) == [[3.0]]


def test_table_differs():
    assert not table_differs(IDLE.values, [0.0], [104.0, 122.0, 140.0], IDLE)
    assert table_differs([[1500.0], [1301.0], [1050.0]], [0.0], [104.0, 122.0, 140.0], IDLE)
    assert table_differs(IDLE.values, [0.0], [104.0, 113.0, 140.0], IDLE)
    assert not table_differs([[1500.001], [1300.0], [1050.0]], [0.0], [104.0, 122.0, 140.0], IDLE)


def test_same_values():
    assert same_values([1.0, 2.0], [1.001, 2.0])
    assert not same_values([1.0], [1.0, 2.0])
