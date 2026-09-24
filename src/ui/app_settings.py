from __future__ import annotations

from PyQt6.QtCore import QByteArray, QSettings
from PyQt6.QtGui import QKeySequence

from src.ui.key_bindings import DEFAULT_KEYS, MODIFIER_KEYS, Action

SETTINGS_ORG = "subaru_ecu_editor"
SETTINGS_APP = "settings"

_KEY_PREFIX = "keys/"
_LAST_ROM = "session/last_rom"
_GRAPH_ENABLED = "graph/enabled"
_GRAPH_GEOMETRY = "graph/geometry"


class AppSettings:
    """Настройки приложения в INI-файле пользователя (QSettings, IniFormat).

    Где лежит файл: macOS/Linux — ~/.config/subaru_ecu_editor/settings.ini,
    Windows — %APPDATA%\\subaru_ecu_editor\\settings.ini. Путь — `file_name`.
    Клавиши хранятся читаемо («=», «-», «]»), файл можно править руками.
    """

    def __init__(self, settings: QSettings | None = None) -> None:
        self._settings = settings or QSettings(
            QSettings.Format.IniFormat,
            QSettings.Scope.UserScope,
            SETTINGS_ORG,
            SETTINGS_APP,
        )

    @property
    def file_name(self) -> str:
        return self._settings.fileName()

    # --- Клавиши ---

    def load_key_bindings(self) -> dict[Action, int]:
        """Сохранённые привязки; для отсутствующих/битых — значения по умолчанию.

        Если после чтения одна клавиша оказалась на двух действиях — все по умолчанию.
        """
        keys: dict[Action, int] = {}
        for action in Action:
            stored = self._settings.value(_KEY_PREFIX + action.value)
            keys[action] = _parse_key(stored) or DEFAULT_KEYS[action]
        if len(set(keys.values())) != len(keys):
            return dict(DEFAULT_KEYS)
        return keys

    def save_key_bindings(self, keys: dict[Action, int]) -> None:
        for action, key in keys.items():
            self._settings.setValue(_KEY_PREFIX + action.value, _format_key(key))
        self._settings.sync()

    # --- Последний открытый ROM ---

    def last_rom_path(self) -> str | None:
        value = self._settings.value(_LAST_ROM)
        return str(value) if value else None

    def set_last_rom_path(self, path: str | None) -> None:
        if path:
            self._settings.setValue(_LAST_ROM, path)
        else:
            self._settings.remove(_LAST_ROM)
        self._settings.sync()

    # --- Окно графика ---

    def graph_enabled(self) -> bool:
        value = self._settings.value(_GRAPH_ENABLED, True)
        # INI хранит bool строкой
        return value not in (False, "false", "0", 0)

    def set_graph_enabled(self, enabled: bool) -> None:
        self._settings.setValue(_GRAPH_ENABLED, enabled)
        self._settings.sync()

    def graph_geometry(self) -> QByteArray | None:
        value = self._settings.value(_GRAPH_GEOMETRY)
        return value if isinstance(value, QByteArray) and not value.isEmpty() else None

    def set_graph_geometry(self, geometry: QByteArray) -> None:
        self._settings.setValue(_GRAPH_GEOMETRY, geometry)
        self._settings.sync()


def _format_key(key: int) -> str:
    return QKeySequence(key).toString(QKeySequence.SequenceFormat.PortableText)


def _parse_key(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    seq = QKeySequence.fromString(value, QKeySequence.SequenceFormat.PortableText)
    if seq.isEmpty():
        return None
    key = seq[0].key().value
    return None if key in MODIFIER_KEYS else key
