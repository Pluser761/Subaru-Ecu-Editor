"""Эмулятор OpenPort 2.0 и ЭБУ Subaru за ним — чтобы проверять логгер без машины.

FakeEcu отвечает на init, читает адреса (`A8`), пишет байт (`B8`) и хранит память ошибок:
reset ЭБУ (0x40 по 0x000060) обнуляет адреса DTC. FakeOpenPort ведёт себя как порт адаптера
(write/read): разбирает команды `at*`, отвечает `aro`/`are`, а передачу на CAN или K-line
отдаёт FakeEcu и возвращает его ответ двоичной записью в том же формате, что и адаптер.
Формат записей K-line взят по аналогии с CAN (на железе не проверен) — эмулятор и op2.py
согласованы между собой.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from src.logger.core import ssm
from src.logger.core.transport import op2

# Память ЭБУ: адрес → байт (функция — чтобы значения менялись во времени)
Memory = Callable[[int], int]

# Ошибки J2534, которые возвращает эмулятор
ERR_FAILED = 7
ERR_TIMEOUT = 9
ERR_INVALID_PROTOCOL_ID = 3


@dataclass
class FakeEcu:
    """ЭБУ: init и чтение адресов. Ответ на init: 3 байта, ID ЭБУ (5 байт), затем
    capabilities — биты возможностей (с 8-го байта данных)."""

    ecu_id: bytes = bytes.fromhex("5C42584007")
    capabilities: bytes = b"\xff" * 48
    memory: Memory = field(default=lambda address: 0)
    can_enabled: bool = True  # False — по CAN ЭБУ молчит (CAN не разведён в разъёме)
    can_rejected: set[int] = field(default_factory=set)  # адреса, на которые по CAN отказ
    # Как ЭБУ 5C42584007 на машине (21.10.2026): адреса RAM (0xFF0000+) по CAN — отказ 7F A8 12
    can_rejects_ram: bool = False
    # Память ошибок: адреса байт DTC, которые обнуляются reset ЭБУ (0x40 по 0x000060)
    dtc_addresses: set[int] = field(default_factory=set)
    # Записи B8 поверх memory: адрес → байт
    writes: dict[int, int] = field(default_factory=dict)
    resets: int = 0  # сколько раз принят «Reset ECU»
    requests: list[tuple[str, bytes]] = field(default_factory=list)

    @property
    def init_data(self) -> bytes:
        return b"\x00\x01\x02" + self.ecu_id + self.capabilities

    def read(self, address: int) -> int:
        """Байт памяти: сначала записи B8, потом memory (функция может меняться во времени)."""
        return self.writes.get(address, self.memory(address)) & 0xFF

    def write(self, address: int, value: int) -> None:
        """Запись байта по адресу (B8); 0x40 по 0x000060 — RomRaider «Reset ECU»:
        стирает коды неисправностей (как и на машине, проверяется тестами)."""
        value &= 0xFF
        self.writes[address] = value
        if address == ssm.ECU_RESET_ADDRESS and value == ssm.ECU_RESET_CODE:
            self.resets += 1
            for dtc in self.dtc_addresses:
                self.writes[dtc] = 0

    def handle_can(self, message: bytes) -> bytes | None:
        self.requests.append(("can", message))
        if not self.can_enabled or not message:
            return None
        command = message[0]
        if command == ssm.CAN_INIT:
            return bytes([ssm.CAN_INIT_RESPONSE]) + self.init_data
        if command == ssm.READ_ADDRESS:
            addresses = _addresses(message[2:])
            if any(a in self.can_rejected for a in addresses):
                return bytes([ssm.NEGATIVE_RESPONSE, command, 0x31])
            if self.can_rejects_ram and any(a >= 0xFF0000 for a in addresses):
                return bytes([ssm.NEGATIVE_RESPONSE, command, 0x12])
            return bytes([ssm.READ_ADDRESS_RESPONSE]) + bytes(self.read(a) for a in addresses)
        if command == ssm.WRITE_ADDRESS:
            if len(message) < 5:
                return bytes([ssm.NEGATIVE_RESPONSE, command, 0x13])
            self.write(int.from_bytes(message[1:4], "big"), message[4])
            return bytes([ssm.WRITE_ADDRESS_RESPONSE, message[4]])
        return bytes([ssm.NEGATIVE_RESPONSE, command, 0x11])

    def handle_kline(self, packet: bytes) -> bytes | None:
        self.requests.append(("kline", packet))
        if len(packet) < 6 or packet[0] != ssm.KLINE_HEADER or ssm.checksum(packet[:-1]) != packet[-1]:
            return None
        command, data = packet[4], packet[5:-1]
        if command == ssm.KLINE_INIT:
            body = bytes([ssm.KLINE_INIT_RESPONSE]) + self.init_data
        elif command == ssm.READ_ADDRESS:
            body = bytes([ssm.READ_ADDRESS_RESPONSE]) + bytes(self.read(a) for a in _addresses(data[1:]))
        elif command == ssm.WRITE_ADDRESS:
            if len(data) < 4:
                return None
            self.write(int.from_bytes(data[:3], "big"), data[3])
            body = bytes([ssm.WRITE_ADDRESS_RESPONSE, data[3]])
        else:
            return None
        return ssm.kline_packet(body[0], body[1:], dest=ssm.TESTER_ADDRESS, src=ssm.ECU_ADDRESS)


def _addresses(data: bytes) -> list[int]:
    return [int.from_bytes(data[i : i + 3], "big") for i in range(0, len(data) - 2, 3)]


class FakeOpenPort:
    """Порт адаптера для OpenPort2: write() принимает команды, read() отдаёт ответы."""

    def __init__(self, ecu: FakeEcu | None = None, battery_mv: int = 12600, kline_delay: float = 0.0) -> None:
        self.ecu = ecu or FakeEcu()
        self.battery_mv = battery_mv
        # Задержка ответа ЭБУ по K-line, с: на 4800 бод ответ идёт десятки мс
        self.kline_delay = kline_delay
        self._delayed: list[tuple[float, bytes]] = []
        # Адаптер «застрял» посреди двоичных данных оборванной команды: столько байт
        # он примет как данные, а не как команды
        self.stuck_bytes = 0
        # Адаптер «замолчал» (как при плохом контакте USB): команды теряются, ответов нет
        self.silent = False
        self.resets = 0  # сколько раз пришёл atz
        self.channels: dict[int, int] = {}  # протокол → скорость
        self.config: dict[tuple[int, int], int] = {}
        self.filters: dict[int, list[bytes]] = {}
        self._in = bytearray()
        self._out = bytearray()
        self._start = time.monotonic()

    # --- Порт ---

    def write(self, data: bytes) -> int:
        if self.silent:
            return len(data)
        swallowed = min(self.stuck_bytes, len(data))
        self.stuck_bytes -= swallowed
        self._in += data[swallowed:]
        while self._handle_one():
            pass
        return len(data)

    def read(self, size: int = 1) -> bytes:
        if self.silent:
            return b""
        now = time.monotonic()
        due = [data for at, data in self._delayed if at <= now]
        self._delayed = [(at, data) for at, data in self._delayed if at > now]
        for data in due:
            self._out += data
        chunk = bytes(self._out[:size])
        del self._out[:size]
        return chunk

    # --- Команды ---

    def _handle_one(self) -> bool:
        end = self._in.find(b"\r\n")
        if end < 0:
            return False
        line = self._in[:end].decode("ascii", errors="replace").strip()
        args = line.split()
        if not line:
            del self._in[: end + 2]
            return True
        name, rest = args[0], args[1:]
        letter = name[2:3]
        proto = int(name[3:]) if name[3:].isdigit() else None
        # Длина двоичных данных после строки — из аргументов команды
        payload_len = 0
        if letter == "t" and rest:
            payload_len = int(rest[0])
        elif letter == "f" and len(rest) >= 3:
            payload_len = int(rest[2]) * (3 if int(rest[0]) == op2.FLOW_CONTROL_FILTER else 2)
        if len(self._in) < end + 2 + payload_len:
            return False
        payload = bytes(self._in[end + 2 : end + 2 + payload_len])
        del self._in[: end + 2 + payload_len]
        seq = rest[-1] if rest else ""
        if name == "ati":
            self._reply("ari main code version : 1.17.4877 (emulator)")
        elif letter in ("a", "z"):
            self.resets += letter == "z"
            self.channels.clear()
            self._reply(f"aro {seq}")
        elif letter == "r":
            pin = int(rest[0])
            self._reply(f"arr {pin} {self.battery_mv if pin == 16 else 0} {seq}")
        elif letter == "o" and proto is not None:
            self.channels[proto] = int(rest[1])
            self._reply(f"aro {seq}")
        elif proto is None or proto not in self.channels:
            self._reply(f"are {ERR_INVALID_PROTOCOL_ID} {seq}")
        elif letter == "c":
            self.channels.pop(proto, None)
            self._reply(f"aro {seq}")
        elif letter == "s":
            self.config[(proto, int(rest[0]))] = int(rest[1])
            self._reply(f"aro {seq}")
        elif letter == "f":
            self.filters.setdefault(proto, []).append(payload)
            self._reply(f"arf{proto} {len(self.filters[proto])} {seq}")
        elif letter == "t":
            self._transmit(proto, payload, seq)
        else:
            self._reply(f"are {ERR_FAILED} {seq}")
        return True

    def _transmit(self, proto: int, payload: bytes, seq: str) -> None:
        if proto == op2.ISO15765:
            can_id, message = payload[:4], payload[4:]
            self._frame(proto, op2.RX_TX_ECHO, can_id)
            self._reply(f"aro {seq}")
            response = self.ecu.handle_can(message) if self.filters.get(proto) else None
            if response is not None:
                # Как на машине: в одну запись адаптера влезает ~69 байт данных,
                # длинный ответ приходит куском 0x00 и остатком 0x40
                response_id = (0x7E8).to_bytes(4, "big")
                chunks = [response[i : i + 69] for i in range(0, len(response), 69)] or [b""]
                for chunk in chunks[:-1]:
                    self._frame(proto, op2.RX_PARTIAL, response_id + chunk)
                self._frame(proto, op2.RX_COMPLETE, response_id + chunks[-1])
        elif proto == op2.ISO9141:
            # Как у настоящего адаптера (журнал 21.10.2026): служебные записи с меткой времени,
            # эхо и данные — без неё; ответ ЭБУ — кусками до 32 байт
            self._frame(proto, 0xA0, b"")
            if self.config.get((proto, op2.LOOPBACK), 0):
                self._kline_data(op2.KLINE_ECHO, payload)
            self._frame(proto, 0x60, b"")
            self._reply(f"aro {seq}")
            response = self.ecu.handle_kline(payload) if self.filters.get(proto) else None
            if response is not None:
                self._frame(proto, op2.RX_FIRST_FRAME, b"")
                for i in range(0, len(response), 32):
                    self._kline_data(op2.KLINE_DATA, response[i : i + 32])
        else:
            self._reply(f"are {ERR_FAILED} {seq}")

    def _reply(self, text: str) -> None:
        self._out += text.encode() + b"\r\n"

    def _kline_data(self, status: int, data: bytes) -> None:
        # Служебная запись адаптера: длина — один байт, длинное эхо (запрос на 84 адреса)
        # отдаём несколькими записями, их собирает SSM
        chunks = [data[i : i + 254] for i in range(0, len(data), 254)] or [b""]
        for chunk in chunks:
            body = bytes([status]) + chunk
            record = b"ar" + str(op2.ISO9141).encode() + bytes([len(body)]) + body
            if status == op2.KLINE_DATA and self.kline_delay > 0:
                self._delayed.append((time.monotonic() + self.kline_delay, record))
            else:
                self._out += record

    def _frame(self, proto: int, status: int, data: bytes) -> None:
        stamp = int((time.monotonic() - self._start) * 1e6) & 0xFFFFFFFF
        body = bytes([status]) + stamp.to_bytes(4, "big") + data
        self._out += b"ar" + str(proto).encode() + bytes([len(body)]) + body


def demo_memory(period: float = 8.0) -> Memory:
    """Память ЭБУ для показа окна без машины: цикл «разгон — сброс газа» за period секунд.

    Стандартные адреса SSM (logger def RomRaider) и свои E9001/E9002/E9007 для AZ1G101M:
    обороты, МАФ, дроссель, педаль, IPW, лямбда, скорость, ОЖ и стадия отсечки на сбросе.
    """
    start = time.monotonic()

    def memory(address: int) -> int:
        t = (time.monotonic() - start) % period
        accel = t < period * 0.6  # первые 60 % — разгон, дальше сброс газа
        phase = t / (period * 0.6) if accel else (t - period * 0.6) / (period * 0.4)
        rpm = 2000 + 3000 * phase if accel else 5000 - 3800 * phase
        pedal = 80.0 if accel else 0.0
        maf = rpm / 50 if accel else 4.0
        cut_stage = 0 if accel else min(4, int(phase * 8))
        values = {
            0x08: 80 + 40,  # ОЖ 80 °C
            0x0E: int(rpm * 4) >> 8,
            0x0F: int(rpm * 4) & 0xFF,
            0x10: int(40 + rpm / 100),  # км/ч
            0x11: int(128 + 2 * (15 if accel else 5)),
            0x13: int(maf * 100) >> 8,
            0x14: int(maf * 100) & 0xFF,
            0x15: int(pedal * 255 / 100),
            0x1C: int(13.8 * 100 / 8),
            0x20: 0 if cut_stage else int((rpm / 600) * 1000 / 256),
            0x29: int(pedal * 255 / 100),
            0x46: int((0.85 if accel else 1.6) * 128),
            0xFF7778: 0,
            0xFF7779: int(0 if accel else phase * 150),  # таймер отсечки, младший байт
            0xFF7768: cut_stage,
            0xFF776C: 0 if accel else 1,
            0xFF67CB: 0,
        }
        return values.get(address, 0)

    return memory
