from __future__ import annotations

from PyQt6.QtCore import QLocale, QSettings, QSize
from PyQt6.QtGui import QKeySequence

from src.core.units import UnitSystem
from src.i18n import Language
from src.ui.key_bindings import DEFAULT_KEYS, MODIFIER_KEYS, Action

SETTINGS_ORG = "subaru_ecu_editor"
SETTINGS_APP = "settings"

_KEY_PREFIX = "keys/"
_LAST_ROM = "session/last_rom"
_GRAPH_ENABLED = "graph/enabled"
_GRAPH_SIZE = "graph/size"
_UNITS = "view/units"
_LANGUAGE = "ui/language"
_AI_SERVER = "ai/mcp_enabled"
_AI_PORT = "ai/port"
# Старый ключ (положение + размер): положение больше не сохраняется — окно встаёт под картой
_GRAPH_GEOMETRY_OLD = "graph/geometry"


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

    # --- Единицы на экране ---

    def unit_system(self) -> UnitSystem:
        value = self._settings.value(_UNITS, UnitSystem.DEFINITIONS.value)
        try:
            return UnitSystem(str(value))
        except ValueError:
            return UnitSystem.DEFINITIONS

    def set_unit_system(self, system: UnitSystem) -> None:
        self._settings.setValue(_UNITS, system.value)
        self._settings.sync()

    # --- Язык интерфейса ---

    def language(self) -> Language:
        """Сохранённый язык; по умолчанию — язык системы (русский или английский)."""
        value = self._settings.value(_LANGUAGE)
        try:
            return Language(str(value))
        except ValueError:
            return Language.RU if QLocale.system().language() == QLocale.Language.Russian else Language.EN

    def set_language(self, language: Language) -> None:
        self._settings.setValue(_LANGUAGE, language.value)
        self._settings.sync()

    # --- MCP-сервер для ИИ ---

    def ai_server_enabled(self) -> bool:
        return self._settings.value(_AI_SERVER, False) not in (False, "false", "0", 0)

    def set_ai_server_enabled(self, enabled: bool) -> None:
        self._settings.setValue(_AI_SERVER, enabled)
        self._settings.sync()

    def ai_port(self, default: int) -> int:
        try:
            port = int(str(self._settings.value(_AI_PORT, default)))
        except ValueError:
            return default
        return port if 1024 <= port <= 65535 else default

    # --- Окно графика ---

    def graph_enabled(self) -> bool:
        value = self._settings.value(_GRAPH_ENABLED, True)
        # INI хранит bool строкой
        return value not in (False, "false", "0", 0)

    def set_graph_enabled(self, enabled: bool) -> None:
        self._settings.setValue(_GRAPH_ENABLED, enabled)
        self._settings.sync()

    def graph_size(self) -> QSize | None:
        value = self._settings.value(_GRAPH_SIZE)
        return value if isinstance(value, QSize) and value.isValid() else None

    def set_graph_size(self, size: QSize) -> None:
        self._settings.setValue(_GRAPH_SIZE, size)
        self._settings.remove(_GRAPH_GEOMETRY_OLD)
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
