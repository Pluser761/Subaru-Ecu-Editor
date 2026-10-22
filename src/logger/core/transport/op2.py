"""Tactrix OpenPort 2.0: текстовый протокол адаптера поверх USB-порта (CDC-ACM).

Протокол разобран в открытом проекте kuznet1/op2j2534 (PROTOCOL.md, Apache-2.0):
- команда: `at<буква>[<протокол J2534>] <аргументы> <номер>\\r\\n`, двоичные данные — сразу
  после CRLF; ответ `ar<тип> … <номер>\\r\\n` повторяет номер команды;
- принятые кадры идут в том же потоке двоичными записями
  `'a' 'r' <цифра протокола> <len> <status> <timestamp:4> <данные…>` (len — байт после него),
  у CAN/ISO15765 первые 4 байта данных — CAN ID; у K-line (проверено на машине 21.10.2026)
  записи с данными — без метки времени: `<status 0x00 | 0x20> <данные…>`, а с меткой —
  только служебные без данных (0xA0, 0x60 — передача, 0x80 — начало приёма);
- без фильтра адаптер не отдаёт ни одного кадра; таймаут передачи должен быть ненулевым.

Команду `atx` (перевод адаптера в загрузчик) не отправляем никогда.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Protocol

from src.logger.core.trace import Trace

# Протоколы J2534 (цифра после буквы команды)
ISO9141 = 3
ISO15765 = 6

# Параметры SET_CONFIG (J2534)
DATA_RATE = 0x01
LOOPBACK = 0x03
P1_MAX = 0x07
P3_MIN = 0x0A
P4_MIN = 0x0C
PARITY = 0x16
ISO15765_BS = 0x1E
ISO15765_STMIN = 0x1F
DATA_BITS = 0x20
BS_TX = 0x22
STMIN_TX = 0x23
ISO15765_WFT_MAX = 0x25

# Флаги
ISO9141_NO_CHECKSUM = 0x200  # контрольную сумму SSM считаем сами
ISO15765_FRAME_PAD = 0x40

# Типы фильтров
PASS_FILTER = 1
FLOW_CONTROL_FILTER = 3

# Биты status принятой записи (свои у адаптера, не J2534 RxStatus)
RX_TX_ECHO = 0x10  # эхо нашей передачи
RX_FIRST_FRAME = 0x80  # ISO-TP: пришёл первый кадр, сообщение собирается
RX_COMPLETE = 0x40  # полное сообщение
# K-line: данные приёма (0x00) и эхо своей передачи (0x20) — без метки времени
KLINE_DATA = 0x00
KLINE_ECHO = 0x20
_KLINE_PROTOCOLS = (ISO9141, 4)

_REPLY_TIMEOUT = 1.5
# Пустых строк для сброса разбора команд адаптера: больше самой длинной передачи
RESYNC_NEWLINES = 160
# Код ошибки «адаптер не ответил» (не J2534) — чтобы интерфейс подсказал переподключить USB
NO_REPLY = -1


class ByteStream(Protocol):
    """Порт адаптера: pyserial.Serial или эмулятор. read(n) возвращает то, что пришло
    за короткий таймаут порта (может быть пусто)."""

    def write(self, data: bytes) -> int | None: ...

    def read(self, size: int = 1) -> bytes: ...


class OpenPortError(Exception):
    """Ошибка адаптера: `are <код J2534>` или нет ответа."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RxFrame:
    protocol: int
    status: int
    timestamp: int  # мкс, часы адаптера
    data: bytes  # у CAN — с 4 байтами CAN ID впереди


@dataclass(frozen=True)
class Reply:
    kind: str  # «o» успех, «e» ошибка, «i», «r», «f», «g»
    args: list[str]


class OpenPort2:
    """Команды адаптера и приём кадров. Не потокобезопасен: один владелец (цикл опроса)."""

    def __init__(self, stream: ByteStream, trace: Trace | None = None) -> None:
        self._stream = stream
        self._trace = trace
        self._buf = bytearray()
        self._seq = 0
        self._frames: dict[int, deque[RxFrame]] = {}

    # --- Команды ---

    def identify(self) -> str:
        """`ati` → «main code version : 1.17.4877» (без номера команды).

        Если адаптер молчит, он мог застрять посреди двоичных данных прошлой команды
        (оборванный `att…`): пустые строки дочитывают их, после чего `ati` повторяется.
        """
        for attempt in range(2):
            if attempt:
                self._write(b"\r\n" * RESYNC_NEWLINES)
            self._write(b"ati\r\n")
            deadline = time.monotonic() + _REPLY_TIMEOUT
            while (item := self._next_item(deadline)) is not None:
                if isinstance(item, Reply) and item.kind == "i":
                    return " ".join(item.args)
        raise OpenPortError("OpenPort: no reply to ati", NO_REPLY)

    def close_all(self) -> None:
        self.command("ata")

    def read_voltage(self, pin: int = 16) -> float:
        """Напряжение на пине OBD, В (16 — питание машины; без машины около 0)."""
        reply = self.command("atr", pin)
        return int(reply.args[1]) / 1000

    def open_channel(self, protocol: int, flags: int, baud: int, channel: int) -> None:
        self.command(f"ato{protocol}", flags, baud, channel)
        self._frames.setdefault(protocol, deque())

    def close_channel(self, protocol: int) -> None:
        self.command(f"atc{protocol}")
        self._frames.pop(protocol, None)

    def set_config(self, protocol: int, param: int, value: int) -> None:
        self.command(f"ats{protocol}", param, value)

    def add_filter(
        self, protocol: int, kind: int, tx_flags: int, mask: bytes, pattern: bytes, flow: bytes = b""
    ) -> int:
        """Фильтр приёма; flow — ID, с которым адаптер шлёт flow control (для FLOW_CONTROL)."""
        if len(mask) != len(pattern) or (flow and len(flow) != len(mask)):
            raise ValueError("filter messages must have the same length")
        reply = self.command(f"atf{protocol}", kind, tx_flags, len(mask), payload=mask + pattern + flow)
        return int(reply.args[0])

    def transmit(
        self, protocol: int, data: bytes, tx_flags: int = 0, timeout_us: int = 200_000, wait: bool = True
    ) -> None:
        """Передать сообщение. wait=False — не ждать «aro»: по K-line он приходит только после
        физической передачи (десятки мс), а за это время можно опрашивать CAN; ответ на такую
        команду потом молча пропускается."""
        # Таймаут 0 адаптер понимает как «не передавать» — всегда ненулевой
        args = (len(data), tx_flags, max(timeout_us, 1000))
        if wait:
            self.command(f"att{protocol}", *args, payload=data)
        else:
            self._send_command(f"att{protocol}", *args, payload=data)

    def _send_command(self, name: str, *args: int, payload: bytes = b"") -> int:
        if name.startswith("atx"):
            raise ValueError("atx (bootloader) is never sent")
        self._seq += 1
        line = " ".join([name, *(str(a) for a in args), str(self._seq)]).encode() + b"\r\n"
        self._write(line + payload)
        return self._seq

    def command(self, name: str, *args: int, payload: bytes = b"") -> Reply:
        """Отправить команду и дождаться ответа с её номером; `are` — OpenPortError."""
        seq = self._send_command(name, *args, payload=payload)
        deadline = time.monotonic() + _REPLY_TIMEOUT
        while True:
            item = self._next_item(deadline)
            if item is None:
                raise OpenPortError(f"OpenPort: no reply to {name}")
            if not isinstance(item, Reply) or not item.args or item.args[-1] != str(seq):
                continue
            if item.kind == "e":
                code = int(item.args[0]) if item.args[0].isdigit() else None
                raise OpenPortError(f"OpenPort: {name} failed, J2534 error {code}", code)
            return Reply(item.kind, item.args[:-1])

    # --- Приём ---

    def receive(self, protocol: int, timeout: float) -> RxFrame | None:
        """Следующий принятый кадр протокола или None за timeout секунд."""
        queue = self._frames.setdefault(protocol, deque())
        deadline = time.monotonic() + timeout
        while not queue:
            item = self._next_item(deadline)
            if item is None:
                return None
        return queue.popleft()

    def drain(self, protocol: int) -> None:
        """Выбросить уже принятые кадры (старые ответы после таймаута)."""
        while self._next_item(time.monotonic()) is not None:
            pass
        self._frames.get(protocol, deque()).clear()

    # --- Поток ---

    def _write(self, data: bytes) -> None:
        if self._trace:
            self._trace.tx(data)
        self._stream.write(data)

    def _fill(self, deadline: float) -> bool:
        """Дочитать из порта; False — до deadline ничего не пришло."""
        while True:
            chunk = self._stream.read(4096)
            if chunk:
                if self._trace:
                    self._trace.rx(chunk)
                self._buf += chunk
                return True
            if time.monotonic() >= deadline:
                return False

    def _next_item(self, deadline: float) -> Reply | RxFrame | None:
        """Разобрать из буфера одну запись: ответ (queue кадров не трогает) или кадр (кладёт
        в очередь своего протокола и тоже возвращает его)."""
        while True:
            item = self._parse_one()
            if item is not None:
                if isinstance(item, RxFrame):
                    self._frames.setdefault(item.protocol, deque()).append(item)
                return item
            if not self._fill(deadline):
                return None

    def _parse_one(self) -> Reply | RxFrame | None:
        buf = self._buf
        start = buf.find(b"ar")
        if start < 0:
            # Мусор до начала записи; последний байт может быть началом «ar»
            del buf[: max(len(buf) - 1, 0)]
            return None
        del buf[:start]
        if len(buf) < 3:
            return None
        kind = chr(buf[2])
        if kind.isdigit():
            # Двоичная запись: читать по длине, данные могут содержать «ar» и CRLF
            if len(buf) < 4 or len(buf) < 4 + buf[3]:
                return None
            length = buf[3]
            payload = bytes(buf[4 : 4 + length])
            del buf[: 4 + length]
            protocol = int(kind)
            if protocol in _KLINE_PROTOCOLS and payload and payload[0] in (KLINE_DATA, KLINE_ECHO):
                return RxFrame(protocol=protocol, status=payload[0], timestamp=0, data=payload[1:])
            if length < 5:
                return None
            return RxFrame(
                protocol=int(kind),
                status=payload[0],
                timestamp=int.from_bytes(payload[1:5], "big"),
                data=payload[5:],
            )
        end = buf.find(b"\r\n")
        if end < 0:
            return None
        text = buf[3:end].decode("ascii", errors="replace")
        del buf[: end + 2]
        # «arf6 1 7»: цифра протокола приклеена к типу ответа — отбрасываем её
        args = text.split()
        if args and args[0].isdigit() and not text.startswith(" "):
            args = args[1:]
        return Reply(kind, args)
