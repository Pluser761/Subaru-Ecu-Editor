"""Окно логгера с презентером на эмуляторе (Qt offscreen)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.logger.core.profile import ProfileItem, list_profiles, load_profile, save_profile
from src.logger.core.session import AflTable, DtcReading, FlkcTable
from src.logger.core.transport.op2 import NO_REPLY, OpenPortError
from src.logger.presenter import EMULATOR_PORT, LOGGER_PROFILES, LoggerPresenter, RomInfo
from src.logger.ui.window import DtcRow, LoggerWindow


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

    @property
    def logger_profiles_dir(self) -> Path:
        return self.root / "profiles"

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


def _wait_until(presenter: LoggerPresenter, condition, timeout: float = 3.0) -> None:  # type: ignore[no-untyped-def]
    """Прокрутить цикл событий, пока идущее в фоне подключение не завершится."""
    from PyQt6.QtWidgets import QApplication

    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timeout waiting for connect"
        QApplication.instance().processEvents()
        time.sleep(0.005)


def _connect_without_thread(presenter: LoggerPresenter) -> None:
    presenter.connect(EMULATOR_PORT)
    _wait_until(presenter, lambda: presenter.runner is not None)
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
    logger.apply_profile([ProfileItem("P8", None), ProfileItem("E9002", None)])
    runner = logger.runner
    assert runner is not None and [pid for pid, _u in runner.poller.selection] == ["P8", "E9002"]
    runner.step()
    logger._refresh()
    # Всё выбранное — и плиткой на дашборде, и графиком
    assert set(window._graphs) == {"P8", "E9002"}
    rpm = window.dashboard.tile("P8")
    assert rpm is not None and rpm._value.text() not in ("", "—") and rpm._unit.text() == "rpm"
    window._record_btn.setChecked(True)  # → record_toggled
    path = runner.recording
    assert path is not None and path.parent == tmp_path / "ecu-editor-logs" / "gd-maf-v4"
    assert path.stem.replace("_", "").isdigit()  # 20261021_170500.csv
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
    assert logger.window.dashboard.tile("S23") is not None
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
    _wait_until(presenter, lambda: "port busy" in presenter.window._status.text())
    assert presenter.runner is None and "port busy" in presenter.window._status.text()


def test_connect_without_adapter_shows_hint(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    presenter = LoggerPresenter(LoggerWindow(), FakeSettings(tmp_path), find_ports=lambda: [])
    presenter.connect("/dev/x")  # порта нет — адаптер не найден в USB
    _wait_until(presenter, lambda: "не найден" in presenter.window._status.text())
    assert "не найден" in presenter.window._status.text()


def test_connect_failure_is_logged(
    qapp, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:  # type: ignore[no-untyped-def]
    presenter = LoggerPresenter(LoggerWindow(), FakeSettings(tmp_path), find_ports=lambda: [])
    with caplog.at_level("INFO"):
        presenter.connect("/dev/x")
        _wait_until(presenter, lambda: "не найден" in presenter.window._status.text())
    assert "connect failed" in caplog.text


def test_bundled_profiles_are_valid(logger: LoggerPresenter) -> None:
    entries = list_profiles([(LOGGER_PROFILES, "extra")])
    assert "Переключения и хлопки" in [e.name for e in entries]
    for entry in entries:
        items = load_profile(entry.path)
        assert items and all(item.id in logger.defs.params for item in items), entry.name


def test_quick_pick_filters_and_applies(logger: LoggerPresenter, tmp_path: Path) -> None:
    own = tmp_path / "profiles"
    own.mkdir()
    save_profile(own / "Мой круиз.xml", [ProfileItem("P8", "rpm"), ProfileItem("P12", "g/s")])
    logger.pick_profile()
    picker = logger.window._picker
    assert "Мой круиз" in picker.visible_names() and "Переключения и хлопки" in picker.visible_names()
    picker._search.setText("мой")
    assert picker.visible_names() == ["Мой круиз"]
    picker.choose_current()  # как Enter
    assert not picker.isVisible()
    assert logger.window.selected() == [("P8", "rpm"), ("P12", "g/s")]


def test_link_status_shows_outage_and_drops(logger: LoggerPresenter) -> None:
    _connect_without_thread(logger)
    runner = logger.runner
    assert runner is not None
    window = logger.window
    logger.apply_profile([ProfileItem("P8", None)])
    runner.step()
    runner.link.drops, runner.link.attempts, runner.link.lost_since = 1, 3, time.monotonic() - 12
    logger._refresh()
    assert "попытка 3" in window._status.text() and "12 с" in window._status.text()
    assert window._connect_btn.text() == "Отключить"  # «Отключить» доступен и во время обрыва
    runner.link.lost_since, runner.link.downtime = None, 14.2
    logger._refresh()
    assert "ЭБУ 5C42584007" in window._status.text() and "обрывов: 1, без связи 14 с" in window._status.text()


def test_silent_adapter_suggests_usb_replug(logger: LoggerPresenter) -> None:
    _connect_without_thread(logger)
    runner = logger.runner
    assert runner is not None
    window = logger.window
    logger.apply_profile([ProfileItem("P8", None)])
    runner.step()
    runner.link.lost_since = time.monotonic()
    runner.link.last_error = OpenPortError("OpenPort: no reply to ati", NO_REPLY)

    runner.link.attempts = 5
    logger._refresh()
    assert "не отвечает" in window._status.text() and "выдерните" not in window._status.text()

    runner.link.attempts = 6  # шесть попыток подряд молчит — сброс не помогает, нужен USB
    logger._refresh()
    assert "выдерните" in window._status.text()


def test_dtc_read_then_clear(logger: LoggerPresenter, monkeypatch: pytest.MonkeyPatch) -> None:
    _connect_without_thread(logger)
    window, runner = logger.window, logger.runner
    assert runner is not None
    assert not window._dtc_clear_btn.isEnabled()  # без чтения сброс недоступен

    readings = [
        DtcReading(logger.defs.dtcodes["D1"], True, False),
        DtcReading(logger.defs.dtcodes["D3"], False, True),
    ]
    monkeypatch.setattr(runner.poller, "read_dtc", lambda: readings)
    window._dtc_read_btn.click()  # цикл остановлен — команда выполняется сразу
    assert logger._dtc_read_done
    first = window._dtc_tree.topLevelItem(0)
    assert first is not None
    assert (first.text(0), first.text(2), first.text(3)) == ("D1", "✓", "")
    second = window._dtc_tree.topLevelItem(1)
    assert second is not None
    assert (second.text(0), second.text(2), second.text(3)) == ("D3", "", "✓")
    assert window._dtc_clear_btn.isEnabled()

    asked: list[bool] = []
    cleared: list[int] = []
    monkeypatch.setattr(runner.poller, "clear_dtc", lambda: cleared.append(1) or [])
    monkeypatch.setattr(window, "ask_clear_dtc", lambda: asked.append(True) or False)
    window._dtc_clear_btn.click()
    assert asked == [True] and not cleared  # подтверждение не дано — сброса нет
    assert window._dtc_tree.topLevelItemCount() == 2

    monkeypatch.setattr(window, "ask_clear_dtc", lambda: True)
    window._dtc_clear_btn.click()
    assert cleared == [1]
    assert window._dtc_tree.topLevelItemCount() == 0
    assert "сброшены" in window._dtc_status.text()


def test_dtc_clear_blocked_without_read_and_while_recording(
    logger: LoggerPresenter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _connect_without_thread(logger)
    window, runner = logger.window, logger.runner
    assert runner is not None
    cleared: list[int] = []
    monkeypatch.setattr(runner.poller, "clear_dtc", lambda: cleared.append(1) or [])

    logger.clear_dtc()
    assert not cleared and "Сначала прочитайте коды" in window._dtc_status.text()

    window.set_dtc_rows([DtcRow("D1", "P0335 TEST", True, False)])
    logger._dtc_read_done = True
    assert window._dtc_clear_btn.isEnabled()

    runner._log_path = tmp_path / "log.csv"  # как будто идёт запись
    window.set_recording(str(runner._log_path))
    assert not window._dtc_clear_btn.isEnabled()
    logger.clear_dtc()
    assert not cleared and "остановите запись" in window._dtc_status.text()

    runner._log_path = None
    window.set_recording(None)
    assert window._dtc_clear_btn.isEnabled()


def test_afl_dump_saves_and_status_restores(
    logger: LoggerPresenter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """После сохранения строка состояния показывает путь, но не висит — вернётся через таймер."""
    _connect_without_thread(logger)
    window, runner = logger.window, logger.runner
    assert runner is not None
    afl = AflTable(stored={"A": -13.0, "B": -14.8, "C": -12.1, "D": -8.5}, current=-13.0, range_index=2)
    flkc = FlkcTable(cells=tuple((0.0,) * 5 for _ in range(7)))
    monkeypatch.setattr(runner.poller, "read_afl", lambda: afl)
    monkeypatch.setattr(runner.poller, "read_flkc", lambda: flkc)
    out = tmp_path / "learning.csv"
    monkeypatch.setattr(window, "show_afl_dump", lambda _text: True)
    monkeypatch.setattr(window, "ask_afl_save_path", lambda _suggested: str(out))

    window._afl_btn.click()  # цикл остановлен — команда выполняется сразу
    assert out.exists() and "A/F Learning #1" in out.read_text()
    assert "сохранены" in window._status.text()
    assert logger._status_timer.isActive()  # сообщение временное, а не висит

    logger._restore_status()  # как по таймеру
    assert window._status.text() == logger._status

    # Отмена сохранения — строку не оставляем на «Чтение…»
    monkeypatch.setattr(window, "show_afl_dump", lambda _text: False)
    window._afl_btn.click()
    assert window._status.text() == logger._status
