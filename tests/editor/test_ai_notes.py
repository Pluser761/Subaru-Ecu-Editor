"""Заметки о прошивке (контекст ИИ): ключ по имени файла и коду прошивки, дописывание, Save As."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.ai.notes import NoteKey, NotesStore


def test_key_uses_file_name_not_folder():
    a = NoteKey.for_rom("/a/stock.hex", "AZ1G301F")
    assert a == NoteKey.for_rom("/b/stock.hex", "AZ1G301F")
    assert a != NoteKey.for_rom("/a/stock.hex", "A2WC522N")
    assert NoteKey.for_rom(None, "AZ1G301F") is None and NoteKey.for_rom("/a/x.hex", None) is None


def test_path_stays_inside_store(tmp_path: Path):
    store = NotesStore(tmp_path)
    path = store.path(NoteKey("../../evil/..", "../x"))
    assert path.resolve().is_relative_to(tmp_path.resolve())


def test_append_dates_entries_and_empty_write_deletes(tmp_path: Path):
    store = NotesStore(tmp_path)
    key = NoteKey("stock.hex", "AZ1G301F")
    assert store.read(key) == ""
    store.append(key, "98 октан", now=datetime(2026, 10, 15, 14, 3))
    text = store.append(key, "  поднял наддув  ", now=datetime(2026, 10, 16, 9, 0))
    assert text == "## 2026-10-15 14:03\n98 октан\n\n## 2026-10-16 09:00\nподнял наддув\n"
    assert store.path(key).parent.name == "AZ1G301F"
    store.write(key, "  \n")
    assert not store.path(key).exists() and store.read(key) == ""


def test_copy_to_new_name_keeps_existing(tmp_path: Path):
    store = NotesStore(tmp_path)
    stock, edited = NoteKey("stock.hex", "AZ1G301F"), NoteKey("stock-edited.hex", "AZ1G301F")
    assert not store.copy(stock, edited)  # копировать нечего
    store.write(stock, "машина")
    assert store.copy(stock, edited) and store.read(edited) == "машина\n"
    store.write(edited, "свои")
    assert not store.copy(stock, edited) and store.read(edited) == "свои\n"
