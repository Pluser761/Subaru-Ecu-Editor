from __future__ import annotations

import sys

from pathlib import Path

from PyQt6.QtCore import QProcess, Qt, QTimer

from src.ai.server import DEFAULT_PORT
from src.ai.tools import EditProposal
from src.i18n import Language, tr
from src.presenters.ai_presenter import AiPresenter
from src.core.parser import EcuParser
from src.core.models import CalibrationTable, Checkpoint
from src.core.session import RomSession
from src.core.units import TableUnits, UnitSystem
from src.core.transfer import check_compatibility, plan_transfer, render_report, save_report
from src.core.xml_reader import EcuDefsRepository
from src.ui.app_settings import AppSettings
from src.ui.main_window import MainWindow
from src.ui.map.cells import describe_shape
from src.ui.map.window import MapWindow
from src.ui.switches_window import SwitchesWindow

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
        # Окно «Ошибки и переключатели» и состояния переключателей на момент открытия файла
        self._switches_window: SwitchesWindow | None = None
        self._switch_original: dict[str, str | None] = {}
        # Единицы на экране; ROM, сессия и checkpoint — всегда в единицах definitions
        self._units_system = settings.unit_system() if settings is not None else UnitSystem.DEFINITIONS

        window.on_open_rom = self.open_rom
        window.on_save_rom = self.save_rom
        window.on_save_rom_as = self.save_rom_as
        window.on_close_requested = self._confirm_discard
        window.on_transfer_maps = self.transfer_maps
        window.on_show_switches = self.show_switches
        window.on_unit_system = self.set_unit_system
        window.on_language = self.set_language
        # ИИ: MCP-сервер в этом же процессе — видит открытую сессию, правки подтверждает пользователь
        self._ai = AiPresenter(
            window,
            session=lambda: self._session,
            units=self._units,
            on_applied=self._on_ai_applied,
            on_show_map=self.select_map,
        )
        window.on_ai_server = self.set_ai_server
        window.on_ai_help = lambda: window.show_ai_help(self._ai.url, self._ai.running)
        window.set_unit_system_checked(self._units_system)
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
        self._window.set_status(tr("Откройте ROM (Файл → Открыть ROM)"))

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
            self._window.set_error(tr("Прошлая прошивка не найдена: {path}", path=path))
            return
        self._open_rom_path(path)

    def _ensure_defs(self) -> EcuDefsRepository:
        if self._defs_repo is None:
            if not self._defs_path.exists():
                raise FileNotFoundError(tr("Не найден файл definitions: {path}", path=self._defs_path))
            self._window.set_status(tr("Загрузка definitions: {name}…", name=self._defs_path.name))
            self._defs_repo = EcuDefsRepository.load(self._defs_path)
        return self._defs_repo

    @property
    def _parser(self) -> EcuParser:
        return self._session.parser

    def _load_map_list(self) -> None:
        maps = self._parser.list_maps()
        self._window.editor_view.set_maps(maps)
        xmlid = self._parser.xmlid or "?"
        self._window.set_status(tr("{xmlid}: карт {n}", xmlid=xmlid, n=len(maps)))

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
            self._window.set_error(tr("Ошибка открытия ROM: {error}", error=exc))
            return False

        self._window.editor_view.close_all_maps()
        self._session = RomSession(parser)
        self._save_path = None
        self._switch_original = {s.id: s.state for s in parser.list_switches()}
        if self._switches_window is not None and self._switches_window.isVisible():
            self._fill_switches()
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
            report = self._session.transfer(donor, names, plan)
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
            self._window.show_info(tr("Сохранение"), tr("Сначала откройте ROM."))
            return False
        if self._session.save_blocker:
            self._window.set_error(self._session.save_blocker)
            self._window.show_info(tr("Сохранение"), self._session.save_blocker)
            return False
        return True

    def _save_to(self, path: str) -> bool:
        if not self._can_save():
            return False
        try:
            fixed = self._session.save(path)
        except (OSError, ValueError) as exc:
            self._window.set_error(tr("Не сохранено: {error}", error=exc))
            self._window.show_info(tr("Сохранение"), tr("Не сохранено: {error}", error=exc))
            return False
        self._save_path = path
        self._window.set_rom_path(path, modified=False)
        definition = self._parser.definition
        self._window.editor_view.set_rom_info(path, definition.romid if definition else None)
        if self._settings is not None:
            self._settings.set_last_rom_path(path)
        self._window.set_status(
            tr("Сохранено: {name} · контрольных сумм пересчитано: {fixed}", name=Path(path).name, fixed=fixed)
        )
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

    def show_switches(self) -> None:
        """Окно «Ошибки и переключатели» (таблицы Switch: DTC, OBD-II…)."""
        if not self._parser.file_path:
            self._window.show_info(tr("Ошибки и переключатели"), tr("Сначала откройте ROM."))
            return
        if self._switches_window is None:
            self._switches_window = SwitchesWindow(self._window)
            self._switches_window.switch_toggled.connect(self.on_switch_toggled)
            # По центру экрана, а не по центру узкого главного окна (иначе уезжает за край)
            screen = self._window.screen()
            if screen is not None:
                area = screen.availableGeometry()
                size = self._switches_window.size()
                self._switches_window.move(
                    area.x() + (area.width() - size.width()) // 2, area.y() + (area.height() - size.height()) // 2
                )
        self._fill_switches()
        self._switches_window.show()
        self._switches_window.raise_()
        self._switches_window.activateWindow()

    def _fill_switches(self) -> None:
        assert self._switches_window is not None
        self._switches_window.set_switches(
            self._parser.list_switches(), self._switch_original, Path(self._parser.file_path or "").name
        )

    def on_switch_toggled(self, switch_id: str, state: str) -> None:
        try:
            self._session.write_switch(switch_id, state)
        except ValueError as exc:
            self._window.set_error(tr("Не удалось переключить: {error}", error=exc))
        info = self._parser.switch_info(switch_id)
        if self._switches_window is not None:
            self._switches_window.update_switch(info)
        self._window.setWindowModified(self._session.dirty)
        label = info.code or info.name
        word = {"on": tr("включена"), "off": tr("выключена")}.get(info.state or "", "?")
        self._window.set_status(f"{label}: {word}")

    def select_map(self, map_id: str) -> None:
        """Открыть окно карты или поднять уже открытое (фокус остаётся в дереве)."""
        editor = self._window.editor_view
        if editor.has_map(map_id):
            editor.raise_map(map_id)
            return
        try:
            table, checkpoint = self._display(map_id)
        except Exception as exc:  # noqa: BLE001
            self._window.set_error(tr("Не удалось загрузить карту: {error}", error=exc))
            return
        editor.open_map(table, checkpoint)
        self._window.set_status(
            tr("Карта: {name} · {shape}", name=table.name.strip(), shape=describe_shape(table.rows, table.cols, ""))
        )

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
        shown = self._window.editor_view.checkpoint(map_id)
        self._session.set_checkpoint(map_id, self._units(map_id).checkpoint_definition(shown) if shown else None)
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
        units = self._units(map_id)
        try:
            stored_def = self._session.write_cell(map_id, row, col, units.value_definition(value))
        except Exception as exc:  # noqa: BLE001 — показать в статусбаре и откатить ячейку
            self._window.set_error(tr("Не удалось записать ({row}, {col}): {error}", row=row, col=col, error=exc))
            editor.update_cell(map_id, row, col, units.value_display(self._parser.read_cell(map_id, row, col)))
            return
        # В ячейке — то, что реально легло в ROM (квантование типа, границы диапазона)
        stored = units.value_display(stored_def)
        editor.update_cell(map_id, row, col, stored)
        self._check_changed(map_id)
        d = units.value.decimals if units.value else 2
        note = "" if round(stored, d) == round(value, d) else " " + tr("(введено {value})", value=f"{value:.{d}f}")
        self._window.set_status(
            tr("Изменено [{map}] ({row}, {col}) = {value}", map=map_id, row=row, col=col, value=f"{stored:.{d}f}")
            + note
        )

    def on_x_axis_changed(self, map_id: str, col: int, value: float) -> None:
        self._on_axis_changed(map_id, "x", col, value)

    def on_y_axis_changed(self, map_id: str, row: int, value: float) -> None:
        self._on_axis_changed(map_id, "y", row, value)

    def _on_axis_changed(self, map_id: str, axis: str, index: int, value: float) -> None:
        editor = self._window.editor_view
        units = self._units(map_id)
        try:
            result = self._session.write_axis(map_id, axis, index, units.axis_definition(axis, value))
        except Exception as exc:  # noqa: BLE001
            self._window.set_error(
                tr("Не удалось записать ось {axis}[{index}]: {error}", axis=axis.upper(), index=index, error=exc)
            )
            current = units.axis_display(axis, self._parser.read_axis(map_id, axis, index))
            editor.update_axis(map_id, _orientation(axis), index, current)
            return
        stored = units.axis_display(axis, result.stored)
        editor.update_axis(map_id, _orientation(axis), index, stored)
        # Открытые окна карт на той же оси показывают её новое значение (у них это может быть
        # другая ось: X вместо Y) — в своих единицах
        for name, kind in result.shared:
            editor.update_axis(name, _orientation(kind), index, self._units(name).axis_display(kind, result.stored))
        self._check_changed(map_id, *(name for name, _ in result.shared))
        shared = result.shared
        note = " — " + tr("ось общая, изменилась и у других таблиц: {n}", n=len(shared)) if shared else ""
        conv = units.axis(axis)
        d = conv.decimals if conv else 2
        self._window.set_status(
            tr("Ось {axis} [{map}] {index} = {value}", axis=axis.upper(), map=map_id, index=index, value=f"{stored:.{d}f}")
            + note
        )

    # --- Единицы на экране ---

    def _units(self, map_id: str) -> TableUnits:
        return TableUnits.for_table(self._parser.get_table_def(map_id), self._units_system)

    def _display(self, map_id: str) -> tuple[CalibrationTable, Checkpoint]:
        """Карта и её checkpoint для окна — в выбранных единицах."""
        units = self._units(map_id)
        return units.table(self._session.load_table(map_id)), units.checkpoint(self._session.checkpoint(map_id))

    def set_unit_system(self, system: UnitSystem) -> None:
        """View → Единицы: перерисовать открытые окна карт в новых единицах."""
        self._units_system = system
        if self._settings is not None:
            self._settings.set_unit_system(system)
        editor = self._window.editor_view
        for map_id in editor.open_maps():
            editor.reload_map(*self._display(map_id))
        names = {
            UnitSystem.DEFINITIONS: tr("как в описаниях (psi, °F, mph)"),
            UnitSystem.METRIC: tr("метрические (bar, °C, km/h)"),
        }
        self._window.set_status(tr("Единицы: {units}", units=names[system]))

    # --- ИИ ---

    def restore_ai_server(self) -> None:
        """Запустить MCP-сервер, если он был включён в прошлый раз."""
        if self._settings is not None and self._settings.ai_server_enabled():
            self.set_ai_server(True)

    def set_ai_server(self, enabled: bool) -> None:
        """ИИ → MCP-сервер: запустить или остановить; выбор сохраняется."""
        if enabled:
            port = self._settings.ai_port(DEFAULT_PORT) if self._settings is not None else DEFAULT_PORT
            try:
                self._ai.start(port)
            except OSError as exc:
                self._window.set_ai_server_checked(False)
                self._window.set_error(tr("MCP-сервер не запущен: {error}", error=exc))
                return
            self._window.set_status(tr("MCP-сервер для ИИ: {url}", url=self._ai.url))
        else:
            self._ai.stop()
            self._window.set_status(tr("MCP-сервер для ИИ остановлен"))
        self._window.set_ai_server_checked(enabled)
        if self._settings is not None:
            self._settings.set_ai_server_enabled(enabled)

    def stop_ai_server(self) -> None:
        self._ai.stop()

    def _on_ai_applied(self, proposal: EditProposal) -> None:
        """Правка ИИ записана: окна карты и соседей по общей оси показывают новые значения."""
        editor = self._window.editor_view
        touched = {proposal.map_id, *(name for name, _ in proposal.shared)}
        for map_id in touched & set(editor.open_maps()):
            editor.reload_map(*self._display(map_id))
        self._check_changed(*touched)
        self._window.set_status(tr("ИИ изменил карту: {name}", name=proposal.name))

    def set_language(self, language: Language) -> None:
        """View → Язык: сохранить; применяется перезапуском (с обычной проверкой правок)."""
        if self._settings is not None:
            self._settings.set_language(language)
        if self._window.ask_restart_for_language() and self._confirm_discard():
            # Правки уже сохранены или отброшены — закрытие без повторного вопроса
            self._window.on_close_requested = None
            QProcess.startDetached(sys.executable, sys.argv)
            self._window.close()

    def on_matrix_copied(self, rows: int, cols: int) -> None:
        self._window.set_status(tr("Скопировано {rows}×{cols}", rows=rows, cols=cols))

    def on_matrix_pasted(self, rows: int, cols: int) -> None:
        self._window.set_status(tr("Вставлено {rows}×{cols}", rows=rows, cols=cols))

    def on_operation_error(self, message: str) -> None:
        self._window.set_error(message)

    def on_operation_info(self, message: str) -> None:
        self._window.set_status(message)


def _orientation(axis: str) -> Qt.Orientation:
    """Ось карты в ROM → ориентация: "x" — Horizontal, "y" — Vertical."""
    return Qt.Orientation.Horizontal if axis == "x" else Qt.Orientation.Vertical
