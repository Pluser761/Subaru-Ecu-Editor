"""Модель таблицы карты: числа, правки, checkpoint, шкала цвета — без окон."""

import math

import pytest
from PyQt6.QtCore import Qt

from src.core.models import CalibrationTable
from src.ui.map.model import MapTableModel

H, V = Qt.Orientation.Horizontal, Qt.Orientation.Vertical


def idle_row() -> CalibrationTable:
    # 2D-карта, показанная строкой: ось X — температура
    return CalibrationTable(
        name="Idle",
        values=[[1500.0, 1300.0, 1050.0]],
        x_axis=[104.0, 122.0, 140.0],
        y_axis=[0.0],
        y_labels=[""],
    )


@pytest.fixture
def model(qapp) -> MapTableModel:
    m = MapTableModel()
    m.load(idle_row())
    return m


def test_display_is_formatted_but_values_are_numbers(model):
    assert model.data(model.index(0, 1)) == "1300.00"
    assert model.headerData(1, H) == "122.00"
    assert model.headerData(0, V) == ""  # статическая ось без подписи
    assert model.value(0, 1) == 1300.0


def test_user_edit_emits_signal(model):
    got = []
    model.cell_edited.connect(lambda r, c, v: got.append((r, c, v)))
    assert model.setData(model.index(0, 1), "1350,5")
    assert got == [(0, 1, 1350.5)]
    assert model.diff(0, 1) == 1


def test_display_update_is_silent(model):
    got = []
    model.cell_edited.connect(lambda *a: got.append(a))
    model.set_value(0, 1, 1200.0)
    assert got == [] and model.diff(0, 1) == -1


def test_axis_edit_and_interpolated_checkpoint(model):
    edited = []
    model.axis_edited.connect(lambda o, i, v: edited.append((o, i, v)))
    assert model.setHeaderData(1, H, 113.0)
    assert edited == [(H, 1, 113.0)]
    assert model.axis_diff(H, 1) == -1
    # Старая карта в 113 °F даёт 1400 — ячейка 1300 теперь «уменьшилась»
    assert model.diff(0, 1) == -1
    assert "Checkpoint: 1400.00" in model.tooltip(0, 1)


def test_static_axis_not_editable(model):
    assert not model.setHeaderData(0, V, 5.0)
    assert model.axis_diff(V, 0) == 0


def test_set_checkpoint_clears_changes(model):
    model.set_value(0, 0, 1600.0)
    model.set_checkpoint()
    assert model.diff(0, 0) == 0 and model.tooltip(0, 0) == ""


def test_value_position_for_color(model):
    assert model.value_position(0, 2) == 0.0  # минимум
    assert model.value_position(0, 0) == 1.0  # максимум
    model.set_value(0, 1, math.inf)
    assert math.isnan(model.value_position(0, 1))
