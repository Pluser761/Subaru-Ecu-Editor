"""Чтение карт как у ЭБУ (разобрано в AZ1G301F, см. src/core/ecu_lookup.py)."""

import numpy as np
import pytest

from src.core.checkpoint import baseline
from src.core.ecu_lookup import axis_position, has_denso_lookup, resample
from src.core.models import Checkpoint, Scaling
from src.core.parser import EcuParser

AXIS = [0.0, 10.0, 20.0]
# Primary Open Loop Fueling: ЭБУ хранит добавку обогащения, RomRaider показывает AFR
AFR = Scaling(expression="14.7/(1+x*.0078125)", to_byte="(14.7/x-1)/.0078125")


@pytest.mark.parametrize(
    ("x", "expected"),
    [
        (-5.0, (0, 0.0)),  # ниже первой точки — первая, без экстраполяции
        (0.0, (0, 0.0)),  # точно в точке — доля 0
        (5.0, (0, 0.5)),
        (10.0, (1, 0.0)),
        (20.0, (2, 0.0)),  # последняя точка
        (99.0, (2, 0.0)),  # выше последней — последняя
    ],
)
def test_axis_position(x, expected):
    index, fraction = axis_position(AXIS, x)
    assert (index, fraction) == (expected[0], pytest.approx(expected[1]))


def test_repeated_axis_point_is_a_step():
    # Повтор точки: в самой точке — верхний отрезок, отрезок нулевой ширины не используется
    axis = [0.0, 1.0, 1.0, 2.0]
    assert axis_position(axis, 1.0) == (2, 0.0)
    assert axis_position(axis, 0.5) == (0, pytest.approx(0.5))
    got = resample([[0.0, 0.0, 65.0, 65.0]], axis, [0.0], [0.999, 1.0], [0.0])
    assert got[0] == pytest.approx([0.0, 65.0])


def test_ecu_interpolates_raw_values_not_afr():
    # Посередине между 11.0 и 14.7 AFR ЭБУ смешивает сырую добавку — 12.58, а не 12.85
    got = resample([[11.0, 14.7]], [0.0, 10.0], [0.0], [5.0], [0.0], AFR)[0][0]
    assert got == pytest.approx(12.58, abs=0.01)
    linear = resample([[11.0, 14.7]], [0.0, 10.0], [0.0], [5.0], [0.0])[0][0]
    assert linear == pytest.approx(12.85)


def test_3d_is_rows_then_columns():
    grid = np.array([[0.0, 10.0], [20.0, 30.0]])
    got = resample(grid, [0.0, 10.0], [0.0, 10.0], [5.0], [5.0])
    assert got[0][0] == pytest.approx(15.0)


def test_checkpoint_baseline_uses_ecu_calculation():
    cp = Checkpoint(values=[[11.0, 14.7]], x_axis=[0.0, 10.0], y_axis=[0.0])
    ecu = baseline(cp, [5.0, 10.0], [0.0], AFR, ecu=True)
    physical = baseline(cp, [5.0, 10.0], [0.0], AFR, ecu=False)
    assert ecu[0][0] == pytest.approx(12.58, abs=0.01)
    assert physical[0][0] == pytest.approx(12.85)


def test_signature_found_only_in_denso_32bit(jdm: EcuParser, edm: EcuParser):
    assert has_denso_lookup(jdm.rom_bytes)
    assert not has_denso_lookup(edm.rom_bytes)
    assert jdm.load_table("Primary Open Loop Fueling").ecu_interpolation
    assert not edm.load_table(edm.list_maps()[0].id).ecu_interpolation
