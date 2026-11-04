"""OpenPort 2.0: разбор потока адаптера и команды — на эмуляторе."""

from __future__ import annotations

import pytest

from src.logger.core.transport import op2
from src.logger.core.transport.emulator import FakeOpenPort
from src.logger.core.transport.links import CanLink
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


def test_no_reply_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(op2, "_REPLY_TIMEOUT", 0.05)
    with pytest.raises(OpenPortError):
        OpenPort2(ScriptedStream(b"")).close_all()


# --- Байты настоящего OpenPort 2.0 на машине (журнал 21.10.2026, ЭБУ 5C42584007) ---

# K-line после передачи init «80 10 F0 01 BF 40»: служебные записи с меткой времени,
# эхо (0x20) и данные (0x00) — без неё; первые 64 байта ответа ЭБУ
REAL_KLINE_INIT_RX = bytes.fromhex(
    "61723305a001ad2f7e61723307208010f001bf40617233056001ad585961726f2032380d0a"
    "617233058001ad68dc617233210080f01069ffa210115c42584007f3fac98c0b83feac0000"
    "0066ced4fdb060001f6172332100200000000000dc00005d1f3080f0e6000043fb00f5c986"
    "00000001edf1800081"
)

# CAN: эхо запроса чтения, «aro», отказ ЭБУ на адрес RAM — «7F A8 12»
REAL_CAN_REJECT_RX = bytes.fromhex(
    "617236091001aa90300000" "07e0"  # эхо: status 0x10, время, CAN ID 0x7E0
    "61726f2031320d0a"  # «aro 12»
    "6172360c4001aa91960000" "07e8" "7fa812"  # ответ 0x7E8: отказ на A8, код 0x12
)


def test_real_kline_records_have_no_timestamp() -> None:
    dev = OpenPort2(ScriptedStream(REAL_KLINE_INIT_RX))
    frames = []
    while (frame := dev.receive(op2.ISO9141, 0.01)) is not None:
        frames.append(frame)
    assert [f.status for f in frames] == [0xA0, op2.KLINE_ECHO, 0x60, 0x80, 0x00, 0x00]
    assert frames[1].data == bytes.fromhex("8010F001BF40")  # эхо своей передачи
    data = b"".join(f.data for f in frames if f.status == op2.KLINE_DATA)
    assert data[:4] == bytes.fromhex("80F01069")  # пакет SSM: к тестеру, от ЭБУ, 0x69 байт
    assert data[5 + 3 : 5 + 8] == bytes.fromhex("5C42584007")  # ID ЭБУ после кода FF


def test_real_can_rejection() -> None:
    dev = OpenPort2(ScriptedStream(REAL_CAN_REJECT_RX))
    statuses = []
    while (frame := dev.receive(op2.ISO15765, 0.01)) is not None:
        statuses.append((frame.status, frame.data))
    assert statuses[0][0] == op2.RX_TX_ECHO
    assert statuses[1] == (op2.RX_COMPLETE, bytes.fromhex("000007E8") + bytes.fromhex("7FA812"))


def test_can_long_response_is_reassembled() -> None:
    # На машине ответ на запрос DTC (84 адреса = 85 байт) приходит записью 0x00
    # (69 байт) и остатком 0x40: CanLink должен собрать их в одно сообщение
    port = FakeOpenPort()
    link = CanLink(OpenPort2(port))
    port._out.clear()
    response_id = (0x7E8).to_bytes(4, "big")
    first = b"\xe8" + bytes(range(10))
    rest = bytes(range(10, 16))
    port._frame(op2.ISO15765, op2.RX_FIRST_FRAME, response_id)  # маркер «будет длинный ответ»
    port._frame(op2.ISO15765, op2.RX_PARTIAL, response_id + first)
    port._frame(op2.ISO15765, op2.RX_COMPLETE, response_id + rest)
    assert link.receive(0.1) == first + rest
    # Следующее короткое сообщение не должно склеиться с остатком прошлого
    port._frame(op2.ISO15765, op2.RX_COMPLETE, response_id + b"\xe8\x01")
    assert link.receive(0.1) == b"\xe8\x01"


def test_identify_resyncs_stuck_adapter() -> None:
    port = FakeOpenPort()
    port.stuck_bytes = 40  # ждёт ещё 40 байт данных оборванной команды att
    assert OpenPort2(port).identify().startswith("main code version")


def test_identify_dead_adapter_has_no_reply_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(op2, "_REPLY_TIMEOUT", 0.05)
    with pytest.raises(OpenPortError) as exc:
        OpenPort2(ScriptedStream(b"")).identify()
    assert exc.value.code == op2.NO_REPLY
