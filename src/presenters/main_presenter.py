from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer

from src.core.parser import EcuParser
from src.core.session import RomSession
from src.core.transfer import check_compatibility, plan_transfer, render_report, save_report
from src.core.xml_reader import EcuDefsRepository
from src.ui.app_settings import AppSettings
from src.ui.main_window import MainWindow
from src.ui.map.cells import describe_shape
from src.ui.map.window import MapWindow

DEFAULT_DEFS = Path(__file__).resolve().parents[2] / "definitions" / "ecu_defs.xml"


class MainPresenter:
    """Связывает окна с сессией ROM: событие окна → сценарий RomSession → показать результат."""

    def __init__(
        self,
        window: MainWindow,
        parser: EcuParser | None = None,
        *,
        defs_path: str | Path | None = None,
        settings: AppSettings | None = None,
    ) -> None:
        self._window = window
        # None — без сохранения настроек (тесты, встраивание)
        self._settings = settings
        self._session = RomSession(parser or EcuParser())
        self._defs_path = Path(defs_path) if defs_path else DEFAULT_DEFS
        self._defs_repo: EcuDefsRepository | None = None
        # Карты, ждущие перепроверки «изменена ли» (отметка в дереве) — отложенно, пачкой
        self._to_check: set[str] = set()
        # Куда пишет Save; None — ещё не сохраняли в этой сессии: первый Save спросит путь,
        # чтобы случайно не перезаписать стоковую прошивку
        self._save_path: str | None = None

        window.on_open_rom = self.open_rom
        window.on_save_rom = self.save_rom
        window.on_save_rom_as = self.save_rom_as
        window.on_close_requested = self._confirm_discard
        window.on_transfer_maps = self.transfer_maps
        window.on_show_editor = window.show_editor
        window.on_show_tools = window.show_tools

        editor = window.editor_view
        editor.map_selected.connect(self.select_map)
        editor.map_opened.connect(self._connect_map_window)
        editor.operation_info.connect(self.on_operation_info)

        if settings is not None:
            window.key_bindings.set_keys(settings.load_key_bindings())
            window.key_bindings.changed.connect(
                lambda: settings.save_key_bindings(window.key_bindings.as_dict())
            )
            graph = window.editor_view.graph_window
            size = settings.graph_size()
            if size is not None:
                graph.resize(size)
            window.graph_action.setChecked(settings.graph_enabled())
            window.graph_action.toggled.connect(settings.set_graph_enabled)
            window.on_close = lambda: settings.set_graph_size(graph.size())

        self._window.editor_view.set_maps([])
        self._window.set_status("Откройте ROM (File → Open ROM)")

    def restore_last_rom(self) -> None:
        """Открыть ROM, который был открыт в прошлый раз (checkpoint не восстанавливаются)."""
        if self._settings is None:
            return
        path = self._settings.last_rom_path()
        if not path:
            return
        if not Path(path).is_file():
            # Файл переместили/удалили — забываем, чтобы не спотыкаться при каждом запуске
            self._settings.set_last_rom_path(None)
            self._window.set_error(f"Прошлая прошивка не найдена: {path}")
            return
        self._open_rom_path(path)

    def _ensure_defs(self) -> EcuDefsRepository:
        if self._defs_repo is None:
            if not self._defs_path.exists():
                raise FileNotFoundError(f"Definitions not found: {self._defs_path}")
            self._window.set_status(f"Загрузка definitions: {self._defs_path.name}…")
            self._defs_repo = EcuDefsRepository.load(self._defs_path)
        return self._defs_repo

    @property
    def _parser(self) -> EcuParser:
        return self._session.parser

    def _load_map_list(self) -> None:
        maps = self._parser.list_maps()
        self._window.editor_view.set_maps(maps)
        xmlid = self._parser.xmlid or "?"
        self._window.set_status(f"{xmlid}: карт {len(maps)}")

    def open_rom(self) -> None:
        if not self._confirm_discard():
            return
        path = self._window.ask_open_rom_path()
        if not path:
            return
        self._open_rom_path(path)

    def _open_rom_path(self, path: str) -> bool:
        try:
            repo = self._ensure_defs()
            parser = EcuParser.open_rom(path, self._defs_path, defs_repo=repo)
        except FileNotFoundError as exc:
            self._window.set_error(str(exc))
            return False
        except LookupError as exc:
            self._window.set_error(str(exc))
            return False
        except Exception as exc:  # noqa: BLE001 — показать в статусбаре
            self._window.set_error(f"Ошибка открытия ROM: {exc}")
            return False

        self._window.editor_view.close_all_maps()
        self._session = RomSession(parser)
        self._save_path = None
        self._to_check.clear()
        self._window.editor_view.set_changed_maps(set())
        self._window.set_rom_path(path)
        definition = self._parser.definition
        self._window.editor_view.set_rom_info(path, definition.romid if definition else None)
        self._window.show_editor()
        # После show_editor — иначе его «Editor» затирает число карт в статусе
        self._load_map_list()
        if self._settings is not None:
            self._settings.set_last_rom_path(path)
        return True

    def transfer_maps(self) -> None:
        """Перенос выбранных карт из донорской прошивки B в открытую A."""
        if not self._parser.file_path or self._parser.definition is None:
            self._window.show_info("Перенос карт", "Сначала откройте целевую прошивку (A).")
            return
        path = self._window.ask_donor_rom_path()
        if not path:
            return
        try:
            donor = EcuParser.open_rom(path, self._defs_path, defs_repo=self._ensure_defs())
        except Exception as exc:  # noqa: BLE001
            self._window.set_error(f"Донор (B): {exc}")
            return
        reason = check_compatibility(donor, self._parser)
        if reason:
            self._window.set_error(reason)
            self._window.show_info("Перенос карт", reason)
            return

        plan = plan_transfer(donor, self._parser)
        names = self._window.ask_transfer_selection(plan)
        if not names:
            return

        editor = self._window.editor_view
        try:
            report = self._session.transfer(donor, names, plan)
        except Exception as exc:  # noqa: BLE001
            self._window.set_error(f"Перенос не выполнен: {exc}")
            return
        # Открытые окна показывают перенесённые значения
        for map_id in editor.open_maps():
            editor.reload_map(self._session.load_table(map_id), self._session.checkpoint(map_id))
        self._check_changed(*report.before)

        markdown = render_report(report)
        report_file: Path | None = None
        try:
            saved = save_report(report)
        except OSError as exc:
            self._window.set_error(f"Отчёт не сохранён: {exc}")
        else:
            report_file = saved
            self._window.set_status(
                f"Перенесено из {report.source_id}: {len(report.before)} таблиц · отчёт {saved.name}"
            )
        self._window.show_transfer_report(markdown, report_file)

    def save_rom(self) -> bool:
        """Сохранить в файл этой сессии; в первый раз — спросить путь. True — сохранено."""
        if self._save_path is None:
            return self.save_rom_as()
        return self._save_to(self._save_path)

    def save_rom_as(self) -> bool:
        if not self._can_save():
            return False
        current = Path(self._parser.file_path or "rom.hex")
        stem = current.stem if current.stem.endswith("-edited") else f"{current.stem}-edited"
        path = self._window.ask_save_rom_path(str(current.with_name(f"{stem}{current.suffix}")))
        if not path:
            return False
        return self._save_to(path)

    def _can_save(self) -> bool:
        if not self._parser.file_path:
            self._window.show_info("Сохранение", "Сначала откройте ROM.")
            return False
        if self._session.save_blocker:
            self._window.set_error(self._session.save_blocker)
            self._window.show_info("Сохранение", self._session.save_blocker)
            return False
        return True

    def _save_to(self, path: str) -> bool:
        if not self._can_save():
            return False
        try:
            fixed = self._session.save(path)
        except (OSError, ValueError) as exc:
            self._window.set_error(f"Не сохранено: {exc}")
            self._window.show_info("Сохранение", f"Не сохранено: {exc}")
            return False
        self._save_path = path
        self._window.set_rom_path(path, modified=False)
        definition = self._parser.definition
        self._window.editor_view.set_rom_info(path, definition.romid if definition else None)
        if self._settings is not None:
            self._settings.set_last_rom_path(path)
        self._window.set_status(f"Сохранено: {Path(path).name} · контрольных сумм пересчитано: {fixed}")
        return True

    def _confirm_discard(self) -> bool:
        """Можно ли бросить текущий образ (закрытие, открытие другого ROM): нет правок, их
        сохранили или пользователь отказался от них."""
        if not self._session.dirty:
            return True
        answer = self._window.ask_unsaved_changes()
        if answer == "save":
            return self.save_rom()
        return answer == "discard"

    def select_map(self, map_id: str) -> None:
        """Открыть окно карты или поднять уже открытое (фокус остаётся в дереве)."""
        editor = self._window.editor_view
        if editor.has_map(map_id):
            editor.raise_map(map_id)
            return
        try:
            table = self._session.load_table(map_id)
        except Exception as exc:  # noqa: BLE001
            self._window.set_error(f"Не удалось загрузить карту: {exc}")
            return
        editor.open_map(table, self._session.checkpoint(map_id))
        self._window.set_status(f"Карта: {table.name.strip()} · {describe_shape(table.rows, table.cols, '')}")

    def _connect_map_window(self, window: MapWindow) -> None:
        """Правки из окна карты — с map_id, в координатах карты в ROM."""
        window.cell_value_changed.connect(self.on_cell_changed)
        window.x_axis_changed.connect(self.on_x_axis_changed)
        window.y_axis_changed.connect(self.on_y_axis_changed)
        window.checkpoint_set.connect(self.on_checkpoint_set)
        window.matrix_copied.connect(self.on_matrix_copied)
        window.matrix_pasted.connect(self.on_matrix_pasted)
        window.operation_error.connect(self.on_operation_error)
        window.operation_info.connect(self.on_operation_info)

    def on_checkpoint_set(self, map_id: str) -> None:
        """В окне карты нажали Set checkpoint — точка отсчёта сессии та же, что на экране."""
        self._session.set_checkpoint(map_id, self._window.editor_view.checkpoint(map_id))
        self._check_changed(map_id)

    def _check_changed(self, *map_ids: str) -> None:
        """Перепроверить, отличаются ли карты от checkpoint — отложенно: шаг по сотне ячеек
        даёт сотню правок, а проверка карты одна."""
        if not self._to_check:
            QTimer.singleShot(0, self._flush_changed)
        self._to_check.update(map_ids)

    def _flush_changed(self) -> None:
        changed = self._session.refresh_changed(self._to_check)
        self._to_check.clear()
        self._window.editor_view.set_changed_maps(changed)
        # Отметка несохранённых правок в заголовке
        self._window.setWindowModified(self._session.dirty)

    def on_cell_changed(self, map_id: str, row: int, col: int, value: float) -> None:
        editor = self._window.editor_view
        try:
            stored = self._session.write_cell(map_id, row, col, value)
        except Exception as exc:  # noqa: BLE001 — показать в статусбаре и откатить ячейку
            self._window.set_error(f"Не удалось записать ({row}, {col}): {exc}")
            editor.update_cell(map_id, row, col, self._parser.read_cell(map_id, row, col))
            return
        # В ячейке — то, что реально легло в ROM (квантование типа, границы диапазона)
        editor.update_cell(map_id, row, col, stored)
        self._check_changed(map_id)
        note = "" if round(stored, 2) == round(value, 2) else f" (введено {value:.2f})"
        self._window.set_status(f"Изменено [{map_id}] ({row}, {col}) = {stored:.2f}{note}")

    def on_x_axis_changed(self, map_id: str, col: int, value: float) -> None:
        self._on_axis_changed(map_id, "x", col, value)

    def on_y_axis_changed(self, map_id: str, row: int, value: float) -> None:
        self._on_axis_changed(map_id, "y", row, value)

    def _on_axis_changed(self, map_id: str, axis: str, index: int, value: float) -> None:
        editor = self._window.editor_view
        try:
            result = self._session.write_axis(map_id, axis, index, value)
        except Exception as exc:  # noqa: BLE001
            self._window.set_error(f"Не удалось записать ось {axis.upper()}[{index}]: {exc}")
            editor.update_axis(map_id, _orientation(axis), index, self._parser.read_axis(map_id, axis, index))
            return
        stored = result.stored
        editor.update_axis(map_id, _orientation(axis), index, stored)
        # Открытые окна карт на той же оси показывают её новое значение (у них это может быть
        # другая ось: X вместо Y)
        for name, kind in result.shared:
            editor.update_axis(name, _orientation(kind), index, stored)
        self._check_changed(map_id, *(name for name, _ in result.shared))
        shared = result.shared
        note = f" — ось общая, изменилась и у других таблиц: {len(shared)}" if shared else ""
        self._window.set_status(f"Ось {axis.upper()} [{map_id}] {index} = {stored:.2f}{note}")

    def on_matrix_copied(self, rows: int, cols: int) -> None:
        self._window.set_status(f"Скопировано {rows}×{cols}")

    def on_matrix_pasted(self, rows: int, cols: int) -> None:
        self._window.set_status(f"Вставлено {rows}×{cols}")

    def on_operation_error(self, message: str) -> None:
        self._window.set_error(message)

    def on_operation_info(self, message: str) -> None:
        self._window.set_status(message)


def _orientation(axis: str) -> Qt.Orientation:
    """Ось карты в ROM → ориентация: "x" — Horizontal, "y" — Vertical."""
    return Qt.Orientation.Horizontal if axis == "x" else Qt.Orientation.Vertical
