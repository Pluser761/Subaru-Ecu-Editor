"""Связка MCP-сервера с редактором: вызовы из потоков сервера → поток GUI, подтверждение правок.

Реализует src.ai.server.EditorHost. Сессию и окна трогает только в потоке GUI: рабочий поток
кладёт задачу в очередь сигналом и ждёт её результат через Future.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Callable, TypeVar

from PyQt6.QtCore import QObject, Qt, pyqtSignal

from src.ai.server import DEFAULT_PORT, ServerThread
from src.ai.tools import EditProposal
from src.core.session import RomSession
from src.core.units import TableUnits
from src.ui.ai_edit_dialog import AiEditDialog, EditPreview
from src.ui.main_window import MainWindow

T = TypeVar("T")

# Сколько ждать ответа пользователя на правку; дольше — считаем отказом
CONFIRM_TIMEOUT_S = 15 * 60


class AiPresenter(QObject):
    _job = pyqtSignal(object)

    def __init__(
        self,
        window: MainWindow,
        session: Callable[[], RomSession],
        units: Callable[[str], TableUnits],
        on_applied: Callable[[EditProposal], None],
        on_show_map: Callable[[str], None],
    ) -> None:
        super().__init__()
        self._window = window
        self._session = session
        self._units = units
        self._on_applied = on_applied
        self._on_show_map = on_show_map
        self._server: ServerThread | None = None
        # Правки, ждущие ответа пользователя: при остановке сервера — отказ
        self._pending: set[Future[bool]] = set()
        # Сигнал из рабочего потока к объекту потока GUI — доставляется в очереди событий GUI
        self._job.connect(self._run_job)

    # --- Сервер ---

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def url(self) -> str:
        return self._server.url if self._server else f"http://127.0.0.1:{DEFAULT_PORT}/mcp"

    def start(self, port: int = DEFAULT_PORT) -> None:
        """OSError — не запустился (порт занят)."""
        if self._server is not None:
            return
        server = ServerThread(self, port)
        server.start()
        self._server = server

    def stop(self) -> None:
        for future in list(self._pending):
            if not future.done():
                future.set_result(False)
        if self._server is not None:
            self._server.stop()
            self._server = None

    # --- EditorHost (вызывается из потоков сервера) ---

    def call(self, fn: Callable[[RomSession], T]) -> T:
        if threading.current_thread() is threading.main_thread():
            return fn(self._session())
        future: Future[T] = Future()

        def job() -> None:
            try:
                future.set_result(fn(self._session()))
            except BaseException as exc:  # noqa: BLE001 — вернуть ошибку в поток сервера
                future.set_exception(exc)

        self._job.emit(job)
        return future.result()

    def confirm(self, proposal: EditProposal) -> bool:
        future: Future[bool] = Future()
        dialogs: list[AiEditDialog] = []

        def job() -> None:
            dialog = AiEditDialog(self._preview(proposal), self._window)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            def finished(code: int) -> None:
                if not future.done():
                    future.set_result(code == AiEditDialog.DialogCode.Accepted)

            dialog.finished.connect(finished)
            dialogs.append(dialog)
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()

        self._pending.add(future)
        self._job.emit(job)
        try:
            return future.result(timeout=CONFIRM_TIMEOUT_S)
        except FutureTimeout:
            def close() -> None:
                for dialog in dialogs:
                    dialog.reject()

            self._job.emit(close)
            return False
        finally:
            self._pending.discard(future)

    def applied(self, proposal: EditProposal) -> None:
        self._on_applied(proposal)

    def show_map(self, map_id: str) -> None:
        self._on_show_map(map_id)

    # --- Поток GUI ---

    def _run_job(self, job: Callable[[], object]) -> None:
        job()

    def _preview(self, proposal: EditProposal) -> EditPreview:
        """Правка для показа — в выбранных пользователем единицах."""
        units = self._units(proposal.map_id)
        table = proposal.table
        shown = units.table(table)
        new = [list(row) for row in table.values]
        for cell in proposal.cells:
            new[cell.row][cell.col] = cell.new
        x_new, y_new = list(table.x_axis), list(table.y_axis)
        for point in proposal.axes:
            (x_new if point.axis == "x" else y_new)[point.index] = point.new
        return EditPreview(
            name=proposal.name,
            reason=proposal.reason,
            unit=shown.unit,
            old=shown.values,
            new=[[units.value_display(v) for v in row] for row in new],
            decimals=shown.decimals,
            x_old=shown.x_axis,
            x_new=[units.axis_display("x", v) for v in x_new],
            y_old=shown.y_axis,
            y_new=[units.axis_display("y", v) for v in y_new],
            x_labels=shown.x_labels,
            y_labels=shown.y_labels,
            x_title=shown.x_title,
            y_title=shown.y_title,
            x_decimals=shown.x_decimals,
            y_decimals=shown.y_decimals,
            shared=sorted({name.strip() for name, _ in proposal.shared}),
        )
