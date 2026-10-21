"""Окно логгера с презентером на эмуляторе (Qt offscreen)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.logger.core.profile import ProfileItem, load_profile, save_profile
from src.logger.presenter import EMULATOR_PORT, LoggerPresenter, RomInfo
from src.logger.ui.window import LoggerWindow


class FakeSettings:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.log_dir: str | None = None
        self.port: str | None = None

    @property
    def logger_extra_dir(self) -> Path:
        return self.root / "extra"

    @property
    def logger_trace_dir(self) -> Path:
        return self.root / "traces"

    def logger_log_dir(self) -> str | None:
        return self.log_dir

    def set_logger_log_dir(self, folder: str) -> None:
        self.log_dir = folder

    def logger_port(self) -> str | None:
        return self.port

    def set_logger_port(self, port: str) -> None:
        self.port = port


@pytest.fixture
def logger(qapp, tmp_path: Path):  # type: ignore[no-untyped-def]
    rom = tmp_path / "gd-maf-v4.bin"
    settings = FakeSettings(tmp_path)
    presenter = LoggerPresenter(
        LoggerWindow(), settings, lambda: RomInfo(str(rom), "5C42584007"), find_ports=lambda: []
    )
    yield presenter
    presenter.disconnect()
    presenter.window.close()


def _connect_without_thread(presenter: LoggerPresenter) -> None:
    presenter.connect(EMULATOR_PORT)
    assert presenter.runner is not None
    presenter.runner.stop()  # опрос в тесте — вручную, step()


def test_connect_shows_only_supported(logger: LoggerPresenter) -> None:
    window = logger.window
    all_ids = {item.data(0, 256) for item in window._param_items()}
    assert "E9002" in all_ids and "P8" in all_ids
    _connect_without_thread(logger)
    assert logger.connection is not None and logger.connection.transport == "can"
    supported = {item.data(0, 256) for item in window._param_items()}
    assert "P8" in supported and "E9002" in supported
    assert supported < all_ids  # параметры других ЭБУ (не 5C42584007) скрыты
    assert "ЭБУ 5C42584007" in window._status.text() and "⚠" not in window._status.text()
    assert logger._settings is not None and logger._settings.logger_port() == EMULATOR_PORT  # запомнен


def test_live_values_record_and_mark(logger: LoggerPresenter, tmp_path: Path) -> None:
    _connect_without_thread(logger)
    window = logger.window
    window.set_checked({"P8": None, "E9002": None})
    runner = logger.runner
    assert runner is not None and [pid for pid, _u in runner.poller.selection] == ["P8", "E9002"]
    runner.step()
    logger._refresh()
    assert window._table.rowCount() == 2
    rpm = window._table.item(0, 2)
    assert rpm is not None and rpm.text() not in ("", "—")
    window._record_btn.setChecked(True)  # → record_toggled
    path = runner.recording
    assert path is not None and path.parent == tmp_path / "logs" and path.name.startswith("gd-maf-v4_")
    logger.mark()
    runner.step()
    runner.step()
    window._record_btn.setChecked(False)
    assert runner.recording is None
    lines = path.read_text().splitlines()
    assert lines[0] == "Time (msec);Engine Speed (rpm);Decel Fuel Cut Stage* (raw);Mark"
    assert [line.rsplit(";", 1)[1] for line in lines[1:]] == ["1", "0"]


def test_profile_round_trip(logger: LoggerPresenter, tmp_path: Path) -> None:
    profile = tmp_path / "p.xml"
    save_profile(profile, [ProfileItem("P8", "rpm", True), ProfileItem("P2", "F"), ProfileItem("S23"),
                           ProfileItem("P999")])
    items = load_profile(profile)
    assert [(i.id, i.units, i.graph) for i in items] == [
        ("P8", "rpm", True), ("P2", "F", False), ("P999", None, False), ("S23", None, False)]  # сначала параметры
    logger.apply_profile(items)
    assert logger.window.selected() == [("P2", "F"), ("P8", "rpm"), ("S23", "on/off")]
    assert logger.window.graphed() == ["P8"]
    assert "1" in logger.window._status.text()  # P999 нет в описаниях — не подошёл


def test_other_ecu_warning(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    presenter = LoggerPresenter(
        LoggerWindow(), FakeSettings(tmp_path), lambda: RomInfo(None, "1111111111"), find_ports=lambda: []
    )
    try:
        _connect_without_thread(presenter)
        assert "⚠" in presenter.window._status.text()
    finally:
        presenter.disconnect()


def test_failed_connect_reports_error(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    def broken(_port: str):  # type: ignore[no-untyped-def]
        raise OSError("port busy")

    presenter = LoggerPresenter(LoggerWindow(), FakeSettings(tmp_path), find_ports=lambda: ["/dev/x"],
                                open_port=broken)
    presenter.connect("/dev/x")
    assert presenter.runner is None and "port busy" in presenter.window._status.text()
