import numpy as np
import pytest

from src.core.interpolation import resample_grid, strictly_monotonic

GRID = np.array([[0.0, 10.0], [20.0, 30.0]])
AXIS = np.array([0.0, 10.0])


def test_exact_on_nodes():
    assert resample_grid(GRID, AXIS, AXIS, AXIS, AXIS) == pytest.approx(GRID)


def test_bilinear_between_nodes():
    assert resample_grid(GRID, AXIS, AXIS, np.array([5.0]), np.array([5.0]))[0][0] == pytest.approx(15.0)


def test_clamped_outside_like_ecu():
    got = resample_grid(GRID, AXIS, AXIS, np.array([20.0]), np.array([-3.0]))
    assert got[0][0] == pytest.approx(10.0)


def test_descending_axis():
    got = resample_grid(GRID[:, ::-1], AXIS[::-1], AXIS, np.array([2.5]), np.array([0.0]))
    assert got[0][0] == pytest.approx(2.5)


# Повтор значений — ось не строго монотонна (статические подписи и т.п.)
FLAT = np.array([1.0, 1.0])


def test_non_monotonic_axis_matches_by_index():
    assert resample_grid(GRID, FLAT, AXIS, FLAT, AXIS) == pytest.approx(GRID)


def test_non_monotonic_axis_with_other_size_fails():
    with pytest.raises(ValueError):
        resample_grid(GRID, FLAT, AXIS, np.array([1.0, 2.0, 3.0]), AXIS)


@pytest.mark.parametrize(
    ("values", "expected"),
    [([1, 2, 3], True), ([3, 2, 1], True), ([1, 1, 2], False), ([1], False), ([1, float("inf")], False)],
)
def test_strictly_monotonic(values, expected):
    assert strictly_monotonic(np.array(values, dtype=float)) is expected
