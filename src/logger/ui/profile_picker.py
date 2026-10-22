"""Быстрый выбор профиля логгера (Cmd/Ctrl+P): всплывающее окошко с поиском и списком.

Набрал часть имени — список сузился; ↑/↓ — выбор, Enter или щелчок — применить, Esc — закрыть.
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QFrame, QLabel, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from src.i18n import tr

PICKER_SIZE = (360, 320)
_ROLE_PATH = int(Qt.ItemDataRole.UserRole)


class ProfilePicker(QFrame):
    picked = pyqtSignal(str)  # путь к файлу профиля

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.resize(*PICKER_SIZE)
        self._search = QLineEdit()
        self._search.setPlaceholderText(tr("Профиль: начните вводить имя"))
        self._search.textChanged.connect(self._filter)
        self._search.installEventFilter(self)
        self._list = QListWidget()
        self._list.itemActivated.connect(self._activate)
        self._list.itemClicked.connect(self._activate)
        self._empty = QLabel(tr("Профилей нет. Сохраните текущий выбор: «Сохранить профиль…»."))
        self._empty.setWordWrap(True)
        self._empty.setStyleSheet("color: #777;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self._search)
        layout.addWidget(self._list, 1)
        layout.addWidget(self._empty)

    def show_profiles(self, profiles: list[tuple[str, str, str]], at: QPoint) -> None:
        """[(имя, путь, подсказка)] — показать окошко в точке at (глобальные координаты)."""
        self._list.clear()
        for name, path, tip in profiles:
            item = QListWidgetItem(name)
            item.setData(_ROLE_PATH, path)
            item.setToolTip(tip)
            self._list.addItem(item)
        self._empty.setVisible(not profiles)
        self._search.clear()
        self._filter("")
        # Высота — по числу профилей, но не больше PICKER_SIZE
        row = self._list.sizeHintForRow(0) if profiles else 0
        list_height = min(row * len(profiles) + 6, PICKER_SIZE[1] - 60)
        self._list.setFixedHeight(max(list_height, 0))
        self._list.setVisible(bool(profiles))
        self.adjustSize()
        self.resize(PICKER_SIZE[0], self.sizeHint().height())
        self.move(at)
        self.show()
        self._search.setFocus()

    def visible_names(self) -> list[str]:
        return [item.text() for item in self._items() if not item.isHidden()]

    def choose_current(self) -> None:
        item = self._list.currentItem()
        if item is not None and not item.isHidden():
            self._activate(item)

    def _items(self) -> list[QListWidgetItem]:
        return [item for i in range(self._list.count()) if (item := self._list.item(i)) is not None]

    def _filter(self, text: str) -> None:
        key = text.strip().lower()
        first: QListWidgetItem | None = None
        for item in self._items():
            hidden = bool(key) and key not in item.text().lower()
            item.setHidden(hidden)
            if not hidden and first is None:
                first = item
        self._list.setCurrentItem(first)

    def _activate(self, item: QListWidgetItem) -> None:
        self.hide()
        self.picked.emit(str(item.data(_ROLE_PATH)))

    def _step(self, delta: int) -> None:
        visible = [item for item in self._items() if not item.isHidden()]
        if not visible:
            return
        current = self._list.currentItem()
        index = visible.index(current) if current in visible else -1
        self._list.setCurrentItem(visible[max(0, min(len(visible) - 1, index + delta))])

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:
        # Стрелки и Enter из строки поиска — списку, чтобы не уводить фокус из поиска
        if obj is self._search and isinstance(event, QKeyEvent) and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                self._step(1 if key == Qt.Key.Key_Down else -1)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.choose_current()
                return True
            if key == Qt.Key.Key_Escape:
                self.hide()
                return True
        return super().eventFilter(obj, event)
