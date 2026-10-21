"""Журнал обмена с адаптером: каждый блок байт туда и обратно с временем.

Нужен для отладки по первому подключению к машине и как образец для эмулятора.
Строка: `<мс от начала> > <hex>` (к адаптеру) или `< <hex>` (от адаптера), плюс текст,
если блок печатный (команды и ответы адаптера).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TextIO


class Trace:
    def __init__(self, out: TextIO) -> None:
        self._out = out
        self._start = time.monotonic()

    @classmethod
    def to_file(cls, path: str | Path) -> Trace:
        return cls(Path(path).open("w", encoding="utf-8"))

    def tx(self, data: bytes) -> None:
        self._line(">", data)

    def rx(self, data: bytes) -> None:
        self._line("<", data)

    def note(self, text: str) -> None:
        self._out.write(f"{self._ms():10.1f} # {text}\n")
        self._out.flush()

    def close(self) -> None:
        self._out.close()

    def _ms(self) -> float:
        return (time.monotonic() - self._start) * 1000

    def _line(self, arrow: str, data: bytes) -> None:
        text = data.decode("ascii", errors="replace").strip()
        printable = text.isprintable() and text
        suffix = f"  | {text}" if printable else ""
        self._out.write(f"{self._ms():10.1f} {arrow} {data.hex(' ')}{suffix}\n")
        self._out.flush()
