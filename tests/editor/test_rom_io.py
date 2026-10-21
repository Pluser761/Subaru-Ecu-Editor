"""Кодирование физических значений в сырые и раскладка байтов — без прошивки."""

import pytest

from src.core.models import Scaling
from src.core.rom_io import _write_raw, apply_scaling, encode_value, read_raw_values

TIMING = Scaling(expression="(x*.3515625)-20", to_byte="(x+20)/.3515625")


def test_nearest_representable_with_offset():
    # Сетка значений сдвинута на −20: 12.3 → ближайшее k·0.3515625 − 20
    raw = encode_value(12.3, TIMING, "uint8", current_raw=0)
    assert raw == 92
    assert apply_scaling([raw], TIMING)[0] == pytest.approx(12.34375)


def test_same_displayed_value_keeps_raw():
    assert encode_value(12.34, TIMING, "uint8", current_raw=92) == 92


def test_small_step_does_not_get_stuck():
    # 12.34 → 12.4: ближайшее — текущее, но значение на экране другое → соседнее вверх
    assert encode_value(12.4, TIMING, "uint8", current_raw=92) == 93


@pytest.mark.parametrize(("value", "raw"), [(200.0, 255), (-50.0, 0)])
def test_clamped_to_type_range(value, raw):
    assert encode_value(value, TIMING, "uint8", current_raw=100) == raw


def test_nonlinear_scaling():
    afr = Scaling(expression="14.7/(1+x*.0078125)", to_byte="(14.7/x-1)/.0078125")
    raw = encode_value(12.3, afr, "uint8", current_raw=0)
    assert apply_scaling([raw], afr)[0] == pytest.approx(12.298, abs=1e-3)


def test_float_uses_to_byte_without_quantization():
    identity = Scaling(expression="x", to_byte="x")
    assert encode_value(1.2345, identity, "float", current_raw=0) == pytest.approx(1.2345)


def test_float_endian_is_inverted_like_romraider():
    # В definitions float помечены little, а в ROM лежат big-endian
    rom = bytearray(4)
    _write_raw(rom, 0, "float", "little", 1.5)
    assert rom.hex() == "3fc00000"
    assert read_raw_values(rom, 0, 1, "float", "little") == [1.5]
