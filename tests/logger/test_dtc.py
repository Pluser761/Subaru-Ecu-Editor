"""DTC: разбор definitions, чтение tmp/mem и сброс reset ЭБУ — на эмуляторе, CAN и K-line."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from src.logger.core.defs import LoggerDefs
from src.logger.core.session import Poller, connect
from src.logger.core.ssm import SsmClient
from src.logger.core.trace import Trace
from src.logger.core.transport.emulator import FakeEcu, FakeOpenPort
from src.logger.core.transport.links import CanLink, KLineLink
from src.logger.core.transport.op2 import OpenPort2

ROOT = Path(__file__).resolve().parents[2]
LOGGER_DEFS = ROOT / "definitions" / "logger" / "logger_defs.xml"
LOGGER_EXTRA = ROOT / "definitions" / "logger" / "extra"

# Данные DTC (`<dtcodes>`): D1 — активна сейчас, D3 — сохранена, D4 — и сейчас, и сохранена
DTC_MEMORY = {
    0x8E: 0b0000_1001,  # tmp: биты 0 (D1) и 3 (D4)
    0xAE: 0b0000_1100,  # mem: биты 2 (D3) и 3 (D4)
}


@pytest.fixture(scope="module")
def defs() -> LoggerDefs:
    return LoggerDefs.load(LOGGER_DEFS, [LOGGER_EXTRA])


def _addresses(defs: LoggerDefs) -> set[int]:
    return {a for code in defs.dtcodes.values() for a in (code.tmp_address, code.mem_address)}


def _device(defs: LoggerDefs, **ecu: object) -> tuple[OpenPort2, FakeEcu]:
    fake = FakeEcu(memory=lambda a: DTC_MEMORY.get(a, 0), dtc_addresses=_addresses(defs),
                   **ecu)  # type: ignore[arg-type]
    return OpenPort2(FakeOpenPort(fake)), fake


def test_defs_parse_dtcodes(defs: LoggerDefs) -> None:
    d1 = defs.dtcodes["D1"]
    assert (d1.tmp_address, d1.mem_address, d1.bit) == (0x00008E, 0x0000AE, 0)
    assert d1.name.startswith("P0335") and d1.source == ""
    assert len(defs.dtcodes) > 500  # второй (не SSM) protocol в файле не разбирается
    assert list(defs.dtcodes)[:2] == ["D1", "D3"]  # порядок definitions


def test_dtcodes_extra_only_add(defs: LoggerDefs, tmp_path: Path) -> None:
    extra = tmp_path / "dtc.xml"
    extra.write_text(
        '<logger><protocols><protocol id="SSM"><dtcodes>'
        '<dtcode id="D9000" name="P9999 TEST CODE" desc="D9000" '
        'tmpaddr="0x000300" memaddr="0x000301" bit="1"/>'
        '<dtcode id="D1" name="HIJACK" desc="D1" tmpaddr="0x1" memaddr="0x2" bit="0"/>'
        "</dtcodes></protocol></protocols></logger>"
    )
    merged = LoggerDefs.load(LOGGER_DEFS, [extra])
    assert merged.dtcodes["D9000"].tmp_address == 0x300 and merged.dtcodes["D9000"].source
    assert merged.dtcodes["D1"].name == defs.dtcodes["D1"].name  # тот же id не перекрыт


@pytest.mark.parametrize("can", [True, False])
def test_read_dtc_current_and_stored(defs: LoggerDefs, can: bool) -> None:
    dev, _ecu = _device(defs, can_enabled=can)
    poller = Poller(connect(dev), defs)
    assert poller.connection.transport == ("can" if can else "kline")
    readings = {r.code.id: (r.current, r.stored) for r in poller.read_dtc()}
    # 1 — tmp, 2 — mem, 3 — оба (как в RomRaider ReadCodesManagerImpl)
    assert readings == {"D1": (True, False), "D3": (False, True), "D4": (True, True)}


def test_ff_pair_is_not_a_code(defs: LoggerDefs) -> None:
    # RomRaider: tmp и mem по 0xFF — код не поддерживается, в список не попадает
    memory = dict(DTC_MEMORY)
    memory[0x90] = memory[0xB0] = 0xFF  # D17/D18 — та же пара байт
    fake = FakeEcu(memory=lambda a: memory.get(a, 0), dtc_addresses=_addresses(defs))
    poller = Poller(connect(OpenPort2(FakeOpenPort(fake))), defs)
    assert [r.code.id for r in poller.read_dtc()] == ["D1", "D3", "D4"]


def test_clear_dtc_erases_codes_and_rereads(defs: LoggerDefs) -> None:
    dev, ecu = _device(defs)
    poller = Poller(connect(dev), defs)
    assert poller.read_dtc()
    assert poller.clear_dtc(pause=0) == []
    assert ecu.resets == 1  # B8 000060 40 дошёл до ЭБУ
    assert ecu.writes[0x8E] == 0 and ecu.writes[0xAE] == 0  # память ошибок стёрта
    assert poller.read_dtc() == []


def test_clear_dtc_forgets_rejected_addresses(defs: LoggerDefs) -> None:
    dev, ecu = _device(defs, can_rejected={0x8E})
    poller = Poller(connect(dev), defs)
    assert {r.code.id for r in poller.read_dtc()} == {"D1", "D3", "D4"}  # 0x8E — через K-line
    assert 0x8E in poller._bad_addresses
    ecu.can_rejected.clear()  # после перезапуска ЭБУ адрес на CAN снова читается
    poller.clear_dtc(pause=0)
    assert not poller._bad_addresses


def test_dtc_read_retries_rejected_address_on_main_channel(defs: LoggerDefs) -> None:
    # Отказ по адресу не вечен: если он снова читается по CAN, из отказавших уходит
    dev, ecu = _device(defs, can_rejected={0x8E})
    poller = Poller(connect(dev), defs)
    poller.read_dtc()
    assert 0x8E in poller._bad_addresses
    ecu.can_rejected.clear()
    poller.read_dtc()
    assert 0x8E not in poller._bad_addresses


def test_clear_dtc_notes_reset_in_trace(defs: LoggerDefs) -> None:
    fake = FakeEcu(memory=lambda a: DTC_MEMORY.get(a, 0), dtc_addresses=_addresses(defs))
    trace = io.StringIO()
    dev = OpenPort2(FakeOpenPort(fake), Trace(trace))
    Poller(connect(dev), defs).clear_dtc(pause=0)
    assert "DTC cleared (ECU reset 0x40)" in trace.getvalue()


@pytest.mark.parametrize("link_cls", [CanLink, KLineLink])
def test_write_address_response(link_cls: type) -> None:
    ecu = FakeEcu()
    client = SsmClient(link_cls(OpenPort2(FakeOpenPort(ecu))))
    client.write_address(0x000060, 0x40)
    assert ecu.resets == 1
    client.reset_ecu()
    assert ecu.resets == 2
