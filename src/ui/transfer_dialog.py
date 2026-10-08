from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)

from src.i18n import tr
from src.core.transfer import TransferPlan

_NAME_ROLE = int(Qt.ItemDataRole.UserRole)
_MAX_TOOLTIP_NAMES = 60


def _children(item: QTreeWidgetItem) -> list[QTreeWidgetItem]:
    return [c for i in range(item.childCount()) if (c := item.child(i)) is not None]


def _shape(shape: tuple[int, int]) -> str:
    return f"{shape[0]}×{shape[1]}"


class TransferDialog(QDialog):
    """Выбор таблиц донора B для переноса в открытую прошивку A."""

    def __init__(self, plan: TransferPlan, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Перенос карт"))
        self.resize(900, 650)
        self._plan = plan

        header = QLabel(
            tr(
                "Донор <b>B: {source}</b> → цель <b>A: {target}</b> (открыта в редакторе). "
                "Размерность таблиц остаётся как в A; при разной размерности значения B интерполируются.",
                source=plan.source_id,
                target=plan.target_id,
            )
        )
        header.setWordWrap(True)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText(tr("Фильтр по названию или категории"))
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter)

        check_all = QPushButton(tr("Выбрать все"))
        check_all.setToolTip(tr("Отметить все доступные таблицы, видимые с учётом фильтра"))
        check_all.clicked.connect(lambda: self._set_all(True))
        uncheck_all = QPushButton(tr("Снять все"))
        uncheck_all.setToolTip(tr("Снять отметку со всех таблиц, видимых с учётом фильтра"))
        uncheck_all.clicked.connect(lambda: self._set_all(False))

        toolbar = QHBoxLayout()
        toolbar.addWidget(self._filter, stretch=1)
        toolbar.addWidget(check_all)
        toolbar.addWidget(uncheck_all)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels([tr("Таблица"), "B → A", tr("Действие")])
        tree_header = self._tree.header()
        if tree_header is not None:
            tree_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            tree_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            tree_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            tree_header.setStretchLastSection(False)
        self._populate()
        self._tree.itemChanged.connect(lambda *_: self._update_counter())

        self._counter = QLabel()
        only = QLabel(
            tr(
                "Только в B: {source} · только в A: {target} (переносить нечего — наведите, чтобы увидеть список)",
                source=len(plan.only_source),
                target=len(plan.only_target),
            )
        )
        only.setStyleSheet("color: #666;")
        only.setToolTip(self._names_tooltip(plan))

        self._buttons = QDialogButtonBox()
        ok = self._buttons.addButton(tr("Перенести"), QDialogButtonBox.ButtonRole.AcceptRole)
        assert ok is not None
        self._ok = ok
        self._buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        footer = QHBoxLayout()
        footer.addWidget(self._counter)
        footer.addStretch(1)
        footer.addWidget(self._buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addLayout(toolbar)
        layout.addWidget(self._tree, stretch=1)
        layout.addWidget(only)
        layout.addLayout(footer)
        self._update_counter()

    @staticmethod
    def _names_tooltip(plan: TransferPlan) -> str:
        def block(title: str, names: list[str]) -> str:
            shown = names[:_MAX_TOOLTIP_NAMES]
            more = "\n" + tr("… ещё {n}", n=len(names) - len(shown)) if len(names) > len(shown) else ""
            return f"{title}:\n" + ("\n".join(shown) or "—") + more

        return block(tr("Только в B"), plan.only_source) + "\n\n" + block(tr("Только в A"), plan.only_target)

    def _populate(self) -> None:
        categories: dict[str, QTreeWidgetItem] = {}
        for candidate in self._plan.candidates:
            parent = categories.get(candidate.category)
            if parent is None:
                parent = QTreeWidgetItem([candidate.category])
                parent.setFlags(
                    parent.flags()
                    | Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsAutoTristate
                )
                parent.setCheckState(0, Qt.CheckState.Unchecked)
                self._tree.addTopLevelItem(parent)
                categories[candidate.category] = parent

            item = QTreeWidgetItem(
                [
                    candidate.name,
                    f"{_shape(candidate.source_shape)} → {_shape(candidate.target_shape)}",
                    candidate.action,
                ]
            )
            item.setData(0, _NAME_ROLE, candidate.name)
            if candidate.blocked:
                # Выбрать нельзя: причина — в колонке «Действие» и в подсказке
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                item.setToolTip(0, candidate.blocked)
                item.setToolTip(2, candidate.blocked)
            else:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Unchecked)
            parent.addChild(item)

    def _leaf_items(self) -> list[QTreeWidgetItem]:
        items: list[QTreeWidgetItem] = []
        iterator = QTreeWidgetItemIterator(self._tree)
        while (item := iterator.value()) is not None:
            if item.data(0, _NAME_ROLE):
                items.append(item)
            iterator += 1
        return items

    def _set_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self._tree.blockSignals(True)
        try:
            for item in self._leaf_items():
                if not item.isHidden() and item.flags() & Qt.ItemFlag.ItemIsEnabled:
                    item.setCheckState(0, state)
        finally:
            self._tree.blockSignals(False)
        # Автотристейт категорий пересчитывается по сигналам — обновим вручную
        for i in range(self._tree.topLevelItemCount()):
            self._refresh_category(self._tree.topLevelItem(i))
        self._update_counter()

    @staticmethod
    def _refresh_category(category: QTreeWidgetItem | None) -> None:
        if category is None:
            return
        states = {
            child.checkState(0)
            for child in _children(category)
            if child.flags() & Qt.ItemFlag.ItemIsEnabled
        }
        if states == {Qt.CheckState.Checked}:
            category.setCheckState(0, Qt.CheckState.Checked)
        elif states <= {Qt.CheckState.Unchecked}:
            category.setCheckState(0, Qt.CheckState.Unchecked)
        else:
            category.setCheckState(0, Qt.CheckState.PartiallyChecked)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self._tree.topLevelItemCount()):
            category = self._tree.topLevelItem(i)
            if category is None:
                continue
            category_match = needle in category.text(0).lower()
            visible = 0
            for child in _children(category):
                show = not needle or category_match or needle in child.text(0).lower()
                child.setHidden(not show)
                visible += show
            category.setHidden(visible == 0)
            category.setExpanded(bool(needle))

    def _update_counter(self) -> None:
        leaves = [i for i in self._leaf_items() if i.flags() & Qt.ItemFlag.ItemIsEnabled]
        checked = sum(1 for i in leaves if i.checkState(0) == Qt.CheckState.Checked)
        self._counter.setText(tr("Выбрано: {checked} из {total}", checked=checked, total=len(leaves)))
        self._ok.setEnabled(checked > 0)

    def selected_names(self) -> list[str]:
        """Выбранные таблицы в порядке списка (порядок важен для общих осей)."""
        return [
            str(item.data(0, _NAME_ROLE))
            for item in self._leaf_items()
            if item.checkState(0) == Qt.CheckState.Checked
        ]


class TransferReportDialog(QDialog):
    """Отчёт о переносе (Markdown) + кнопка открыть сохранённый файл."""

    def __init__(self, markdown: str, path: Path | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Отчёт о переносе"))
        self.resize(1000, 700)

        view = QTextBrowser()
        view.setMarkdown(markdown)

        where = QLabel(tr("Сохранён: {path}", path=path) if path else tr("Отчёт не удалось сохранить в файл"))
        where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        where.setStyleSheet("color: #666;")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        if path is not None:
            open_button = buttons.addButton(tr("Открыть файл"), QDialogButtonBox.ButtonRole.ActionRole)
            if open_button is not None:
                open_button.clicked.connect(
                    lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
                )

        layout = QVBoxLayout(self)
        layout.addWidget(view, stretch=1)
        layout.addWidget(where)
        layout.addWidget(buttons)
