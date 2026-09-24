import math

import numpy as np
import pytest

from src.core.scaling import eval_expression, eval_expression_array


def test_arithmetic_with_offset():
    assert eval_expression("(x*.3515625)-20", 128) == 25.0


def test_division_by_zero_gives_inf_like_romraider():
    assert math.isinf(eval_expression("96560.6/x", 0))


def test_array_matches_scalar():
    xs = np.array([0.0, 25.0])
    got = eval_expression_array("14.7/(1+x*.0078125)", xs)
    assert got == pytest.approx([eval_expression("14.7/(1+x*.0078125)", x) for x in xs])


@pytest.mark.parametrize("expression", ["__import__('os')", "abs(x)", "x.real", "y+1"])
def test_only_arithmetic_and_x_allowed(expression):
    with pytest.raises(ValueError):
        eval_expression(expression, 1)
