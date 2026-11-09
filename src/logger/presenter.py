"""Связка окна логгера с ядром: адаптер, подключение, опрос, запись, отметки, профили.

Опрос идёт в своём потоке (core.runner); точки приходят в поток интерфейса Qt-сигналом,
значения в таблице перерисовываются таймером (не чаще UI_HZ), графики — вместе с ними.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices

from src.i18n import N_, tr
from src.logger.core.adapter import Adapter, Opened
from src.logger.core.csv_log import write_learning_csv
from src.logger.core.defs import LoggerDefs, LoggerParam
from src.logger.core.maf import (
    COLLECT_IDS,
    MAX_POINTS,
    REQUIRED_IDS,
    MafCollector,
    MafTable,
    MafTrend,
    fit_trend,
    scale_values,
    trend_curve,
)
from src.logger.core.params import decimals
from src.logger.core.paths import default_logs_root, rom_log_dir
from src.logger.core.profile import ProfileItem, list_profiles, load_profile, save_profile
from src.logger.ui.maf import MafWindow
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
    """Из потоков подключения/опроса в поток интерфейса."""

    sample = pyqtSignal(object)
    error = pyqtSignal(object)
    connect_opened = pyqtSignal(object)
    connect_failed = pyqtSignal(object)
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
        maf_table: Callable[[], MafTable | None] | None = None,
        maf_apply: Callable[[str, dict[int, float]], int] | None = None,
    ) -> None:
        self.window = window
        self._settings = settings
        self._rom_info = rom_info or RomInfo
        self._find_ports = find_ports or _find_openport
        self._open_port = open_port or _open_serial
        self._rom_table_reader = rom_table_reader
        self._maf_table = maf_table
        self._maf_apply = maf_apply
        extra: list[tuple[str | Path, str]] = [(LOGGER_EXTRA, "extra")]
        if settings is not None:
            extra.append((settings.logger_extra_dir, "user"))
        self.defs = LoggerDefs.load(LOGGER_DEFS, extra)
        self._adapter: Adapter | None = None
        self.connection: Connection | None = None
        self._connect_token: object | None = None  # текущее подключение (защита от устаревшего ответа)
        self._connecting_port: str | None = None  # порт идущего подключения (для настроек)
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
        # Вкладка MAF: точки копит сборщик, кривая — полином по ним
        self.maf = MafCollector()
        self._maf_active = False
        self._maf_added: list[str] = []  # параметры, добавленные в опрос ради точек (в дереве их нет)
        self._maf_trend: MafTrend | None = None
        self._maf_xs: list[float] = []  # точки для графика (растут инкрементально)
        self._maf_ys: list[float] = []
        self._maf_shown = 0  # сколько точек уже отдано окну
        # MAF-скейлинг — отдельное окно: живёт рядом с дашбордом, данные из этого же опроса
        self.maf_window = MafWindow()
        self.maf_window.record_toggled.connect(self._on_maf_record)
        self.maf_window.interpolate_requested.connect(self.interpolate_maf)
        self.maf_window.apply_requested.connect(self.apply_maf)
        self.maf_window.reset_requested.connect(self.reset_maf)
        self.maf_window.closed.connect(self._on_maf_closed)
        self._bridge = _Bridge()
        self._bridge.sample.connect(self._on_sample)
        self._bridge.error.connect(self._on_error)
        self._bridge.connect_opened.connect(self._on_connect_opened)
        self._bridge.connect_failed.connect(self._on_connect_failed)
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
        w.maf_requested.connect(self.show_maf)
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
        """Начать подключение в фоновом потоке: открытие порта и init ЭБУ не блокируют GUI."""
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
        token = object()
        self._connect_token = token
        self._connecting_port = port
        self.window.set_connecting(True, tr("Подключение…"))

        def run() -> None:
            try:
                opened = adapter.open()
            except Exception as exc:  # noqa: BLE001 — показать в окне; порт закрыть
                # Закрыть полуоткрытый адаптер в этом же потоке: GUI не должен ждать его
                try:
                    adapter.close()
                except Exception:  # noqa: BLE001
                    pass
                self._bridge.connect_failed.emit((token, exc))
            else:
                self._bridge.connect_opened.emit((token, opened, adapter))

        threading.Thread(target=run, name="logger-connect", daemon=True).start()

    def _on_connect_opened(self, payload: tuple[object, Opened, Adapter]) -> None:
        token, opened, adapter = payload
        if token is not self._connect_token:
            # Пока открывали порт, отключились или начали другое подключение — закрыть адаптер
            try:
                adapter.close()
            except Exception:  # noqa: BLE001
                pass
            return
        self._connect_token = None
        self.connection = opened.connection
        if self._settings is not None and self._connecting_port is not None:
            self._settings.set_logger_port(self._connecting_port)
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

    def _on_connect_failed(self, payload: tuple[object, Exception]) -> None:
        token, error = payload
        if token is not self._connect_token:
            return  # отключились/переключились — адаптер уже закрыл поток подключения
        self._connect_token = None
        self._close_port()
        log.warning("connect failed: port %s: %s", self._connecting_port, error)
        self.window.set_connected(False, tr("Не подключено"))
        if isinstance(error, OpenPortNotFound):
            self.window.set_error(tr("OpenPort 2.0 не найден — проверьте USB-подключение"))
        elif isinstance(error, OpenPortError) and error.code == NO_REPLY:
            self.window.set_error(tr("Адаптер не отвечает — отключите и снова подключите его USB"))
        else:
            self.window.set_error(tr("Не удалось подключиться: {error}", error=error))

    def disconnect(self) -> None:
        self._maf_active = False
        self._maf_added = []
        self._clear_maf()  # точки другой сессии/ЭБУ не смешивать с новыми
        self.maf_window.set_recording(False)
        self._timer.stop()
        self._status_timer.stop()
        self._dtc_token = None  # ответы на чтение/сброс, пришедшие позже, игнорируются
        self._afl_token = None  # ответ на выгрузку A/F Learning, пришедший позже, игнорируется
        self._dtc_read_done = False
        connecting = self._connect_token is not None
        self._connect_token = None  # результат идущего подключения игнорируется
        adapter = self._adapter
        self._adapter = None
        if adapter is not None:
            adapter.interrupt()  # прервать переподключение/подключение, не ждать его конца
        if self.runner is not None:
            path = self.runner.stop_recording()
            self.runner.stop()
            self.runner = None
            if path is not None:
                self.window.set_log_folder(str(path))
        self.connection = None
        if adapter is not None and not connecting:
            # Нет идущего подключения — закрыть сразу; при идущем его закрывает поток
            # подключения (или _on_connect_opened для устаревшего успеха), иначе GUI
            # снова ждал бы конца open().
            adapter.close()
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
            # Параметры, нужные вкладке MAF, опрашиваются, но на дашборд/график не попадают
            ids = {pid for pid, _units in selected}
            selection: list[tuple[str, str | None]] = [(pid, units) for pid, units in selected]
            selection += [(pid, None) for pid in self._maf_added if pid not in ids]
            if selection != self.runner.poller.selection:
                self.runner.select(selection)

    # --- Опрос ---

    def _on_sample(self, sample: Sample) -> None:
        self._latest = sample
        if self._maf_active:
            self.maf.filters = self.maf_window.filters()
            point = self.maf.accept(sample.values, sample.time_ms)
            if point is not None:
                self._maf_xs.append(point.volts)
                self._maf_ys.append(point.trim)
                if len(self._maf_xs) > MAX_POINTS:
                    del self._maf_xs[: len(self._maf_xs) - MAX_POINTS]
                    del self._maf_ys[: len(self._maf_ys) - MAX_POINTS]
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
        self._refresh_maf()

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
        if readings:
            self.window.set_dtc_status(
                tr("Коды не стёрты: {n}. Неисправность активна или ЭБУ не очистил память.",
                   n=len(readings))
            )
        else:
            self.window.set_dtc_status(tr("Ошибки сброшены. Кодов нет."))

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

    # --- Вкладка MAF ---

    def show_maf(self) -> None:
        """Показать окно MAF-скейлинга (Cmd/Ctrl+M или кнопка в логгере)."""
        self.maf_window.show()
        self.maf_window.raise_()
        self.maf_window.activateWindow()

    def _on_maf_closed(self) -> None:
        """Окно закрыли: точки не собираем, скрытые параметры из опроса убираем."""
        self._maf_active = False
        self.maf_window.set_recording(False)
        self._remove_maf_params()

    def _on_maf_record(self, on: bool) -> None:
        """Запись точек: нужные параметры добавляются в опрос скрыто (в дереве их нет)."""
        if not on:
            self._maf_active = False
            self._remove_maf_params()
            return
        if self.runner is None:
            self.maf_window.set_recording(False)
            self.maf_window.set_info(tr("Сначала подключитесь к ЭБУ"), error=True)
            return
        wanted = self._maf_param_ids()
        missing = [pid for pid in REQUIRED_IDS if pid not in wanted]
        if missing:
            self.maf_window.set_recording(False)
            self.maf_window.set_info(
                tr("ЭБУ не отдаёт параметры для точек: {ids}", ids=", ".join(missing)), error=True)
            return
        selected = {pid for pid, _units in self.window.selected()}
        self._maf_added = [pid for pid in wanted if pid not in selected]
        self._maf_active = True
        try:
            self._apply_selection()
        except RuntimeError:
            # Идёт запись CSV: колонки на ходу не меняются — без параметров точку не собрать
            self._maf_added = []
            self._maf_active = False
            self.maf_window.set_recording(False)
            self.maf_window.set_info(
                tr("Пока пишется лог, набор параметров менять нельзя — остановите запись"), error=True)
            return
        self.maf_window.set_info(tr("Запись точек включена"))

    def _remove_maf_params(self) -> None:
        added, self._maf_added = self._maf_added, []
        if not added or self.runner is None:
            return
        try:
            self._apply_selection()
        except RuntimeError:
            self._maf_added = added  # запись CSV: убрать нельзя, останутся в опросе до её конца

    def _maf_param_ids(self) -> list[str]:
        """Что ЭБУ реально отдаёт из нужного вкладке (после подключения — по битам init)."""
        return [
            pid for pid in COLLECT_IDS
            if pid in self.defs.params and (self._supported is None or pid in self._supported)
        ]

    def _refresh_maf(self) -> None:
        if len(self._maf_xs) == self._maf_shown:
            return
        self.maf_window.set_points(self._maf_xs, self._maf_ys)
        self._maf_shown = len(self._maf_xs)
        self.maf_window.set_info(tr("Точек: {n}", n=self._maf_shown))

    def interpolate_maf(self, order: int) -> None:
        """Подогнать полином степени order по накопленным точкам и показать кривую."""
        points = self.maf.points
        if len(points) < order + 1:
            self.maf_window.set_info(
                tr("Для степени {order} нужно не меньше {need} точек, а собрано {n}",
                   order=order, need=order + 1, n=len(points)), error=True)
            return
        volts = {round(p.volts, 3) for p in points}
        if len(volts) < 2:
            self.maf_window.set_info(tr("Все точки на одном напряжении — кривую не построить"), error=True)
            return
        try:
            trend = fit_trend(points, order)
        except Exception as exc:  # noqa: BLE001 — показать в окне, не ронять логгер
            self.maf_window.set_info(tr("Кривую не построить: {error}", error=exc), error=True)
            return
        self._maf_trend = trend
        low, high = min(volts), max(volts)
        grid = [low + (high - low) * i / 199 for i in range(200)]
        xs, ys = trend_curve(trend, grid)
        self.maf_window.set_curve(xs, ys)
        self.maf_window.set_info(tr("Кривая степени {order} по {n} точкам", order=order, n=len(points)))

    def apply_maf(self, low: float, high: float) -> None:
        """Записать новые значения строк «MAF Sensor Scaling» в открытую прошивку."""
        trend = self._maf_trend
        if trend is None:
            self.maf_window.set_info(tr("Сначала постройте кривую (Интерполировать)"), error=True)
            return
        if not low < high:
            self.maf_window.set_info(tr("Минимум напряжения должен быть меньше максимума"), error=True)
            return
        if self._maf_table is None or self._maf_apply is None:
            self.maf_window.set_info(tr("Прошивка не открыта в редакторе"), error=True)
            return
        table = self._maf_table()
        if table is None:
            self.maf_window.set_info(tr("В этой прошивке нет таблицы «MAF Sensor Scaling»"), error=True)
            return
        try:
            values = scale_values(table, low, high, trend)
        except ValueError:
            self.maf_window.set_info(tr("Таблица не изменена"), error=True)
            return
        if not values:
            self.maf_window.set_info(
                tr("В диапазоне {low}–{high} В нет строк таблицы", low=f"{low:.2f}", high=f"{high:.2f}"),
                error=True)
            return
        try:
            written = self._maf_apply(table.name, values)
        except Exception as exc:  # noqa: BLE001 — показать в окне, не ронять логгер
            self.maf_window.set_info(tr("Таблица не обновлена: {error}", error=exc), error=True)
            return
        self.maf_window.set_info(
            tr("Обновлено строк: {n} — откат: «Карты» → «Откатить все карты к checkpoint…»", n=written))

    def reset_maf(self) -> None:
        self._clear_maf()
        self.maf_window.set_info(tr("Точки сброшены"))

    def _clear_maf(self) -> None:
        """Сбросить собранные точки и кривую без сообщения в статусе (отключение, новая сессия)."""
        self.maf.clear()
        self._maf_xs.clear()
        self._maf_ys.clear()
        self._maf_trend = None
        self._maf_shown = 0
        self.maf_window.clear()

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
            # Набор параметров снова можно менять: убрать отложенные скрытые параметры MAF
            if not self._maf_active:
                self._remove_maf_params()
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
        if self.runner is not None and self.runner.recording is not None:
            # Колонки CSV зафиксированы на старте записи — набор менять нельзя
            self.window.set_error(tr("Пока пишется лог, набор параметров менять нельзя — остановите запись"))
            return
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
