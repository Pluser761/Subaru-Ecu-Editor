"""Контрольные суммы Denso 32-bit (SH7055/SH7058): таблица «Checksum Fix» в definitions.

Таблица — записи по 12 байт (big-endian): начало, конец (включительно), сумма. Для каждой
записи сумма 32-битных слов участка [начало, конец] плюс записанное значение равна
0x5AA5A55A (по модулю 2³²). Пустая запись — 0, 0, 0x5AA5A55A. Проверено на стоковых
прошивках архива RomRaider: суммы сходятся у 405 из 409 32-битных с такой таблицей
(не сходятся у дизелей 2009–2011 и Forester 2.5i AT 2011 — другая схема).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from src.i18n import tr

TOTAL = 0x5AA5A55A
ENTRY_SIZE = 12
TABLE_NAME = "Checksum Fix"


@dataclass(frozen=True)
class ChecksumEntry:
    index: int
    start: int
    end: int  # включительно
    stored: int
    expected: int

    @property
    def empty(self) -> bool:
        return self.start == 0 and self.end == 0

    @property
    def ok(self) -> bool:
        return self.stored == self.expected


def read_entries(rom: bytes | bytearray, address: int, size: int) -> list[ChecksumEntry]:
    """Записи таблицы сумм с вычисленными по образу значениями; ValueError — это не таблица сумм."""
    if address < 0 or address + size > len(rom) or size % ENTRY_SIZE:
        raise ValueError(tr("Таблица контрольных сумм 0x{address}+{size} вне образа", address=f"{address:X}", size=size))
    entries: list[ChecksumEntry] = []
    for index, offset in enumerate(range(address, address + size, ENTRY_SIZE)):
        start, end, stored = struct.unpack(">III", rom[offset : offset + ENTRY_SIZE])
        if start == 0 and end == 0:
            expected = TOTAL
        else:
            if end < start or end >= len(rom) or (end - start + 1) % 4:
                raise ValueError(
                    tr(
                        "Запись контрольной суммы #{index}: участок 0x{start}–0x{end} некорректен",
                        index=index,
                        start=f"{start:X}",
                        end=f"{end:X}",
                    )
                )
            words = struct.unpack(f">{(end - start + 1) // 4}I", rom[start : end + 1])
            expected = (TOTAL - sum(words)) & 0xFFFFFFFF
        entries.append(ChecksumEntry(index, start, end, stored, expected))
    return entries


def verify(rom: bytes | bytearray, address: int, size: int) -> bool:
    return all(entry.ok for entry in read_entries(rom, address, size))


def fix(rom: bytearray, address: int, size: int) -> int:
    """Пересчитать суммы в образе; вернуть число исправленных записей."""
    fixed = 0
    for entry in read_entries(rom, address, size):
        if entry.empty or entry.ok:
            continue
        offset = address + entry.index * ENTRY_SIZE + 8
        rom[offset : offset + 4] = struct.pack(">I", entry.expected)
        fixed += 1
    if not verify(rom, address, size):
        # Участки перекрываются, но сами суммы лежат вне них — второй проход не нужен; если
        # всё же не сошлось, таблица устроена иначе, и записывать такой образ нельзя
        raise ValueError(tr("После пересчёта контрольные суммы не сходятся"))
    return fixed
