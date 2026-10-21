import pytest

from src.core.matrix_clipboard import (
    format_matrix,
    parse_axis,
    parse_matrix,
    validate_axis_paste,
    validate_paste_size,
    validate_rectangular,
)


def test_tsv_roundtrip():
    values = [[1.0, -2.5], [3.25, 4.0]]
    assert parse_matrix(format_matrix(values)) == values


def test_comma_is_decimal_separator_in_single_value():
    assert parse_matrix("1,5") == [[1.5]]


def test_csv_with_several_commas_and_semicolons():
    assert parse_matrix("1,2,3") == [[1.0, 2.0, 3.0]]
    assert parse_matrix("1,5;2,5") == [[1.5, 2.5]]


def test_axis_from_column():
    assert parse_axis("1\n2\n3") == [1.0, 2.0, 3.0]


def test_non_numeric_rejected():
    with pytest.raises(ValueError):
        parse_matrix("abc")


def test_validation_messages():
    assert validate_rectangular([[1.0, 2.0], [3.0]]) is not None
    assert validate_rectangular([[1.0], [2.0]]) is None
    assert validate_paste_size(2, 3, sel_rows=2, sel_cols=3) is None
    assert validate_paste_size(2, 3, sel_rows=3, sel_cols=2) is not None
    assert validate_axis_paste(0, target_count=1) is not None
