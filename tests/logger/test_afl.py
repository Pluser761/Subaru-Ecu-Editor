"""Выгрузка таблиц обучения: A/F Learning #1 (stored A/B/C/D, текущее, зона) и FLKC (карта
RPM × Load) из RAM + запись CSV — на эмуляторе, CAN и K-line."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from src.logger.core.csv_log import write_learning_csv
from src.logger.core.defs import LoggerDefs
from src.logger.core.session import AflTable, FlkcTable, Poller, connect
from src.logger.core.transport.emulator import FakeEcu, FakeOpenPort
from src.logger.core.transport.op2 import OpenPort2

ROOT = Path(__file__).resolve().parents[2]
LOGGER_DEFS = ROOT / "definitions" / "logger" / "logger_defs.xml"
LOGGER_EXTRA = ROOT / "definitions" / "logger" / "extra"

# RAM ЭБУ: stored A/B/C/D — float в долях (−0.13 = −13 %), текущее — float, зона — байт 1 → 2
_AFL = {"A": -0.13, "B": -0.148, "C": -0.121, "D": -0.085}
_AFL_ADDR = {"A": 0xFF2C90, "B": 0xFF2C98, "C": 0xFF2CA0, "D": 0xFF2CA8}
MEMORY = {
    **{0xFF73B0 + i: b for i, b in enumerate(struct.pack(">f", -0.13))},
    0xFF73B7: 1,
}
for _zone, _value in _AFL.items():
    MEMORY.update({_AFL_ADDR[_zone] + i: b for i, b in enumerate(struct.pack(">f", _value))})

# FLKC: база 0xFF2D30, 7×5 ячеек float с шагом 8, ячейка i = i.0
_FLKC_BASE = 0xFF2D30
for _i in range(7 * 5):
    MEMORY.update({_FLKC_BASE + _i * 8 + j: b for j, b in enumerate(struct.pack(">f", float(_i)))})


@pytest.fixture(scope="module")
def defs() -> LoggerDefs:
    return LoggerDefs.load(LOGGER_DEFS, [LOGGER_EXTRA])


def _device(**ecu: object) -> tuple[OpenPort2, FakeEcu]:
    fake = FakeEcu(memory=lambda a: MEMORY.get(a, 0), **ecu)  # type: ignore[arg-type]
    return OpenPort2(FakeOpenPort(fake)), fake


def test_defs_have_afl_addresses_for_ecu(defs: LoggerDefs) -> None:
    assert defs.get("E44").addresses_for("5C42584007")[0].address == 0xFF2C90
    assert defs.get("E61").addresses_for("5C42584007")[0].address == 0xFF73B7


@pytest.mark.parametrize("can", [True, False])
def test_read_afl_table(defs: LoggerDefs, can: bool) -> None:
    dev, _ecu = _device(can_enabled=can)
    poller = Poller(connect(dev), defs)
    table = poller.read_afl()
    assert table.stored["A"] == pytest.approx(-13.0)
    assert table.stored["B"] == pytest.approx(-14.8)
    assert table.stored["C"] == pytest.approx(-12.1)
    assert table.stored["D"] == pytest.approx(-8.5)
    assert table.current == pytest.approx(-13.0)
    assert table.range_index == 2


def test_read_afl_rejected_address_reads_via_kline(defs: LoggerDefs) -> None:
    # Один адрес RAM отклонён по CAN — выгрузка всё равно читает значение (уходит на K-line)
    dev, _ecu = _device(can_rejected={0xFF2C90})
    poller = Poller(connect(dev), defs)
    assert poller.read_afl().stored["A"] == pytest.approx(-13.0)


def test_read_flkc_table(defs: LoggerDefs) -> None:
    dev, _ecu = _device()
    flkc = Poller(connect(dev), defs).read_flkc()
    assert len(flkc.cells) == 7 and len(flkc.cells[0]) == 5
    assert flkc.cells[0][0] == pytest.approx(0.0)
    assert flkc.cells[1][0] == pytest.approx(5.0)  # индекс 5 — строка 1, колонка 0
    assert flkc.cells[6][4] == pytest.approx(34.0)


def test_read_learning_uses_kline_for_ram_when_can_rejects(defs: LoggerDefs) -> None:
    """RAM по CAN у AZ1G101M отклоняется (7F A8 12). Выгрузка читает её по K-line целиком,
    а не перебором по одному адресу (иначе на 140 адресах FLKC это ~300 запросов)."""
    dev, ecu = _device(can_rejects_ram=True)
    poller = Poller(connect(dev), defs)
    before = len(ecu.requests)
    afl = poller.read_afl()
    flkc = poller.read_flkc()
    assert afl.stored["A"] == pytest.approx(-13.0)
    assert flkc.cells[6][4] == pytest.approx(34.0)
    assert len(ecu.requests) - before <= 12  # AFL и FLKC по K-line кусками, без CAN-перебора (~300)


def test_write_learning_csv(tmp_path: Path) -> None:
    afl = AflTable(
        stored={"A": -13.0, "B": -14.8, "C": -12.1, "D": -8.5},
        current=-13.0,
        range_index=2,
    )
    flkc = FlkcTable(
        cells=tuple(tuple(float(r * 5 + c) for c in range(5)) for r in range(7)),
        rpm_axis=(3200.0, 4400.0, 5600.0, 10000.0, 11000.0, 12000.0),
        load_axis=(1.3, 1.64, 2.0, 5.0),
    )
    path = tmp_path / "learning.csv"
    write_learning_csv(path, "5C42584007", afl, (5.6, 10.0, 40.0), flkc)
    lines = path.read_text().splitlines()
    assert lines[0] == "ECU;5C42584007"
    assert lines[2] == "A/F Learning #1"
    assert lines[3] == "Zone;Airflow (g/s);Value (%)"
    assert lines[4] == "A;0 - 5,6;-13,00"
    assert lines[8] == "Current;;-13,00"
    assert lines[9] == "Range;;2"
    assert lines[11] == "Fine Learning Knock Correction (deg)"
    assert lines[12] == "Load \\ RPM;0 - 3200;3200 - 4400;4400 - 5600;5600 - 10000;10000 - 11000;11000 - 12000;12000 - "
    assert lines[13] == "0 - 1,3;0,00;1,00;2,00;3,00;4,00"
