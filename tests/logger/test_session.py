"""Описания параметров, автовыбор протокола, опрос и CSV — на эмуляторе и настоящем logger def."""

from __future__ import annotations

import io
import math
import struct
from pathlib import Path

import pytest

from src.ai.logs import load_log
from src.logger.core.csv_log import CsvLogWriter
from src.logger.core.defs import LoggerDefs
from src.logger.core.session import Poller, connect
from src.logger.core.transport.emulator import FakeEcu, FakeOpenPort
from src.logger.core.transport.op2 import OpenPort2

ROOT = Path(__file__).resolve().parents[2]
LOGGER_DEFS = ROOT / "definitions" / "logger" / "logger_defs.xml"
LOGGER_EXTRA = ROOT / "definitions" / "logger" / "extra"

# RAM ЭБУ: обороты 3000 (0x2EE0/4), МАФ 25.00 г/с, ОЖ 80 °C, стадия отсечки 2,
# выход AF3 = 0.25 (float), бит дворника (0x64, бит 2)
MEMORY = {
    0x0E: 0x2E, 0x0F: 0xE0,
    0x13: 0x09, 0x14: 0xC4,
    0x08: 120,
    0xFF7768: 2,
    0x64: 0b100,
    **{0xFF743C + i: b for i, b in enumerate(struct.pack(">f", 0.25))},
}


@pytest.fixture(scope="module")
def defs() -> LoggerDefs:
    return LoggerDefs.load(LOGGER_DEFS, [LOGGER_EXTRA])


def _device(**ecu: object) -> tuple[OpenPort2, FakeEcu]:
    fake = FakeEcu(memory=lambda a: MEMORY.get(a, 0), **ecu)  # type: ignore[arg-type]
    return OpenPort2(FakeOpenPort(fake)), fake


def test_defs_extra_only_add(defs: LoggerDefs, tmp_path: Path) -> None:
    assert defs.get("E9002").source == "extra/AZ1G101M.xml"
    assert defs.find("engine speed").id == "P8"
    # Дополнение с тем же id обычного параметра не перекрывает его, а у extended —
    # добавляет адрес для нового ID ЭБУ
    extra = tmp_path / "x.xml"
    extra.write_text(
        '<logger><protocols><protocol id="SSM"><parameters>'
        '<parameter id="P8" name="Hijack"><address>0x1</address></parameter></parameters>'
        '<ecuparams><ecuparam id="E59" name="Gear"><ecu id="1111111111"><address>0xFF0001</address></ecu>'
        "<conversions><conversion units='position' expr='x'/></conversions></ecuparam></ecuparams>"
        "</protocol></protocols></logger>"
    )
    merged = LoggerDefs.load(LOGGER_DEFS, [extra])
    assert merged.get("P8").name == "Engine Speed"
    assert merged.get("E59").addresses_for("1111111111")[0].address == 0xFF0001
    assert merged.get("E59").addresses_for("5C42584007")[0].address == 0xFF6A19


def test_connect_prefers_can_and_falls_back_to_kline() -> None:
    dev, _ecu = _device()
    assert connect(dev).transport == "can"
    dev, ecu = _device(can_enabled=False)
    connection = connect(dev)
    assert connection.transport == "kline" and connection.init.ecu_id == "5C42584007"


def test_poll_values_calculated_switch_and_extended(defs: LoggerDefs) -> None:
    dev, _ecu = _device()
    poller = Poller(connect(dev), defs)
    poller.select([("P8", None), ("P12", None), ("P200", None), ("P2", "F"), ("S23", None), ("E9002", None),
                   ("E9009", None)])
    sample = poller.poll()
    assert sample.values["P8"] == 3000
    assert sample.values["P12"] == pytest.approx(25.0)
    assert sample.values["P200"] == pytest.approx(25.0 * 60 / 3000)  # (P12*60)/P8, г/об
    assert sample.values["P2"] == pytest.approx(176)  # 80 °C в °F
    assert sample.values["S23"] == 1
    assert sample.values["E9002"] == 2
    assert sample.values["E9009"] == pytest.approx(0.25)
    poller.mark()
    assert poller.poll().mark == 1 and poller.poll().mark == 0


def test_rejected_address_moves_to_kline(defs: LoggerDefs) -> None:
    # По CAN ЭБУ отказывает на адресе стадии отсечки — он читается через K-line, остальное по CAN
    dev, ecu = _device(can_rejected={0xFF7768})
    poller = Poller(connect(dev), defs)
    poller.select([("P8", None), ("E9002", None)])
    sample = poller.poll()
    assert sample.values == {"P8": 3000, "E9002": 2}
    assert poller.slow == {"E9002"} and not poller.unsupported
    kline_reads = [m for kind, m in ecu.requests if kind == "kline" and m[4] == 0xA8]
    assert kline_reads and all(m[6:9] == bytes.fromhex("FF7768") for m in kline_reads)


def test_unsupported_when_no_address_for_ecu(defs: LoggerDefs) -> None:
    dev, _ecu = _device(ecu_id=bytes.fromhex("1234567890"))
    poller = Poller(connect(dev), defs)
    poller.select([("P8", None), ("E9002", None)])
    sample = poller.poll()
    assert sample.values["P8"] == 3000 and math.isnan(sample.values["E9002"])
    assert poller.unsupported == {"E9002"}


def test_supported_by_capability_bits(defs: LoggerDefs) -> None:
    dev, _ecu = _device(capabilities=b"\x00" * 48)
    connection = connect(dev)
    ids = {p.id for p in defs.supported(connection.init)}
    assert "P8" not in ids and "P200" not in ids  # биты возможностей нулевые
    assert "E9002" in ids  # extended — по ID ЭБУ


def test_csv_readable_by_log_analysis(defs: LoggerDefs, tmp_path: Path) -> None:
    dev, _ecu = _device()
    poller = Poller(connect(dev), defs)
    poller.select([("P8", None), ("P12", None)])
    path = tmp_path / "log.csv"
    writer = CsvLogWriter.open(path, [("P8", "Engine Speed", "rpm", 0), ("P12", "Mass Airflow", "g/s", 2)])
    poller.mark()
    for _ in range(3):
        writer.write(poller.poll())
    writer.close()
    text = path.read_text()
    assert text.splitlines()[0] == "Time (msec);Engine Speed (rpm);Mass Airflow (g/s);Mark"
    assert ";3000;25,00;1" in text.splitlines()[1]
    log = load_log(path)
    assert list(log.column("Engine Speed")) == [3000, 3000, 3000]


def test_csv_empty_cell_for_nan() -> None:
    out = io.StringIO()
    from src.logger.core.session import Sample

    CsvLogWriter(out, [("P8", "Engine Speed", "rpm", 0)]).write(Sample(1.6, {"P8": math.nan}))
    assert out.getvalue().splitlines()[1] == "2;;0"
