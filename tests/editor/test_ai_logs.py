"""Чтение логов для ИИ: форматы RomRaider и Car Scanner, фильтры, раскладка по сетке карты."""

from pathlib import Path

import pytest

from src.ai import logs
from src.ai.tools import ToolError

ROMRAIDER = """Time (msec);Engine Speed (rpm);Engine Load (g/rev);Feedback Knock Correction (degrees)
0;1000;0,50;0,00
100;2000;1,00;-1,40
200;2100;1,10;-2,80
300;4000;1,90;0,00
"""

CAR_SCANNER_LONG = """"SECONDS";"PID";"VALUE";"UNITS";
"10.0";"Обороты двигателя";"1500";"rpm";
"10.0";"Температура";"70";"℃";
"10.5";"Обороты двигателя";"1600";"rpm";
"11.0";"Температура";"71";"℃";
"""

SPARSE_WIDE = """time,"Обороты двигателя (rpm)","Температура (℃)"
20:09:03.500,,72
20:09:03.833,1502,
20:09:04.000,1600,73
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_romraider_semicolon_and_decimal_comma(tmp_path):
    log = logs.load_log(_write(tmp_path, "rr.csv", ROMRAIDER))
    assert log.columns[0] == logs.TIME
    assert log.column("Engine Load").tolist() == [0.5, 1.0, 1.1, 1.9]
    # Миллисекунды → секунды от начала
    assert log.column(logs.TIME).tolist() == [0.0, 0.1, 0.2, 0.3]


def test_car_scanner_long_format_pivots_with_forward_fill(tmp_path):
    log = logs.load_log(_write(tmp_path, "cs.csv", CAR_SCANNER_LONG))
    assert log.columns == [logs.TIME, "Обороты двигателя (rpm)", "Температура (℃)"]
    assert log.column("Обороты").tolist() == [1500, 1600, 1600]
    assert log.column("Температура").tolist() == [70, 70, 71]
    assert log.column(logs.TIME).tolist() == [0.0, 0.5, 1.0]


def test_sparse_wide_log_forward_fills_but_keeps_leading_gap(tmp_path):
    log = logs.load_log(_write(tmp_path, "sparse.csv", SPARSE_WIDE))
    rpm = log.column("Обороты")
    assert rpm[0] != rpm[0]  # nan: до первого значения ничего не известно
    assert rpm[1:].tolist() == [1502, 1600]
    assert log.column("Температура").tolist() == [72, 72, 73]
    assert log.column(logs.TIME)[1] == pytest.approx(0.333)


def test_column_lookup_errors_are_explicit(tmp_path):
    log = logs.load_log(_write(tmp_path, "rr.csv", ROMRAIDER))
    with pytest.raises(ToolError, match="ambiguous"):
        logs.find_column(log, "e")
    with pytest.raises(ToolError, match="not found"):
        logs.find_column(log, "Boost")


def test_rows_filter_every_and_limit(tmp_path):
    path = _write(tmp_path, "rr.csv", ROMRAIDER)
    result = logs.rows(path, ["Engine Speed"], where=[{"column": "Feedback Knock", "op": "<", "value": 0}])
    assert result["matched_rows"] == 2
    assert [r[1] for r in result["rows"]] == [2000, 2100]
    assert logs.rows(path, ["Engine Speed"], every=2, limit=1)["returned_rows"] == 1


def test_to_map_grid_nearest_axis_point(tmp_path):
    path = _write(tmp_path, "rr.csv", ROMRAIDER)
    grid = logs.to_map_grid(
        path, "Feedback Knock", "Engine Load", [0.5, 1.0, 2.0], "Engine Speed", [1000, 2000, 4000], stat="min"
    )
    # 2000/1.00 и 2100/1.10 попадают в одну ячейку (строка 2000, столбец 1.0)
    assert grid["counts"] == [[1, 0, 0], [0, 2, 0], [0, 0, 1]]
    assert grid["grid"][1][1] == -2.8
    assert grid["grid"][0][1] is None
