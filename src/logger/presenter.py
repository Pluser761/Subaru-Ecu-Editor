"""Связка окна логгера с ядром: адаптер, подключение, опрос, запись, отметки, профили.

Опрос идёт в своём потоке (core.runner); точки приходят в поток интерфейса Qt-сигналом,
значения в таблице перерисовываются таймером (не чаще UI_HZ), графики — вместе с ними.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from src.i18n import N_, tr
from src.logger.core.defs import LoggerDefs, LoggerParam
from src.logger.core.params import decimals
from src.logger.core.profile import ProfileItem, load_profile, save_profile
from src.logger.core.runner import LoggerRunner
from src.logger.core.session import Connection, Poller, Sample, connect
from src.logger.core.trace import Trace
from src.logger.core.transport.emulator import FakeEcu, FakeOpenPort, demo_memory
from src.logger.core.transport.op2 import ByteStream, OpenPort2
from src.logger.ui.window import LiveRow, LoggerWindow, ParamRow

ROOT = Path(__file__).resolve().parents[2]
LOGGER_DEFS = ROOT / "definitions" / "logger" / "logger_defs.xml"
LOGGER_EXTRA = LOGGER_DEFS.parent / "extra"
EMULATOR_PORT = "emulator"
UI_HZ = 10
DEFAULT_GRAPHS = 4

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

    def logger_log_dir(self) -> str | None: ...

    def set_logger_log_dir(self, folder: str) -> None: ...

    def logger_port(self) -> str | None: ...

    def set_logger_port(self, port: str) -> None: ...


class _Bridge(QObject):
    """Из потока опроса в поток интерфейса."""

    sample = pyqtSignal(object)
    error = pyqtSignal(object)


class LoggerPresenter:
    def __init__(
        self,
        window: LoggerWindow,
        settings: LoggerSettings | None = None,
        rom_info: Callable[[], RomInfo] | None = None,
        *,
        find_ports: Callable[[], list[str]] | None = None,
        open_port: Callable[[str], ByteStream] | None = None,
    ) -> None:
        self.window = window
        self._settings = settings
        self._rom_info = rom_info or RomInfo
        self._find_ports = find_ports or _find_openport
        self._open_port = open_port or _open_serial
        extra: list[tuple[str | Path, str]] = [(LOGGER_EXTRA, "extra")]
        if settings is not None:
            extra.append((settings.logger_extra_dir, "user"))
        self.defs = LoggerDefs.load(LOGGER_DEFS, extra)
        self._stream: ByteStream | None = None
        self._trace: Trace | None = None
        self.connection: Connection | None = None
        self.runner: LoggerRunner | None = None
        self._supported: set[str] | None = None  # None — не подключены, показываем всё
        self._units: dict[str, str] = {}  # выбранные единицы (переживают перестройку дерева)
        self._graphs: set[str] = set()
        self._latest: Sample | None = None
        self._min: dict[str, float] = {}
        self._max: dict[str, float] = {}
        self._bridge = _Bridge()
        self._bridge.sample.connect(self._on_sample)
        self._bridge.error.connect(self._on_error)
        self._timer = QTimer()
        self._timer.setInterval(1000 // UI_HZ)
        self._timer.timeout.connect(self._refresh)

        w = window
        w.connect_requested.connect(self.connect)
        w.disconnect_requested.connect(self.disconnect)
        w.refresh_ports_requested.connect(self.refresh_ports)
        w.selection_changed.connect(self._on_selection)
        w.graphs_changed.connect(self._on_graphs)
        w.record_toggled.connect(self._on_record)
        w.mark_requested.connect(self.mark)
        w.open_profile_requested.connect(self._open_profile)
        w.save_profile_requested.connect(self._save_profile)
        w.choose_folder_requested.connect(self._choose_folder)
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
        try:
            if port == EMULATOR_PORT:
                self._stream = FakeOpenPort(FakeEcu(memory=demo_memory()))
            else:
                self._stream = self._open_port(port)
                self._trace = self._open_trace()
            device = OpenPort2(self._stream, self._trace)
            version = device.identify()
            voltage = device.read_voltage()
            if self._trace:
                self._trace.note(f"{version}; OBD pin 16 = {voltage:.2f} V")
            self.connection = connect(device)
        except Exception as exc:  # noqa: BLE001 — показать в окне; порт закрыть
            self._close_port()
            self.window.set_connected(False, tr("Не подключено"))
            self.window.set_error(tr("Не удалось подключиться: {error}", error=exc))
            return
        if self._settings is not None:
            self._settings.set_logger_port(port)
        self._supported = {p.id for p in self.defs.supported(self.connection.init)}
        self.runner = LoggerRunner(Poller(self.connection, self.defs), self._bridge.sample.emit, self._bridge.error.emit)
        selected = self.window.selected()
        self._fill_tree({pid for pid, _units in selected})
        self.window.set_connected(True, self._status_text(voltage))
        self._apply_selection()
        self.runner.start()
        self._timer.start()

    def disconnect(self) -> None:
        self._timer.stop()
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
        return Trace.to_file(folder / f"trace_{datetime.now():%Y%m%d_%H%M%S}.log")

    def _close_port(self) -> None:
        stream, self._stream = self._stream, None
        close = getattr(stream, "close", None)
        if callable(close):
            close()
        if self._trace is not None:
            self._trace.close()
            self._trace = None

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
        if not self._graphs:
            self._graphs = {pid for pid, _units in selected[:DEFAULT_GRAPHS]}
        rows = [
            LiveRow(pid, self.defs.get(pid).name, units, pid in self._graphs)
            for pid, units in selected
        ]
        self.window.set_live_rows(rows)
        if self.runner is not None:
            self.runner.select([(pid, units) for pid, units in selected])
        self._on_graphs()

    def _on_graphs(self) -> None:
        graphed = self.window.graphed()
        live = {pid for pid, _units in self.window.selected()}
        self._graphs = set(graphed) | (self._graphs - live)
        units = dict(self.window.selected())
        self.window.set_graphs([(pid, f"{self.defs.get(pid).name}, {units[pid]}") for pid in graphed])

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
        self.disconnect()
        self.window.set_error(tr("Связь с ЭБУ потеряна: {error}", error=error))

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
                else tr("Читается по K-line: замедляет весь опрос") if slow else ""
            )
            self.window.set_row_state(pid, tip, slow, unsupported)
        self.window.set_rate(runner.rate)
        self.window.redraw_graphs(sample.time_ms / 1000)

    def mark(self) -> None:
        if self.runner is not None:
            self.runner.mark()

    # --- Запись ---

    def log_folder(self) -> Path:
        saved = self._settings.logger_log_dir() if self._settings else None
        if saved:
            return Path(saved)
        rom = self._rom_info()
        if rom.path:
            return Path(rom.path).parent / "logs"  # там же ищет логи ИИ (list_logs)
        return Path.home() / "subaru_logs"

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
        rom = self._rom_info()
        stem = Path(rom.path).stem if rom.path else "log"
        path = self.log_folder() / f"{stem}_{datetime.now():%Y%m%d_%H%M%S}.csv"
        try:
            self.runner.start_recording(path)
        except OSError as exc:
            self.window.set_recording(None)
            self.window.set_error(tr("Лог не создан: {error}", error=exc))
            return
        self.window.set_recording(str(path))

    def _choose_folder(self) -> None:
        folder = self.window.ask_folder(str(self.log_folder()))
        if folder and self._settings is not None:
            self._settings.set_logger_log_dir(folder)
        if folder:
            self.window.set_log_folder(folder)

    # --- Профили ---

    def _open_profile(self) -> None:
        path = self.window.ask_profile_path(False, str(self.log_folder()))
        if not path:
            return
        try:
            items = load_profile(path)
        except Exception as exc:  # noqa: BLE001
            self.window.set_error(tr("Профиль не прочитан: {error}", error=exc))
            return
        self.apply_profile(items)

    def apply_profile(self, items: list[ProfileItem]) -> None:
        known = [item for item in items if item.id in self.defs.params]
        self._graphs = {item.id for item in known if item.graph}
        for item in known:
            if item.units:
                self._units[item.id] = item.units
        self._fill_tree({item.id for item in known})
        self._apply_selection()
        missing = len(items) - len([i for i in known if i.id in dict(self.window.selected())])
        if missing:
            self.window.set_error(tr("Из профиля не подходят этому ЭБУ параметров: {n}", n=missing))

    def _save_profile(self) -> None:
        path = self.window.ask_profile_path(True, str(self.log_folder()))
        if not path:
            return
        graphed = set(self.window.graphed())
        items = [ProfileItem(pid, units, pid in graphed) for pid, units in self.window.selected()]
        save_profile(path, items)


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
