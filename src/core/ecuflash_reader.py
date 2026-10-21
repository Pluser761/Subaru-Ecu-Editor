"""Описания в формате EcuFlash (SubaruDefs от td-d/Merp): один `<rom>` на файл.

Отличия от RomRaider, которые здесь сводятся к моделям `core.models`:
- наследование через `<include>XMLID</include>` (одна или несколько баз), таблицы
  и их оси мержатся по имени; в child оси часто названы просто «X»/«Y»;
- формула, тип хранения и порядок байт — в именованном `<scaling>`, на таблицу
  ссылается атрибут `scaling`; `storagetype="bloblist"` — переключатель (Switch);
- адрес — атрибут `address` (hex без 0x), размер оси — `elements`, уровень — `level`;
- float помечены честно `endian="big"`, а в моделях endian float инвертирован, как
  в RomRaider (см. `rom_io._TYPE_FMT`) — при переводе меняем местами.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from src.core.models import AxisDef, RomDefinition, RomId, Scaling, TableDef

log = logging.getLogger(__name__)

# Имена состояний bloblist → состояния переключателей редактора (on/off, как в RomRaider)
_STATE_NAMES = {"enabled": "on", "disabled": "off", "on": "on", "off": "off"}
# printf-формат EcuFlash «%.2f» → число знаков
_PRINTF_DECIMALS = re.compile(r"%[-+ #0]*\d*\.(\d+)[fF]")


@dataclass
class _Table:
    """Таблица EcuFlash как в XML (ещё не смерженная с базой): атрибуты + оси по имени."""

    attrs: dict[str, str]
    description: str = ""
    data: list[str] = field(default_factory=list)
    axes: dict[str, _Table] = field(default_factory=dict)


@dataclass
class EcuFlashRom:
    """Один файл EcuFlash: romid, базы, scaling и таблицы."""

    path: Path
    label: str  # подпись источника для UI: путь от папки дополнений
    romid: RomId
    includes: list[str]
    scalings: dict[str, ET.Element]
    tables: dict[str, _Table]


def parse_address(text: str | None) -> int | None:
    """Адрес EcuFlash — hex без 0x («C7B94»)."""
    if text is None or not text.strip():
        return None
    return int(text.strip(), 16)


def is_ecuflash_file(path: Path) -> bool:
    """Файл EcuFlash: корень `<rom>` (у RomRaider — `<roms>`)."""
    try:
        for _event, el in ET.iterparse(path, events=("start",)):
            return el.tag == "rom"
    except ET.ParseError:
        return False
    return False


def parse_ecuflash_file(path: Path, label: str = "") -> EcuFlashRom | None:
    root = ET.parse(path).getroot()
    rid = root.find("romid")
    xmlid = _text(rid.find("xmlid")) if rid is not None else ""
    if root.tag != "rom" or rid is None or not xmlid:
        return None
    includes = [_text(el) for el in root.findall("include") if _text(el)]

    def text(tag: str) -> str:
        return _text(rid.find(tag))

    romid = RomId(
        xmlid=xmlid,
        base=includes[0] if includes else None,
        internalidaddress=parse_address(text("internalidaddress") or None),
        internalidstring=text("internalidstring") or None,
        ecuid=text("ecuid") or None,
        market=text("market"),
        make=text("make"),
        model=text("model"),
        submodel=text("submodel"),
        year=text("year"),
        memmodel=text("memmodel"),
    )
    scalings = {el.attrib["name"]: el for el in root.findall("scaling") if el.attrib.get("name")}
    tables = {
        el.attrib["name"]: _read_table(el) for el in root.findall("table") if el.attrib.get("name")
    }
    return EcuFlashRom(path=path, label=label or path.name, romid=romid, includes=includes, scalings=scalings, tables=tables)


def _text(el: ET.Element | None) -> str:
    return el.text.strip() if el is not None and el.text else ""


def _read_table(el: ET.Element) -> _Table:
    table = _Table(
        attrs=dict(el.attrib),
        description=_text(el.find("description")),
        data=[_text(d) for d in el.findall("data")],
    )
    for index, child in enumerate(el.findall("table")):
        # У статических осей базы имени иногда нет — ключ по типу, чтобы не потерять
        name = child.attrib.get("name") or child.attrib.get("type") or f"#{index}"
        table.axes[name] = _read_table(child)
    return table


def _merge(base: _Table, overlay: _Table) -> _Table:
    axes = dict(base.axes)
    for name, axis in overlay.axes.items():
        key = _axis_key(axes, name)
        axes[key] = _merge(axes[key], axis) if key in axes else axis
    return _Table(
        attrs={**base.attrs, **overlay.attrs},
        description=overlay.description or base.description,
        data=overlay.data or base.data,
        axes=axes,
    )


def _axis_key(axes: dict[str, _Table], name: str) -> str:
    """Какую ось базы уточняет ось child: по имени, а «X»/«Y» — по типу оси."""
    if name in axes or name not in ("X", "Y"):
        return name
    matches = [k for k, a in axes.items() if f"{name} Axis" in a.attrs.get("type", "")]
    return matches[0] if len(matches) == 1 else name


class EcuFlashLibrary:
    """Все файлы EcuFlash из папок дополнений; разбор и мердж базы — по требованию."""

    def __init__(self) -> None:
        self.roms: dict[str, EcuFlashRom] = {}
        self._resolved: dict[str, dict[str, _Table]] = {}

    def add_file(self, path: Path, label: str = "") -> EcuFlashRom | None:
        try:
            rom = parse_ecuflash_file(path, label)
        except (ET.ParseError, ValueError) as exc:
            log.warning("EcuFlash: не разобран %s: %s", path, exc)
            return None
        if rom is None:
            return None
        if rom.romid.xmlid in self.roms:
            log.warning(
                "EcuFlash: %s уже есть в %s, пропущен %s",
                rom.romid.xmlid, self.roms[rom.romid.xmlid].path, path,
            )
            return None
        self.roms[rom.romid.xmlid] = rom
        return rom

    def _merged_tables(self, xmlid: str, seen: frozenset[str] = frozenset()) -> dict[str, _Table]:
        if xmlid in self._resolved:
            return self._resolved[xmlid]
        rom = self.roms.get(xmlid)
        if rom is None or xmlid in seen:
            return {}
        tables: dict[str, _Table] = {}
        for include in rom.includes:
            for name, table in self._merged_tables(include, seen | {xmlid}).items():
                tables[name] = _merge(tables[name], table) if name in tables else table
        for name, table in rom.tables.items():
            tables[name] = _merge(tables[name], table) if name in tables else table
        self._resolved[xmlid] = tables
        return tables

    def _scaling(self, xmlid: str, name: str, seen: frozenset[str] = frozenset()) -> ET.Element | None:
        """Scaling по имени: сначала в самом описании, затем вверх по include."""
        rom = self.roms.get(xmlid)
        if rom is None or xmlid in seen:
            return None
        if name in rom.scalings:
            return rom.scalings[name]
        for include in rom.includes:
            found = self._scaling(include, name, seen | {xmlid})
            if found is not None:
                return found
        return None

    def definition(self, xmlid: str) -> RomDefinition | None:
        """Таблицы описания с адресами (база без адресов в ROM не попадает), в моделях редактора."""
        rom = self.roms.get(xmlid)
        if rom is None:
            return None
        tables: dict[str, TableDef] = {}
        for name, raw in self._merged_tables(xmlid).items():
            try:
                table = self._convert(xmlid, name, raw)
            except (KeyError, ValueError) as exc:
                log.debug("EcuFlash %s: таблица %r пропущена: %s", xmlid, name, exc)
                continue
            if table is not None:
                tables[name] = table
        return RomDefinition(romid=rom.romid, tables=tables)

    def lineage(self, xmlid: str) -> list[RomId]:
        chain: list[RomId] = []
        current: str | None = xmlid
        while current and current in self.roms and all(r.xmlid != current for r in chain):
            rom = self.roms[current]
            chain.append(rom.romid)
            current = rom.includes[0] if rom.includes else None
        return chain

    # --- Перевод в модели редактора ---

    def _convert(self, xmlid: str, name: str, raw: _Table) -> TableDef | None:
        address = parse_address(raw.attrs.get("address"))
        if address is None:
            return None
        if raw.attrs.get("swapxy", "").lower() == "true":
            # Хранение по столбцам — модель редактора его не знает
            raise ValueError("swapxy")
        scaling_el = self._scaling(xmlid, raw.attrs.get("scaling", ""))
        if scaling_el is None:
            raise KeyError(f"scaling {raw.attrs.get('scaling')!r}")
        level = raw.attrs.get("level", "")
        common = dict(
            name=name,
            category=raw.attrs.get("category", ""),
            storageaddress=address,
            description=raw.description,
            userlevel=int(level) if level.isdigit() else None,
        )
        if scaling_el.attrib.get("storagetype") == "bloblist":
            states = {
                _STATE_NAMES.get(d.attrib.get("name", "").lower(), d.attrib.get("name", "")):
                    d.attrib.get("value", "").replace(" ", "").lower()
                for d in scaling_el.findall("data")
                if d.attrib.get("name")
            }
            return TableDef(table_type="Switch", states=states, **common)  # type: ignore[arg-type]

        storagetype, endian = _storage(scaling_el)
        x_axis = y_axis = None
        for axis in raw.axes.values():
            kind = axis.attrs.get("type", "")
            converted = self._convert_axis(xmlid, axis)
            if "X Axis" in kind:
                x_axis = converted
            elif "Y Axis" in kind:
                y_axis = converted
        table_type = raw.attrs.get("type", "")
        sizex = sizey = None
        if table_type == "3D":
            if x_axis is None or y_axis is None:
                raise ValueError("3D без осей")
            sizex, sizey = x_axis.size, y_axis.size
        elif table_type == "2D":
            # Вектор редактор держит по Y (как RomRaider), ось X EcuFlash переносим туда же
            axis_def = y_axis or x_axis
            if axis_def is None:
                raise ValueError("2D без оси")
            y_axis, x_axis = axis_def.model_copy(update={"axis_type": axis_def.axis_type.replace("X", "Y")}), None
            sizey = axis_def.size
        elif table_type != "1D":
            raise ValueError(f"тип {table_type!r}")
        return TableDef(
            table_type=table_type,
            storagetype=storagetype,
            endian=endian,
            sizex=sizex,
            sizey=sizey,
            scaling=_to_scaling(scaling_el),
            x_axis=x_axis,
            y_axis=y_axis,
            **common,  # type: ignore[arg-type]
        )

    def _convert_axis(self, xmlid: str, axis: _Table) -> AxisDef:
        kind = axis.attrs.get("type", "")
        elements = axis.attrs.get("elements", "")
        size = int(elements) if elements.isdigit() else (len(axis.data) or None)
        if kind.startswith("Static"):
            return AxisDef(axis_type=kind, name=axis.attrs.get("name", ""), size=size, static_data=axis.data)
        scaling_el = self._scaling(xmlid, axis.attrs.get("scaling", ""))
        if scaling_el is None:
            raise KeyError(f"scaling оси {axis.attrs.get('scaling')!r}")
        address = parse_address(axis.attrs.get("address"))
        if address is None:
            raise ValueError("ось без адреса")
        storagetype, endian = _storage(scaling_el)
        return AxisDef(
            axis_type=kind,
            name=axis.attrs.get("name", ""),
            storageaddress=address,
            storagetype=storagetype,
            endian=endian,
            size=size,
            scaling=_to_scaling(scaling_el),
        )


def _storage(scaling: ET.Element) -> tuple[str, str]:
    storagetype = scaling.attrib.get("storagetype", "uint8").lower()
    endian = scaling.attrib.get("endian", "big").lower()
    if storagetype == "float":
        endian = "little" if endian == "big" else "big"
    return storagetype, endian


def _to_scaling(el: ET.Element) -> Scaling:
    match = _PRINTF_DECIMALS.search(el.attrib.get("format", ""))
    decimals = int(match.group(1)) if match else 2
    inc = el.attrib.get("inc")
    try:
        fine = float(inc) if inc else None
    except ValueError:
        fine = None
    return Scaling(
        units=el.attrib.get("units", ""),
        expression=el.attrib.get("toexpr", "x") or "x",
        to_byte=el.attrib.get("frexpr", "x") or "x",
        format="0." + "0" * decimals if decimals else "0",
        fine_increment=fine,
    )
