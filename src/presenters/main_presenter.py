from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt

from src.core.parser import EcuParser
from src.core.transfer import apply_transfer, check_compatibility, plan_transfer, render_report, save_report
from src.core.xml_reader import EcuDefsRepository
from src.ui.app_settings import AppSettings
from src.ui.main_window import MainWindow

DEFAULT_DEFS = Path(__file__).resolve().parents[2] / "definitions" / "ecu_defs.xml"


class MainPresenter:
    """Связывает MainWindow / EditorView с EcuParser."""

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
        self._parser = parser or EcuParser()
        self._defs_path = Path(defs_path) if defs_path else DEFAULT_DEFS
        self._defs_repo: EcuDefsRepository | None = None
        self._current_map_id: str | None = None
        self._dirty = False
        # Checkpoint каждой карты живёт, пока открыт ROM: переключение карт его не сбрасывает
        self._checkpoints: dict[str, list[list[float]]] = {}

        window.on_open_rom = self.open_rom
        window.on_save_rom = self.save_rom
        window.on_transfer_maps = self.transfer_maps
        window.on_show_editor = window.show_editor
        window.on_show_tools = window.show_tools

        editor = window.editor_view
        editor.map_selected.connect(self.select_map)
        editor.table_editor.cell_value_changed.connect(self.on_cell_changed)
        editor.table_editor.x_axis_changed.connect(self.on_x_axis_changed)
        editor.table_editor.y_axis_changed.connect(self.on_y_axis_changed)
        editor.table_editor.matrix_copied.connect(self.on_matrix_copied)
        editor.table_editor.matrix_pasted.connect(self.on_matrix_pasted)
        editor.table_editor.operation_error.connect(self.on_operation_error)
        editor.table_editor.operation_info.connect(self.on_operation_info)

        if settings is not None:
            window.key_bindings.set_keys(settings.load_key_bindings())
            window.key_bindings.changed.connect(
                lambda: settings.save_key_bindings(window.key_bindings.as_dict())
            )

        self._window.editor_view.clear_table()
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

    def _load_map_list(self) -> None:
        maps = self._parser.list_maps()
        self._window.editor_view.set_maps(maps)
        xmlid = self._parser.xmlid or "?"
        self._window.set_status(f"{xmlid}: карт {len(maps)}")

    def open_rom(self) -> None:
        path = self._window.ask_open_rom_path()
        if not path:
            return
        self._open_rom_path(path)

    def _open_rom_path(self, path: str) -> bool:
        try:
            repo = self._ensure_defs()
            self._parser = EcuParser.open_rom(path, self._defs_path, defs_repo=repo)
        except FileNotFoundError as exc:
            self._window.set_error(str(exc))
            return False
        except LookupError as exc:
            self._window.set_error(str(exc))
            return False
        except Exception as exc:  # noqa: BLE001 — показать в статусбаре
            self._window.set_error(f"Ошибка открытия ROM: {exc}")
            return False

        self._current_map_id = None
        self._dirty = False
        self._checkpoints.clear()
        self._window.set_rom_path(path)
        self._window.editor_view.clear_table()
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
        if self._current_map_id is not None:
            self._checkpoints[self._current_map_id] = editor.checkpoint_values()
        try:
            report = apply_transfer(donor, self._parser, names, plan)
        except Exception as exc:  # noqa: BLE001
            self._window.set_error(f"Перенос не выполнен: {exc}")
            return

        # Рамки checkpoint показывают изменения относительно состояния до переноса
        for name, values in report.before_values.items():
            self._checkpoints.setdefault(name, values)
        self._dirty = self._dirty or bool(report.before_values)
        if self._current_map_id is not None:
            self.select_map(self._current_map_id)

        markdown = render_report(report)
        report_file: Path | None = None
        try:
            saved = save_report(report)
        except OSError as exc:
            self._window.set_error(f"Отчёт не сохранён: {exc}")
        else:
            report_file = saved
            self._window.set_status(
                f"Перенесено из {report.source_id}: {len(report.before_values)} таблиц · отчёт {saved.name}"
            )
        self._window.show_transfer_report(markdown, report_file)

    def save_rom(self) -> None:
        if not self._parser.file_path:
            self._window.show_info("Save", "Сначала откройте ROM.")
            return
        self._window.set_status("Сохранение пока не реализовано.")
        self._window.show_info("Save", "Запись ROM пока не реализована.")

    def select_map(self, map_id: str) -> None:
        editor = self._window.editor_view
        if self._current_map_id is not None:
            self._checkpoints[self._current_map_id] = editor.checkpoint_values()
        try:
            table = self._parser.load_table(map_id)
        except Exception as exc:  # noqa: BLE001
            self._window.set_error(f"Не удалось загрузить карту: {exc}")
            return
        self._current_map_id = map_id
        editor.show_table(table, self._checkpoints.get(map_id))
        self._window.set_status(f"Карта: {table.name} ({table.rows}×{table.cols})")

    def on_cell_changed(self, row: int, col: int, value: float) -> None:
        map_id = self._current_map_id
        if map_id is None:
            return
        editor = self._window.editor_view
        try:
            stored = self._parser.write_cell(map_id, row, col, value)
        except Exception as exc:  # noqa: BLE001 — показать в статусбаре и откатить ячейку
            self._window.set_error(f"Не удалось записать ({row}, {col}): {exc}")
            editor.update_cell(row, col, self._parser.read_cell(map_id, row, col))
            return
        # В ячейке — то, что реально легло в ROM (квантование типа, границы диапазона)
        editor.update_cell(row, col, stored)
        self._dirty = True
        note = "" if round(stored, 2) == round(value, 2) else f" (введено {value:.2f})"
        self._window.set_status(f"Изменено [{map_id}] ({row}, {col}) = {stored:.2f}{note}")

    def on_x_axis_changed(self, col: int, value: float) -> None:
        self._on_axis_changed("x", Qt.Orientation.Horizontal, col, value)

    def on_y_axis_changed(self, row: int, value: float) -> None:
        self._on_axis_changed("y", Qt.Orientation.Vertical, row, value)

    def _on_axis_changed(self, axis: str, orientation: Qt.Orientation, index: int, value: float) -> None:
        map_id = self._current_map_id
        if map_id is None:
            return
        editor = self._window.editor_view
        try:
            stored = self._parser.write_axis(map_id, axis, index, value)
        except Exception as exc:  # noqa: BLE001
            self._window.set_error(f"Не удалось записать ось {axis.upper()}[{index}]: {exc}")
            editor.update_axis(orientation, index, self._parser.read_axis(map_id, axis, index))
            return
        editor.update_axis(orientation, index, stored)
        self._dirty = True
        self._window.set_status(f"Ось {axis.upper()} [{map_id}] {index} = {stored:.2f}")

    def on_matrix_copied(self, rows: int, cols: int) -> None:
        self._window.set_status(f"Скопировано {rows}×{cols}")

    def on_matrix_pasted(self, rows: int, cols: int) -> None:
        self._dirty = True
        self._window.set_status(f"Вставлено {rows}×{cols}")

    def on_operation_error(self, message: str) -> None:
        self._window.set_error(message)

    def on_operation_info(self, message: str) -> None:
        self._window.set_status(message)
