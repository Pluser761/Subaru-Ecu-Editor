"""Контрольные суммы Denso 32-bit и сохранение образа."""

import struct

import pytest

from src.core import checksum
from src.core.parser import EcuParser
from src.core.session import RomSession


def test_stock_rom_checksums_are_valid(jdm: EcuParser):
    address, size = jdm.checksum_table()
    assert (address, size) == (0xFFB80, 168)
    entries = checksum.read_entries(jdm.rom_bytes, address, size)
    assert all(e.ok for e in entries)
    assert sum(not e.empty for e in entries) == 12


def test_fix_after_edit(jdm: EcuParser):
    table = jdm.checksum_table()
    jdm.write_cell("Target Boost ", 3, 3, 9.0)
    assert not checksum.verify(jdm.rom_bytes, *table)
    # Правка карты попадает в общий участок (#0) и в свой — исправить нужно именно их
    assert checksum.fix(jdm.rom_bytes, *table) >= 1
    assert checksum.verify(jdm.rom_bytes, *table)
    assert checksum.fix(jdm.rom_bytes, *table) == 0


def test_save_writes_fixed_image(tmp_path, repo, jdm: EcuParser):
    session = RomSession(jdm)
    assert session.save_blocker is None
    session.write_cell("Target Boost ", 3, 3, 9.0)
    out = tmp_path / "edited.hex"
    assert session.save(out) >= 1
    assert not session.dirty and jdm.file_path == str(out)
    saved = EcuParser.open_rom(out, repo.path, defs_repo=repo)
    assert checksum.verify(saved.rom_bytes, *saved.checksum_table())
    assert saved.read_cell("Target Boost ", 3, 3) == pytest.approx(jdm.read_cell("Target Boost ", 3, 3))


def test_save_refused_without_checksum_table(edm: EcuParser, tmp_path):
    session = RomSession(edm)
    assert session.save_blocker is not None
    with pytest.raises(ValueError):
        session.save(tmp_path / "x.hex")
    assert not (tmp_path / "x.hex").exists()


def test_save_refused_when_original_sums_do_not_match(jdm: EcuParser):
    # Файл «уже изменён без пересчёта»: суммы не сходятся — алгоритму не доверяем
    address, _ = jdm.checksum_table()
    stored = struct.unpack(">I", jdm.rom_bytes[address + 8 : address + 12])[0]
    jdm.rom_bytes[address + 8 : address + 12] = struct.pack(">I", stored ^ 1)
    assert RomSession(jdm).save_blocker is not None
