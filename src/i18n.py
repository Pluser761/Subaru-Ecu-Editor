"""Язык интерфейса: русский (язык исходных строк) или английский (словарь src/locale/en.py).

Без Qt — пользуются и ядро (отчёт переноса, причины отказа сохранения), и интерфейс.
Строка пишется по-русски и оборачивается в tr(): tr("Карта не открыта: {name}", name=…).
Тест tests/test_i18n.py проверяет, что у каждой tr("…") в коде есть перевод.
Описания карт из definitions не переводятся — они на английском.
"""

from __future__ import annotations

from enum import Enum


class Language(Enum):
    RU = "ru"
    EN = "en"


_language = Language.RU


def set_language(language: Language) -> None:
    global _language
    _language = language


def language() -> Language:
    return _language


def plural(n: int, ru: tuple[str, str, str], en: tuple[str, str]) -> str:
    """Слово при числе n на языке интерфейса: ru — (1, 2–4, 5+) «таблица/таблицы/таблиц»,
    en — (1, много) «table/tables»."""
    if _language is Language.EN:
        return en[0] if n == 1 else en[1]
    if n % 10 == 1 and n % 100 != 11:
        return ru[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return ru[1]
    return ru[2]


def N_(text: str) -> str:
    """Пометить строку для перевода, не переводя её сейчас (константы уровня модуля
    вычисляются до выбора языка); переводится tr(значение) в момент показа."""
    return text


def tr(text: str, **kwargs: object) -> str:
    """Строка на языке интерфейса; {имя} подставляются из kwargs. Нет перевода — как есть."""
    if _language is Language.EN:
        from src.locale.en import EN

        text = EN.get(text, text)
    return text.format(**kwargs) if kwargs else text
