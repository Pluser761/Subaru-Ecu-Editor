"""Каналы к ЭБУ поверх OpenPort 2.0: CAN (ISO15765) и K-line (ISO9141).

Настройки повторяют то, что ставит RomRaider (J2534ConnectionISO15765 / ISO9141):
- CAN 500 кбит/с, запрос на 0x7E0, ответ с 0x7E8, flow control-фильтр, кадры дополняются
  до 8 байт (ISO15765_FRAME_PAD);
- K-line 4800 бод 8N1 без контрольной суммы адаптера (SSM считает свою), P1_MAX=1,
  P3_MIN=1, P4_MIN=0, LOOPBACK=1 — эхо своей передачи приходит обратно, его снимает SSM.
"""

from __future__ import annotations

from typing import Protocol

from src.logger.core.transport import op2
from src.logger.core.transport.op2 import OpenPort2

ECU_REQUEST_ID = 0x7E0
ECU_RESPONSE_ID = 0x7E8
CAN_BAUD = 500_000
KLINE_BAUD = 4800


class Link(Protocol):
    """Канал сообщений к ЭБУ. CAN отдаёт сообщения целиком, K-line — куски потока."""

    name: str  # «can» / «kline»

    def send(self, payload: bytes) -> None: ...

    def receive(self, timeout: float) -> bytes | None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


class CanLink:
    name = "can"

    def __init__(
        self,
        device: OpenPort2,
        channel: int = 0,
        request_id: int = ECU_REQUEST_ID,
        response_id: int = ECU_RESPONSE_ID,
    ) -> None:
        self._dev = device
        self._tx_id = request_id.to_bytes(4, "big")
        self._rx_id = response_id.to_bytes(4, "big")
        p = op2.ISO15765
        device.open_channel(p, 0, CAN_BAUD, channel)
        for param, value in (
            (op2.LOOPBACK, 0),
            (op2.ISO15765_BS, 0),
            (op2.ISO15765_STMIN, 0),
            (op2.BS_TX, 0xFFFF),
            (op2.STMIN_TX, 0xFFFF),
            (op2.ISO15765_WFT_MAX, 0),
        ):
            device.set_config(p, param, value)
        device.add_filter(
            p, op2.FLOW_CONTROL_FILTER, op2.ISO15765_FRAME_PAD, b"\xff\xff\xff\xff", self._rx_id, self._tx_id
        )

    def send(self, payload: bytes) -> None:
        self._dev.transmit(op2.ISO15765, self._tx_id + payload, op2.ISO15765_FRAME_PAD)

    def receive(self, timeout: float) -> bytes | None:
        """Следующее полное сообщение от ЭБУ (без CAN ID); эхо и «первый кадр» пропускаются."""
        while True:
            frame = self._dev.receive(op2.ISO15765, timeout)
            if frame is None:
                return None
            if frame.status & (op2.RX_TX_ECHO | op2.RX_FIRST_FRAME):
                continue
            if frame.data[:4] == self._rx_id:
                return frame.data[4:]

    def flush(self) -> None:
        self._dev.drain(op2.ISO15765)

    def close(self) -> None:
        self._dev.close_channel(op2.ISO15765)


class KLineLink:
    name = "kline"

    def __init__(self, device: OpenPort2, channel: int = 1) -> None:
        self._dev = device
        p = op2.ISO9141
        device.open_channel(p, op2.ISO9141_NO_CHECKSUM, KLINE_BAUD, channel)
        for param, value in (
            (op2.P1_MAX, 1),
            (op2.P3_MIN, 1),
            (op2.P4_MIN, 0),
            (op2.LOOPBACK, 1),
            (op2.DATA_BITS, 0),  # 0 — 8 бит
            (op2.PARITY, 0),
        ):
            device.set_config(p, param, value)
        device.add_filter(p, op2.PASS_FILTER, 0, b"\x00", b"\x00")

    def send(self, payload: bytes) -> None:
        self._dev.transmit(op2.ISO9141, payload)

    def receive(self, timeout: float) -> bytes | None:
        # Формат записей K-line у адаптера на железе не проверен: считаем, что данные идут
        # сразу после status и timestamp, без ID. Пакеты SSM собирает уровень выше.
        frame = self._dev.receive(op2.ISO9141, timeout)
        return None if frame is None else frame.data

    def flush(self) -> None:
        self._dev.drain(op2.ISO9141)

    def close(self) -> None:
        self._dev.close_channel(op2.ISO9141)
