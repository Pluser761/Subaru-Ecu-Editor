"""Адаптер целиком: открыть порт, проверить адаптер, подключиться к ЭБУ; переоткрыть после
обрыва связи (из цикла опроса); закрыть со сбросом `atz`.

После `atz` адаптер перезагружается (op2j2534 после него закрывает порт и ждёт 1.6 с), поэтому
порт закрывается сразу, а следующее открытие ждёт RESET_PAUSE с момента сброса — в том числе
новое подключение сразу после «Отключить».

Потоки: open/reopen идут в одном потоке (подключение — в потоке интерфейса, переподключение —
в потоке опроса), interrupt() можно звать из любого: он прерывает паузу и открытие порта
переподключения, чтобы «Отключить» не ждал его до конца.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from src.logger.core.session import Connection, connect
from src.logger.core.trace import Trace
from src.logger.core.transport.op2 import ByteStream, OpenPort2

# Пауза после atz, пока адаптер перезагружается, с
RESET_PAUSE = 2.0

_last_reset = 0.0  # time.monotonic() последнего atz — общий для всех Adapter (адаптер один)

log = logging.getLogger(__name__)


class AdapterClosed(Exception):
    """Адаптер закрыт (нажали «Отключить»), пока шло переподключение."""


@dataclass(frozen=True)
class Opened:
    connection: Connection
    version: str  # ответ на ati
    voltage: float  # пин 16 OBD, В


class Adapter:
    def __init__(
        self, open_stream: Callable[[], ByteStream], trace: Trace | None = None, reset_pause: float = RESET_PAUSE
    ) -> None:
        self._open_stream = open_stream
        self.trace = trace
        self._reset_pause = reset_pause
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._stream: ByteStream | None = None
        self._device: OpenPort2 | None = None

    def open(self) -> Opened:
        """Открыть порт, ati, напряжение, init ЭБУ (CAN, иначе K-line)."""
        global _last_reset
        wait = _last_reset + self._reset_pause - time.monotonic()
        if wait > 0 and self._closed.wait(wait):
            raise AdapterClosed("adapter closed")
        stream = self._open_stream()
        with self._lock:
            if self._closed.is_set():
                _close_stream(stream)
                raise AdapterClosed("adapter closed")
            self._stream = stream
            self._device = device = OpenPort2(stream, self.trace)
        version = device.identify()
        voltage = device.read_voltage()
        self.note(f"{version}; OBD pin 16 = {voltage:.2f} V")
        if self._closed.is_set():
            raise AdapterClosed("adapter closed")
        return Opened(connect(device), version, voltage)

    def reopen(self, reason: str) -> Connection:
        """После обрыва: сброс адаптера, закрыть порт, подождать, открыть и подключиться заново.

        Про обрыв пишет LoggerRunner (одна строка на обрыв, а не на попытку), поэтому здесь
        только отладочная запись причины.
        """
        log.debug("adapter: reopen after %s", reason)
        self._release(close_channels=False)
        connection = self.open().connection
        self.note(f"reconnected: SSM over {connection.transport}")
        return connection

    def interrupt(self) -> None:
        """Прервать переподключение и не начинать новое (перед остановкой цикла опроса)."""
        self._closed.set()

    def close(self) -> None:
        """Закрыть каналы, сбросить адаптер, закрыть порт и журнал. Цикл опроса уже остановлен."""
        self._closed.set()
        self._release(close_channels=True)
        if self.trace is not None:
            self.trace.close()
            self.trace = None

    def note(self, text: str) -> None:
        log.info("adapter: %s", text)
        if self.trace is not None:
            self.trace.note(text)

    def _release(self, close_channels: bool) -> None:
        global _last_reset
        with self._lock:
            device, stream = self._device, self._stream
            self._device = self._stream = None
        if device is not None:
            # Адаптер уже мог пропасть — порт закроем всё равно
            if close_channels:
                try:
                    device.close_all()
                except Exception:  # noqa: BLE001
                    pass
            try:
                device.reset()
                _last_reset = time.monotonic()
            except Exception:  # noqa: BLE001
                pass
        if stream is not None:
            _close_stream(stream)


def _close_stream(stream: ByteStream) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass
