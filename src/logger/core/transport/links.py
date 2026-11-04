"""Каналы к ЭБУ поверх OpenPort 2.0: CAN (ISO15765) и K-line (ISO9141).

Настройки повторяют то, что ставит RomRaider (J2534ConnectionISO15765 / ISO9141):
- CAN 500 кбит/с, запрос на 0x7E0, ответ с 0x7E8, flow control-фильтр, кадры дополняются
  до 8 байт (ISO15765_FRAME_PAD);
- K-line 4800 бод 8N1 без контрольной суммы адаптера (SSM считает свою), P1_MAX=1,
  P3_MIN=1, P4_MIN=0, LOOPBACK=1 — эхо своей передачи приходит обратно, его снимает SSM.
"""

from __future__ import annotations

import time
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

    def send(self, payload: bytes, wait: bool = True) -> None: ...

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
        self._partial = bytearray()  # куски длинного ответа (status 0x00), ещё не полный
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

    def send(self, payload: bytes, wait: bool = True) -> None:
        self._dev.transmit(op2.ISO15765, self._tx_id + payload, op2.ISO15765_FRAME_PAD, wait=wait)

    def receive(self, timeout: float) -> bytes | None:
        """Следующее полное сообщение от ЭБУ (без CAN ID).

        Длинный ответ адаптер отдаёт несколькими записями (на машине — status 0x00
        с первым куском и 0x40 с остатком: запрос DTC на 84 адреса — это 85 байт).
        Собираем куски до записи «сообщение готово»; эхо своей передачи пропускаем.
        """
        deadline = time.monotonic() + timeout
        while True:
            frame = self._dev.receive(op2.ISO15765, max(deadline - time.monotonic(), 0))
            if frame is None:
                self._partial.clear()
                return None
            if frame.status & op2.RX_TX_ECHO:
                continue
            if frame.data[:4] != self._rx_id:
                continue
            payload = frame.data[4:]
            if frame.status & op2.RX_COMPLETE:
                data = bytes(self._partial) + payload
                self._partial.clear()
                return data
            # Первый/промежуточный кусок длинного ответа: status 0x00 (машина)
            # или 0x80 (эмулятор), продолжение придёт записью с RX_COMPLETE
            self._partial += payload

    def flush(self) -> None:
        self._dev.drain(op2.ISO15765)
        self._partial.clear()

    def close(self) -> None:
        self._partial.clear()
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

    def send(self, payload: bytes, wait: bool = True) -> None:
        self._dev.transmit(op2.ISO9141, payload, wait=wait)

    def receive(self, timeout: float) -> bytes | None:
        """Следующий кусок принятых байт (пакеты SSM собирает уровень выше). Эхо своей
        передачи и служебные записи без данных пропускаются."""
        deadline = time.monotonic() + timeout
        while True:
            frame = self._dev.receive(op2.ISO9141, max(deadline - time.monotonic(), 0))
            if frame is None:
                return None
            if frame.status == op2.KLINE_DATA and frame.data:
                return frame.data

    def flush(self) -> None:
        self._dev.drain(op2.ISO9141)

    def close(self) -> None:
        self._dev.close_channel(op2.ISO9141)
