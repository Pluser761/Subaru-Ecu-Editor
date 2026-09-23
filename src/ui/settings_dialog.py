from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.ui.key_bindings import ACTION_TITLES, DEFAULT_KEYS, Action, KeyBindings, key_name
from src.ui.widgets.key_capture import KeyCaptureButton


class SettingsDialog(QDialog):
    """Окно настроек. Сейчас — привязка клавиш; изменения применяются по OK."""

    def __init__(self, bindings: KeyBindings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Настройки")
        self._bindings = bindings

        self._buttons: dict[Action, KeyCaptureButton] = {}
        form = QFormLayout()
        for action in Action:
            button = KeyCaptureButton(bindings.key(action))
            button.key_captured.connect(lambda _key: self._validate())
            self._buttons[action] = button
            form.addRow(ACTION_TITLES[action], button)

        hint = QLabel(
            "Кликните по кнопке и нажмите клавишу. Модификаторы не записываются: "
            "без них применяется крупный шаг, с зажатым Control — мелкий."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666;")

        reset = QPushButton("По умолчанию")
        reset.clicked.connect(self._reset_defaults)

        keys_box = QGroupBox("Клавиши")
        keys_layout = QVBoxLayout(keys_box)
        keys_layout.addLayout(form)
        keys_layout.addWidget(hint)
        keys_layout.addWidget(reset)

        self._error = QLabel()
        self._error.setStyleSheet("color: #c0392b;")
        self._error.hide()

        self._box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._box.accepted.connect(self.accept)
        self._box.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(keys_box)
        layout.addWidget(self._error)
        layout.addWidget(self._box)

    def pending_keys(self) -> dict[Action, int]:
        return {action: button.key for action, button in self._buttons.items()}

    def _reset_defaults(self) -> None:
        for action, button in self._buttons.items():
            button.set_key(DEFAULT_KEYS[action])
        self._validate()

    def _validate(self) -> bool:
        keys = self.pending_keys()
        used: dict[int, Action] = {}
        conflict: tuple[Action, Action, int] | None = None
        for action, key in keys.items():
            if key in used:
                conflict = (used[key], action, key)
                break
            used[key] = action

        ok_button = self._box.button(QDialogButtonBox.StandardButton.Ok)
        if conflict is None:
            self._error.hide()
            if ok_button is not None:
                ok_button.setEnabled(True)
            return True
        first, second, key = conflict
        self._error.setText(
            f"Клавиша «{key_name(key)}» назначена и на «{ACTION_TITLES[first]}», "
            f"и на «{ACTION_TITLES[second]}»"
        )
        self._error.show()
        if ok_button is not None:
            ok_button.setEnabled(False)
        return False

    def accept(self) -> None:
        if not self._validate():
            return
        self._bindings.set_keys(self.pending_keys())
        super().accept()
