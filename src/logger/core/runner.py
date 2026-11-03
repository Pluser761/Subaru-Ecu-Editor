"""Цикл опроса в отдельном потоке (без Qt): опрос, запись CSV, команды из интерфейса.

Команды (выбор параметров, отметка, начало/конец записи) приходят из потока интерфейса и
применяются между опросами под замком, поэтому опрос не видит полусобранный план.
CSV пишется прямо здесь — ни одна точка не теряется, даже если интерфейс не успевает
перерисовываться; интерфейсу уходит каждая точка через on_sample (он сам решает, как часто
рисовать).

Обрыв связи: одиночная ошибка (нет ответа ЭБУ или адаптера) — повтор опроса; MAX_MISSES
подряд — связь потеряна. С reconnect цикл не останавливается, а переподключается (пауза
RETRY_PAUSE между попытками, пока не остановят), запись продолжается в тот же CSV: на время
обрыва строк нет, у первой строки после него отметка 2. Без reconnect — остановка и on_error.

Разовые команды интерфейса (читать/сбросить DTC) выполняются в этом же потоке между опросами
под замком (`call`) — GUI и цикл опроса не трогают канал ЭБУ одновременно.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.logger.core.csv_log import CsvLogWriter
from src.logger.core.params import decimals
from src.logger.core.session import Connection, Poller, Sample

log = logging.getLogger(__name__)

# Пауза между опросами, если запрос вернулся мгновенно (эмулятор): не крутить процессор
MIN_PERIOD = 0.01
# Ошибки опроса подряд, после которых связь считается потерянной (одиночный пропуск
# бывает — например, в момент выключения зажигания или помехи)
MAX_MISSES = 3
# Пауза между неудачными попытками переподключения, с (как у RomRaider)
RETRY_PAUSE = 0.5
# Как часто сбрасывать CSV на диск, с: при вылете теряется не больше этого
FLUSH_PERIOD = 1.0


@dataclass
class LinkStats:
    """Обрывы связи за сессию (пишет поток опроса, читает интерфейс)."""

    drops: int = 0
    downtime: float = 0.0  # с без связи, по закончившимся обрывам
    lost_since: float | None = None  # time.monotonic() начала текущего обрыва
    attempts: int = 0  # попыток переподключения в текущем обрыве
    last_error: Exception | None = None  # причина обрыва или последней неудачной попытки


@dataclass
class _Call:
    """Команда интерфейса, которую надо выполнить в потоке опроса."""

    fn: Callable[[], Any]
    done: Callable[[Any], None]
    failed: Callable[[Exception], None]


class LoggerRunner:
    def __init__(
        self,
        poller: Poller,
        on_sample: Callable[[Sample], None],
        on_error: Callable[[Exception], None],
        min_period: float = MIN_PERIOD,
        reconnect: Callable[[str], Connection] | None = None,
        retry_pause: float = RETRY_PAUSE,
        note: Callable[[str], None] | None = None,
    ) -> None:
        """reconnect(причина) — новое подключение после обрыва (Adapter.reopen);
        note(событие) — куда писать события обрыва/переподключения (Adapter.note)."""
        self.poller = poller
        self._on_sample = on_sample
        self._on_error = on_error
        self._min_period = min_period
        self._reconnect = reconnect
        self._retry_pause = retry_pause
        self._note = note or (lambda _text: None)
        self.link = LinkStats()
        self._flushed = 0.0
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._calls_lock = threading.Lock()
        self._calls: deque[_Call] = deque()
        self._writer: CsvLogWriter | None = None
        self._log_path: Path | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self.rate = 0.0  # опросов в секунду (скользящая оценка)

    # --- Команды из интерфейса ---

    def select(self, selection: Sequence[tuple[str, str | None]]) -> None:
        with self._lock:
            if self._writer is not None:
                raise RuntimeError("selection cannot change while recording")
            self.poller.select(selection)

    def mark(self) -> None:
        self.poller.mark()

    def call(
        self,
        fn: Callable[[], Any],
        done: Callable[[Any], None],
        failed: Callable[[Exception], None] | None = None,
    ) -> None:
        """Выполнить fn в потоке опроса между опросами, под тем же замком, что poll().

        done(результат) / failed(исключение) вызываются в потоке опроса — интерфейс
        переправляет их себе Qt-сигналом. Если цикл уже не идёт, fn выполняется сразу.
        """
        item = _Call(fn, done, failed or (lambda _exc: None))
        with self._calls_lock:
            if self._running.is_set():
                self._calls.append(item)
                return
        self._execute(item)

    @property
    def recording(self) -> Path | None:
        return self._log_path

    def start_recording(self, path: str | Path) -> None:
        """Писать CSV: колонки — выбранные параметры в выбранных единицах."""
        with self._lock:
            columns = []
            for pid, units in self.poller.selection:
                param = self.poller.defs.get(pid)
                conversion = param.conversion(units)
                columns.append((pid, param.name, conversion.units, decimals(conversion)))
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._writer = CsvLogWriter.open(path, columns)
            self._log_path = Path(path)

    def stop_recording(self) -> Path | None:
        with self._lock:
            path, writer = self._log_path, self._writer
            self._writer = None
            self._log_path = None
        if writer is not None:
            writer.close()
        return path

    # --- Цикл ---

    def start(self) -> None:
        if self._thread is not None:
            return
        self._running.set()
        self._stopped.clear()
        self._thread = threading.Thread(target=self._loop, name="logger-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        self._stopped.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._thread = None
        with self._calls_lock:
            pending, self._calls = self._calls, deque()
        for item in pending:
            try:
                item.failed(RuntimeError("polling stopped"))
            except Exception:  # noqa: BLE001 — ошибка интерфейса не мешает остановке
                log.exception("logger runner: command callback failed")
        self.stop_recording()

    def step(self) -> Sample:
        """Один опрос (цикл вызывает его сам; тесты — напрямую)."""
        with self._lock:
            sample = self.poller.poll()
            if self._writer is not None:
                self._writer.write(sample)
                if time.monotonic() - self._flushed >= FLUSH_PERIOD:
                    self._writer.flush()
                    self._flushed = time.monotonic()
        self._on_sample(sample)
        return sample

    def _run_calls(self) -> None:
        while True:
            with self._calls_lock:
                if not self._calls:
                    return
                item = self._calls.popleft()
            self._execute(item)

    def _execute(self, item: _Call) -> None:
        try:
            with self._lock:
                result = item.fn()
        except Exception as exc:  # noqa: BLE001 — ошибка команды уходит в интерфейс
            try:
                item.failed(exc)
            except Exception:  # noqa: BLE001 — ошибка интерфейса не должна рвать опрос
                log.exception("logger runner: command callback failed")
        else:
            try:
                item.done(result)
            except Exception:  # noqa: BLE001
                log.exception("logger runner: command callback failed")

    def _loop(self) -> None:
        last: float | None = None
        misses = 0
        first_miss = 0.0
        while self._running.is_set():
            started = time.monotonic()
            if last is not None:
                # Период от начала прошлого опроса до начала этого — вместе с паузой
                self.rate = 0.8 * self.rate + 0.2 / (started - last) if self.rate else 1 / (started - last)
            last = started
            try:
                self.step()
                misses = 0
                self._run_calls()
            except Exception as exc:  # noqa: BLE001 — нет ответа, адаптер молчит, порт пропал
                if not self._running.is_set():
                    return  # остановили посреди опроса (закрыли порт) — это не обрыв
                if not misses:
                    first_miss = started
                misses += 1
                if misses < MAX_MISSES:
                    continue  # одиночный пропуск — повторить опрос
                if self._reconnect is None or not self._restore(exc, first_miss):
                    if self._running.is_set():
                        self._running.clear()
                        self.stop_recording()
                        self._on_error(exc)
                    return
                misses = 0
                last = None
                continue
            spent = time.monotonic() - started
            if spent < self._min_period:
                time.sleep(self._min_period - spent)

    def _restore(self, error: Exception, since: float) -> bool:
        """Переподключаться, пока не выйдет или не остановят. False — остановили."""
        assert self._reconnect is not None
        link = self.link
        link.drops += 1
        link.lost_since = since
        link.attempts = 0
        link.last_error = error
        self._note(f"link lost: {error}")
        reason = str(error)
        while self._running.is_set():
            link.attempts += 1
            try:
                connection = self._reconnect(reason)
            except Exception as exc:  # noqa: BLE001 — адаптер ещё не вернулся
                link.last_error = exc
                self._note(f"reconnect attempt {link.attempts} failed: {exc}")
                if self._stopped.wait(self._retry_pause):
                    return False
                continue
            if not self._running.is_set():
                return False
            with self._lock:
                self.poller.reconnect(connection)
            downtime = time.monotonic() - since
            link.downtime += downtime
            link.lost_since = None
            self._note(f"link restored after {link.attempts} attempt(s) in {downtime:.1f} s")
            return True
        return False
