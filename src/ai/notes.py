"""Заметки о прошивке — контекст для ИИ между разговорами: машина, топливо, моды, цели,
что уже меняли и почему, выводы по логам.

Хранятся на компьютере пользователя, рядом с settings.ini: `notes/<xmlid>/<имя файла>.md` —
обычный Markdown, можно править руками. Прошивка сопоставляется по имени файла и коду
прошивки (xmlid): одинаково названные файлы разных ЭБУ не смешиваются, а перенос файла
в другую папку заметки не теряет. «Сохранить как» копирует заметки на новое имя.

Без Qt. Читают и пишут из потока GUI (сервер — через EditorHost.call), поэтому без блокировок.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_UNSAFE = re.compile(r"[^\w.\- ]+")


@dataclass(frozen=True)
class NoteKey:
    """Чьи заметки: имя файла прошивки (без папки) и код прошивки."""

    file_name: str
    xmlid: str

    @classmethod
    def for_rom(cls, file_path: str | None, xmlid: str | None) -> NoteKey | None:
        if not file_path or not xmlid:
            return None
        return cls(Path(file_path).name, xmlid)


def _safe(name: str) -> str:
    # Имя в файловой системе: без разделителей пути и «..»; пустое — «_»
    return _UNSAFE.sub("_", name).strip(" .") or "_"


class NotesStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def path(self, key: NoteKey) -> Path:
        return self.directory / _safe(key.xmlid) / f"{_safe(key.file_name)}.md"

    def read(self, key: NoteKey) -> str:
        try:
            return self.path(key).read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def write(self, key: NoteKey, text: str) -> None:
        """Заменить заметки целиком (через временный файл); пустой текст — удалить файл."""
        target = self.path(key)
        text = text.strip()
        if not text:
            target.unlink(missing_ok=True)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def append(self, key: NoteKey, text: str, now: datetime | None = None) -> str:
        """Дописать запись с датой («## 2026-10-15 14:03»); вернуть заметки целиком."""
        stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
        current = self.read(key).strip()
        entry = f"## {stamp}\n{text.strip()}"
        notes = f"{current}\n\n{entry}" if current else entry
        self.write(key, notes)
        return self.read(key)

    def copy(self, source: NoteKey, target: NoteKey) -> bool:
        """Скопировать заметки на другое имя файла (Save As), если там своих ещё нет."""
        if source == target or self.read(target):
            return False
        text = self.read(source)
        if not text:
            return False
        self.write(target, text)
        return True
