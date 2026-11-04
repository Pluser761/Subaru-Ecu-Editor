"""Протокол Subaru SSM: init (ID ЭБУ, байты возможностей) и чтение байт по адресам.

Константы — как в RomRaider (io.protocol.ssm.iso9141 / iso15765):
- K-line: пакет `80 <кому> <от кого> <длина> <команда> <данные…> <сумма>`, ЭБУ 0x10,
  тестер 0xF0, сумма — младший байт суммы всех байт до неё; init `BF` → `FF`;
- CAN: сообщение `<команда> <данные…>` на 0x7E0, ответ с 0x7E8; init `AA` → `EA`,
  отказ ЭБУ — `7F <команда> <код>`;
- чтение адресов одинаковое: `A8 00 <адрес:3>…` → `E8 <байт>…`, по байту на адрес;
- запись байта по адресу: `B8 <адрес:3> <значение>` → `F8 <значение>` (по CAN — без
  заполнителя; на K-line — `B8 <адрес:3> <значение>`).

ID ЭБУ — байты 3–7 данных ответа на init (RomRaider SSMEcuInit); по нему logger def
выбирает адреса extended-параметров. С байта 8 идут биты возможностей: параметр
поддерживается, если в байте `ecubyteindex` стоит бит `ecubit`.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from src.logger.core.transport.links import Link

KLINE_HEADER = 0x80
ECU_ADDRESS = 0x10
TESTER_ADDRESS = 0xF0

READ_ADDRESS = 0xA8
READ_ADDRESS_RESPONSE = 0xE8
WRITE_ADDRESS = 0xB8
WRITE_ADDRESS_RESPONSE = 0xF8
KLINE_INIT = 0xBF
KLINE_INIT_RESPONSE = 0xFF
CAN_INIT = 0xAA
CAN_INIT_RESPONSE = 0xEA
NEGATIVE_RESPONSE = 0x7F

# RomRaider «Reset ECU»: запись 0x40 по адресу 0x000060 (стирает DTC и обученные значения)
ECU_RESET_ADDRESS = 0x000060
ECU_RESET_CODE = 0x40
# OBD-II mode 04 — штатная очистка кодов неисправностей и сохранённых значений
OBD_CLEAR_DTC = 0x04
OBD_CLEAR_DTC_RESPONSE = 0x44

# Длина пакета K-line — один байт: команда + заполнитель + 3 байта на адрес ≤ 255. 84 адреса
# влезли бы в длину, но K-line идёт 4800 бод: запрос на 84 адреса + эхо + ответ ≈ 348 байт
# ≈ 0.7 с — больше таймаута DEFAULT_TIMEOUT (0.5 с), и ответ не дожидаются. 32 адреса —
# ≈ 140 байт ≈ 0.3 с, с запасом.
KLINE_MAX_ADDRESSES = 32
CAN_MAX_ADDRESSES = 84
DEFAULT_TIMEOUT = 0.5


class SsmError(Exception):
    """Нет ответа, битый ответ или отказ ЭБУ (`code` — код отказа на CAN)."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class EcuInit:
    data: bytes  # данные ответа на init без кода ответа

    @property
    def ecu_id(self) -> str:
        """«5C42584007» — как в logger def RomRaider и в romid ecu_defs.xml."""
        return self.data[3:8].hex().upper()

    def supports(self, byte_index: int | None, bit: int | None) -> bool:
        """Поддерживается ли параметр по битам возможностей; без индекса — да."""
        if byte_index is None or bit is None:
            return True
        return byte_index < len(self.data) and bool(self.data[byte_index] & (1 << bit))


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def kline_packet(command: int, data: bytes = b"", dest: int = ECU_ADDRESS, src: int = TESTER_ADDRESS) -> bytes:
    body = bytes([KLINE_HEADER, dest, src, len(data) + 1, command]) + data
    return body + bytes([checksum(body)])


def split_kline(buffer: bytes) -> tuple[bytes | None, bytes]:
    """Выделить первый целый пакет K-line: (пакет или None, остаток). Мусор до заголовка
    отбрасывается; пакет с неверной суммой — SsmError."""
    start = buffer.find(bytes([KLINE_HEADER]))
    if start < 0:
        return None, b""
    buffer = buffer[start:]
    if len(buffer) < 4:
        return None, buffer
    size = 4 + buffer[3] + 1
    if len(buffer) < size:
        return None, buffer
    packet, rest = buffer[:size], buffer[size:]
    if checksum(packet[:-1]) != packet[-1]:
        raise SsmError(f"SSM: bad checksum in {packet.hex(' ')}")
    return packet, rest


@dataclass
class _PendingRead:
    """Запрос чтения по K-line, ответ на который ещё собирается."""

    request: bytes
    count: int
    deadline: float
    buffer: bytes = field(default=b"")


def address_bytes(addresses: Sequence[int]) -> bytes:
    return b"".join(a.to_bytes(3, "big") for a in addresses)


class SsmClient:
    """SSM поверх одного канала. Не потокобезопасен."""

    def __init__(self, link: Link, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.link = link
        self.timeout = timeout
        self.max_addresses = CAN_MAX_ADDRESSES if link.name == "can" else KLINE_MAX_ADDRESSES
        self._pending: _PendingRead | None = None

    @property
    def transport(self) -> str:
        return self.link.name

    def init(self) -> EcuInit:
        if self.link.name == "can":
            data = self._request_can(bytes([CAN_INIT]), CAN_INIT_RESPONSE)
        else:
            data = self._request_kline(KLINE_INIT, b"", KLINE_INIT_RESPONSE)
        if len(data) < 8:
            raise SsmError(f"SSM: init response too short: {data.hex(' ')}")
        return EcuInit(data)

    def read_addresses(self, addresses: Sequence[int]) -> bytes:
        """По байту на каждый адрес; длинные списки делятся на несколько запросов."""
        out = bytearray()
        for i in range(0, len(addresses), self.max_addresses):
            chunk = addresses[i : i + self.max_addresses]
            payload = b"\x00" + address_bytes(chunk)
            if self.link.name == "can":
                data = self._request_can(bytes([READ_ADDRESS]) + payload, READ_ADDRESS_RESPONSE)
            else:
                data = self._request_kline(READ_ADDRESS, payload, READ_ADDRESS_RESPONSE)
            if len(data) != len(chunk):
                raise SsmError(f"SSM: asked {len(chunk)} addresses, got {len(data)} bytes")
            out += data
        return bytes(out)

    def write_address(self, address: int, value: int) -> None:
        """Записать байт по адресу SSM (`B8` → `F8`); ответ ЭБУ должен повторить значение."""
        data = address.to_bytes(3, "big") + bytes([value & 0xFF])
        if self.link.name == "can":
            reply = self._request_can(bytes([WRITE_ADDRESS]) + data, WRITE_ADDRESS_RESPONSE)
        else:
            reply = self._request_kline(WRITE_ADDRESS, data, WRITE_ADDRESS_RESPONSE)
        if not reply or reply[0] != value & 0xFF:
            raise SsmError(f"SSM: write {address:06x} returned {reply.hex(' ') or 'nothing'}")

    def reset_ecu(self) -> None:
        """RomRaider «Reset ECU»: запись 0x40 по 0x000060. Стирает DTC, обученные значения
        и мониторы готовности; после него ЭБУ инициализируют заново (`init`)."""
        self.write_address(ECU_RESET_ADDRESS, ECU_RESET_CODE)

    def clear_obd_dtc(self) -> None:
        """OBD-II mode 04 — стереть коды и сохранённые значения (только CAN).

        На AZ1G101M SSM reset 0x40 коды не стирает, а сервис 04 OBD-II — штатная
        команда очистки. Отказ ЭБУ (`7F 04 xx`) поднимается как SsmError с кодом.
        """
        if self.link.name != "can":
            raise SsmError("SSM: OBD-II mode 04 is supported over CAN only")
        self._request_can(bytes([OBD_CLEAR_DTC]), OBD_CLEAR_DTC_RESPONSE)

    # --- Чтение без ожидания (K-line рядом с быстрым CAN) ---

    @property
    def pending(self) -> bool:
        return self._pending is not None

    def begin_read(self, addresses: Sequence[int]) -> None:
        """Отправить чтение по K-line и не ждать ответа: его собирает poll_read между
        опросами CAN. Не больше max_addresses адресов за раз."""
        if self.link.name == "can":
            raise ValueError("begin_read is for K-line")
        request = kline_packet(READ_ADDRESS, b"\x00" + address_bytes(addresses[: self.max_addresses]))
        self.link.flush()
        self.link.send(request, wait=False)
        count = min(len(addresses), self.max_addresses)
        self._pending = _PendingRead(request, count, time.monotonic() + self.timeout + count * 0.005)

    def poll_read(self) -> bytes | None:
        """Ответ на begin_read, если уже пришёл целиком; None — ещё нет; SsmError — не дождались."""
        pending = self._pending
        if pending is None:
            return None
        while (chunk := self.link.receive(0)) is not None:
            pending.buffer += chunk
        while True:
            try:
                packet, pending.buffer = split_kline(pending.buffer)
            except SsmError:
                self._pending = None
                raise
            if packet is None:
                break
            if packet == pending.request:
                continue  # эхо своей передачи
            if packet[1] == TESTER_ADDRESS and packet[4] == READ_ADDRESS_RESPONSE:
                self._pending = None
                data = packet[5:-1]
                if len(data) != pending.count:
                    raise SsmError(f"SSM: asked {pending.count} addresses, got {len(data)} bytes")
                return data
        if time.monotonic() > pending.deadline:
            self._pending = None
            raise SsmError("SSM/K-line: no response to a8")
        return None

    # --- Обмен ---

    def _request_can(self, message: bytes, expected: int) -> bytes:
        self.link.flush()
        self.link.send(message)
        deadline = time.monotonic() + self.timeout
        while True:
            reply = self.link.receive(max(deadline - time.monotonic(), 0))
            if reply is None:
                raise SsmError(f"SSM/CAN: no response to {message[:1].hex()}")
            if reply[:1] == bytes([NEGATIVE_RESPONSE]) and len(reply) >= 3 and reply[1] == message[0]:
                raise SsmError(f"SSM/CAN: ECU rejected {message[0]:02x}, code {reply[2]:02x}", reply[2])
            if reply[:1] == bytes([expected]):
                return reply[1:]

    def _request_kline(self, command: int, data: bytes, expected: int) -> bytes:
        request = kline_packet(command, data)
        self.link.flush()
        self.link.send(request)
        deadline = time.monotonic() + self.timeout
        buffer = b""
        while True:
            chunk = self.link.receive(max(deadline - time.monotonic(), 0))
            if chunk is None:
                raise SsmError(f"SSM/K-line: no response to {command:02x}")
            buffer += chunk
            while True:
                packet, buffer = split_kline(buffer)
                if packet is None:
                    break
                # Эхо своей передачи (K-line однопроводная, LOOPBACK=1) — пропустить
                if packet == request:
                    continue
                if packet[1] == TESTER_ADDRESS and packet[4] == expected:
                    return packet[5:-1]
