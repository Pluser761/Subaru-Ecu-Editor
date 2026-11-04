"""Связка окна логгера с ядром: адаптер, подключение, опрос, запись, отметки, профили.

Опрос идёт в своём потоке (core.runner); точки приходят в поток интерфейса Qt-сигналом,
значения в таблице перерисовываются таймером (не чаще UI_HZ), графики — вместе с ними.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices

from src.i18n import N_, tr
from src.logger.core.adapter import Adapter
from src.logger.core.csv_log import write_learning_csv
from src.logger.core.defs import LoggerDefs, LoggerParam
from src.logger.core.params import decimals
from src.logger.core.paths import default_logs_root, rom_log_dir
from src.logger.core.profile import ProfileItem, list_profiles, load_profile, save_profile
from src.logger.core.runner import LinkStats, LoggerRunner
from src.logger.core.session import AflTable, Connection, DtcReading, FlkcTable, Poller, Sample, connect
from src.logger.core.trace import Trace
from src.logger.core.transport.emulator import FakeEcu, FakeOpenPort, demo_memory
from src.logger.core.transport.op2 import NO_REPLY, ByteStream, OpenPortError
from src.logger.core.transport.serial_port import OpenPortNotFound
from src.logger.ui.window import DtcRow, LiveRow, LoggerWindow, ParamRow

ROOT = Path(__file__).resolve().parents[2]
LOGGER_DEFS = ROOT / "definitions" / "logger" / "logger_defs.xml"
LOGGER_EXTRA = LOGGER_DEFS.parent / "extra"
LOGGER_PROFILES = LOGGER_DEFS.parent / "profiles"
EMULATOR_PORT = "emulator"
UI_HZ = 10
# Сколько держать временное сообщение (выгрузка сохранена / ошибка) в строке состояния,
# прежде чем вернуть строку подключения
STATUS_TIMEOUT_MS = 5000
# Неудачных попыток переподключения, после которых подсказываем выдернуть USB
# (адаптер иногда поднимается сам на 4–8-й попытке — не дёргать USB раньше времени)
REPLUG_AFTER = 6

log = logging.getLogger(__name__)

_KIND_TITLES = {
    "parameter": N_("Стандартные"),
    "ecuparam": N_("Extended (адреса RAM)"),
    "switch": N_("Переключатели"),
    "calculated": N_("Вычисляемые"),
}


@dataclass
class RomInfo:
    """Открытая в редакторе прошивка: путь и ID ЭБУ из definitions (для сверки и имени лога)."""

    path: str | None = None
    ecu_id: str | None = None


class LoggerSettings(Protocol):
    """Что логгер хранит в настройках редактора (реализация — AppSettings)."""

    @property
    def logger_extra_dir(self) -> Path: ...

    @property
    def logger_trace_dir(self) -> Path: ...

    @property
    def logger_profiles_dir(self) -> Path: ...

    def logger_log_dir(self) -> str | None: ...

    def set_logger_log_dir(self, folder: str) -> None: ...

    def logger_port(self) -> str | None: ...

    def set_logger_port(self, port: str) -> None: ...


class _Bridge(QObject):
    """Из потока опроса в поток интерфейса."""

    sample = pyqtSignal(object)
    error = pyqtSignal(object)
    dtc_read = pyqtSignal(object)
    dtc_cleared = pyqtSignal(object)
    dtc_error = pyqtSignal(object)
    afl_dump = pyqtSignal(object)
    afl_error = pyqtSignal(object)


class LoggerPresenter:
    def __init__(
        self,
        window: LoggerWindow,
        settings: LoggerSettings | None = None,
        rom_info: Callable[[], RomInfo] | None = None,
        *,
        find_ports: Callable[[], list[str]] | None = None,
        open_port: Callable[[str], ByteStream] | None = None,
        rom_table_reader: Callable[[str], list[float]] | None = None,
    ) -> None:
        self.window = window
        self._settings = settings
        self._rom_info = rom_info or RomInfo
        self._find_ports = find_ports or _find_openport
        self._open_port = open_port or _open_serial
        self._rom_table_reader = rom_table_reader
        extra: list[tuple[str | Path, str]] = [(LOGGER_EXTRA, "extra")]
        if settings is not None:
            extra.append((settings.logger_extra_dir, "user"))
        self.defs = LoggerDefs.load(LOGGER_DEFS, extra)
        self._adapter: Adapter | None = None
        self.connection: Connection | None = None
        self._status = ""  # строка состояния подключения (без обрывов)
        self._shown_status = ""
        self._trace_path: Path | None = None  # текущий trace-файл (для показа в ошибке)
        self.runner: LoggerRunner | None = None
        self._supported: set[str] | None = None  # None — не подключены, показываем всё
        self._units: dict[str, str] = {}  # выбранные единицы (переживают перестройку дерева)
        self._latest: Sample | None = None
        self._min: dict[str, float] = {}
        self._max: dict[str, float] = {}
        self._dtc_token: object | None = None  # текущее чтение/сброс кодов
        self._dtc_read_done = False  # коды прочитаны — сброс разрешён
        self._afl_token: object | None = None  # текущая выгрузка таблицы A/F Learning
        self._bridge = _Bridge()
        self._bridge.sample.connect(self._on_sample)
        self._bridge.error.connect(self._on_error)
        self._bridge.dtc_read.connect(self._on_dtc_read)
        self._bridge.dtc_cleared.connect(self._on_dtc_cleared)
        self._bridge.dtc_error.connect(self._on_dtc_error)
        self._bridge.afl_dump.connect(self._on_afl_dump)
        self._bridge.afl_error.connect(self._on_afl_error)
        self._timer = QTimer()
        self._timer.setInterval(1000 // UI_HZ)
        self._timer.timeout.connect(self._refresh)
        self._status_timer = QTimer()
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._restore_status)

        w = window
        w.connect_requested.connect(self.connect)
        w.disconnect_requested.connect(self.disconnect)
        w.refresh_ports_requested.connect(self.refresh_ports)
        w.selection_changed.connect(self._on_selection)
        w.record_toggled.connect(self._on_record)
        w.mark_requested.connect(self.mark)
        w.pick_profile_requested.connect(self.pick_profile)
        w.profile_chosen.connect(self._apply_profile_file)
        w.open_profile_requested.connect(self._open_profile)
        w.save_profile_requested.connect(self._save_profile)
        w.choose_folder_requested.connect(self._choose_folder)
        w.open_logs_requested.connect(self._open_logs)
        w.dtc_read_requested.connect(self.read_dtc)
        w.dtc_clear_requested.connect(self.clear_dtc)
        w.afl_dump_requested.connect(self.dump_afl)
        w.closed.connect(self.disconnect)
        self.refresh_ports()
        self._fill_tree(set())
        w.set_log_folder(str(self.log_folder()))

    # --- Адаптер и подключение ---

    def refresh_ports(self) -> None:
        ports = [(p, tr("OpenPort 2.0 — {port}", port=p)) for p in self._find_ports()]
        ports.append((EMULATOR_PORT, tr("Эмулятор (без машины)")))
        current = self._settings.logger_port() if self._settings else None
        self.window.set_ports(ports, current)

    def connect(self, port: str) -> None:
        self.disconnect()
        self._trace_path = None
        if port == EMULATOR_PORT:
            # Как на машине: RAM по CAN не отдаётся, K-line отвечает с задержкой
            adapter = Adapter(
                lambda: FakeOpenPort(FakeEcu(memory=demo_memory(), can_rejects_ram=True), kline_delay=0.08),
                reset_pause=0,
            )
        else:
            adapter = Adapter(lambda: self._open_port(self._current_path(port)), self._open_trace())
        self._adapter = adapter
        try:
            opened = adapter.open()
            self.connection = opened.connection
        except Exception as exc:  # noqa: BLE001 — показать в окне; порт закрыть
            self._close_port()
            log.warning("connect failed: port %s: %s", port, exc)
            self.window.set_connected(False, tr("Не подключено"))
            if isinstance(exc, OpenPortNotFound):
                self.window.set_error(tr("OpenPort 2.0 не найден — проверьте USB-подключение"))
            elif isinstance(exc, OpenPortError) and exc.code == NO_REPLY:
                self.window.set_error(tr("Адаптер не отвечает — отключите и снова подключите его USB"))
            else:
                self.window.set_error(tr("Не удалось подключиться: {error}", error=exc))
            return
        if self._settings is not None:
            self._settings.set_logger_port(port)
        self._supported = {p.id for p in self.defs.supported(self.connection.init)}
        self.runner = LoggerRunner(
            Poller(self.connection, self.defs), self._bridge.sample.emit, self._bridge.error.emit,
            reconnect=adapter.reopen, note=adapter.note,
        )
        selected = self.window.selected()
        self._fill_tree({pid for pid, _units in selected})
        self._status = self._shown_status = self._status_text(opened.voltage)
        self.window.set_connected(True, self._status)
        self._apply_selection()
        self.runner.start()
        self._timer.start()

    def disconnect(self) -> None:
        self._timer.stop()
        self._status_timer.stop()
        self._dtc_token = None  # ответы на чтение/сброс, пришедшие позже, игнорируются
        self._afl_token = None  # ответ на выгрузку A/F Learning, пришедший позже, игнорируется
        self._dtc_read_done = False
        if self._adapter is not None:
            self._adapter.interrupt()  # не ждать переподключения, если оно идёт
        if self.runner is not None:
            path = self.runner.stop_recording()
            self.runner.stop()
            self.runner = None
            if path is not None:
                self.window.set_log_folder(str(path))
        self.connection = None
        self._close_port()
        if self._supported is not None:
            self._supported = None
            self._fill_tree({pid for pid, _units in self.window.selected()})
        self.window.set_connected(False, tr("Не подключено"))

    def _status_text(self, voltage: float) -> str:
        assert self.connection is not None
        transport = tr("SSM по CAN") if self.connection.transport == "can" else tr("SSM по K-line")
        text = tr("{transport} · ЭБУ {ecu} · {volts} В", transport=transport, ecu=self.connection.init.ecu_id,
                  volts=f"{voltage:.1f}")
        rom = self._rom_info()
        if rom.ecu_id and rom.ecu_id.upper() != self.connection.init.ecu_id:
            text += " · " + tr("⚠ в редакторе открыта прошивка другого ЭБУ ({ecu})", ecu=rom.ecu_id)
        return text

    def _open_trace(self) -> Trace | None:
        if self._settings is None:
            return None
        folder = self._settings.logger_trace_dir
        folder.mkdir(parents=True, exist_ok=True)
        self._trace_path = folder / f"trace_{datetime.now():%Y%m%d_%H%M%S}.log"
        return Trace.to_file(self._trace_path)

    def _current_path(self, port: str) -> str:
        """Путь порта для (пере)открытия: после сброса адаптер может появиться под другим
        именем — тогда берётся единственный найденный OpenPort; нет ни одного — нет адаптера."""
        ports = self._find_ports()
        if port in ports:
            return port
        if len(ports) == 1:
            return ports[0]
        if not ports:
            raise OpenPortNotFound(f"OpenPort 2.0 not found on USB (was {port})")
        return port  # несколько портов — не угадываем; pyserial даст обычную ошибку

    def _close_port(self) -> None:
        # Каналы закрыть, адаптер сбросить — не оставлять его с открытым CAN и фильтрами
        adapter, self._adapter = self._adapter, None
        if adapter is not None:
            adapter.close()

    # --- Параметры ---

    def _fill_tree(self, checked: set[str]) -> None:
        params = sorted(self.defs.params.values(), key=lambda p: p.name.lower())
        groups: dict[str, list[ParamRow]] = {kind: [] for kind in _KIND_TITLES}
        for param in params:
            if param.kind not in groups or param.target not in (1, 3):
                continue
            supported = self._supported is None or param.id in self._supported
            if self._supported is not None and not supported:
                continue  # подключены — показываем только то, что этот ЭБУ отдаёт
            units = param.units() or ["raw"]
            groups[param.kind].append(ParamRow(
                id=param.id,
                name=param.name,
                units=units,
                unit=self._units.get(param.id, units[0]),
                checked=param.id in checked,
                tooltip=_tooltip(param),
            ))
        self.window.set_parameters([(tr(_KIND_TITLES[k]), rows) for k, rows in groups.items() if rows])

    def _on_selection(self) -> None:
        for pid, units in self.window.selected():
            self._units[pid] = units
        self._apply_selection()

    def _apply_selection(self) -> None:
        selected = self.window.selected()
        self._min.clear()
        self._max.clear()
        # Всё отмеченное слева — и на дашборд, и на график
        self.window.set_live_rows([LiveRow(pid, self.defs.get(pid).name, units) for pid, units in selected])
        if self.runner is not None:
            self.runner.select([(pid, units) for pid, units in selected])

    # --- Опрос ---

    def _on_sample(self, sample: Sample) -> None:
        self._latest = sample
        for pid, value in sample.values.items():
            if math.isnan(value):
                continue
            self._min[pid] = min(self._min.get(pid, value), value)
            self._max[pid] = max(self._max.get(pid, value), value)
        self.window.add_points(sample.time_ms / 1000, sample.values)
        if sample.mark:
            self.window.flash_mark(sample.time_ms / 1000)

    def _on_error(self, error: Exception) -> None:
        path = self._trace_path
        self.disconnect()
        text = tr("Связь с ЭБУ потеряна: {error}", error=error)
        if path is not None:
            text += " · " + tr("журнал: {path}", path=str(path))
        self.window.set_error(text)

    def _refresh(self) -> None:
        sample, runner = self._latest, self.runner
        if sample is None or runner is None:
            return
        units = dict(self.window.selected())
        texts = {}
        for pid, value in sample.values.items():
            if pid not in units:
                continue
            digits = decimals(self.defs.get(pid).conversion(units[pid]))
            texts[pid] = (_fmt(value, digits), _fmt(self._min.get(pid, math.nan), digits),
                          _fmt(self._max.get(pid, math.nan), digits))
        self.window.update_values(texts)
        poller = runner.poller
        for pid in units:
            slow, unsupported = pid in poller.slow, pid in poller.unsupported
            tip = (
                tr("Этот ЭБУ не отдаёт параметр") if unsupported
                else tr("ЭБУ отдаёт его только по K-line: обновляется реже, остальное не тормозит") if slow else ""
            )
            self.window.set_row_state(pid, tip, slow, unsupported)
        self.window.set_rate(runner.rate)
        self._show_link(runner)
        self.window.redraw_graphs(sample.time_ms / 1000)

    def _show_link(self, runner: LoggerRunner) -> None:
        """Строка состояния: обрыв идёт — сколько длится и какая попытка; после — счётчик."""
        link = runner.link
        hint = self._reconnect_hint(link.last_error)
        if link.lost_since is not None:
            text = tr("Связь потеряна {seconds} с назад — переподключение, попытка {n}",
                      seconds=f"{time.monotonic() - link.lost_since:.0f}", n=link.attempts)
            if self._needs_replug(link):
                hint = tr("адаптер завис — выдерните и вставьте USB")
            if hint:
                text += " · " + hint
            if text != self._shown_status:
                self._shown_status = text
                self.window.set_error(text)
            return
        text = self._status
        if link.drops:
            text += " · " + tr("обрывов: {n}, без связи {seconds} с", n=link.drops,
                               seconds=f"{link.downtime:.0f}")
            if hint:
                text += " · " + hint
        if text != self._shown_status:
            self._shown_status = text
            self.window.set_connected(True, text)

    def _show_status_temporarily(self, text: str, *, error: bool = False) -> None:
        """Показать сообщение в строке состояния и через STATUS_TIMEOUT_MS вернуть подключение.
        Иначе временная строка (например, «Таблицы обучения сохранены…») висела бы до события связи."""
        if error:
            self.window.set_afl_error(text)
        else:
            self.window.set_afl_status(text)
        self._status_timer.start(STATUS_TIMEOUT_MS)

    def _restore_status(self) -> None:
        """Вернуть строку подключения после временного сообщения в статусе."""
        if self.runner is None:
            return
        self._shown_status = self._status
        self.window.set_connected(True, self._status)

    def _reconnect_hint(self, error: Exception | None) -> str:
        """Короткая подсказка о причине неудачного переподключения; иначе пусто."""
        if isinstance(error, OpenPortNotFound):
            return tr("OpenPort не найден в USB")
        if isinstance(error, OpenPortError) and error.code == NO_REPLY:
            return tr("адаптер на USB есть, но не отвечает")
        return ""

    def _needs_replug(self, link: LinkStats) -> bool:
        """Порт открывается, а адаптер молчит несколько попыток подряд — сброс не помогает."""
        error = link.last_error
        return (
            link.attempts >= REPLUG_AFTER
            and isinstance(error, OpenPortError)
            and error.code == NO_REPLY
        )

    def mark(self) -> None:
        if self.runner is not None:
            self.runner.mark()

    # --- Ошибки (DTC) ---

    def read_dtc(self) -> None:
        """Прочитать коды: в потоке опроса, результат — сигналом в GUI."""
        runner = self.runner
        if runner is None or self._dtc_token is not None:
            return
        token = object()
        self._dtc_token = token
        self.window.set_dtc_busy(True, tr("Чтение кодов…"))
        runner.call(
            runner.poller.read_dtc,
            lambda rows: self._bridge.dtc_read.emit((token, rows)),
            lambda exc: self._bridge.dtc_error.emit((token, "read", exc)),
        )

    def clear_dtc(self) -> None:
        """Сброс памяти ошибок — только после чтения и подтверждения пользователя."""
        runner = self.runner
        if runner is None or self._dtc_token is not None:
            return
        if not self._dtc_read_done:
            self.window.set_dtc_error(tr("Сначала прочитайте коды"))
            return
        if runner.recording is not None:
            self.window.set_dtc_error(tr("Пока пишется лог, сброс недоступен — остановите запись"))
            return
        if not self.window.ask_clear_dtc():
            return
        token = object()
        self._dtc_token = token
        self.window.set_dtc_busy(True, tr("Сброс ошибок…"))
        runner.call(
            runner.poller.clear_dtc,
            lambda rows: self._bridge.dtc_cleared.emit((token, rows)),
            lambda exc: self._bridge.dtc_error.emit((token, "clear", exc)),
        )

    def _on_dtc_read(self, payload: tuple[object, list[DtcReading]]) -> None:
        token, readings = payload
        if token is not self._dtc_token:
            return  # ответ на прежнее подключение/чтение
        self._dtc_token = None
        self._dtc_read_done = True
        self.window.set_dtc_busy(False)
        self.window.set_dtc_rows(_dtc_rows(readings))

    def _on_dtc_cleared(self, payload: tuple[object, list[DtcReading]]) -> None:
        token, readings = payload
        if token is not self._dtc_token:
            return
        self._dtc_token = None
        self._dtc_read_done = True  # после сброса коды прочитаны заново
        self.window.set_dtc_busy(False)
        self.window.set_dtc_rows(_dtc_rows(readings))
        self.window.set_dtc_status(
            tr("Ошибки сброшены (reset ЭБУ). Кодов после сброса: {n}", n=len(readings))
        )

    def _on_dtc_error(self, payload: tuple[object, str, Exception]) -> None:
        token, action, error = payload
        if token is not self._dtc_token:
            return
        self._dtc_token = None
        self.window.set_dtc_busy(False)
        if action == "clear":
            # Сброс мог и пройти: старое содержимое памяти уже не достоверно
            self._dtc_read_done = False
            self.window.forget_dtc()
            self.window.set_dtc_error(tr("Сброс не завершён: {error}", error=error))
        else:
            self.window.set_dtc_error(tr("Ошибки не прочитаны: {error}", error=error))

    # --- Таблица A/F Learning ---

    def dump_afl(self) -> None:
        """Выгрузить таблицы обучения (A/F Learning #1 + FLKC): разовое чтение из RAM."""
        runner = self.runner
        if runner is None or self._afl_token is not None:
            return
        token = object()
        self._afl_token = token
        self._status_timer.stop()  # отменить прежнее временное сообщение
        self.window.set_afl_busy(True, tr("Чтение таблиц обучения…"))

        def read() -> tuple[AflTable, FlkcTable]:
            return runner.poller.read_afl(), runner.poller.read_flkc()

        runner.call(
            read,
            lambda result: self._bridge.afl_dump.emit((token, result)),
            lambda exc: self._bridge.afl_error.emit((token, exc)),
        )

    def _on_afl_dump(self, payload: tuple[object, tuple[AflTable, FlkcTable]]) -> None:
        token, (afl, flkc) = payload
        if token is not self._afl_token:
            return
        self._afl_token = None
        self.window.set_afl_busy(False)
        self.window.set_afl_status(self._status)  # вернуть строку подключения после «Чтение…»
        ecu = self.runner.poller.ecu_id if self.runner else ""
        afl_ranges = tuple(self._rom_table_values("A/F Learning #1 Airflow Ranges"))
        flkc = FlkcTable(
            cells=flkc.cells,
            rpm_axis=tuple(self._rom_table_values("Fine Correction Rows (RPM)")),
            load_axis=tuple(self._rom_table_values("Fine Correction Columns (Load)")),
        )
        text = _learning_text(ecu, afl, afl_ranges, flkc)
        if not self.window.show_afl_dump(text):
            return
        path = self.window.ask_afl_save_path(self._afl_suggested_name())
        if not path:
            return
        try:
            write_learning_csv(path, ecu, afl, afl_ranges, flkc)
        except OSError as exc:
            self._show_status_temporarily(tr("Таблица не сохранена: {error}", error=exc), error=True)
            return
        self._show_status_temporarily(tr("Таблицы обучения сохранены: {path}", path=path))

    def _on_afl_error(self, payload: tuple[object, Exception]) -> None:
        token, error = payload
        if token is not self._afl_token:
            return
        self._afl_token = None
        self.window.set_afl_busy(False)
        self._show_status_temporarily(tr("Таблицы обучения не прочитаны: {error}", error=error), error=True)

    def _afl_suggested_name(self) -> str:
        return str(self.log_folder() / f"learning_{datetime.now():%Y%m%d_%H%M%S}.csv")

    def _rom_table_values(self, name: str) -> list[float]:
        """Значения 2D-таблицы ROM (столбец) в физических единицах; нет читателя — пусто."""
        if self._rom_table_reader is None:
            return []
        return self._rom_table_reader(name)

    # --- Запись ---

    def log_root(self) -> Path:
        """Корень логов: выбранный пользователем или ecu-editor-logs рядом с прошивкой."""
        saved = self._settings.logger_log_dir() if self._settings else None
        return Path(saved) if saved else default_logs_root(self._rom_info().path)

    def log_folder(self) -> Path:
        """Логи открытой прошивки: <корень>/<имя прошивки> (там же ищет ИИ — list_logs)."""
        return rom_log_dir(self._rom_info().path, self.log_root())

    def _on_record(self, on: bool) -> None:
        if self.runner is None:
            self.window.set_recording(None)
            return
        if not on:
            path = self.runner.stop_recording()
            self.window.set_recording(None)
            if path is not None:
                self.window.set_log_folder(str(self.log_folder()))
                self.window.set_connected(True, tr("Лог сохранён: {path}", path=path))
            return
        if not self.window.selected():
            self.window.set_recording(None)
            self.window.set_error(tr("Выберите параметры для записи"))
            return
        # Прошивка уже в имени папки — файл называется датой и временем
        path = self.log_folder() / f"{datetime.now():%Y%m%d_%H%M%S}.csv"
        try:
            self.runner.start_recording(path)
        except OSError as exc:
            self.window.set_recording(None)
            self.window.set_error(tr("Лог не создан: {error}", error=exc))
            return
        self.window.set_recording(str(path))

    def _choose_folder(self) -> None:
        # Выбирается корень; подпапка по имени прошивки добавляется сама
        folder = self.window.ask_folder(str(self.log_root()))
        if folder and self._settings is not None:
            self._settings.set_logger_log_dir(folder)
        if folder:
            self.window.set_log_folder(str(self.log_folder()))

    def _open_logs(self) -> None:
        """Открыть папку с журналами адаптера (trace-файлы и events.log)."""
        if self._settings is None:
            return
        folder = self._settings.logger_trace_dir
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # --- Профили ---

    def profile_folders(self) -> list[tuple[Path, str]]:
        """Профили из сборки (definitions/logger/profiles) и свои — рядом с settings.ini."""
        folders = [(LOGGER_PROFILES, "extra")]
        if self._settings is not None:
            folders.append((self._settings.logger_profiles_dir, "user"))
        return folders

    def pick_profile(self) -> None:
        entries = list_profiles(self.profile_folders())
        self.window.show_profile_picker([
            (e.name, str(e.path), tr("Свой профиль") if e.source == "user" else tr("Из сборки"))
            for e in entries
        ])

    def _open_profile(self) -> None:
        path = self.window.ask_profile_path(False, str(self._profiles_dir()))
        if path:
            self._apply_profile_file(path)

    def _profiles_dir(self) -> Path:
        return self._settings.logger_profiles_dir if self._settings is not None else LOGGER_PROFILES

    def _apply_profile_file(self, path: str) -> None:
        try:
            items = load_profile(path)
        except Exception as exc:  # noqa: BLE001
            self.window.set_error(tr("Профиль не прочитан: {error}", error=exc))
            return
        self.apply_profile(items)

    def apply_profile(self, items: list[ProfileItem]) -> None:
        known = [item for item in items if item.id in self.defs.params]
        for item in known:
            if item.units:
                self._units[item.id] = item.units
        self._fill_tree({item.id for item in known})
        self._apply_selection()
        missing = len(items) - len([i for i in known if i.id in dict(self.window.selected())])
        if missing:
            self.window.set_error(tr("Из профиля не подходят этому ЭБУ параметров: {n}", n=missing))

    def _save_profile(self) -> None:
        folder = self._profiles_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = self.window.ask_profile_path(True, str(folder))
        if not path:
            return
        # График у нас — всё выбранное; в профиле RomRaider отмечаем так же
        items = [ProfileItem(pid, units, True) for pid, units in self.window.selected()]
        save_profile(path, items)


def _dtc_rows(readings: list[DtcReading]) -> list[DtcRow]:
    return [DtcRow(r.code.id, r.code.name, r.current, r.stored) for r in readings]


_AFL_ZONES = ("A", "B", "C", "D")


def _learning_text(ecu_id: str, afl: AflTable, ranges: Sequence[float], flkc: FlkcTable) -> str:
    """Текст таблиц обучения (A/F Learning #1 + кратко FLKC) для показа в окне."""
    lines = [tr("A/F Learning #1 — {ecu}", ecu=ecu_id), ""]
    air = [""] * len(_AFL_ZONES)
    if len(ranges) == 3:
        air = [
            f"0 – {_fmt(ranges[0], 2)} г/с",
            f"{_fmt(ranges[0], 2)} – {_fmt(ranges[1], 2)} г/с",
            f"{_fmt(ranges[1], 2)} – {_fmt(ranges[2], 2)} г/с",
            f"{_fmt(ranges[2], 2)} – … г/с",
        ]
    for i, zone in enumerate(_AFL_ZONES):
        value = _fmt(afl.stored.get(zone, math.nan), 2)
        label = f" ({air[i]})" if air[i] else ""
        lines.append(f"{zone}{label}: {value} %")
    lines.append("")
    lines.append(tr("Текущее: {value} %", value=_fmt(afl.current, 2)))
    lines.append(tr("Зона: {value}", value=str(afl.range_index) if afl.range_index else "—"))
    rows = len(flkc.cells)
    cols = len(flkc.cells[0]) if rows else 0
    lines.append("")
    lines.append(tr("FLKC: {rows} × {cols} ячеек (карта RPM × Load) — в CSV", rows=rows, cols=cols))
    return "\n".join(lines)


def _tooltip(param: LoggerParam) -> str:
    text = f"{param.id}"
    if param.description and param.description != param.id:
        text += f" — {param.description}"
    if param.source:
        text += "\n" + tr("Из дополнения: {file}", file=param.source)
    return text


def _fmt(value: float, digits: int) -> str:
    if math.isnan(value) or math.isinf(value):
        return "—"
    return f"{value:.{digits}f}"


def _find_openport() -> list[str]:
    from src.logger.core.transport.serial_port import find_openport

    return find_openport()


def _open_serial(port: str) -> ByteStream:
    from src.logger.core.transport.serial_port import open_port

    stream: ByteStream = open_port(port)
    return stream
