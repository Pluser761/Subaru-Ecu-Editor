from __future__ import annotations

import sys
from enum import Enum

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent, QKeySequence

# Физическая клавиша Control: на macOS Qt отдаёт её как Meta (Control ↔ Command)
PHYSICAL_CONTROL = (
    Qt.KeyboardModifier.MetaModifier if sys.platform == "darwin" else Qt.KeyboardModifier.ControlModifier
)

# Одна физическая клавиша с Shift и без даёт разные коды — считаем их одной
_KEY_ALIASES: dict[int, set[int]] = {
    Qt.Key.Key_Equal.value: {Qt.Key.Key_Plus.value},
    Qt.Key.Key_Plus.value: {Qt.Key.Key_Equal.value},
    Qt.Key.Key_Minus.value: {Qt.Key.Key_Underscore.value},
    Qt.Key.Key_Underscore.value: {Qt.Key.Key_Minus.value},
}

# Клавиши-модификаторы сами по себе не назначаются
MODIFIER_KEYS = frozenset(
    k.value
    for k in (
        Qt.Key.Key_Shift,
        Qt.Key.Key_Control,
        Qt.Key.Key_Meta,
        Qt.Key.Key_Alt,
        Qt.Key.Key_AltGr,
        Qt.Key.Key_CapsLock,
        Qt.Key.Key_unknown,
    )
)


class Action(Enum):
    INCREASE = "increase"
    DECREASE = "decrease"


ACTION_TITLES = {
    Action.INCREASE: "Увеличить значение",
    Action.DECREASE: "Уменьшить значение",
}

# «+» и «−» основной клавиатуры без Shift
DEFAULT_KEYS: dict[Action, int] = {
    Action.INCREASE: Qt.Key.Key_Equal.value,
    Action.DECREASE: Qt.Key.Key_Minus.value,
}


def key_name(key: int) -> str:
    return QKeySequence(Qt.Key(key)).toString(QKeySequence.SequenceFormat.NativeText)


class KeyBindings(QObject):
    """Привязки клавиш к действиям редактора. Пока живут только в памяти процесса."""

    changed = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._keys: dict[Action, int] = dict(DEFAULT_KEYS)

    def key(self, action: Action) -> int:
        return self._keys[action]

    def as_dict(self) -> dict[Action, int]:
        return dict(self._keys)

    def set_keys(self, keys: dict[Action, int]) -> None:
        if len(set(keys.values())) != len(keys):
            raise ValueError("Одна клавиша назначена на несколько действий")
        if keys != self._keys:
            self._keys = dict(keys)
            self.changed.emit()

    def action_for(self, event: QKeyEvent) -> Action | None:
        """Действие для нажатой клавиши (модификаторы не учитываются)."""
        pressed = event.key()
        for action, key in self._keys.items():
            if pressed == key or pressed in _KEY_ALIASES.get(key, set()):
                return action
        return None


def is_fine_modifier(event: QKeyEvent) -> bool:
    """Зажат ли физический Control — тогда применяется мелкий шаг."""
    return bool(event.modifiers() & PHYSICAL_CONTROL)
