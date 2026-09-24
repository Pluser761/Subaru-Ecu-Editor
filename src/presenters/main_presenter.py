from __future__ import annotations

from pathlib import Path

import math

from PyQt6.QtCore import Qt, QTimer

from src.core.models import Checkpoint
from src.core.parser import EcuParser
from src.core.transfer import apply_transfer, check_compatibility, plan_transfer, render_report, save_report
from src.core.xml_reader import EcuDefsRepository
from src.ui.app_settings import AppSettings
from src.ui.main_window import MainWindow
from src.ui.map_window import describe_shape

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
        self._dirty = False
        # Checkpoint карт, чьи окна закрыты (или ещё не открывались): живёт, пока открыт ROM.
        # У открытых окон актуальный checkpoint — в самом окне
        self._checkpoints: dict[str, Checkpoint] = {}
        # Карты, отличающиеся от своего checkpoint (отметка в дереве), и ждущие перепроверки
        self._changed: set[str] = set()
        self._to_check: set[str] = set()

        window.on_open_rom = self.open_rom
        window.on_save_rom = self.save_rom
        window.on_transfer_maps = self.transfer_maps
        window.on_show_editor = window.show_editor
        window.on_show_tools = window.show_tools

        editor = window.editor_view
        editor.map_selected.connect(self.select_map)
        # Правки — от окон карт через EditorView: с map_id, в координатах карты в ROM
        editor.cell_value_changed.connect(self.on_cell_changed)
        editor.x_axis_changed.connect(self.on_x_axis_changed)
        editor.y_axis_changed.connect(self.on_y_axis_changed)
        editor.map_closed.connect(self.on_map_closed)
        editor.checkpoint_set.connect(self._check_changed)
        editor.matrix_copied.connect(self.on_matrix_copied)
        editor.matrix_pasted.connect(self.on_matrix_pasted)
        editor.operation_error.connect(self.on_operation_error)
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

        # Окна карт старого ROM закрываем до сброса checkpoint (закрытие их сохраняет)
        self._window.editor_view.close_all_maps()
        self._dirty = False
        self._checkpoints.clear()
        self._changed.clear()
        self._to_check.clear()
        self._window.set_rom_path(path)
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
        # Checkpoint открытых окон — в общий словарь, чтобы перенос не затёр их точку отсчёта
        for map_id in editor.open_maps():
            checkpoint = editor.checkpoint(map_id)
            if checkpoint is not None:
                self._checkpoints[map_id] = checkpoint
        try:
            report = apply_transfer(donor, self._parser, names, plan)
        except Exception as exc:  # noqa: BLE001
            self._window.set_error(f"Перенос не выполнен: {exc}")
            return

        # Рамки checkpoint показывают изменения относительно состояния до переноса
        for name, checkpoint in report.before.items():
            self._checkpoints.setdefault(name, checkpoint)
        self._dirty = self._dirty or bool(report.before)
        # Открытые окна показывают перенесённые значения
        for map_id in editor.open_maps():
            editor.reload_map(self._parser.load_table(map_id), self._checkpoints.pop(map_id, None))
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

    def save_rom(self) -> None:
        if not self._parser.file_path:
            self._window.show_info("Save", "Сначала откройте ROM.")
            return
        self._window.set_status("Сохранение пока не реализовано.")
        self._window.show_info("Save", "Запись ROM пока не реализована.")

    def select_map(self, map_id: str) -> None:
        """Открыть окно карты или поднять уже открытое (фокус остаётся в дереве)."""
        editor = self._window.editor_view
        if editor.has_map(map_id):
            editor.raise_map(map_id)
            return
        try:
            table = self._parser.load_table(map_id)
        except Exception as exc:  # noqa: BLE001
            self._window.set_error(f"Не удалось загрузить карту: {exc}")
            return
        # Checkpoint переходит в окно и вернётся в словарь при его закрытии
        editor.open_map(table, self._checkpoints.pop(map_id, None))
        self._window.set_status(f"Карта: {table.name.strip()} · {describe_shape(table.rows, table.cols, '')}")

    def on_map_closed(self, map_id: str, checkpoint: Checkpoint) -> None:
        self._checkpoints[map_id] = checkpoint

    def _check_changed(self, *map_ids: str) -> None:
        """Перепроверить, отличаются ли карты от checkpoint — отложенно: шаг по сотне ячеек
        даёт сотню правок, а проверка карты одна."""
        if not self._to_check:
            QTimer.singleShot(0, self._flush_changed)
        self._to_check.update(map_ids)

    def _flush_changed(self) -> None:
        editor = self._window.editor_view
        for map_id in self._to_check:
            checkpoint = editor.checkpoint(map_id) or self._checkpoints.get(map_id)
            if checkpoint is not None and _differs(self._parser, map_id, checkpoint):
                self._changed.add(map_id)
            else:
                self._changed.discard(map_id)
        self._to_check.clear()
        editor.set_changed_maps(self._changed)

    def on_cell_changed(self, map_id: str, row: int, col: int, value: float) -> None:
        editor = self._window.editor_view
        try:
            stored = self._parser.write_cell(map_id, row, col, value)
        except Exception as exc:  # noqa: BLE001 — показать в статусбаре и откатить ячейку
            self._window.set_error(f"Не удалось записать ({row}, {col}): {exc}")
            editor.update_cell(map_id, row, col, self._parser.read_cell(map_id, row, col))
            return
        # В ячейке — то, что реально легло в ROM (квантование типа, границы диапазона)
        editor.update_cell(map_id, row, col, stored)
        self._check_changed(map_id)
        self._dirty = True
        note = "" if round(stored, 2) == round(value, 2) else f" (введено {value:.2f})"
        self._window.set_status(f"Изменено [{map_id}] ({row}, {col}) = {stored:.2f}{note}")

    def on_x_axis_changed(self, map_id: str, col: int, value: float) -> None:
        self._on_axis_changed(map_id, "x", col, value)

    def on_y_axis_changed(self, map_id: str, row: int, value: float) -> None:
        self._on_axis_changed(map_id, "y", row, value)

    def _on_axis_changed(self, map_id: str, axis: str, index: int, value: float) -> None:
        editor = self._window.editor_view
        users = self._parser.shared_axis_users(map_id, axis)
        shared = [name for name, _ in users]
        # Общая ось поменяется и у других таблиц: снимаем checkpoint закрытых до записи,
        # иначе при первом открытии они примут новую ось за исходную и рамок не будет
        for name in shared:
            if name not in self._checkpoints and not editor.has_map(name):
                self._checkpoints[name] = _checkpoint_of(self._parser, name)
        try:
            stored = self._parser.write_axis(map_id, axis, index, value)
        except Exception as exc:  # noqa: BLE001
            self._window.set_error(f"Не удалось записать ось {axis.upper()}[{index}]: {exc}")
            editor.update_axis(map_id, _orientation(axis), index, self._parser.read_axis(map_id, axis, index))
            return
        editor.update_axis(map_id, _orientation(axis), index, stored)
        # Открытые окна карт на той же оси показывают её новое значение (у них это может быть
        # другая ось: X вместо Y)
        for name, kind in users:
            editor.update_axis(name, _orientation(kind), index, stored)
        self._check_changed(map_id, *shared)
        self._dirty = True
        note = f" — ось общая, изменилась и у других таблиц: {len(shared)}" if shared else ""
        self._window.set_status(f"Ось {axis.upper()} [{map_id}] {index} = {stored:.2f}{note}")

    def on_matrix_copied(self, rows: int, cols: int) -> None:
        self._window.set_status(f"Скопировано {rows}×{cols}")

    def on_matrix_pasted(self, rows: int, cols: int) -> None:
        self._dirty = True
        self._window.set_status(f"Вставлено {rows}×{cols}")

    def on_operation_error(self, message: str) -> None:
        self._window.set_error(message)

    def on_operation_info(self, message: str) -> None:
        self._window.set_status(message)


def _checkpoint_of(parser: EcuParser, map_id: str) -> Checkpoint:
    """Текущее состояние карты как checkpoint (значения и оси, координаты ROM)."""
    table = parser.load_table(map_id)
    return Checkpoint(values=table.values, x_axis=table.x_axis, y_axis=table.y_axis)


def _orientation(axis: str) -> Qt.Orientation:
    """Ось карты в ROM → ориентация: "x" — Horizontal, "y" — Vertical."""
    return Qt.Orientation.Horizontal if axis == "x" else Qt.Orientation.Vertical


# Совпадение с точностью отображения (2 знака), как у рамок checkpoint
_EPSILON = 0.005


def _differs(parser: EcuParser, map_id: str, checkpoint: Checkpoint) -> bool:
    """Отличаются ли значения или оси карты в ROM от checkpoint."""
    table = parser.load_table(map_id)
    pairs = [(table.values, checkpoint.values)]
    if checkpoint.x_axis:
        pairs.append(([table.x_axis], [checkpoint.x_axis]))
    if checkpoint.y_axis:
        pairs.append(([table.y_axis], [checkpoint.y_axis]))
    for current, base in pairs:
        if len(current) != len(base) or any(len(a) != len(b) for a, b in zip(current, base)):
            return True
        for row_now, row_base in zip(current, base):
            for now, was in zip(row_now, row_base):
                if now == was:  # в т.ч. inf == inf
                    continue
                if not (math.isfinite(now) and math.isfinite(was)) or abs(now - was) >= _EPSILON:
                    return True
    return False
