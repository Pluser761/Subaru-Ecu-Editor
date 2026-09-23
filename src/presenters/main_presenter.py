from __future__ import annotations

from pathlib import Path

from src.core.parser import EcuParser
from src.core.xml_reader import EcuDefsRepository
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
    ) -> None:
        self._window = window
        self._parser = parser or EcuParser()
        self._defs_path = Path(defs_path) if defs_path else DEFAULT_DEFS
        self._defs_repo: EcuDefsRepository | None = None
        self._current_map_id: str | None = None
        self._dirty = False

        window.on_open_rom = self.open_rom
        window.on_save_rom = self.save_rom
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

        self._window.editor_view.clear_table()
        self._window.editor_view.set_maps([])
        self._window.set_status("Откройте ROM (File → Open ROM)")

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
        try:
            repo = self._ensure_defs()
            self._parser = EcuParser.open_rom(path, self._defs_path, defs_repo=repo)
        except FileNotFoundError as exc:
            self._window.set_error(str(exc))
            return
        except LookupError as exc:
            self._window.set_error(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 — показать в статусбаре
            self._window.set_error(f"Ошибка открытия ROM: {exc}")
            return

        self._current_map_id = None
        self._dirty = False
        self._window.set_rom_path(path)
        self._window.editor_view.clear_table()
        self._load_map_list()
        self._window.show_editor()

    def save_rom(self) -> None:
        if not self._parser.file_path:
            self._window.show_info("Save", "Сначала откройте ROM.")
            return
        self._window.set_status("Сохранение пока не реализовано.")
        self._window.show_info("Save", "Запись ROM пока не реализована.")

    def select_map(self, map_id: str) -> None:
        try:
            table = self._parser.load_table(map_id)
        except Exception as exc:  # noqa: BLE001
            self._window.set_error(f"Не удалось загрузить карту: {exc}")
            return
        self._current_map_id = map_id
        self._window.editor_view.show_table(table)
        self._window.set_status(f"Карта: {table.name} ({table.rows}×{table.cols})")

    def on_cell_changed(self, row: int, col: int, value: float) -> None:
        self._dirty = True
        name = self._current_map_id or "?"
        self._window.set_status(f"Изменено [{name}] ({row}, {col}) = {value:.2f}")

    def on_x_axis_changed(self, col: int, value: float) -> None:
        self._dirty = True
        name = self._current_map_id or "?"
        self._window.set_status(f"Ось X [{name}] col {col} = {value:.2f}")

    def on_y_axis_changed(self, row: int, value: float) -> None:
        self._dirty = True
        name = self._current_map_id or "?"
        self._window.set_status(f"Ось Y [{name}] row {row} = {value:.2f}")

    def on_matrix_copied(self, rows: int, cols: int) -> None:
        self._window.set_status(f"Скопировано {rows}×{cols}")

    def on_matrix_pasted(self, rows: int, cols: int) -> None:
        self._dirty = True
        self._window.set_status(f"Вставлено {rows}×{cols}")

    def on_operation_error(self, message: str) -> None:
        self._window.set_error(message)

    def on_operation_info(self, message: str) -> None:
        self._window.set_status(message)
