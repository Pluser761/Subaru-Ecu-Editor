"""Цикл опроса в отдельном потоке (без Qt): опрос, запись CSV, команды из интерфейса.

Команды (выбор параметров, отметка, начало/конец записи) приходят из потока интерфейса и
применяются между опросами под замком, поэтому опрос не видит полусобранный план.
CSV пишется прямо здесь — ни одна точка не теряется, даже если интерфейс не успевает
перерисовываться; интерфейсу уходит каждая точка через on_sample (он сам решает, как часто
рисовать). Ошибка связи останавливает цикл и уходит в on_error.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from src.logger.core.csv_log import CsvLogWriter
from src.logger.core.params import decimals
from src.logger.core.session import Poller, Sample
from src.logger.core.ssm import SsmError

# Пауза между опросами, если запрос вернулся мгновенно (эмулятор): не крутить процессор
MIN_PERIOD = 0.01
# Пропуски ответа ЭБУ подряд, после которых связь считается потерянной (одиночный пропуск
# бывает — например, в момент выключения зажигания или помехи)
MAX_MISSES = 3


class LoggerRunner:
    def __init__(
        self,
        poller: Poller,
        on_sample: Callable[[Sample], None],
        on_error: Callable[[Exception], None],
        min_period: float = MIN_PERIOD,
    ) -> None:
        self.poller = poller
        self._on_sample = on_sample
        self._on_error = on_error
        self._min_period = min_period
        self._lock = threading.Lock()
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
        self._thread = threading.Thread(target=self._loop, name="logger-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        self._thread = None
        self.stop_recording()

    def step(self) -> Sample:
        """Один опрос (цикл вызывает его сам; тесты — напрямую)."""
        with self._lock:
            sample = self.poller.poll()
            if self._writer is not None:
                self._writer.write(sample)
        self._on_sample(sample)
        return sample

    def _loop(self) -> None:
        last: float | None = None
        misses = 0
        while self._running.is_set():
            started = time.monotonic()
            if last is not None:
                # Период от начала прошлого опроса до начала этого — вместе с паузой
                self.rate = 0.8 * self.rate + 0.2 / (started - last) if self.rate else 1 / (started - last)
            last = started
            try:
                self.step()
                misses = 0
            except SsmError as exc:
                misses += 1
                if exc.code is None and misses < MAX_MISSES:
                    continue  # одиночный пропуск ответа — повторить опрос
                self._running.clear()
                self.stop_recording()
                self._on_error(exc)
                return
            except Exception as exc:  # noqa: BLE001 — связь оборвалась: остановиться и сообщить
                self._running.clear()
                self.stop_recording()
                self._on_error(exc)
                return
            spent = time.monotonic() - started
            if spent < self._min_period:
                time.sleep(self._min_period - spent)
