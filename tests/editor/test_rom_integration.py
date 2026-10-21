"""На реальных прошивках из ecu/ (пропускаются, если файлов нет)."""

from collections import Counter

import pytest

from src.core.parser import EcuParser
from src.core.transfer import ResultStatus, apply_transfer, check_compatibility, plan_transfer

ECT_AXIS = 0xC82A0


def test_identify_and_list_maps(jdm: EcuParser):
    assert jdm.xmlid == "AZ1G301F"
    maps = jdm.list_maps()
    assert len(maps) == 185
    assert Counter(m.userlevel for m in maps) == {1: 23, 2: 31, 3: 29, 4: 76, 5: 26}


def test_load_3d_table(jdm: EcuParser):
    table = jdm.load_table("Target Boost ")
    assert (table.rows, table.cols) == (18, 13)
    assert table.x_title == "Requested Torque, raw ecu value"
    assert table.y_axis[0] == 800 and table.y_axis[-1] == 7600


def test_write_cell_returns_quantized_value(jdm: EcuParser):
    stored = jdm.write_cell("Base Timing Primary Non-Cruise", 0, 0, 12.3)
    assert stored == pytest.approx(12.34375)
    assert jdm.read_cell("Base Timing Primary Non-Cruise", 0, 0) == pytest.approx(stored)


def test_shared_axis_users_know_axis_kind(jdm: EcuParser):
    assert len(jdm.axis_users()[ECT_AXIS]) == 19
    users = dict(jdm.shared_axis_users("Idle Speed Target A", "y"))
    assert users == {"Idle Speed Target B": "y", "Idle Speed Target C": "y"}
    # Та же ось ECT — Y у 2D-таблиц и X у 3D
    kinds = dict(jdm.shared_axis_users("Primary Open Loop Fueling Compensation (ECT)", "y"))
    assert kinds["CL Fueling Target Compensation Imm. Cruise (ECT)"] == "x"


def test_axis_write_changes_all_tables_on_shared_axis(jdm: EcuParser):
    jdm.write_axis("Idle Speed Target A", "y", 4, 40.0)
    assert jdm.load_table("Idle Speed Target C").y_axis[4] == pytest.approx(40.0)


def test_self_transfer_copies_without_changes(repo, jdm: EcuParser):
    donor = EcuParser.open_rom(jdm.file_path, repo.path, defs_repo=repo)
    before = jdm.load_table("Target Boost ").values
    report = apply_transfer(donor, jdm, ["Target Boost "], plan_transfer(donor, jdm))
    assert [r.status for r in report.results] == [ResultStatus.COPIED]
    assert jdm.load_table("Target Boost ").values == before


def test_incompatible_platforms_refused(jdm: EcuParser, edm: EcuParser):
    reason = check_compatibility(edm, jdm)
    assert reason is not None and "16BITBASE" in reason
