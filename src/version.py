"""Версия программы. Источник истины — git-теги vX.Y.Z (SemVer).

В репозитории здесь "dev": номер ставит сборка релиза (GitHub Actions → Release), записывая
его в __version__ перед PyInstaller. Из исходников версия берётся из `git describe`
(«0.3.1-4-gabc1234» — четыре коммита после v0.3.1). Без Qt и зависимостей: модуль
используется и скриптом релиза.
"""

from __future__ import annotations

import re
import subprocess
from functools import cache
from pathlib import Path

__version__ = "dev"

APP_NAME = "Subaru ECU Editor"
# Первый релиз, если тегов ещё нет
FIRST_VERSION = "0.1.0"
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


@cache
def version() -> str:
    """Номер для показа: из сборки, иначе из git, иначе "dev"."""
    if __version__ != "dev":
        return __version__
    try:
        described = subprocess.run(
            ["git", "describe", "--tags", "--match", "v[0-9]*", "--dirty"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "dev"
    return described.removeprefix("v") or "dev"


def parse(text: str) -> tuple[int, int, int]:
    match = _SEMVER.match(text.strip())
    if not match:
        raise ValueError(f"Not a version X.Y.Z: {text!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def next_version(last_tag: str | None, bump: str, explicit: str = "") -> str:
    """Номер следующего релиза: явный (explicit) или last_tag, увеличенный по bump
    (major / minor / patch). Нет тегов — FIRST_VERSION. Новый номер должен быть больше старого."""
    if explicit.strip():
        new = parse(explicit)
    elif not last_tag:
        new = parse(FIRST_VERSION)
    else:
        major, minor, patch = parse(last_tag)
        bumps = {
            "major": (major + 1, 0, 0),
            "minor": (major, minor + 1, 0),
            "patch": (major, minor, patch + 1),
        }
        if bump not in bumps:
            raise ValueError(f"bump must be major, minor or patch, got {bump!r}")
        new = bumps[bump]
    if last_tag and new <= parse(last_tag):
        raise ValueError(f"Version {'.'.join(map(str, new))} is not greater than {last_tag}")
    return ".".join(map(str, new))
