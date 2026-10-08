"""Полнота перевода: у каждой tr("…") в коде есть английская строка с теми же подстановками."""

import ast
import string
from pathlib import Path

from PyQt6.QtCore import QSettings

from src.core.transfer import ResultStatus
from src.i18n import Language, plural, set_language, tr
from src.locale.en import EN
from src.ui.app_settings import AppSettings

ROOT = Path(__file__).resolve().parents[1] / "src"


def _tr_literals() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("tr", "N_")
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                found[node.args[0].value] = f"{path.relative_to(ROOT.parent)}:{node.lineno}"
    return found


def _fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_every_string_translated():
    missing = {text: where for text, where in _tr_literals().items() if text not in EN}
    assert not missing, "нет перевода:\n" + "\n".join(f"{w}: {t!r}" for t, w in missing.items())


def test_placeholders_match():
    bad = [ru for ru, en in EN.items() if _fields(ru) != _fields(en)]
    assert not bad, f"подстановки не совпадают: {bad}"


def test_no_unused_translations():
    unused = sorted(set(EN) - set(_tr_literals()))
    assert not unused, f"переводы без строки в коде: {unused}"


def test_switch_language():
    set_language(Language.EN)
    try:
        assert tr("Сохранение") == EN["Сохранение"]
    finally:
        set_language(Language.RU)
    assert tr("Сохранение") == "Сохранение"


def test_plural():
    forms = ("таблица", "таблицы", "таблиц"), ("table", "tables")
    assert [plural(n, *forms) for n in (1, 3, 5, 11, 21)] == ["таблица", "таблицы", "таблиц", "таблиц", "таблица"]
    set_language(Language.EN)
    try:
        assert [plural(n, *forms) for n in (1, 2)] == ["table", "tables"]
        # Подписи из enum и форматирование — тоже на выбранном языке
        assert ResultStatus.SKIPPED.title == "skipped"
        assert tr("Единицы: {units}", units="bar") == "Units: bar"
    finally:
        set_language(Language.RU)


def test_language_setting(tmp_path):
    settings = AppSettings(QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    assert settings.language() in (Language.RU, Language.EN)  # по умолчанию — язык системы
    settings.set_language(Language.EN)
    assert settings.language() is Language.EN
