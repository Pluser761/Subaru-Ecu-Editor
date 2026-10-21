"""SSM: пакеты K-line, init и чтение адресов по CAN и K-line — на эмуляторе."""

from __future__ import annotations

import pytest

from src.logger.core import ssm
from src.logger.core.ssm import EcuInit, SsmClient, SsmError
from src.logger.core.transport.emulator import FakeEcu, FakeOpenPort
from src.logger.core.transport.links import CanLink, KLineLink
from src.logger.core.transport.op2 import OpenPort2


def _memory(address: int) -> int:
    return {0x0E: 0x2E, 0x0F: 0xE0, 0x08: 120}.get(address, address & 0xFF)


def test_kline_packet_and_checksum() -> None:
    packet = ssm.kline_packet(ssm.KLINE_INIT)
    assert packet == bytes([0x80, 0x10, 0xF0, 0x01, 0xBF, 0x40])  # известный init RomRaider


def test_split_kline_fragments_and_garbage() -> None:
    packet = ssm.kline_packet(ssm.READ_ADDRESS_RESPONSE, b"\x01\x02", dest=0xF0, src=0x10)
    assert ssm.split_kline(b"\x00\x11" + packet[:3]) == (None, packet[:3])
    assert ssm.split_kline(b"\x11" + packet + b"\x80") == (packet, b"\x80")
    broken = packet[:-1] + bytes([packet[-1] ^ 1])
    with pytest.raises(SsmError):
        ssm.split_kline(broken)


def test_ecu_init_id_and_capabilities() -> None:
    init = EcuInit(b"\x00\x01\x02" + bytes.fromhex("5C42584007") + b"\x81")
    assert init.ecu_id == "5C42584007"
    assert init.supports(8, 0) and init.supports(8, 7) and not init.supports(8, 3)
    assert not init.supports(40, 0)  # байта нет — не поддерживается
    assert init.supports(None, None)


@pytest.mark.parametrize("link_cls", [CanLink, KLineLink])
def test_init_and_read(link_cls: type) -> None:
    port = FakeOpenPort(FakeEcu(memory=_memory))
    client = SsmClient(link_cls(OpenPort2(port)))
    assert client.init().ecu_id == "5C42584007"
    assert client.read_addresses([0x0E, 0x0F, 0x08]) == bytes([0x2E, 0xE0, 120])


def test_kline_echo_and_split_response() -> None:
    # Эмулятор отдаёт эхо запроса и ответ двумя кусками — как может прийти по K-line
    port = FakeOpenPort(FakeEcu(memory=_memory))
    client = SsmClient(KLineLink(OpenPort2(port)))
    assert client.read_addresses([0x08]) == bytes([120])
    assert port.config[(3, 0x03)] == 1  # LOOPBACK включён, как у RomRaider


def test_long_request_is_split() -> None:
    ecu = FakeEcu(memory=_memory)
    client = SsmClient(CanLink(OpenPort2(FakeOpenPort(ecu))))
    addresses = list(range(0x1000, 0x1000 + 200))
    assert client.read_addresses(addresses) == bytes(a & 0xFF for a in addresses)
    reads = [m for kind, m in ecu.requests if m[:1] == bytes([ssm.READ_ADDRESS])]
    assert [len(m[2:]) // 3 for m in reads] == [84, 84, 32]


def test_can_rejection_has_code_and_silence_has_none() -> None:
    ecu = FakeEcu(memory=_memory, can_rejected={0x0E})
    client = SsmClient(CanLink(OpenPort2(FakeOpenPort(ecu))), timeout=0.05)
    with pytest.raises(SsmError) as exc:
        client.read_addresses([0x0E])
    assert exc.value.code == 0x31
    ecu.can_enabled = False
    with pytest.raises(SsmError) as silent:
        client.init()
    assert silent.value.code is None
