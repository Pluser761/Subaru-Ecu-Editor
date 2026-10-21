"""OpenPort 2.0: разбор потока адаптера и команды — на эмуляторе."""

from __future__ import annotations

import pytest

from src.logger.core.transport import op2
from src.logger.core.transport.emulator import FakeOpenPort
from src.logger.core.transport.op2 import OpenPort2, OpenPortError


class ScriptedStream:
    """Порт, который отдаёт заранее заданные байты (как пришли бы от адаптера)."""

    def __init__(self, data: bytes) -> None:
        self.data = bytearray(data)
        self.written = bytearray()

    def write(self, data: bytes) -> int:
        self.written += data
        return len(data)

    def read(self, size: int = 1) -> bytes:
        chunk = bytes(self.data[:size])
        del self.data[:size]
        return chunk


def _record(proto: int, status: int, data: bytes) -> bytes:
    body = bytes([status]) + (1234).to_bytes(4, "big") + data
    return b"ar" + str(proto).encode() + bytes([len(body)]) + body


def test_binary_record_with_ar_and_crlf_inside_data() -> None:
    # Данные кадра содержат «ar» и CRLF — читать надо по длине, а не по строкам
    payload = (0x7E8).to_bytes(4, "big") + b"ar\r\nar5"
    stream = ScriptedStream(b"junk" + _record(6, op2.RX_COMPLETE, payload) + b"aro 1\r\n")
    dev = OpenPort2(stream)
    frame = dev.receive(op2.ISO15765, timeout=0.05)
    assert frame is not None and frame.data == payload and frame.status == op2.RX_COMPLETE
    assert frame.timestamp == 1234


def test_reply_with_protocol_digit_and_error() -> None:
    dev = OpenPort2(FakeOpenPort())
    with pytest.raises(OpenPortError) as exc:
        dev.set_config(op2.ISO15765, op2.LOOPBACK, 0)  # канал не открыт
    assert exc.value.code == 3
    dev.open_channel(op2.ISO15765, 0, 500_000, 0)
    filter_id = dev.add_filter(op2.ISO15765, op2.PASS_FILTER, 0, b"\x00" * 4, b"\x00" * 4)
    assert filter_id == 1  # «arf6 1 <seq>» — цифра протокола отброшена


def test_identify_and_voltage() -> None:
    dev = OpenPort2(FakeOpenPort(battery_mv=12450))
    assert dev.identify().startswith("main code version")
    assert dev.read_voltage(16) == pytest.approx(12.45)


def test_bootloader_command_never_sent() -> None:
    stream = ScriptedStream(b"")
    with pytest.raises(ValueError):
        OpenPort2(stream).command("atx", 1)
    assert stream.written == b""


def test_no_reply_is_error() -> None:
    with pytest.raises(OpenPortError):
        OpenPort2(ScriptedStream(b"")).close_all()
