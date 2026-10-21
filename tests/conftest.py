"""Общее для всех тестов (tests/editor, tests/logger): Qt без окна на экране."""

from __future__ import annotations

import os

# Модели и виджеты Qt в тестах — без окна на экране; до первого импорта PyQt6
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])
