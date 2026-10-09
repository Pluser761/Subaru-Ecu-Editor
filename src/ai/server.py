"""MCP-сервер редактора: инструменты для ИИ поверх открытой сессии и поток с HTTP-сервером.

Сервер слушает только localhost (streamable HTTP, путь /mcp). Инструменты MCP SDK вызывает в
рабочих потоках, а сессия ROM живёт в потоке GUI, поэтому всё, что касается сессии, идёт через
EditorHost.call. Правка карты — только через propose_map_edit: хост показывает её пользователю
и пишет в образ, лишь если тот согласился. Файл на диск ИИ не сохраняет.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from src.ai import logs, tools
from src.ai.tools import EditProposal, ToolError
from src.core.parser import EcuParser
from src.core.session import RomSession
from src.version import version

T = TypeVar("T")

DEFAULT_PORT = 8765
PATH = "/mcp"

INSTRUCTIONS = """\
You are connected to a desktop editor of Subaru ECU ROM calibration maps (RomRaider definitions).
- The user has one ROM open in the editor; map values and axes are in definitions units
  (as in ecu_defs.xml: psi, °F, mph, g/rev, rpm), regardless of what the user sees on screen.
- Matrices are values[row][col]: rows follow y_axis, columns follow x_axis.
- To change a map call propose_map_edit: the user reviews a diff and accepts or rejects it.
  Accepted edits go to the ROM image in memory and are highlighted against the checkpoint;
  saving the file (with checksum fix) is always done by the user.
- Axes shared by several tables change all of them; check shared_with before editing an axis.
- Logs: RomRaider/Car Scanner CSV files; use log_summary first, then log_rows or log_to_map.
- Be conservative with ignition timing and boost; explain your reasoning in `reason`.
"""


class EditorHost(Protocol):
    """Что сервер требует от редактора."""

    def call(self, fn: Callable[[RomSession], T]) -> T:
        """Выполнить fn с текущей сессией в потоке GUI и вернуть результат (исключения — наверх)."""
        ...

    def confirm(self, proposal: EditProposal) -> bool:
        """Показать правку пользователю и дождаться ответа (True — применить)."""
        ...

    def applied(self, proposal: EditProposal) -> None:
        """Правка записана: обновить окна карт и отметки (в потоке GUI)."""
        ...

    def show_map(self, map_id: str) -> None:
        """Открыть окно карты для пользователя (в потоке GUI)."""
        ...


def _require_rom(session: RomSession) -> None:
    if session.parser.definition is None:
        raise ToolError("No ROM is open in the editor. Ask the user to open one (File → Open ROM).")


def build_server(host: EditorHost) -> MCPServer:
    server: MCPServer = MCPServer(
        "subaru-ecu-editor", version=version(), instructions=INSTRUCTIONS, log_level="WARNING"
    )
    read_only = ToolAnnotations(read_only_hint=True)
    references: dict[Path, EcuParser] = {}

    def with_rom(fn: Callable[[RomSession], T]) -> T:
        def run(session: RomSession) -> T:
            _require_rom(session)
            return fn(session)

        return host.call(run)

    @server.tool(annotations=read_only)
    def rom_info() -> dict[str, Any]:
        """The ROM open in the editor: file, ECU, car, unsaved changes, maps changed since checkpoint."""
        return host.call(tools.rom_info)

    @server.tool(annotations=read_only)
    def list_maps(category: str | None = None, query: str | None = None) -> list[dict[str, Any]]:
        """Maps of the open ROM (name, category, shape, unit). Filter by exact category or name substring."""
        return with_rom(lambda s: tools.list_maps(s, category, query))

    @server.tool(annotations=read_only)
    def list_categories() -> list[str]:
        """Map categories of the open ROM."""
        return with_rom(lambda s: sorted({m.category for m in s.parser.list_maps()}))

    @server.tool(annotations=read_only)
    def read_map(name: str, include_checkpoint: bool = False) -> dict[str, Any]:
        """A map of the open ROM: axes, values[row][col], unit, formula, description.
        include_checkpoint adds the values the user started from (changes are compared to it)."""
        return with_rom(lambda s: tools.read_map(s, name, include_checkpoint))

    @server.tool(annotations=read_only)
    def read_reference_map(rom_path: str, name: str) -> dict[str, Any]:
        """A map from another ROM file (read-only), e.g. a stock ROM of a donor car for comparison.
        rom_path is absolute or relative to the folder of the open ROM."""

        def run(session: RomSession) -> EcuParser:
            _require_rom(session)
            path = tools.reference_path(rom_path, session.parser.file_path)
            parser = references.get(path)
            if parser is None:
                repo = session.parser.defs_repo
                if repo is None:
                    raise ToolError("Definitions are not loaded")
                try:
                    parser = EcuParser.open_rom(path, repo.path, defs_repo=repo)
                except LookupError as exc:
                    raise ToolError(str(exc)) from None
                references[path] = parser
            return parser

        return tools.read_reference_map(host.call(run), name)

    @server.tool()
    def show_map(name: str) -> str:
        """Open the map window in the editor so the user can see it."""

        def run(session: RomSession) -> str:
            _require_rom(session)
            map_id = tools.find_map(session.parser, name)
            host.show_map(map_id)
            return f"Opened {map_id.strip()}"

        return host.call(run)

    @server.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False))
    def propose_map_edit(
        name: str,
        reason: str,
        values: list[list[float]] | None = None,
        cells: list[dict[str, Any]] | None = None,
        x_axis: list[float] | None = None,
        y_axis: list[float] | None = None,
    ) -> dict[str, Any]:
        """Propose a change to a map; the user sees a diff and accepts or rejects it.
        Give either the whole matrix `values` (same shape as read_map) or `cells`
        [{"row": r, "col": c, "value": v}]; x_axis / y_axis replace the whole axis.
        Values are in definitions units. `reason` is shown to the user — explain why.
        Returns what was stored (values are quantized to the ROM data type) or that it was rejected."""
        proposal = with_rom(lambda s: tools.plan_edit(s, name, reason, values=values, cells=cells, x_axis=x_axis, y_axis=y_axis))
        if not host.confirm(proposal):
            return {"applied": False, "rejected_by_user": True, **tools.describe_proposal(proposal)}

        def apply(session: RomSession) -> dict[str, Any]:
            result = tools.apply_edit(session, proposal)
            host.applied(proposal)
            return result

        return host.call(apply)

    @server.tool(annotations=read_only)
    def list_logs(directory: str | None = None) -> list[dict[str, Any]]:
        """CSV logs in a folder (default: `logs` next to the open ROM, recursively)."""

        def folder(session: RomSession) -> Path:
            if directory:
                return Path(directory).expanduser()
            base = session.parser.file_path
            if not base:
                raise ToolError("No ROM is open; pass a directory")
            return Path(base).parent / "logs"

        return logs.list_logs(host.call(folder))

    @server.tool(annotations=read_only)
    def log_summary(path: str) -> dict[str, Any]:
        """Columns of a log with min / max / mean, row count and duration."""
        return logs.summary(path)

    @server.tool(annotations=read_only)
    def log_rows(
        path: str,
        columns: list[str],
        where: list[dict[str, Any]] | None = None,
        every: int = 1,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Rows of a log: chosen columns (name or unique substring), optional filter
        [{"column": "Engine Speed", "op": ">", "value": 3000}], every Nth row, up to `limit` (max 2000)."""
        return logs.rows(path, columns, where, every, limit)

    @server.tool(annotations=read_only)
    def log_to_map(
        path: str,
        map_name: str,
        value_column: str,
        x_column: str | None = None,
        y_column: str | None = None,
        where: list[dict[str, Any]] | None = None,
        stat: str = "mean",
        min_samples: int = 1,
    ) -> dict[str, Any]:
        """Bin a log column into the cells of a map (nearest axis point, like RomRaider scatter-to-table):
        e.g. Feedback Knock Correction over Base Timing with x_column "Engine Load", y_column "Engine Speed".
        Map axes are in definitions units: make sure the log columns use the same units.
        stat: mean | min | max | count. Returns grid[row][col] and sample counts."""

        def axes(session: RomSession) -> tuple[list[float], list[float]]:
            table = session.load_table(tools.find_map(session.parser, map_name))
            return (table.x_axis if table.x_labels is None else []), (table.y_axis if table.y_labels is None else [])

        x_axis, y_axis = with_rom(axes)
        return logs.to_map_grid(path, value_column, x_column, x_axis, y_column, y_axis, where, stat, min_samples)

    return server


class ServerThread:
    """HTTP-сервер MCP в фоновом потоке (uvicorn со своим event loop)."""

    def __init__(self, host: EditorHost, port: int = DEFAULT_PORT) -> None:
        self.port = port
        app = build_server(host).streamable_http_app(streamable_http_path=PATH, host="127.0.0.1")
        # Короткое завершение: открытые сессии клиентов не держат закрытие программы
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", lifespan="on", timeout_graceful_shutdown=1
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="mcp-server", daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{PATH}"

    def start(self, timeout: float = 5.0) -> None:
        """Запустить и дождаться, пока порт откроется; OSError — не удалось (порт занят)."""
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                self.stop()
                raise OSError(f"MCP server did not start on port {self.port}")
            time.sleep(0.05)

    def stop(self) -> None:
        self._server.should_exit = True
        if self._thread.is_alive():
            self._thread.join(timeout=5)
