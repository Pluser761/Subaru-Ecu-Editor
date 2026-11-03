"""Порт настоящего OpenPort 2.0: USB CDC-ACM (на macOS — /dev/cu.usbmodem…).

Скорость порта адаптер игнорирует (115200 8N1 — как у op2j2534), DTR и RTS включены.
Таймаут чтения короткий: OpenPort2 сам ждёт ответа до своего срока.
"""

from __future__ import annotations

import serial
from serial.tools import list_ports

OPENPORT_VID = 0x0403
OPENPORT_PID = 0xCC4D
READ_TIMEOUT = 0.005


class OpenPortNotFound(OSError):
    """OpenPort 2.0 не найден среди USB-устройств (нет порта с VID/PID адаптера)."""


def find_openport() -> list[str]:
    """Пути портов подключённых OpenPort 2.0."""
    return [p.device for p in list_ports.comports() if p.vid == OPENPORT_VID and p.pid == OPENPORT_PID]


def open_port(device: str) -> serial.Serial:
    port = serial.Serial(device, 115200, timeout=READ_TIMEOUT)
    port.dtr = True
    port.rts = True
    return port
