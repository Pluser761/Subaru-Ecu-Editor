"""Общие фикстуры. Прошивки лежат в ecu/ и не коммитятся (.gitignore): без них
интеграционные тесты пропускаются."""

from __future__ import annotations

import os
from pathlib import Path

# Модели и виджеты Qt в тестах — без окна на экране
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from src.core.parser import EcuParser
from src.core.xml_reader import EcuDefsRepository

ROOT = Path(__file__).resolve().parents[1]
DEFS = ROOT / "definitions" / "ecu_defs.xml"
JDM_ROM = ROOT / "ecu" / "AZ1G301F-2008-JDM-Subaru-Impreza-STI.hex"
EDM_ROM = ROOT / "ecu" / "A4TE300D-2005-EDM-Subaru-Impreza-WRX-MT-AT.hex"


@pytest.fixture(scope="session")
def repo() -> EcuDefsRepository:
    # Разбор всего ecu_defs.xml занимает секунды — один раз на прогон
    return EcuDefsRepository.load(DEFS)


def _open(path: Path, repo: EcuDefsRepository) -> EcuParser:
    if not path.is_file():
        pytest.skip(f"нет прошивки {path.name} в ecu/")
    return EcuParser.open_rom(path, DEFS, defs_repo=repo)


@pytest.fixture
def jdm(repo: EcuDefsRepository) -> EcuParser:
    """Свежий образ JDM STI 2008 (правки в тесте не видны другим тестам)."""
    return _open(JDM_ROM, repo)


@pytest.fixture
def edm(repo: EcuDefsRepository) -> EcuParser:
    return _open(EDM_ROM, repo)


@pytest.fixture(scope="session")
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])
