"""Контроллеры подсистем редактора: сохранение, перенос карт, переключатели.

Вынесены из MainPresenter, чтобы он оставался связкой «событие окна → сценарий сессии».
Контроллеры получают окно, сессию (через callable — сессия меняется при открытии ROM) и,
где нужно, ссылки на методы презентера, чтобы не дублировать их логику.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from src.ai.notes import NoteKey, NotesStore
from src.core.models import CalibrationTable, Checkpoint
from src.core.parser import EcuParser
from src.core.session import RomSession
from src.core.transfer import check_compatibility, plan_transfer, render_report, save_report
from src.core.xml_reader import EcuDefsRepository
from src.i18n import tr
from src.ui.app_settings import AppSettings
from src.ui.main_window import MainWindow
from src.ui.switches_window import SwitchesWindow


_VERSION_SUFFIX = re.compile(r"[_-][vV](\d+)$")


def next_save_stem(stem: str) -> str:
    """Имя для «Save As»: `…_v14`/`…-v14` → `…_v15`/`…-v15` (ширина номера сохраняется), иначе `…-edited`."""
    match = _VERSION_SUFFIX.search(stem)
    if match:
        digits = match.group(1)
        number = str(int(digits) + 1).zfill(len(digits))
        return f"{stem[:match.start(1)]}{number}"
    return stem if stem.endswith("-edited") else f"{stem}-edited"


class SaveController:
    """Сохранение ROM: путь сессии, Save As, подтверждение отмены несохранённых правок."""

    def __init__(
        self,
        window: MainWindow,
        session: Callable[[], RomSession],
        settings: AppSettings | None,
        notes: NotesStore | None,
    ) -> None:
        self._window = window
        self._session = session
        self._settings = settings
        self._notes = notes
        # Куда пишет Save; None — ещё не сохраняли в этой сессии: первый Save спросит путь,
        # чтобы случайно не перезаписать стоковую прошивку
        self._save_path: str | None = None

    @property
    def _parser(self) -> EcuParser:
        return self._session().parser

    def _note_key(self) -> NoteKey | None:
        return NoteKey.for_rom(self._parser.file_path, self._parser.xmlid)

    def reset_path(self) -> None:
        """Новый ROM — первый Save снова спросит путь."""
        self._save_path = None

    def save(self) -> bool:
        """Сохранить в файл этой сессии; в первый раз — спросить путь. True — сохранено."""
        if self._save_path is None:
            return self.save_as()
        return self._save_to(self._save_path)

    def save_as(self) -> bool:
        if not self._can_save():
            return False
        current = Path(self._parser.file_path or "rom.hex")
        path = self._window.ask_save_rom_path(str(current.with_name(f"{next_save_stem(current.stem)}{current.suffix}")))
        if not path:
            return False
        return self._save_to(path)

    def _can_save(self) -> bool:
        if not self._parser.file_path:
            self._window.show_info(tr("Сохранение"), tr("Сначала откройте ROM."))
            return False
        blocker = self._session().save_blocker
        if blocker:
            self._window.set_error(blocker)
            self._window.show_info(tr("Сохранение"), blocker)
            return False
        return True

    def _save_to(self, path: str) -> bool:
        if not self._can_save():
            return False
        old_key = self._note_key()
        try:
            fixed = self._session().save(path)
        except (OSError, ValueError) as exc:
            self._window.set_error(tr("Не сохранено: {error}", error=exc))
            self._window.show_info(tr("Сохранение"), tr("Не сохранено: {error}", error=exc))
            return False
        self._save_path = path
        # Заметки ИИ привязаны к имени файла — новое имя получает копию
        new_key = self._note_key()
        if self._notes is not None and old_key is not None and new_key is not None:
            try:
                self._notes.copy(old_key, new_key)
            except OSError as exc:
                self._window.set_error(tr("Заметки не скопированы: {error}", error=exc))
        self._window.set_rom_path(path, modified=False)
        definition = self._parser.definition
        self._window.editor_view.set_rom_info(path, definition.romid if definition else None)
        if self._settings is not None:
            self._settings.set_last_rom_path(path)
        self._window.set_status(
            tr("Сохранено: {name} · контрольных сумм пересчитано: {fixed}", name=Path(path).name, fixed=fixed)
        )
        return True

    def confirm_discard(self) -> bool:
        """Можно ли бросить текущий образ (закрытие, открытие другого ROM): нет правок, их
        сохранили или пользователь отказался от них."""
        if not self._session().dirty:
            return True
        answer = self._window.ask_unsaved_changes()
        if answer == "save":
            return self.save()
        return answer == "discard"


class TransferController:
    """Перенос выбранных карт из донорской прошивки B в открытую A."""

    def __init__(
        self,
        window: MainWindow,
        session: Callable[[], RomSession],
        defs_path: Path,
        ensure_defs: Callable[[], EcuDefsRepository],
        display: Callable[[str], tuple[CalibrationTable, Checkpoint]],
        check_changed: Callable[..., None],
    ) -> None:
        self._window = window
        self._session = session
        self._defs_path = defs_path
        self._ensure_defs = ensure_defs
        self._display = display
        self._check_changed = check_changed

    @property
    def _parser(self) -> EcuParser:
        return self._session().parser

    def run(self) -> None:
        """Перенос выбранных карт из донорской прошивки B в открытую A."""
        if not self._parser.file_path or self._parser.definition is None:
            self._window.show_info(tr("Перенос карт"), tr("Сначала откройте целевую прошивку (A)."))
            return
        path = self._window.ask_donor_rom_path()
        if not path:
            return
        try:
            donor = EcuParser.open_rom(path, self._defs_path, defs_repo=self._ensure_defs())
        except Exception as exc:  # noqa: BLE001
            self._window.set_error(tr("Донор (B): {error}", error=exc))
            return
        reason = check_compatibility(donor, self._parser)
        if reason:
            self._window.set_error(reason)
            self._window.show_info(tr("Перенос карт"), reason)
            return

        plan = plan_transfer(donor, self._parser)
        names = self._window.ask_transfer_selection(plan)
        if not names:
            return

        editor = self._window.editor_view
        try:
            report = self._session().transfer(donor, names, plan)
        except Exception as exc:  # noqa: BLE001
            self._window.set_error(tr("Перенос не выполнен: {error}", error=exc))
            return
        # Открытые окна показывают перенесённые значения
        for map_id in editor.open_maps():
            editor.reload_map(*self._display(map_id))
        self._check_changed(*report.before)

        markdown = render_report(report)
        report_file: Path | None = None
        try:
            saved = save_report(report)
        except OSError as exc:
            self._window.set_error(tr("Отчёт не сохранён: {error}", error=exc))
        else:
            report_file = saved
            self._window.set_status(
                tr(
                    "Перенесено из {source}: таблиц {n} · отчёт {report}",
                    source=report.source_id,
                    n=len(report.before),
                    report=saved.name,
                )
            )
        self._window.show_transfer_report(markdown, report_file)


class SwitchesController:
    """Окно «Ошибки и переключатели» (таблицы Switch: DTC, OBD-II…) и переключение состояний."""

    def __init__(self, window: MainWindow, session: Callable[[], RomSession]) -> None:
        self._window = window
        self._session = session
        self._switches_window: SwitchesWindow | None = None
        self._original: dict[str, str | None] = {}

    @property
    def _parser(self) -> EcuParser:
        return self._session().parser

    def reset(self) -> None:
        """Новый ROM: запомнить его состояния и перечитать уже открытое окно."""
        self._original = {s.id: s.state for s in self._parser.list_switches()}
        if self._switches_window is not None and self._switches_window.isVisible():
            self._fill()

    def show(self) -> None:
        if not self._parser.file_path:
            self._window.show_info(tr("Ошибки и переключатели"), tr("Сначала откройте ROM."))
            return
        if self._switches_window is None:
            self._switches_window = SwitchesWindow(self._window)
            self._switches_window.switch_toggled.connect(self.toggle)
            # По центру экрана, а не по центру узкого главного окна (иначе уезжает за край)
            screen = self._window.screen()
            if screen is not None:
                area = screen.availableGeometry()
                size = self._switches_window.size()
                self._switches_window.move(
                    area.x() + (area.width() - size.width()) // 2, area.y() + (area.height() - size.height()) // 2
                )
        self._fill()
        self._switches_window.show()
        self._switches_window.raise_()
        self._switches_window.activateWindow()

    def _fill(self) -> None:
        assert self._switches_window is not None
        self._switches_window.set_switches(
            self._parser.list_switches(), self._original, Path(self._parser.file_path or "").name
        )

    def toggle(self, switch_id: str, state: str) -> None:
        try:
            self._session().write_switch(switch_id, state)
        except ValueError as exc:
            self._window.set_error(tr("Не удалось переключить: {error}", error=exc))
        info = self._parser.switch_info(switch_id)
        if self._switches_window is not None:
            self._switches_window.update_switch(info)
        self._window.setWindowModified(self._session().dirty)
        label = info.code or info.name
        word = {"on": tr("включена"), "off": tr("выключена")}.get(info.state or "", "?")
        self._window.set_status(f"{label}: {word}")
