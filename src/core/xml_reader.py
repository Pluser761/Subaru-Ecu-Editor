from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from src.core.models import (
    AxisDef,
    RomDefinition,
    RomId,
    Scaling,
    TableDef,
)


EDITABLE_TABLE_TYPES = frozenset({"3D", "2D", "1D"})

# Значения по умолчанию подставляются только после мерджа child поверх base:
# при разборе отсутствующий атрибут остаётся пустым, иначе дефолт child
# затирал бы значение из base.
DEFAULT_CATEGORY = "Maps"
DEFAULT_TABLE_TYPE = "3D"
DEFAULT_TABLE_STORAGETYPE = "uint8"
DEFAULT_AXIS_STORAGETYPE = "uint16"
DEFAULT_ENDIAN = "big"


def parse_filesize(text: str | None) -> int | None:
    if not text:
        return None
    raw = text.strip().lower().replace(" ", "")
    try:
        if raw.endswith("kb"):
            return int(float(raw[:-2]) * 1024)
        if raw.endswith("mb"):
            return int(float(raw[:-2]) * 1024 * 1024)
        return int(raw, 0)
    except ValueError:
        return None


def parse_address(text: str | None) -> int | None:
    """Адреса RomRaider всегда hex: с префиксом 0x или без («2000», «CC176»)."""
    if text is None or text.strip() == "":
        return None
    return int(text.strip(), 16)


def _text(el: ET.Element | None, default: str = "") -> str:
    if el is None or el.text is None:
        return default
    return el.text.strip()


def _parse_float(text: str | None) -> float | None:
    if text is None or text.strip() == "":
        return None
    try:
        return float(text.strip())
    except ValueError:
        return None


def _parse_scaling(el: ET.Element | None) -> Scaling | None:
    if el is None:
        return None
    return Scaling(
        units=el.attrib.get("units", ""),
        expression=el.attrib.get("expression", "x"),
        to_byte=el.attrib.get("to_byte", "x"),
        format=el.attrib.get("format", "0.00"),
        fine_increment=_parse_float(el.attrib.get("fineincrement")),
        coarse_increment=_parse_float(el.attrib.get("coarseincrement")),
    )


def _parse_axis(el: ET.Element) -> AxisDef:
    axis_type = el.attrib.get("type", "")
    size = None
    if "sizex" in el.attrib:
        size = int(el.attrib["sizex"])
    elif "sizey" in el.attrib:
        size = int(el.attrib["sizey"])
    static = [_text(d) for d in el.findall("data")]
    return AxisDef(
        axis_type=axis_type,
        name=el.attrib.get("name", ""),
        storageaddress=parse_address(el.attrib.get("storageaddress")),
        storagetype=el.attrib.get("storagetype", ""),
        endian=el.attrib.get("endian", ""),
        size=size,
        scaling=_parse_scaling(el.find("scaling")),
        static_data=static,
    )


def _parse_table(el: ET.Element) -> TableDef:
    table_type = el.attrib.get("type", "")
    x_axis = None
    y_axis = None
    for child in el.findall("table"):
        ctype = child.attrib.get("type", "")
        if ctype in ("X Axis", "Static X Axis"):
            x_axis = _parse_axis(child)
        elif ctype in ("Y Axis", "Static Y Axis"):
            y_axis = _parse_axis(child)

    sizex = int(el.attrib["sizex"]) if "sizex" in el.attrib else None
    sizey = int(el.attrib["sizey"]) if "sizey" in el.attrib else None

    return TableDef(
        name=el.attrib.get("name", ""),
        category=el.attrib.get("category", ""),
        table_type=table_type,
        storageaddress=parse_address(el.attrib.get("storageaddress")),
        storagetype=el.attrib.get("storagetype", ""),
        endian=el.attrib.get("endian", ""),
        sizex=sizex,
        sizey=sizey,
        scaling=_parse_scaling(el.find("scaling")),
        x_axis=x_axis,
        y_axis=y_axis,
        description=_text(el.find("description")),
    )


def _parse_romid(rom_el: ET.Element, base: str | None) -> RomId:
    rid = rom_el.find("romid")
    if rid is None:
        return RomId(xmlid="", base=base)
    return RomId(
        xmlid=_text(rid.find("xmlid")),
        base=base,
        internalidaddress=parse_address(_text(rid.find("internalidaddress")) or None),
        internalidstring=_text(rid.find("internalidstring")) or None,
        ecuid=_text(rid.find("ecuid")) or None,
        filesize=parse_filesize(_text(rid.find("filesize")) or None),
        market=_text(rid.find("market")),
        make=_text(rid.find("make")),
        model=_text(rid.find("model")),
        submodel=_text(rid.find("submodel")),
        year=_text(rid.find("year")),
        memmodel=_text(rid.find("memmodel")),
    )


def _tables_from_rom_el(rom_el: ET.Element) -> dict[str, TableDef]:
    tables: dict[str, TableDef] = {}
    for table_el in rom_el.findall("table"):
        name = table_el.attrib.get("name")
        if not name:
            continue
        # Child overlays often omit type — keep empty and fill on merge
        tables[name] = _parse_table(table_el)
    return tables


def _merge_axis(base: AxisDef | None, overlay: AxisDef | None) -> AxisDef | None:
    if base is None:
        return overlay
    if overlay is None:
        return base
    return AxisDef(
        axis_type=overlay.axis_type or base.axis_type,
        name=overlay.name or base.name,
        storageaddress=(
            overlay.storageaddress
            if overlay.storageaddress is not None
            else base.storageaddress
        ),
        storagetype=overlay.storagetype or base.storagetype,
        endian=overlay.endian or base.endian,
        size=overlay.size if overlay.size is not None else base.size,
        scaling=overlay.scaling or base.scaling,
        static_data=overlay.static_data or base.static_data,
    )


def _merge_table(base: TableDef, overlay: TableDef) -> TableDef:
    return TableDef(
        name=overlay.name or base.name,
        category=overlay.category or base.category,
        table_type=overlay.table_type or base.table_type,
        storageaddress=(
            overlay.storageaddress
            if overlay.storageaddress is not None
            else base.storageaddress
        ),
        storagetype=overlay.storagetype or base.storagetype,
        endian=overlay.endian or base.endian,
        sizex=overlay.sizex if overlay.sizex is not None else base.sizex,
        sizey=overlay.sizey if overlay.sizey is not None else base.sizey,
        scaling=overlay.scaling or base.scaling,
        x_axis=_merge_axis(base.x_axis, overlay.x_axis),
        y_axis=_merge_axis(base.y_axis, overlay.y_axis),
        description=overlay.description or base.description,
    )


def _axis_with_defaults(axis: AxisDef | None) -> AxisDef | None:
    if axis is None:
        return None
    return axis.model_copy(
        update={
            "storagetype": axis.storagetype or DEFAULT_AXIS_STORAGETYPE,
            "endian": axis.endian or DEFAULT_ENDIAN,
        }
    )


def _table_with_defaults(table: TableDef) -> TableDef:
    return table.model_copy(
        update={
            "category": table.category or DEFAULT_CATEGORY,
            "table_type": table.table_type or DEFAULT_TABLE_TYPE,
            "storagetype": table.storagetype or DEFAULT_TABLE_STORAGETYPE,
            "endian": table.endian or DEFAULT_ENDIAN,
            "x_axis": _axis_with_defaults(table.x_axis),
            "y_axis": _axis_with_defaults(table.y_axis),
        }
    )


def with_defaults(definition: RomDefinition) -> RomDefinition:
    """Заполнить пустые атрибуты таблиц дефолтами RomRaider (после мерджа)."""
    tables = {name: _table_with_defaults(t) for name, t in definition.tables.items()}
    return definition.model_copy(update={"tables": tables})


def merge_rom_definitions(base: RomDefinition, child: RomDefinition) -> RomDefinition:
    """Смержить child поверх base по имени таблицы."""
    tables = dict(base.tables)
    for name, overlay in child.tables.items():
        if name in tables:
            tables[name] = _merge_table(tables[name], overlay)
        else:
            tables[name] = overlay
    romid = child.romid.model_copy(
        update={"base": child.romid.base or base.romid.xmlid}
    )
    return RomDefinition(romid=romid, tables=tables)


class EcuDefsRepository:
    """Индекс RomRaider ecu_defs.xml."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.bases: dict[str, RomDefinition] = {}
        self.children: list[RomDefinition] = []
        self._by_xmlid: dict[str, RomDefinition] = {}

    @classmethod
    def load(cls, path: str | Path) -> EcuDefsRepository:
        repo = cls(path)
        repo._parse()
        return repo

    def _parse(self) -> None:
        tree = ET.parse(self.path)
        root = tree.getroot()
        for rom_el in root.findall("rom"):
            base_name = rom_el.attrib.get("base")
            romid = _parse_romid(rom_el, base_name)
            tables = _tables_from_rom_el(rom_el)
            definition = RomDefinition(romid=romid, tables=tables)
            if not romid.xmlid:
                continue
            self._by_xmlid[romid.xmlid] = definition
            if base_name:
                self.children.append(definition)
            else:
                self.bases[romid.xmlid] = definition

    def match(self, rom_bytes: bytes) -> list[RomDefinition]:
        """Найти child-definitions, подходящие под образ ROM."""
        hits: list[RomDefinition] = []
        size = len(rom_bytes)
        for child in self.children:
            rid = child.romid
            if rid.filesize is not None and rid.filesize != size:
                continue
            if rid.internalidaddress is None or not rid.internalidstring:
                continue
            start = rid.internalidaddress
            end = start + len(rid.internalidstring)
            if end > size:
                continue
            try:
                found = rom_bytes[start:end].decode("ascii", errors="ignore")
            except Exception:
                continue
            if found == rid.internalidstring:
                hits.append(child)
        return hits

    def resolve(self, child: RomDefinition) -> RomDefinition:
        """Смержить child с цепочкой base."""
        base_name = child.romid.base
        if not base_name:
            return child
        base = self.bases.get(base_name) or self._by_xmlid.get(base_name)
        if base is None:
            return child
        # base itself may inherit (rare) — one level is enough for this file
        if base.romid.base:
            base = self.resolve(base)
        return merge_rom_definitions(base, child)

    def lineage(self, xmlid: str) -> list[RomId]:
        """Цепочка romid от описания до корневой базы: [child, …, base]."""
        chain: list[RomId] = []
        seen: set[str] = set()
        current: str | None = xmlid
        while current and current not in seen:
            seen.add(current)
            definition = self._by_xmlid.get(current)
            if definition is None:
                break
            chain.append(definition.romid)
            current = definition.romid.base
        return chain

    def root_base(self, xmlid: str) -> str:
        """Корневая база (16BITBASE, 32BITBASE, …) — последнее звено цепочки."""
        chain = self.lineage(xmlid)
        return chain[-1].xmlid if chain else ""

    def memmodel(self, xmlid: str) -> str:
        """Процессор ЭБУ: первое заданное memmodel вверх по цепочке."""
        return next((rid.memmodel for rid in self.lineage(xmlid) if rid.memmodel), "")

    def identify(self, rom_bytes: bytes) -> RomDefinition | None:
        hits = self.match(rom_bytes)
        if not hits:
            return None
        # Prefer exact unique hit; if several, first
        return with_defaults(self.resolve(hits[0]))
