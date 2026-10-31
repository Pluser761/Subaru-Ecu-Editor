"""Цикл опроса: одиночные пропуски ответа ЭБУ не рвут сессию."""

from __future__ import annotations

import threading
from typing import Any

from src.logger.core.runner import MAX_MISSES, LoggerRunner
from src.logger.core.session import Sample
from src.logger.core.ssm import SsmError


class FlakyPoller:
    """Poller, который пропускает ответы по заданному сценарию."""

    def __init__(self, script: list[bool]) -> None:
        self.script = script  # True — ответ есть, False — пропуск
        self.calls = 0
        self.done = threading.Event()

    def poll(self) -> Sample:
        ok = self.script[self.calls] if self.calls < len(self.script) else True
        self.calls += 1
        if self.calls >= len(self.script):
            self.done.set()
        if not ok:
            raise SsmError("SSM/CAN: no response to a8")
        return Sample(0.0, {})


def _run(script: list[bool]) -> tuple[list[Sample], list[Exception]]:
    poller: Any = FlakyPoller(script)
    samples: list[Sample] = []
    errors: list[Exception] = []
    runner = LoggerRunner(poller, samples.append, errors.append, min_period=0)
    runner.start()
    poller.done.wait(2)
    runner.stop()
    return samples, errors


def test_single_misses_are_tolerated() -> None:
    samples, errors = _run([True, False, True, False, False, True, True])
    assert not errors and len(samples) >= 4


def test_consecutive_misses_stop() -> None:
    _samples, errors = _run([True] + [False] * MAX_MISSES)
    assert len(errors) == 1


def test_call_runs_in_poll_thread() -> None:
    """Чтение/сброс DTC из GUI идёт в потоке опроса, а не в потоке интерфейса."""
    poller: Any = FlakyPoller([True] * 50)
    runner = LoggerRunner(poller, lambda s: None, lambda e: None, min_period=0)
    runner.start()
    threads: list[str] = []
    done = threading.Event()
    try:
        runner.call(lambda: threading.current_thread().name, lambda name: (threads.append(name), done.set()))
        assert done.wait(2)
    finally:
        runner.stop()
    assert threads == ["logger-poll"]


def test_call_without_loop_runs_immediately() -> None:
    poller: Any = FlakyPoller([True])
    runner = LoggerRunner(poller, lambda s: None, lambda e: None, min_period=0)
    results: list[int] = []
    runner.call(lambda: 42, results.append)
    assert results == [42]
