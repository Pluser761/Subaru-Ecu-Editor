"""Переключатели (Switch): ошибки DTC — чтение состояния, запись, сохранение."""

from src.core import checksum
from src.core.parser import EcuParser
from src.core.session import RomSession

P0031 = "(P0031) FRONT O2 SENSOR LOW INPUT"


def test_list_dtc_switches(jdm: EcuParser):
    switches = jdm.list_switches()
    dtc = [s for s in switches if s.category == "Diagnostic Trouble Codes"]
    assert len(dtc) == 108 and all(s.state == "on" for s in dtc)
    p0031 = next(s for s in dtc if s.code == "P0031")
    assert p0031.name == "FRONT O2 SENSOR LOW INPUT" and p0031.description
    assert all(s.id != "Checksum Fix" for s in switches)


def test_disable_dtc_and_save(tmp_path, repo, jdm: EcuParser):
    session = RomSession(jdm)
    session.write_switch(P0031, "off")
    assert session.dirty and jdm.switch_info(P0031).state == "off"
    out = tmp_path / "dtc-off.hex"
    session.save(out)
    saved = EcuParser.open_rom(out, repo.path, defs_repo=repo)
    assert saved.switch_info(P0031).state == "off"
    assert checksum.verify(saved.rom_bytes, *saved.checksum_table())
    session.write_switch(P0031, "on")
    assert jdm.switch_info(P0031).state == "on"


def test_unknown_bytes_reported(jdm: EcuParser):
    table = jdm.get_table_def(P0031)
    jdm.rom_bytes[table.storageaddress] ^= 0xFF
    info = jdm.switch_info(P0031)
    assert info.state is None and info.raw
