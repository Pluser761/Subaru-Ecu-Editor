"""Окно «Ошибки и переключатели»: плашки с тумблерами для таблиц Switch (DTC, OBD-II…)."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.i18n import N_, tr
from src.core.models import SwitchInfo
from src.ui.widgets.flow_layout import FlowLayout
from src.ui.widgets.toggle_switch import ToggleSwitch

CARD_WIDTH = 252
CARD_HEIGHT = 96
CATEGORY_TITLES = {
    "Diagnostic Trouble Codes": N_("Ошибки (DTC)"),
    "OBD-II": "OBD-II",
}
# Плашка: обычная / выключена / изменена с открытия файла / неизвестные байты
_CARD_STYLE = {
    "on": "background: #ffffff; border: 1px solid #dcdce0;",
    "off": "background: #fff6ec; border: 1px solid #f0c897;",
    "unknown": "background: #f2f2f7; border: 1px dashed #b8b8c0;",
}
_CHANGED_BORDER = "border: 2px solid #c05000;"
# Режимы фильтра (ключи); подписи кнопок — tr(режим)
_FILTERS = (N_("Все"), N_("Выключенные"), N_("Изменённые"))


class SwitchCard(QFrame):
    """Плашка одного переключателя: код (или имя), название, тумблер; описание — в подсказке."""

    toggled = pyqtSignal(str, bool)  # switch_id, включено

    def __init__(self, info: SwitchInfo, original: str | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setFixedSize(CARD_WIDTH, CARD_HEIGHT)
        self._id = info.id
        self._original = original
        self._info = info

        self._code = QLabel(info.code or info.name)
        self._code.setStyleSheet("font-size: 15px; font-weight: 700; color: #1c1c1e; background: transparent; border: none;")
        self._toggle = ToggleSwitch()
        self._toggle.toggled.connect(lambda on: self.toggled.emit(self._id, on))
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self._code, stretch=1)
        top.addWidget(self._toggle, alignment=Qt.AlignmentFlag.AlignTop)

        self._name = QLabel(info.name if info.code else "")
        self._name.setWordWrap(True)
        self._name.setStyleSheet("font-size: 11px; color: #3a3a3c; background: transparent; border: none;")
        self._status = QLabel()
        self._status.setStyleSheet("font-size: 10px; color: #8e8e93; background: transparent; border: none;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 8)
        layout.setSpacing(3)
        layout.addLayout(top)
        layout.addWidget(self._name, stretch=1)
        layout.addWidget(self._status)
        self.set_info(info)

    @property
    def info(self) -> SwitchInfo:
        return self._info

    @property
    def changed(self) -> bool:
        return self._info.state != self._original

    def set_info(self, info: SwitchInfo) -> None:
        self._info = info
        known = info.state in ("on", "off")
        self._toggle.setEnabled(known)
        self._toggle.set_checked_silently(info.state == "on")
        kind = info.state if info.state in ("on", "off") else "unknown"
        style = _CARD_STYLE[kind] + (_CHANGED_BORDER if self.changed else "")
        self.setStyleSheet(f"QFrame#card {{ {style} border-radius: 10px; }}")
        if not known:
            status = tr("неизвестные байты {raw}", raw=info.raw)
        elif info.state == "off":
            status = tr("выключена") if info.code else tr("выключено")
        else:
            status = tr("включена") if info.code else tr("включено")
        if self.changed and known:
            status += " · " + tr("изменено")
        self._status.setText(status)
        tip = info.name if not info.code else f"{info.code} — {info.name}"
        self.setToolTip(f"{tip}\n\n{info.description}" if info.description else tip)

    def matches(self, text: str, mode: str) -> bool:
        if mode == "Выключенные" and self._info.state != "off":
            return False
        if mode == "Изменённые" and not self.changed:
            return False
        needle = text.strip().lower()
        return not needle or needle in f"{self._info.code} {self._info.name}".lower()


class SwitchesWindow(QWidget):
    """Отдельное окно: разделы по категориям, поиск, фильтр, плашки с тумблерами."""

    # Пользователь переключил: switch_id, «on» / «off»
    switch_toggled = pyqtSignal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(tr("Ошибки и переключатели"))
        self.resize(860, 640)
        self._cards: dict[str, SwitchCard] = {}
        self._sections: list[tuple[QLabel, QWidget, list[SwitchCard]]] = []

        self._title = QLabel()
        self._title.setStyleSheet("font-size: 16px; font-weight: 700;")
        self._summary = QLabel()
        self._summary.setStyleSheet("color: #666;")
        warning = QLabel(
            tr("⚠ Выключенная ошибка не исправляет неисправность, а прячет её: ЭБУ перестаёт сообщать о проблеме.")
        )
        warning.setWordWrap(True)
        warning.setStyleSheet(
            "background: #fff4e5; border: 1px solid #f0c897; border-radius: 8px; padding: 8px; color: #7a4100;"
        )

        self._search = QLineEdit()
        self._search.setPlaceholderText(tr("Поиск: код или название (P013, O2, AVCS…)"))
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._apply_filter)
        self._filter = QButtonGroup(self)
        filters = QHBoxLayout()
        filters.setSpacing(0)
        for i, name in enumerate(_FILTERS):
            button = QPushButton(tr(name))
            button.setCheckable(True)
            button.setChecked(i == 0)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._filter.addButton(button, i)
            filters.addWidget(button)
        self._filter.idClicked.connect(lambda _id: self._apply_filter())
        tools = QHBoxLayout()
        tools.addWidget(self._search, stretch=1)
        tools.addLayout(filters)

        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(4, 4, 4, 4)
        self._content_layout.setSpacing(10)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self._content)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        layout.addWidget(self._title)
        layout.addWidget(self._summary)
        layout.addWidget(warning)
        layout.addLayout(tools)
        layout.addWidget(scroll, stretch=1)

    def set_switches(self, switches: list[SwitchInfo], original: dict[str, str | None], rom_name: str) -> None:
        """Показать переключатели прошивки; original — состояния на момент открытия файла."""
        self._title.setText(tr("Ошибки и переключатели · {rom}", rom=rom_name))
        self.setWindowTitle(tr("Ошибки и переключатели — {rom}", rom=rom_name))
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item is not None and item.widget() is not None:
                item.widget().deleteLater()  # type: ignore[union-attr]
        self._cards, self._sections = {}, []
        by_category: dict[str, list[SwitchInfo]] = {}
        for info in switches:
            by_category.setdefault(info.category, []).append(info)
        for category, items in by_category.items():
            header = QLabel(tr(CATEGORY_TITLES[category]) if category in CATEGORY_TITLES else category)
            header.setStyleSheet("font-size: 13px; font-weight: 700; color: #3a3a3c; padding-top: 4px;")
            grid = QWidget()
            flow = FlowLayout(grid, spacing=8)
            cards = []
            for info in items:
                card = SwitchCard(info, original.get(info.id))
                card.toggled.connect(lambda sid, on: self.switch_toggled.emit(sid, "on" if on else "off"))
                flow.addWidget(card)
                self._cards[info.id] = card
                cards.append(card)
            self._content_layout.addWidget(header)
            self._content_layout.addWidget(grid)
            self._sections.append((header, grid, cards))
        self._content_layout.addStretch(1)
        self._apply_filter()

    def update_switch(self, info: SwitchInfo) -> None:
        card = self._cards.get(info.id)
        if card is not None:
            card.set_info(info)
            self._apply_filter()

    def _apply_filter(self) -> None:
        text = self._search.text()
        mode = _FILTERS[max(self._filter.checkedId(), 0)]
        for header, grid, cards in self._sections:
            visible = 0
            for card in cards:
                shown = card.matches(text, mode)
                card.setVisible(shown)
                visible += shown
            header.setVisible(visible > 0)
            grid.setVisible(visible > 0)
            layout = grid.layout()
            if layout is not None:
                layout.invalidate()
        cards = list(self._cards.values())
        off = sum(c.info.state == "off" for c in cards)
        changed = sum(c.changed for c in cards)
        self._summary.setText(tr("Всего {total} · выключено {off} · изменено с открытия {changed}", total=len(cards), off=off, changed=changed))
