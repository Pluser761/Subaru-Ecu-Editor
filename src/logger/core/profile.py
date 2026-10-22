"""Профили логгера в формате RomRaider (File → Load / Save Profile).

```
<profile protocol="SSM">
    <parameters><parameter id="P8" livedata="selected" graph="selected" units="rpm"/></parameters>
    <switches><switch id="S23" livedata="selected"/></switches>
    <externals></externals>
</profile>
```
Формат взят из RomRaider.jar (UserProfileImpl.buildXml / UserProfileHandler); файл в ISO-8859-1.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProfileItem:
    id: str
    units: str | None = None
    graph: bool = False


def load_profile(path: str | Path) -> list[ProfileItem]:
    """Параметры и переключатели профиля, отмеченные для живых данных или графика."""
    root = ET.parse(path).getroot()
    items: list[ProfileItem] = []
    for el in [*root.iter("parameter"), *root.iter("switch")]:
        live = el.get("livedata") == "selected"
        graph = el.get("graph") == "selected"
        if el.get("id") and (live or graph or el.get("dash") == "selected"):
            items.append(ProfileItem(el.get("id", ""), el.get("units") or None, graph))
    return items


def save_profile(path: str | Path, items: Sequence[ProfileItem]) -> None:
    lines = ['<?xml version="1.0" encoding="ISO-8859-1"?>', '<profile protocol="SSM">', "    <parameters>"]
    switches = []
    for item in items:
        if item.id.startswith("S"):
            switches.append(item)
            continue
        graph = ' graph="selected"' if item.graph else ""
        units = f' units="{_escape(item.units)}"' if item.units else ""
        lines.append(f'        <parameter id="{_escape(item.id)}" livedata="selected"{graph}{units}/>')
    lines += ["    </parameters>", "    <switches>"]
    for item in switches:
        graph = ' graph="selected"' if item.graph else ""
        lines.append(f'        <switch id="{_escape(item.id)}" livedata="selected"{graph}/>')
    lines += ["    </switches>", "    <externals>", "    </externals>", "</profile>", ""]
    Path(path).write_text("\r\n".join(lines), encoding="iso-8859-1", errors="replace")


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


@dataclass(frozen=True)
class ProfileEntry:
    """Профиль в списке быстрого выбора: имя — имя файла без .xml."""

    name: str
    path: Path
    source: str  # «extra» — из сборки, «user» — своя папка


def list_profiles(folders: Sequence[tuple[str | Path, str]]) -> list[ProfileEntry]:
    """Профили из папок [(папка, подпись)] по алфавиту внутри каждой; несуществующие — пропуск."""
    entries: list[ProfileEntry] = []
    for folder, source in folders:
        root = Path(folder)
        if root.is_dir():
            files = sorted(root.glob("*.xml"), key=lambda p: p.stem.lower())
            entries += [ProfileEntry(path.stem, path, source) for path in files]
    return entries
