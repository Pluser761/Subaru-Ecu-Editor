"""Сессия логгера: подключение (автовыбор CAN / K-line) и цикл опроса выбранных параметров.

Протокол выбирается сам: сначала SSM по CAN (быстро), если ЭБУ по CAN молчит — SSM по
K-line. Если по CAN ЭБУ отказывает на части адресов, они находятся делением запроса
пополам и переезжают на K-line (открывается при первой нужде), остальное остаётся на CAN. K-line опрашивается
без ожидания между опросами CAN: CAN идёт на своей скорости (~50 опросов/с на машине),
параметры K-line обновляются реже (~5–8 раз/с) и показывают последнее значение — они в `slow`.

Коды неисправностей (DTC) читаются тем же каналом по адресам из logger def (tmp — активна
сейчас, mem — сохранена). Сброс — RomRaider «Reset ECU» (запись 0x40 по 0x000060): после него
Poller делает init заново и читает коды повторно; кэши отказов по адресам сбрасываются.
"""

from __future__ import annotations

import math
import struct
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from src.logger.core.defs import DtcCode, LoggerDefs, LoggerParam
from src.logger.core.params import byte_addresses, calculate, convert_all
from src.logger.core.ssm import EcuInit, SsmClient, SsmError
from src.logger.core.transport.links import CanLink, KLineLink
from src.logger.core.transport.op2 import OpenPort2, OpenPortError


@dataclass
class Connection:
    device: OpenPort2
    client: SsmClient  # основной канал (CAN, если ЭБУ на нём ответил)
    init: EcuInit
    # Запасной канал K-line — открывается, только когда понадобится
    open_fallback: Callable[[], SsmClient] | None = None

    @property
    def transport(self) -> str:
        return self.client.transport


def connect(device: OpenPort2, transports: Sequence[str] = ("can", "kline")) -> Connection:
    """Открыть канал и сделать init: по порядку transports, первый ответивший — основной."""
    device.close_all()
    errors: list[str] = []
    for name in transports:
        link = CanLink(device) if name == "can" else KLineLink(device)
        client = SsmClient(link)
        try:
            init = client.init()
        except (SsmError, OpenPortError) as exc:
            errors.append(f"{name}: {exc}")
            link.close()
            continue
        fallback = None
        if name == "can" and "kline" in transports:
            fallback = _kline_opener(device)
        return Connection(device, client, init, fallback)
    raise SsmError("SSM: ECU did not answer (" + "; ".join(errors) + ")")


def _kline_opener(device: OpenPort2) -> Callable[[], SsmClient]:
    def open_kline() -> SsmClient:
        client = SsmClient(KLineLink(device))
        client.init()
        return client

    return open_kline


# Значения колонки отметок
MARK_USER = 1
MARK_RESTORED = 2
# Пауза после reset ЭБУ, с: он перезагружается, init может ответить не сразу
DTC_RESET_PAUSE = 1.5

# Таблица A/F Learning #1 (долговременная коррекция топлива по зонам расхода воздуха):
# stored A/B/C/D — E44–E47 (4-байтовый float), текущее — E48, текущая зона — E61 (1…4).
_AFL_ZONES = ("A", "B", "C", "D")
_AFL_PARAM = dict(zip(_AFL_ZONES, ("E44", "E45", "E46", "E47")))
_AFL_CURRENT = "E48"
_AFL_RANGE = "E61"

# FLKC (Fine Learning Knock Correction) — карта обучения по детонации RPM × Load в RAM.
# AZ1G101M: база 0xFFFF2D30, ячейки float (4 байта) с шагом 8 байт, 7 строк (RPM) × 5 колонок
# (Load), row-major (индекс = строка*5 + колонка). Найдено дизассемблером: функция 0x3ED16
# читает ячейку по offset 0xFFFF7D08, а выбор ячейки в 0x3EC52 даёт строку*5 + колонку (max 35).
_FLKC_RAM_BASE = 0xFF2D30
_FLKC_ROWS = 7
_FLKC_COLS = 5
_FLKC_STRIDE = 8


@dataclass(frozen=True)
class Sample:
    time_ms: float  # от начала опроса
    values: dict[str, float]  # id параметра → значение в выбранных единицах (nan — не прочитан)
    mark: int = 0  # 1 — в этом опросе нажата отметка, 2 — первый опрос после обрыва связи


@dataclass(frozen=True)
class DtcReading:
    """Код неисправности в памяти ЭБУ: current — активна сейчас (tmp), stored — сохранена (mem)."""

    code: DtcCode
    current: bool
    stored: bool


@dataclass(frozen=True)
class AflTable:
    """Таблица A/F Learning #1, прочитанная из RAM: stored A/B/C/D и текущее — в %, зона — 1…4."""

    stored: dict[str, float]  # «A»…«D» → % (nan — не прочитано)
    current: float  # % (nan — не прочитано)
    range_index: int  # 1…4 (0 — не прочитано)


@dataclass(frozen=True)
class FlkcTable:
    """Fine Learning Knock Correction: 2D-карта RPM × Load, ячейки — коррекция УОЗ в градусах.

    cells — row-major (_FLKC_ROWS строк × _FLKC_COLS колонок); rpm_axis / load_axis — границы
    диапазонов (по одной меньше числа строк/колонок), заполняются из ROM.
    """

    cells: tuple[tuple[float, ...], ...]
    rpm_axis: tuple[float, ...] = ()
    load_axis: tuple[float, ...] = ()


@dataclass
class _Plan:
    params: list[LoggerParam]  # читаемые с ЭБУ (выбранные и зависимости вычисляемых)
    calculated: list[LoggerParam]  # в порядке вычисления
    addresses: list[int]  # уникальные адреса байт
    index: dict[str, list[int]] = field(default_factory=dict)  # id → позиции его байт


class Poller:
    """Опрос выбранных параметров. poll() — один запрос-ответ по всем адресам."""

    def __init__(self, connection: Connection, defs: LoggerDefs) -> None:
        self.connection = connection
        self.defs = defs
        self.selection: list[tuple[str, str | None]] = []
        self.unsupported: set[str] = set()  # не читаются ни по одному каналу
        self.slow: set[str] = set()  # читаются через запасной K-line
        self._bad_addresses: set[int] = set()  # отказ на основном канале
        self._fallback: SsmClient | None = None
        # K-line рядом с CAN: последние значения, адреса текущего запроса, чей черёд
        self._fallback_values: dict[int, int] = {}
        self._fallback_chunk: list[int] = []
        self._fallback_turn = -1
        self._plan = _Plan([], [], [])
        self._start = time.monotonic()
        self._mark = 0

    @property
    def ecu_id(self) -> str:
        return self.connection.init.ecu_id

    def select(self, selection: Sequence[tuple[str, str | None]]) -> None:
        """Что опрашивать: [(id параметра, единицы или None — первые)]."""
        self.selection = [(self.defs.get(pid).id, units) for pid, units in selection]
        self._plan = self._build_plan()
        self._start = time.monotonic()

    def mark(self) -> None:
        """Отметка события (хлопок и т.п.): попадёт в ближайший опрос."""
        self._mark = MARK_USER

    def reconnect(self, connection: Connection) -> None:
        """Продолжить на новом подключении после обрыва: выбор, план и время — прежние,
        запасной K-line откроется заново; ближайший опрос получит отметку MARK_RESTORED."""
        self.connection = connection
        self._forget_fallback()
        self._mark = MARK_RESTORED

    # --- Коды неисправностей (DTC) ---

    def read_dtc(self) -> list[DtcReading]:
        """Прочитать коды из памяти ЭБУ: tmp — активна сейчас, mem — сохранена.

        Как в RomRaider: бит tmp даёт «сейчас» (1), бит mem — «сохранена» (2), оба — 3;
        пара байт 0xFF/0xFF означает, что адрес код не отдаёт, такие пропускаются.
        """
        codes = list(self.defs.dtcodes.values())
        if not codes:
            return []
        raw = self._read_blocking(self._dtc_addresses())
        readings: list[DtcReading] = []
        for code in codes:
            tmp, mem = raw.get(code.tmp_address), raw.get(code.mem_address)
            if tmp is None or mem is None:
                continue
            if tmp == 0xFF and mem == 0xFF:
                continue
            mask = 1 << code.bit
            current, stored = bool(tmp & mask), bool(mem & mask)
            if current or stored:
                readings.append(DtcReading(code, current, stored))
        return readings

    def clear_dtc(self, pause: float = DTC_RESET_PAUSE) -> list[DtcReading]:
        """Сбросить память ошибок: RomRaider «Reset ECU» (0x40 по 0x000060), затем init
        заново и повторное чтение. Забывает отказы по адресам: после перезапуска ЭБУ они
        могут читаться снова."""
        client = self.connection.client
        client.reset_ecu()
        self.connection.device.note("DTC cleared (ECU reset 0x40)")
        if pause > 0:
            time.sleep(pause)
        self._forget_fallback()
        self._bad_addresses.clear()
        self.unsupported.clear()
        self.slow.clear()
        self.connection.init = client.init()
        self._plan = self._build_plan()
        return self.read_dtc()

    def read_afl(self) -> AflTable:
        """Прочитать таблицу A/F Learning #1 одним разом: stored A/B/C/D, текущее значение и
        зону. Читаются extended-параметры E44–E47, E48, E61 по адресам этого ЭБУ."""
        param_ids = [*_AFL_PARAM.values(), _AFL_CURRENT, _AFL_RANGE]
        addresses: list[int] = []
        index: dict[str, list[int]] = {}
        for pid in param_ids:
            param = self.defs.get(pid)
            own = byte_addresses(param.addresses_for(self.ecu_id))
            if not own:
                self.unsupported.add(pid)
                continue
            index[pid] = own
            for address in own:
                if address not in addresses:
                    addresses.append(address)
        raw = self._read_blocking(addresses)
        values: dict[str, dict[str, float]] = {}
        for pid, own in index.items():
            param = self.defs.get(pid)
            data = bytes(raw[address] for address in own)
            bit = param.addresses_for(self.ecu_id)[0].bit
            values[pid] = convert_all(param, data, bit)

        def pct(pid: str) -> float:
            return values.get(pid, {}).get("%", math.nan)

        stored = {zone: pct(pid) for zone, pid in _AFL_PARAM.items()}
        current = pct(_AFL_CURRENT)
        range_index = int(values.get(_AFL_RANGE, {}).get("offset", 0))
        return AflTable(stored=stored, current=current, range_index=range_index)

    def read_flkc(self) -> FlkcTable:
        """Прочитать таблицу FLKC (7×5 float, градусы) из RAM по базе _FLKC_RAM_BASE."""
        addresses: list[int] = []
        for index in range(_FLKC_ROWS * _FLKC_COLS):
            base = _FLKC_RAM_BASE + index * _FLKC_STRIDE
            addresses += [base + offset for offset in range(4)]  # 4 байта float
        raw = self._read_blocking(addresses)
        cells: list[tuple[float, ...]] = []
        for row in range(_FLKC_ROWS):
            row_values: list[float] = []
            for column in range(_FLKC_COLS):
                base = _FLKC_RAM_BASE + (row * _FLKC_COLS + column) * _FLKC_STRIDE
                data = bytes(raw[base + offset] for offset in range(4))
                row_values.append(struct.unpack(">f", data)[0])
            cells.append(tuple(row_values))
        return FlkcTable(cells=tuple(cells))

    def poll(self) -> Sample:
        plan = self._plan
        raw = self._read(plan.addresses)
        values_all: dict[str, dict[str, float]] = {}
        for param in plan.params:
            positions = plan.index[param.id]
            if not positions or any(raw.get(plan.addresses[i]) is None for i in positions):
                values_all[param.id] = {}
                continue
            data = bytes(raw[plan.addresses[i]] or 0 for i in positions)
            bit = param.addresses_for(self.ecu_id)[0].bit
            values_all[param.id] = convert_all(param, data, bit)
        for param in plan.calculated:
            values_all[param.id] = {
                c.units: calculate(param, values_all, c.units) for c in param.conversions
            }
        values: dict[str, float] = {}
        for pid, units in self.selection:
            param = self.defs.get(pid)
            by_units = values_all.get(pid, {})
            key = units or (param.conversions[0].units if param.conversions else "raw")
            values[pid] = by_units.get(key, math.nan)
        mark, self._mark = self._mark, 0
        return Sample((time.monotonic() - self._start) * 1000, values, mark)

    # --- План опроса ---

    def _build_plan(self) -> _Plan:
        readable: dict[str, LoggerParam] = {}
        calculated: list[LoggerParam] = []

        def need(pid: str) -> None:
            param = self.defs.get(pid)
            if param.kind == "calculated":
                for dep in param.depends:
                    need(dep)
                if param not in calculated:
                    calculated.append(param)  # после своих зависимостей
            else:
                readable.setdefault(pid, param)

        for pid, _units in self.selection:
            need(pid)
        addresses: list[int] = []
        position: dict[int, int] = {}
        index: dict[str, list[int]] = {}
        for param in readable.values():
            own = byte_addresses(param.addresses_for(self.ecu_id))
            if not own:
                self.unsupported.add(param.id)
            for address in own:
                if address not in position:
                    position[address] = len(addresses)
                    addresses.append(address)
            index[param.id] = [position[a] for a in own]
        return _Plan(list(readable.values()), calculated, addresses, index)

    # --- Чтение ---

    def _forget_fallback(self) -> None:
        """Закрыть и забыть запасной K-line и его последние значения (откроется заново)."""
        fallback, self._fallback = self._fallback, None
        if fallback is not None:
            try:
                fallback.link.close()
            except Exception:  # noqa: BLE001 — после обрыва старый адаптер уже мог пропасть
                pass
        self._fallback_values.clear()
        self._fallback_chunk = []
        self._fallback_turn = -1

    def _dtc_addresses(self) -> list[int]:
        """Уникальные адреса байт DTC: tmp и mem всех кодов, в порядке definitions."""
        addresses: list[int] = []
        seen: set[int] = set()
        for code in self.defs.dtcodes.values():
            for address in (code.tmp_address, code.mem_address):
                if address not in seen:
                    seen.add(address)
                    addresses.append(address)
        return addresses

    def _read_blocking(self, addresses: list[int]) -> dict[int, int]:
        """Прочитать адреса одним разом и дождаться ответа: основной канал, отказавшие на нём —
        сразу по K-line. В отличие от обычного опроса нельзя показывать последнее значение
        K-line — разовое чтение (DTC, таблица A/F Learning) требует свежих байт, поэтому без
        ответа сообщаем об ошибке.
        """
        out = self._read_isolating(self.connection.client, addresses)
        # По K-line читаем все отказавшие адреса заново: последнее значение опроса могло
        # быть получено раньше, чем нужно
        bad = [a for a in addresses if a in self._bad_addresses]
        if bad:
            # Пересоздать K-line: у него мог висеть фоновый запрос опроса, ответ на
            # который уже не отличить от ответа на разовое чтение
            self._forget_fallback()
            fallback = self._open_fallback()
            if fallback is not None:
                data = fallback.read_addresses(bad)
                out.update(zip(bad, data))
                self._fallback_values.update(zip(bad, data))
            else:
                for address in bad:
                    out.pop(address, None)  # без K-line эти байты не прочитать
        missing = [a for a in addresses if out.get(a) is None]
        if missing:
            raise SsmError("SSM: addresses not read: " + ", ".join(f"{a:06x}" for a in missing))
        return {a: value for a, value in out.items() if value is not None}

    def _read(self, addresses: list[int]) -> dict[int, int | None]:
        """Байты по адресам: основной канал — каждый опрос; отказавшие на нём адреса — через
        K-line без ожидания (последнее полученное значение, пока не пришло новое)."""
        good = [a for a in addresses if a not in self._bad_addresses]
        out: dict[int, int | None] = {}
        self._service_fallback(addresses)
        if good:
            out.update(self._read_isolating(self.connection.client, good))
        self._service_fallback(addresses)
        for address in addresses:
            if address in self._bad_addresses:
                out[address] = self._fallback_values.get(address)
        self._update_status()
        return out

    def _read_isolating(self, client: SsmClient, addresses: list[int]) -> dict[int, int | None]:
        """Прочитать; на отказ ЭБУ (код отказа) — делить пополам и найти виноватые адреса."""
        try:
            data = client.read_addresses(addresses)
            # Адрес мог начать читаться (после перезапуска ЭБУ) — больше не отказ
            self._bad_addresses.difference_update(addresses)
            return dict(zip(addresses, data))
        except SsmError as exc:
            if exc.code is None:
                raise  # нет ответа / битый ответ — не отказ по адресу
            if len(addresses) == 1:
                self._bad_addresses.add(addresses[0])
                return {addresses[0]: self._fallback_values.get(addresses[0])}
            half = len(addresses) // 2
            out = self._read_isolating(client, addresses[:half])
            out.update(self._read_isolating(client, addresses[half:]))
            return out

    def _service_fallback(self, addresses: list[int]) -> None:
        """K-line рядом с CAN: забрать готовый ответ и сразу отправить следующий запрос
        (адреса по кругу кусками), не дожидаясь ответа."""
        bad = [a for a in addresses if a in self._bad_addresses]
        if not bad:
            return
        fallback = self._open_fallback()
        if fallback is None:
            return
        if fallback.pending:
            try:
                data = fallback.poll_read()
            except SsmError:
                data = None
                self._fallback_chunk = []  # не дождались — запросить заново
            if data is not None:
                self._fallback_values.update(zip(self._fallback_chunk, data))
                self._fallback_chunk = []
            if fallback.pending:
                return
        chunks = [bad[i : i + fallback.max_addresses] for i in range(0, len(bad), fallback.max_addresses)]
        self._fallback_turn = (self._fallback_turn + 1) % len(chunks)
        self._fallback_chunk = chunks[self._fallback_turn]
        fallback.begin_read(self._fallback_chunk)

    def _open_fallback(self) -> SsmClient | None:
        if self._fallback is None and self.connection.open_fallback is not None:
            try:
                self._fallback = self.connection.open_fallback()
            except (SsmError, OpenPortError):
                self.connection.open_fallback = None  # K-line тоже не отвечает — больше не пробуем
        return self._fallback

    def _update_status(self) -> None:
        self.slow.clear()
        for param in self._plan.params:
            own = set(byte_addresses(param.addresses_for(self.ecu_id)))
            if own & self._bad_addresses:
                if self._fallback is None:
                    self.unsupported.add(param.id)
                else:
                    self.slow.add(param.id)
