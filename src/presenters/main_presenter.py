from __future__ import annotations

import sys

from pathlib import Path

from PyQt6.QtCore import QProcess, QTimer

from src.ai.notes import NoteKey, NotesStore
from src.ai.server import DEFAULT_PORT
from src.ai.tools import EditProposal
from src.i18n import Language, tr
from src.presenters.ai_presenter import AiPresenter
from src.presenters.controllers import SaveController, SwitchesController, TransferController
from src.core.parser import EcuParser
from src.core.models import Axis, CalibrationTable, Checkpoint
from src.core.session import RomSession
from src.core.units import Conversion, TableUnits, UnitSystem
from src.core.xml_reader import EcuDefsRepository
from src.logger.core.maf import MAF_TABLE, MafTable
from src.logger.presenter import LoggerPresenter, RomInfo
from src.logger.ui.window import LoggerWindow
from src.ui.app_settings import AppSettings
from src.ui.axes import axis_orientation
from src.ui.main_window import MainWindow
from src.ui.map.cells import describe_shape
from src.ui.map.window import MapWindow

DEFAULT_DEFS = Path(__file__).resolve().parents[2] / "definitions" / "editor" / "ecu_defs.xml"
# Дополнения из сборки: таблицы, которых нет в ecu_defs.xml (core.xml_reader.EcuDefsRepository)
DEFAULT_EXTRA_DEFS = DEFAULT_DEFS.parent / "extra"


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
        # Папки дополнений: из сборки (только для стандартного ecu_defs.xml) и свои — рядом с settings.ini
        # Подписи источника: «extra/…» — из сборки, «user/…» — своя папка
        self._extra_defs: list[tuple[Path, str]] = [(DEFAULT_EXTRA_DEFS, "extra")] if defs_path is None else []
        if settings is not None:
            self._extra_defs.append((settings.editor_extra_dir, "user"))
        self._defs_repo: EcuDefsRepository | None = None
        # Карты, ждущие перепроверки «изменена ли» (отметка в дереве) — отложенно, пачкой
        self._to_check: set[str] = set()
        # Заметки о прошивках для ИИ — рядом с settings.ini; без настроек — нет и заметок
        self._notes = NotesStore(settings.notes_dir) if settings is not None else None
        # Единицы на экране; ROM, сессия и checkpoint — всегда в единицах definitions
        self._units_system = settings.unit_system() if settings is not None else UnitSystem.DEFINITIONS

        # Подсистемы, вынесенные из презентера
        self._save = SaveController(window, lambda: self._session, settings, self._notes)
        self._transfer = TransferController(
            window, lambda: self._session, self._defs_path, self._ensure_defs, self._display, self._check_changed
        )
        self._switches = SwitchesController(window, lambda: self._session)

        window.open_rom_requested.connect(self.open_rom)
        window.save_rom_requested.connect(self._save.save)
        window.save_rom_as_requested.connect(self._save.save_as)
        window.on_close_requested = self._save.confirm_discard
        window.transfer_maps_requested.connect(self._transfer.run)
        window.show_switches_requested.connect(self._switches.show)
        window.revert_all_requested.connect(self.revert_all)
        window.unit_system_requested.connect(self.set_unit_system)
        window.language_requested.connect(self.set_language)
        # ИИ: MCP-сервер в этом же процессе — видит открытую сессию, правки подтверждает пользователь
        self._ai = AiPresenter(
            window,
            session=lambda: self._session,
            units=self._units,
            on_applied=self._on_ai_applied,
            on_show_map=self.select_map,
            notes=self._notes,
        )
        window.ai_server_toggled.connect(self.set_ai_server)
        window.ai_help_requested.connect(lambda: window.show_ai_help(self._ai.url, self._ai.running))
        window.ai_notes_requested.connect(self.show_notes)
        window.open_defs_folder_requested.connect(self.open_defs_folder)
        window.open_logger_requested.connect(self.open_logger)
        window.open_maf_requested.connect(self.open_maf)
        # Логгер — отдельное окно, создаётся при первом открытии
        self._logger: LoggerPresenter | None = None
        window.set_unit_system_checked(self._units_system)

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
            self._defs_repo = EcuDefsRepository.load(self._defs_path, self._extra_defs)
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
        if not self._save.confirm_discard():
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
        self._save.reset_path()
        self._switches.reset()
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
        window.revert_requested.connect(self.revert_map)
        window.matrix_copied.connect(self.on_matrix_copied)
        window.matrix_pasted.connect(self.on_matrix_pasted)
        window.operation_error.connect(self.on_operation_error)
        window.operation_info.connect(self.on_operation_info)

    def on_checkpoint_set(self, map_id: str) -> None:
        """В окне карты нажали Set checkpoint — точка отсчёта сессии та же, что на экране."""
        shown = self._window.editor_view.checkpoint(map_id)
        self._session.set_checkpoint(map_id, self._units(map_id).checkpoint_definition(shown) if shown else None)
        self._check_changed(map_id)

    def revert_map(self, map_id: str) -> None:
        """Кнопка в окне карты: откатить эту карту к checkpoint."""
        if map_id not in self._session.refresh_changed({map_id}):
            self._window.set_status(tr("Карта не отличается от checkpoint"))
            return
        self._revert([map_id])

    def revert_all(self) -> None:
        """Карты → откатить все изменённые карты к checkpoint."""
        if not self._parser.file_path:
            return
        changed = sorted(self._session.find_changed())
        self._window.editor_view.set_changed_maps(set(changed))
        if not changed:
            self._window.show_info(tr("Откат к checkpoint"), tr("Нет карт, изменённых относительно checkpoint."))
            return
        self._revert(changed)

    def _revert(self, names: list[str]) -> None:
        if not self._window.ask_revert(names):
            return
        try:
            touched = self._session.revert(set(names))
        except Exception as exc:  # noqa: BLE001
            self._window.set_error(tr("Откат не выполнен: {error}", error=exc))
            return
        editor = self._window.editor_view
        for map_id in touched & set(editor.open_maps()):
            editor.reload_map(*self._display(map_id))
        self._check_changed(*touched)
        if len(names) == 1:
            self._window.set_status(tr("Карта откачена к checkpoint: {name}", name=names[0].strip()))
        else:
            self._window.set_status(tr("Откачено к checkpoint карт: {n}", n=len(names)))

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
        d = _decimals(units.value)
        note = "" if round(stored, d) == round(value, d) else " " + tr("(введено {value})", value=_fmt(value, d))
        self._window.set_status(
            tr("Изменено [{map}] ({row}, {col}) = {value}", map=map_id, row=row, col=col, value=_fmt(stored, d))
            + note
        )

    def on_x_axis_changed(self, map_id: str, col: int, value: float) -> None:
        self._on_axis_changed(map_id, Axis.X, col, value)

    def on_y_axis_changed(self, map_id: str, row: int, value: float) -> None:
        self._on_axis_changed(map_id, Axis.Y, row, value)

    def _on_axis_changed(self, map_id: str, axis: Axis, index: int, value: float) -> None:
        editor = self._window.editor_view
        units = self._units(map_id)
        try:
            result = self._session.write_axis(map_id, axis, index, units.axis_definition(axis, value))
        except Exception as exc:  # noqa: BLE001
            self._window.set_error(
                tr("Не удалось записать ось {axis}[{index}]: {error}", axis=axis.name, index=index, error=exc)
            )
            current = units.axis_display(axis, self._parser.read_axis(map_id, axis, index))
            editor.update_axis(map_id, axis_orientation(axis), index, current)
            return
        stored = units.axis_display(axis, result.stored)
        editor.update_axis(map_id, axis_orientation(axis), index, stored)
        # Открытые окна карт на той же оси показывают её новое значение (у них это может быть
        # другая ось: X вместо Y) — в своих единицах
        for name, kind in result.shared:
            editor.update_axis(name, axis_orientation(kind), index, self._units(name).axis_display(kind, result.stored))
        self._check_changed(map_id, *(name for name, _ in result.shared))
        shared = result.shared
        note = " — " + tr("ось общая, изменилась и у других таблиц: {n}", n=len(shared)) if shared else ""
        d = _decimals(units.axis(axis))
        self._window.set_status(
            tr("Ось {axis} [{map}] {index} = {value}", axis=axis.name, map=map_id, index=index, value=_fmt(stored, d))
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

    def _note_key(self) -> NoteKey | None:
        return NoteKey.for_rom(self._parser.file_path, self._parser.xmlid)

    def show_notes(self) -> None:
        """ИИ → Заметки о прошивке: посмотреть и поправить контекст ИИ для открытого ROM."""
        key = self._note_key()
        if self._notes is None or key is None:
            self._window.show_info(tr("Заметки о прошивке"), tr("Сначала откройте ROM."))
            return
        try:
            original = self._notes.read(key)
        except OSError as exc:
            self._window.set_error(tr("Заметки не прочитаны: {error}", error=exc))
            return
        text = self._window.ask_rom_notes(f"{key.file_name} · {key.xmlid}", original, str(self._notes.path(key)))
        if text is None or text == original:
            return
        try:
            # Пока окно было открыто, ИИ мог дописать заметки — не затирать молча
            if self._notes.read(key) != original and not self._window.ask_overwrite_notes():
                return
            self._notes.write(key, text)
        except OSError as exc:
            self._window.set_error(tr("Заметки не сохранены: {error}", error=exc))
            return
        self._window.set_status(tr("Заметки о прошивке сохранены"))

    def open_defs_folder(self) -> None:
        """Файл → Папка definitions: свои дополнения; подхватываются при следующем запуске."""
        if self._settings is None:
            return
        folder = self._settings.editor_extra_dir
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._window.set_error(tr("Папка не создана: {error}", error=exc))
            return
        self._window.open_folder(folder)

    def open_maf(self) -> None:
        """MAF-скейлинг — отдельное окно (Cmd/Ctrl+M); подключение живёт в окне логгера.

        Если логгер ещё не подключён, показываем и его: без опроса ЭБУ точки брать неоткуда."""
        if self._logger is None or self._logger.runner is None:
            self.open_logger()
        if self._logger is not None:
            self._logger.show_maf()

    def open_logger(self) -> None:
        """Логгер → Открыть логгер: отдельное окно; прошивка редактора — для сверки ЭБУ и имени лога."""
        if self._logger is None:
            self._logger = LoggerPresenter(
                LoggerWindow(), self._settings, self._logger_rom_info,
                rom_table_reader=self._logger_read_table,
                maf_table=self._logger_maf_table,
                maf_apply=self._logger_maf_apply,
            )
        window = self._logger.window
        window.show()
        window.raise_()
        window.activateWindow()

    def _logger_rom_info(self) -> RomInfo:
        definition = self._parser.definition
        return RomInfo(self._parser.file_path, definition.romid.ecuid if definition else None)

    def _logger_read_table(self, name: str) -> list[float]:
        """Значения 2D-таблицы ROM (столбец) для логгера — границы зон AFL и оси FLKC."""
        parser = self._parser
        if parser.file_path is None:
            return []
        try:
            table = parser.load_table(name)
        except (KeyError, AttributeError):
            return []
        return [row[0] for row in table.values]

    def _logger_maf_table(self) -> MafTable | None:
        """Таблица «MAF Sensor Scaling» открытой прошивки: ось (В) и расход (г/с).

        Берётся точное имя, иначе вариант с хвостовым пробелом; «(Reference Only)» —
        копия для справки, её не правим."""
        parser = self._parser
        if parser.file_path is None:
            return None
        names = [
            info.name for info in parser.list_maps()
            if info.name.strip().startswith(MAF_TABLE) and "Reference Only" not in info.name
        ]
        exact = [name for name in names if name.strip() == MAF_TABLE]
        if not names:
            return None
        name = (exact or names)[0]
        try:
            table = parser.load_table(name)
        except (KeyError, AttributeError):
            return None
        if not table.values or len(table.values[0]) != 1:
            return None
        axis = tuple(float(v) for v in table.y_axis)
        values = tuple(float(row[0]) for row in table.values)
        if not axis or len(axis) != len(values):
            return None
        return MafTable(name, axis, values)

    def _logger_maf_apply(self, name: str, values: dict[int, float]) -> int:
        """Записать новые значения строк (г/с) через RomSession: checkpoint даёт откат,
        окно карты и дерево обновляются как после обычной правки."""
        session = self._session
        for row in sorted(values):
            session.write_cell(name, row, 0, values[row])
        editor = self._window.editor_view
        if name in set(editor.open_maps()):
            editor.reload_map(*self._display(name))
        self._check_changed(name)
        self._window.set_status(tr("MAF: обновлено строк {n}", n=len(values)))
        return len(values)

    def stop_logger(self) -> None:
        if self._logger is not None:
            self._logger.disconnect()

    def stop_ai_server(self) -> None:
        self._ai.stop()

    def _on_ai_applied(self, proposal: EditProposal) -> None:
        """Правка ИИ записана: окна карты и соседей по общей оси показывают новые значения,
        окно изменённой карты открывается или поднимается — график следует за ним."""
        editor = self._window.editor_view
        touched = {proposal.map_id, *(name for name, _ in proposal.shared)}
        for map_id in touched & set(editor.open_maps()):
            editor.reload_map(*self._display(map_id))
        self.select_map(proposal.map_id)
        self._check_changed(*touched)
        self._window.set_status(tr("ИИ изменил карту: {name}", name=proposal.name))

    def set_language(self, language: Language) -> None:
        """View → Язык: сохранить; применяется перезапуском (с обычной проверкой правок)."""
        if self._settings is not None:
            self._settings.set_language(language)
        if self._window.ask_restart_for_language() and self._save.confirm_discard():
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


def _decimals(conv: Conversion | None) -> int:
    """Знаков после точки на экране для величины; без пересчёта единиц — 2."""
    return conv.decimals if conv is not None else 2


def _fmt(value: float, decimals: int) -> str:
    """Число в том виде, как в статусбаре (по знакам отображения величины)."""
    return f"{value:.{decimals}f}"
