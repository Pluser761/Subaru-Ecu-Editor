"""Дополнения definitions: EcuFlash (SubaruDefs) и свои RomRaider-файлы поверх ecu_defs.xml."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from src.core.parser import EcuParser
from src.core.xml_reader import EcuDefsRepository, definition_files

ROOT = Path(__file__).resolve().parents[2]
STOCK_101M = ROOT / "ecu" / "RomRaider" / "EDM" / "Forester" / "AZ1G101M-2009-10-EDM-Subaru-Forester-XT-MT.hex"

# Основной файл: база с таблицей без адреса и прошивка TEST0001 с одной картой
MAIN = """<roms>
 <rom>
  <romid><xmlid>TESTBASE</xmlid></romid>
  <table type="2D" name="Base Only" category="Base" storagetype="uint8" sizey="2">
   <scaling units="u" expression="x" to_byte="x" />
   <table type="Static Y Axis" name="Idx" sizey="2"><data>a</data><data>b</data></table>
  </table>
 </rom>
 <rom base="TESTBASE">
  <romid>
   <xmlid>TEST0001</xmlid><internalidaddress>10</internalidaddress><internalidstring>TEST0001</internalidstring>
  </romid>
  <table type="2D" name="Main Map" category="Main" storagetype="uint8" sizey="2" storageaddress="0x40">
   <scaling units="u" expression="x" to_byte="x" />
   <table type="Static Y Axis" name="Idx" sizey="2"><data>a</data><data>b</data></table>
  </table>
 </rom>
</roms>
"""

# EcuFlash: база со scaling и таблицами без адресов, child — только адреса (оси «X»/«Y»)
EF_BASE = """<rom>
 <romid><xmlid>EFBASE</xmlid></romid>
 <scaling name="RPM" units="RPM" toexpr="x" frexpr="x" format="%.0f" inc="50" storagetype="float" endian="big"/>
 <scaling name="Half" units="u" toexpr="x/2" frexpr="x*2" format="%.1f" storagetype="uint16" endian="big"/>
 <scaling name="Byte" units="b" toexpr="x" frexpr="x" format="%.0f" storagetype="uint8" endian="big"/>
 <scaling name="Toggle" storagetype="bloblist">
  <data name="disabled" value="00"/>
  <data name="enabled" value="01"/>
 </scaling>
 <table name="EF Vector" category="EF" type="2D" level="2" scaling="Half">
  <description>vector</description>
  <table name="Engine Speed" type="X Axis" elements="2" scaling="RPM"/>
 </table>
 <table name="EF Map" category="EF" type="3D" scaling="Byte">
  <table name="Engine Speed" type="X Axis" elements="2" scaling="RPM"/>
  <table name="Load" type="Y Axis" elements="2" scaling="Half"/>
 </table>
 <table name="EF Constant" category="EF" type="1D" scaling="Half"/>
 <table name="EF Switch" category="EF" type="1D" scaling="Toggle"/>
 <table name="EF Same Address" category="EF" type="1D" scaling="Byte"/>
 <table name="Main Map" category="EF" type="1D" scaling="Byte"/>
</rom>
"""


def _child(xmlid: str, idstring: str) -> str:
    return f"""<rom>
 <romid><xmlid>{xmlid}</xmlid><internalidaddress>10</internalidaddress><internalidstring>{idstring}</internalidstring></romid>
 <include>EFBASE</include>
 <table name="EF Vector" address="100"><table name="X" address="110"/></table>
 <table name="EF Map" address="120"><table name="X" address="110"/><table name="Y" address="130"/></table>
 <table name="EF Constant" address="140"/>
 <table name="EF Switch" address="150"/>
 <table name="EF Same Address" address="40"/>
 <table name="Main Map" address="160"/>
</rom>
"""


# Своё дополнение RomRaider к TEST0001: новая карта, адрес для таблицы базы, повтор имени
OVERLAY = """<roms>
 <rom>
  <romid><xmlid>TEST0001</xmlid></romid>
  <table type="2D" name="Custom Map" category="Custom" storagetype="uint8" sizey="1" storageaddress="0x170">
   <scaling units="u" expression="x*10" to_byte="x/10" />
   <table type="Static Y Axis" name="Idx" sizey="1"><data>only</data></table>
  </table>
  <table name="Base Only" storageaddress="0x180" />
  <table type="2D" name="Main Map" category="Custom" storagetype="uint8" sizey="1" storageaddress="0x190" />
 </rom>
</roms>
"""


def _rom(idstring: str) -> bytes:
    data = bytearray(0x200)
    data[0x10 : 0x10 + len(idstring)] = idstring.encode()  # internalidaddress «10» — hex
    data[0x40:0x42] = bytes([7, 8])
    data[0x100:0x104] = struct.pack(">HH", 10, 20)
    data[0x110:0x118] = struct.pack(">ff", 1000.0, 2000.0)
    data[0x120:0x124] = bytes([1, 2, 3, 4])
    data[0x130:0x134] = struct.pack(">HH", 4, 8)
    data[0x140:0x142] = struct.pack(">H", 9)
    data[0x150] = 1
    data[0x170] = 5
    data[0x180:0x182] = bytes([11, 12])
    return bytes(data)


@pytest.fixture
def defs(tmp_path: Path) -> tuple[Path, Path]:
    main = tmp_path / "ecu_defs.xml"
    main.write_text(MAIN)
    extra = tmp_path / "extra"
    (extra / "SubaruDefs" / "Bases").mkdir(parents=True)
    (extra / "SubaruDefs" / "Bases" / "EFBASE.xml").write_text(EF_BASE)
    (extra / "SubaruDefs" / "TEST0001.xml").write_text(_child("TEST0001", "TEST0001"))
    (extra / "SubaruDefs" / "EFONLY01.xml").write_text(_child("EFONLY01", "EFONLY01"))
    # Метрическая копия того же описания в SubaruDefs — пропускается
    (extra / "subaru metric").mkdir()
    (extra / "subaru metric" / "TEST0001.xml").write_text(_child("TEST0001", "TEST0001").replace('"100"', '"1F0"'))
    (extra / "custom.xml").write_text(OVERLAY)
    return main, extra


def _open(defs: tuple[Path, Path], idstring: str) -> EcuParser:
    main, extra = defs
    repo = EcuDefsRepository.load(main, [extra])
    definition = repo.identify(_rom(idstring))
    assert definition is not None
    return EcuParser(rom_bytes=_rom(idstring), definition=definition, defs_repo=repo)


def test_definition_files_skip_metric(defs: tuple[Path, Path]) -> None:
    _main, extra = defs
    labels = [label for _path, label in definition_files([extra])]
    assert "extra/custom.xml" in labels
    assert "extra/SubaruDefs/TEST0001.xml" in labels
    assert not any("metric" in label for label in labels)


def test_ecuflash_tables_read_like_romraider(defs: tuple[Path, Path]) -> None:
    parser = _open(defs, "TEST0001")
    vector = parser.load_table("EF Vector")
    # Ось X вектора EcuFlash редактор держит по Y, как у RomRaider; float — big-endian в ROM
    assert vector.values == [[5.0], [10.0]]
    assert vector.y_axis == [1000.0, 2000.0]
    assert vector.source == "extra/SubaruDefs/TEST0001.xml"
    assert vector.userlevel == 2 and vector.description == "vector"
    grid = parser.load_table("EF Map")
    assert grid.values == [[1.0, 2.0], [3.0, 4.0]]
    assert grid.x_axis == [1000.0, 2000.0] and grid.y_axis == [2.0, 4.0]
    assert parser.load_table("EF Constant").values == [[4.5]]
    assert parser.switch_info("EF Switch").state == "on"


def test_extras_only_add(defs: tuple[Path, Path]) -> None:
    parser = _open(defs, "TEST0001")
    tables = parser.definition.tables if parser.definition else {}
    # Имя и адрес основного описания не перекрываются ни EcuFlash, ни своим файлом
    assert tables["Main Map"].storageaddress == 0x40 and tables["Main Map"].source == ""
    assert "EF Same Address" not in tables
    assert parser.load_table("Custom Map").values == [[50.0]]
    assert tables["Custom Map"].source == "extra/custom.xml"
    # Карты дополнений в дереве — «не проверена» (значок по source)
    sources = {m.id: m.source for m in parser.list_maps()}
    assert sources["Custom Map"] == "extra/custom.xml" and sources["Main Map"] == ""
    # Таблица базы без адреса в этой прошивке получает его из дополнения
    base_only = parser.load_table("Base Only")
    assert base_only.values == [[11.0], [12.0]] and base_only.y_labels == ["a", "b"]


def test_ecuflash_only_rom_is_identified(defs: tuple[Path, Path]) -> None:
    parser = _open(defs, "EFONLY01")
    assert parser.xmlid == "EFONLY01"
    assert parser.load_table("EF Vector").values == [[5.0], [10.0]]
    assert parser.defs_repo is not None and parser.defs_repo.root_base("EFONLY01") == "EFBASE"
    # Контрольных сумм нет — сохранение такой прошивки запрещено (RomSession.save_blocker)
    assert parser.checksum_table() is None


def test_bundled_extra_for_az1g101m() -> None:
    if not STOCK_101M.is_file():
        pytest.skip(f"нет прошивки {STOCK_101M.name}")
    repo = EcuDefsRepository.load(ROOT / "definitions" / "ecu_defs.xml", [ROOT / "definitions" / "extra"])
    parser = EcuParser.open_rom(STOCK_101M, "", defs_repo=repo)
    table = parser.load_table("Overrun Fueling Cut Counter RPM Threshold")
    assert [row[0] for row in table.values] == [1800.0, 2800.0, 4500.0]
    assert table.source == "extra/AZ1G101M.xml"
    for info in parser.list_maps():
        parser.load_table(info.id)
