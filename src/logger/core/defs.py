"""Описания параметров логгера в формате RomRaider (logger def) — протокол SSM.

Основной файл — definitions/logger/logger_defs.xml (как у RomRaider), дополнения —
definitions/logger/extra/*.xml в той же схеме. Как и у карт, дополнения только
добавляют: новый id — новый параметр; у существующего extended-параметра — адреса
для ID ЭБУ, которых у него ещё нет; остальное с тем же id пропускается.

Виды параметров:
- `parameter` (P…) — стандартные SSM: адрес общий для всех ЭБУ, поддержка — по битам
  возможностей из ответа на init (`ecubyteindex` / `ecubit`); без адреса, с `<depends>` —
  вычисляемые по другим параметрам (формулы вида `(P12*60)/P8`, `[P21:ms]`);
- `ecuparam` (E…) — extended: адреса RAM свои у каждого ID ЭБУ;
- `switch` (S…) — бит байта по адресу; поддержка — бит `bit` в байте `ecubyteindex`.

Отдельно разбираются коды неисправностей (`<dtcodes>`): у каждого свой бит в байте
tmp (активна сейчас) и mem (сохранена); читает и сбрасывает их Poller.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from src.logger.core.ssm import EcuInit

log = logging.getLogger(__name__)

# Цели в logger def: 1 — ЭБУ, 2 — АКПП, 3 — оба
TARGET_ECU = {1, 3}


@dataclass(frozen=True)
class Conversion:
    units: str
    expr: str
    format: str = "0.00"
    storagetype: str = ""  # пусто — беззнаковое целое длины адреса


@dataclass(frozen=True)
class Address:
    address: int
    length: int = 1
    bit: int | None = None


@dataclass(frozen=True)
class DtcCode:
    """Код неисправности SSM: бит `bit` в байте tmp (активна сейчас) и mem (сохранена)."""

    id: str
    name: str
    tmp_address: int
    mem_address: int
    bit: int
    description: str = ""
    source: str = ""  # файл дополнения; пусто — основной logger def


@dataclass
class LoggerParam:
    id: str
    name: str
    kind: str  # «parameter» | «ecuparam» | «switch» | «calculated»
    description: str = ""
    conversions: list[Conversion] = field(default_factory=list)
    # Стандартный параметр / переключатель: байты по этим адресам подряд (обычно один адрес)
    addresses: list[Address] = field(default_factory=list)
    # Extended: ID ЭБУ → адреса
    ecu_addresses: dict[str, list[Address]] = field(default_factory=dict)
    depends: list[str] = field(default_factory=list)
    byte_index: int | None = None
    bit_index: int | None = None
    target: int = 1
    source: str = ""  # файл дополнения; пусто — основной logger def

    def addresses_for(self, ecu_id: str) -> list[Address]:
        return self.ecu_addresses.get(ecu_id, []) if self.kind == "ecuparam" else self.addresses

    def units(self) -> list[str]:
        return [c.units for c in self.conversions]

    def conversion(self, units: str | None = None) -> Conversion:
        """Пересчёт в заданные единицы (по умолчанию — первые, как у RomRaider)."""
        if not self.conversions:
            return Conversion(units="raw", expr="x", format="0")
        if units is None:
            return self.conversions[0]
        for conversion in self.conversions:
            if conversion.units == units:
                return conversion
        raise KeyError(f"{self.id}: no conversion to {units!r}")


def _int(text: str | None) -> int | None:
    return int(text, 0) if text not in (None, "") else None


def _addresses(parent: ET.Element) -> list[Address]:
    out = []
    for el in parent.findall("address"):
        if el.text and el.text.strip():
            out.append(Address(int(el.text.strip(), 16), int(el.get("length", "1")), _int(el.get("bit"))))
    return out


def _conversions(el: ET.Element) -> list[Conversion]:
    return [
        Conversion(
            units=c.get("units", ""),
            expr=c.get("expr", "x"),
            format=c.get("format", "0.00"),
            storagetype=c.get("storagetype", ""),
        )
        for c in el.iter("conversion")
    ]


def _common(el: ET.Element, kind: str, source: str) -> LoggerParam:
    return LoggerParam(
        id=el.get("id", ""),
        name=el.get("name", ""),
        kind=kind,
        description=el.get("desc", ""),
        conversions=_conversions(el),
        byte_index=_int(el.get("ecubyteindex")),
        bit_index=_int(el.get("ecubit")),
        target=_int(el.get("target")) or 1,
        source=source,
    )


def parse_protocol(protocol: ET.Element, source: str = "") -> list[LoggerParam]:
    """Параметры одного `<protocol id="SSM">`."""
    params: list[LoggerParam] = []
    for el in protocol.iter("parameter"):
        depends = [ref.get("parameter", "") for ref in el.iter("ref")]
        param = _common(el, "calculated" if depends else "parameter", source)
        param.depends = depends
        param.addresses = _addresses(el)
        params.append(param)
    for el in protocol.iter("ecuparam"):
        param = _common(el, "ecuparam", source)
        for ecu in el.findall("ecu"):
            addresses = _addresses(ecu)
            for ecu_id in ecu.get("id", "").split(","):
                if ecu_id.strip():
                    param.ecu_addresses[ecu_id.strip().upper()] = addresses
        params.append(param)
    for el in protocol.iter("switch"):
        param = _common(el, "switch", source)
        param.addresses = [Address(int(el.get("byte", "0"), 16), 1, _int(el.get("bit")))]
        # Поддержка переключателя — его же бит в байте ecubyteindex
        param.bit_index = _int(el.get("bit"))
        param.conversions = [Conversion(units="on/off", expr="x", format="0")]
        params.append(param)
    return params


def parse_dtcodes(protocol: ET.Element, source: str = "") -> list[DtcCode]:
    """Коды неисправностей одного `<protocol id="SSM">` (блок `<dtcodes>`)."""
    codes: list[DtcCode] = []
    for el in protocol.iter("dtcode"):
        tmp, mem, bit = _int(el.get("tmpaddr")), _int(el.get("memaddr")), _int(el.get("bit"))
        if tmp is None or mem is None or bit is None:
            log.warning("logger defs: dtcode без адреса или бита: %s", el.get("id"))
            continue
        codes.append(DtcCode(
            id=el.get("id", ""),
            name=el.get("name", ""),
            tmp_address=tmp,
            mem_address=mem,
            bit=bit,
            description=el.get("desc", ""),
            source=source,
        ))
    return codes


class LoggerDefs:
    """Параметры SSM: основной logger def + дополнения."""

    def __init__(self) -> None:
        self.params: dict[str, LoggerParam] = {}
        self.dtcodes: dict[str, DtcCode] = {}  # id → код; порядок — как в definitions
        self.files: list[Path] = []

    @classmethod
    def load(cls, path: str | Path, extra: Iterable[str | Path | tuple[str | Path, str]] = ()) -> LoggerDefs:
        defs = cls()
        defs._load(Path(path), "")
        for item in extra:
            root, prefix = item if isinstance(item, tuple) else (item, None)
            root = Path(root)
            files = [root] if root.is_file() else sorted(root.rglob("*.xml")) if root.is_dir() else []
            for file in files:
                rel = file.name if root.is_file() else file.relative_to(root).as_posix()
                defs._load(file, f"{prefix or root.name}/{rel}")
        return defs

    def _load(self, path: Path, source: str) -> None:
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            log.warning("logger defs: не разобран %s: %s", path, exc)
            return
        protocols = [p for p in root.iter("protocol") if p.get("id") == "SSM"]
        if not protocols:
            return
        for protocol in protocols:
            for param in parse_protocol(protocol, source):
                self._add(param)
            for code in parse_dtcodes(protocol, source):
                self._add_dtc(code)
        self.files.append(path)

    def _add(self, param: LoggerParam) -> None:
        existing = self.params.get(param.id)
        if existing is None:
            self.params[param.id] = param
        elif existing.kind == "ecuparam" and param.kind == "ecuparam":
            for ecu_id, addresses in param.ecu_addresses.items():
                existing.ecu_addresses.setdefault(ecu_id, addresses)

    def _add_dtc(self, code: DtcCode) -> None:
        # Как и параметры, дополнения только добавляют: тот же id не перекрывается
        self.dtcodes.setdefault(code.id, code)

    def get(self, param_id: str) -> LoggerParam:
        return self.params[param_id]

    def find(self, name_or_id: str) -> LoggerParam:
        """По id («P8») или имени без учёта регистра и звёздочки («Engine Speed»)."""
        if name_or_id in self.params:
            return self.params[name_or_id]
        key = name_or_id.strip().rstrip("*").lower()
        for param in self.params.values():
            if param.name.strip().rstrip("*").lower() == key:
                return param
        raise KeyError(name_or_id)

    def supported(self, init: EcuInit) -> list[LoggerParam]:
        """Параметры, которые этот ЭБУ отдаёт (по битам возможностей и ID ЭБУ)."""
        ok: dict[str, LoggerParam] = {}
        for param in self.params.values():
            if param.target not in TARGET_ECU:
                continue
            if param.kind == "ecuparam":
                if init.ecu_id in param.ecu_addresses:
                    ok[param.id] = param
            elif param.kind in ("parameter", "switch"):
                if param.addresses and init.supports(param.byte_index, param.bit_index):
                    ok[param.id] = param
        # Вычисляемые могут зависеть от вычисляемых (P242 ← P200) — до неподвижной точки
        added = True
        while added:
            added = False
            for param in self.params.values():
                if (
                    param.kind == "calculated"
                    and param.id not in ok
                    and param.target in TARGET_ECU
                    and all(d in ok for d in param.depends)
                ):
                    ok[param.id] = param
                    added = True
        return sorted(ok.values(), key=lambda p: p.name.lower())


# «[P21:ms]» в формуле вычисляемого параметра — значение P21 в мс
REF_PATTERN = re.compile(r"\[(\w+):([^\]]+)\]")
