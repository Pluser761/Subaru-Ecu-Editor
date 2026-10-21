"""Сессия логгера: подключение (автовыбор CAN / K-line) и цикл опроса выбранных параметров.

Протокол выбирается сам: сначала SSM по CAN (быстро), если ЭБУ по CAN молчит — SSM по
K-line. Если по CAN ЭБУ отказывает на части адресов, они находятся делением запроса
пополам и переезжают на K-line (открывается при первой нужде), остальное остаётся на CAN;
такие параметры — в `slow`: они тормозят весь опрос, интерфейс об этом предупреждает.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from src.logger.core.defs import LoggerDefs, LoggerParam
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


@dataclass(frozen=True)
class Sample:
    time_ms: float  # от начала опроса
    values: dict[str, float]  # id параметра → значение в выбранных единицах (nan — не прочитан)
    mark: int = 0  # 1 — в этом опросе нажата отметка


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
        self._mark = 1

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

    def _read(self, addresses: list[int]) -> dict[int, int | None]:
        """Байты по адресам: основной канал, отказавшие адреса — через K-line."""
        good = [a for a in addresses if a not in self._bad_addresses]
        bad = [a for a in addresses if a in self._bad_addresses]
        out: dict[int, int | None] = {}
        if good:
            out.update(self._read_isolating(self.connection.client, good))
        if bad:
            out.update(self._read_fallback(bad))
        self._update_status()
        return out

    def _read_isolating(self, client: SsmClient, addresses: list[int]) -> dict[int, int | None]:
        """Прочитать; на отказ ЭБУ (код отказа) — делить пополам и найти виноватые адреса."""
        try:
            data = client.read_addresses(addresses)
            return dict(zip(addresses, data))
        except SsmError as exc:
            if exc.code is None:
                raise  # нет ответа / битый ответ — не отказ по адресу
            if len(addresses) == 1:
                self._bad_addresses.add(addresses[0])
                return self._read_fallback(addresses)
            half = len(addresses) // 2
            out = self._read_isolating(client, addresses[:half])
            out.update(self._read_isolating(client, addresses[half:]))
            return out

    def _read_fallback(self, addresses: list[int]) -> dict[int, int | None]:
        if self._fallback is None and self.connection.open_fallback is not None:
            try:
                self._fallback = self.connection.open_fallback()
            except (SsmError, OpenPortError):
                self.connection.open_fallback = None  # K-line тоже не отвечает — больше не пробуем
        if self._fallback is None:
            return {a: None for a in addresses}
        try:
            data = self._fallback.read_addresses(addresses)
        except SsmError:
            return {a: None for a in addresses}
        return dict(zip(addresses, data))

    def _update_status(self) -> None:
        self.slow.clear()
        for param in self._plan.params:
            own = set(byte_addresses(param.addresses_for(self.ecu_id)))
            if own & self._bad_addresses:
                if self._fallback is None:
                    self.unsupported.add(param.id)
                else:
                    self.slow.add(param.id)
