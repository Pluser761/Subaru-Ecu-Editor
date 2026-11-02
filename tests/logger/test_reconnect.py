"""Обрыв связи: адаптер замолкает (плохой контакт USB) — цикл опроса переподключается,
запись продолжается в тот же CSV с отметкой 2; закрытие — со сбросом atz."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest

from src.logger.core.adapter import Adapter, AdapterClosed
from src.logger.core.defs import LoggerDefs
from src.logger.core.runner import LoggerRunner
from src.logger.core.session import MARK_RESTORED, Connection, Poller, Sample
from src.logger.core.transport import op2
from src.logger.core.transport.emulator import FakeEcu, FakeOpenPort

ROOT = Path(__file__).resolve().parents[2]
LOGGER_DEFS = ROOT / "definitions" / "logger" / "logger_defs.xml"


@pytest.fixture(scope="module")
def defs() -> LoggerDefs:
    return LoggerDefs.load(LOGGER_DEFS)


@pytest.fixture(autouse=True)
def fast_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(op2, "_REPLY_TIMEOUT", 0.05)


def _wait(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timeout"
        time.sleep(0.01)


def test_outage_reconnects_and_continues_csv(defs: LoggerDefs, tmp_path: Path) -> None:
    port = FakeOpenPort(FakeEcu(memory=lambda a: 0x2E if a == 0x0E else 0))
    opened: list[int] = []

    def open_stream() -> FakeOpenPort:
        opened.append(1)
        return port

    adapter = Adapter(open_stream, reset_pause=0)
    samples: list[Sample] = []
    errors: list[Exception] = []
    runner = LoggerRunner(Poller(adapter.open().connection, defs), samples.append, errors.append,
                          min_period=0.005, reconnect=adapter.reopen, retry_pause=0.01)
    runner.select([("P8", None)])
    path = tmp_path / "log.csv"
    runner.start_recording(path)
    runner.start()
    try:
        _wait(lambda: len(samples) > 5)
        port.silent = True
        _wait(lambda: runner.link.lost_since is not None)
        time.sleep(0.2)  # переподключение не выходит, пока адаптер молчит
        assert runner.link.attempts >= 2 and not errors
        port.silent = False
        _wait(lambda: runner.link.lost_since is None)
        count = len(samples)
        _wait(lambda: len(samples) > count + 5)
    finally:
        runner.stop()
        adapter.close()
    assert not errors and runner.link.drops == 1 and runner.link.downtime > 0.2
    assert len(opened) >= 3  # каждая попытка переподключения — новое открытие порта
    assert port.resets >= 1  # закрытие сбрасывает адаптер (atz)
    rows = [line.split(";") for line in path.read_text().splitlines()[1:]]
    marks = [int(row[-1]) for row in rows]
    assert marks.count(MARK_RESTORED) == 1 and marks[-1] == 0
    times = [int(row[0]) for row in rows]
    restored = marks.index(MARK_RESTORED)
    assert times[restored] - times[restored - 1] >= 200  # дырка в колонке времени, отсчёт тот же
    assert all(row[1] == str(0x2E00 // 4) for row in rows)


def test_stop_during_reconnect_is_quick_and_silent(defs: LoggerDefs) -> None:
    port = FakeOpenPort()
    adapter = Adapter(lambda: port, reset_pause=0)
    errors: list[Exception] = []
    runner = LoggerRunner(Poller(adapter.open().connection, defs), lambda s: None, errors.append,
                          min_period=0.005, reconnect=adapter.reopen, retry_pause=10)
    runner.select([("P8", None)])
    runner.start()
    port.silent = True
    _wait(lambda: runner.link.attempts >= 1)
    started = time.monotonic()
    adapter.interrupt()
    runner.stop()
    assert time.monotonic() - started < 1 and not errors


def test_without_reconnect_error_stops(defs: LoggerDefs) -> None:
    port = FakeOpenPort()
    errors: list[Exception] = []
    adapter = Adapter(lambda: port, reset_pause=0)
    runner = LoggerRunner(Poller(adapter.open().connection, defs), lambda s: None, errors.append, min_period=0)
    runner.select([("P8", None)])
    runner.start()
    port.silent = True
    _wait(lambda: bool(errors))
    runner.stop()


def test_reopen_resets_adapter() -> None:
    """Переподключение сбрасывает адаптер (atz) — как и закрытие, но без close_all."""
    port = FakeOpenPort()
    adapter = Adapter(lambda: port, reset_pause=0)
    adapter.open()
    assert port.resets == 0
    adapter.reopen("test")
    assert port.resets == 1  # reopen: сброс до нового открытия порта
    adapter.close()
    assert port.resets == 2  # close: сброс ещё раз


def test_close_resets_adapter_and_next_open_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    port = FakeOpenPort()
    adapter = Adapter(lambda: port, reset_pause=0.3)
    connection: Connection = adapter.open().connection
    assert connection.transport == "can"
    adapter.close()
    assert port.resets == 1 and not port.channels  # ata + atz
    started = time.monotonic()
    Adapter(lambda: port, reset_pause=0.3).open()  # адаптер перезагружается — подождать
    assert time.monotonic() - started >= 0.25
    waiting = Adapter(lambda: port, reset_pause=5)
    waiting.interrupt()
    with pytest.raises(AdapterClosed):
        waiting.open()
