"""Единицы на экране: psi → bar, °F → °C, MPH → km/h и обратно."""

import pytest

from src.core.models import Checkpoint, Scaling
from src.core.parser import EcuParser
from src.core.rom_io import apply_scaling, encode_value
from src.core.units import TableUnits, UnitSystem, conversion_for, display_scaling, unit_token

METRIC = UnitSystem.METRIC


@pytest.mark.parametrize(
    ("units", "token"),
    [
        ("Boost Error (psi)", "psi"),
        ("psi relative sea level", "psi relative sea level"),
        ("Target Boost (psia) Compensation (%)", "%"),
        ("Coolant Temp (Degrees F)", "Degrees F"),
    ],
)
def test_unit_token(units, token):
    assert unit_token(units) == token


def test_conversions():
    bar = conversion_for("Boost Target (psi relative sea level)", METRIC)
    assert bar.target == "bar relative sea level" and bar.decimals == 3
    assert bar.display(14.5038) == pytest.approx(1.0, abs=1e-4)
    assert bar.definition(bar.display(7.3)) == pytest.approx(7.3)
    c = conversion_for("Degrees F", METRIC)
    assert c.display(212) == pytest.approx(100) and c.display(-40) == pytest.approx(-40)
    assert conversion_for("Vehicle Speed (MPH)", METRIC).display(60) == pytest.approx(96.56, abs=0.01)
    # Проценты с «psia» в названии — не давление
    assert conversion_for("Target Boost (psia) Compensation (%)", METRIC) is None
    assert conversion_for("Boost Error (psi)", UnitSystem.DEFINITIONS) is None


def test_display_scaling_keeps_raw_exact():
    # Порог передачи: нелинейная формула в MPH → km/h, кодирование сырых чисел то же
    mph = Scaling(units="Vehicle Speed (MPH)", expression="96560.6/x", to_byte="96560.6/x", coarse_increment=1.0)
    kmh = display_scaling(mph, conversion_for(mph.units, METRIC))
    assert kmh.units == "Vehicle Speed (km/h)" and kmh.coarse_increment == pytest.approx(1.609344)
    raw = encode_value(50 * 1.609344, kmh, "uint16", current_raw=0)
    assert raw == encode_value(50.0, mph, "uint16", current_raw=0)
    assert apply_scaling([raw], kmh)[0] == pytest.approx(apply_scaling([raw], mph)[0] * 1.609344)


def test_table_units_on_rom(jdm: EcuParser):
    name = "Target Boost "
    units = TableUnits.for_table(jdm.get_table_def(name), METRIC)
    shown = units.table(jdm.load_table(name))
    original = jdm.load_table(name)
    assert shown.unit.startswith("Boost Target (bar") and shown.decimals == 3
    assert shown.values[5][5] == pytest.approx(original.values[5][5] * 0.0689475729)
    assert units.value_definition(shown.values[5][5]) == pytest.approx(original.values[5][5])
    # Ось ECT у компенсаций — °C; в checkpoint туда и обратно без потерь
    ect = TableUnits.for_table(jdm.get_table_def("Idle Speed Target A"), METRIC)
    table = ect.table(jdm.load_table("Idle Speed Target A"))
    assert table.y_axis[0] == pytest.approx(-40.0) and "°C" in table.y_title
    shown_cp = ect.checkpoint(Checkpoint(values=[[700.0]], x_axis=[0.0], y_axis=[212.0]))
    assert shown_cp.y_axis == [pytest.approx(100.0)] and shown_cp.values == [[700.0]]
    assert ect.checkpoint_definition(shown_cp).y_axis == [pytest.approx(212.0)]
