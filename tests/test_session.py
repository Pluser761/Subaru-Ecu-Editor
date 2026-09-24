"""Сценарии сессии ROM на реальной прошивке (пропускаются без ecu/)."""

import pytest

from src.core.parser import EcuParser
from src.core.session import RomSession
from src.core.transfer import plan_transfer

IDLE_A, IDLE_C, BOOST = "Idle Speed Target A", "Idle Speed Target C", "Target Boost "


@pytest.fixture
def session(jdm: EcuParser) -> RomSession:
    return RomSession(jdm)


def test_cell_write_marks_changed_and_dirty(session: RomSession):
    stored = session.write_cell(BOOST, 0, 5, 1.5)
    assert session.dirty
    assert session.refresh_changed({BOOST}) == {BOOST}
    # Вернули значение — карта больше не изменена
    session.write_cell(BOOST, 0, 5, session.checkpoint(BOOST).values[0][5])
    assert session.refresh_changed({BOOST}) == set()
    assert stored == pytest.approx(1.5, abs=0.2)


def test_shared_axis_snapshots_untouched_tables_before_write(session: RomSession):
    before = session.parser.load_table(IDLE_C).y_axis[4]
    result = session.write_axis(IDLE_A, "y", 4, 40.0)
    assert {name for name, _ in result.shared} == {"Idle Speed Target B", IDLE_C}
    # C не открывали, но её checkpoint — ось до правки, поэтому она отмечена изменённой
    assert session.checkpoint(IDLE_C).y_axis[4] == before
    assert IDLE_C in session.refresh_changed({IDLE_A, IDLE_C})


def test_set_checkpoint_clears_change(session: RomSession):
    session.write_cell(BOOST, 1, 1, 5.0)
    session.refresh_changed({BOOST})
    session.set_checkpoint(BOOST)
    assert session.refresh_changed({BOOST}) == set()


def test_transfer_keeps_user_checkpoint(repo, session: RomSession):
    # Своя точка отсчёта пользователя (после правки) не затирается состоянием «до переноса»
    session.write_cell(BOOST, 0, 0, 3.0)
    session.set_checkpoint(BOOST)
    user_cp = session.checkpoint(BOOST)
    donor = EcuParser.open_rom(session.parser.file_path, repo.path, defs_repo=repo)
    session.transfer(donor, [BOOST], plan_transfer(donor, session.parser))
    assert session.checkpoint(BOOST) == user_cp
    assert BOOST in session.refresh_changed({BOOST})
