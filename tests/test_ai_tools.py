"""Инструменты ИИ на реальной прошивке (пропускаются без ecu/) и сквозной вызов через MCP по HTTP."""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any, Callable, TypeVar

import pytest
from mcp import Client

from src.ai import tools
from src.ai.server import ServerThread
from src.ai.tools import EditProposal, ToolError
from src.core.parser import EcuParser
from src.core.session import RomSession

T = TypeVar("T")
BOOST = "Target Boost"  # в definitions — «Target Boost » с пробелом


@pytest.fixture
def session(jdm: EcuParser) -> RomSession:
    return RomSession(jdm)


def test_find_map_ignores_spaces_and_case_and_suggests(session: RomSession):
    assert tools.find_map(session.parser, "target boost") == "Target Boost "
    with pytest.raises(ToolError, match="Did you mean"):
        tools.find_map(session.parser, "Target Bost")


def test_read_map_layout(session: RomSession):
    data = tools.read_map(session, BOOST)
    rows, cols = map(int, data["shape"].split("x"))
    assert len(data["values"]) == rows and len(data["values"][0]) == cols
    assert len(data["x_axis"]["values"]) == cols and len(data["y_axis"]["values"]) == rows


def test_plan_edit_lists_only_changed_cells_and_does_not_write(session: RomSession):
    before = session.load_table("Target Boost ").values
    proposal = tools.plan_edit(session, BOOST, "test", cells=[
        {"row": 0, "col": 0, "value": before[0][0] + 1},
        {"row": 1, "col": 1, "value": before[1][1]},  # без изменения — не попадает
    ])
    assert [(c.row, c.col) for c in proposal.cells] == [(0, 0)]
    assert session.load_table("Target Boost ").values == before
    assert not session.dirty


def test_plan_edit_validates(session: RomSession):
    table = session.load_table("Target Boost ")
    with pytest.raises(ToolError, match="must be"):
        tools.plan_edit(session, BOOST, "", values=[[1.0]])
    with pytest.raises(ToolError, match="outside"):
        tools.plan_edit(session, BOOST, "", cells=[{"row": 99, "col": 0, "value": 1}])
    with pytest.raises(ToolError, match="Nothing to change"):
        tools.plan_edit(session, BOOST, "", values=table.values)
    with pytest.raises(ToolError, match="finite"):
        tools.plan_edit(session, BOOST, "", cells=[{"row": 0, "col": 0, "value": float("nan")}])


def test_apply_edit_writes_and_reports_quantization(session: RomSession):
    proposal = tools.plan_edit(session, BOOST, "test", cells=[{"row": 0, "col": 0, "value": 3.14159}])
    result = tools.apply_edit(session, proposal)
    stored = session.load_table("Target Boost ").values[0][0]
    assert result["cells_written"] == 1
    assert session.dirty and session.refresh_changed({"Target Boost "}) == {"Target Boost "}
    assert stored == pytest.approx(3.14159, abs=0.1)
    assert result["quantized_cells"] == 1  # 3.14159 в uint8/uint16 не ложится ровно
    assert result["largest_quantization"][0]["stored"] == round(stored, 4)


def test_plan_edit_shows_stored_values_and_drops_rounding_noise(session: RomSession):
    table = session.load_table("Target Boost ")
    rounded = [[round(v, 4) for v in row] for row in table.values]  # как отдаёт read_map
    rounded[0][0] = table.values[0][0] + 1.00001
    proposal = tools.plan_edit(session, BOOST, "", values=rounded)
    assert [(c.row, c.col) for c in proposal.cells] == [(0, 0)]
    cell = proposal.cells[0]
    assert cell.requested == rounded[0][0]
    # В плане — уже значение после квантования: его пользователь и подтверждает
    tools.apply_edit(session, proposal)
    assert session.load_table("Target Boost ").values[0][0] == cell.new


def test_apply_refuses_when_map_changed_while_waiting(session: RomSession):
    proposal = tools.plan_edit(session, BOOST, "", cells=[{"row": 0, "col": 0, "value": 5.0}])
    session.write_cell("Target Boost ", 0, 1, 7.0)  # пользователь поправил карту, пока думал
    with pytest.raises(ToolError, match="changed"):
        tools.apply_edit(session, proposal)


def test_shared_axis_edit_lists_other_tables(session: RomSession):
    axis = session.load_table("Idle Speed Target A").y_axis
    proposal = tools.plan_edit(session, "Idle Speed Target A", "", y_axis=[*axis[:4], axis[4] + 1, *axis[5:]])
    assert {n for n, _ in proposal.shared} == {"Idle Speed Target B", "Idle Speed Target C"}


# --- Сквозной вызов через MCP по HTTP ---------------------------------------------------


class FakeHost:
    """Хост без Qt: всё в одном потоке, ответ пользователя задан заранее."""

    def __init__(self, session: RomSession, accept: bool) -> None:
        self.session = session
        self.accept = accept
        self.applied_maps: list[str] = []

    def call(self, fn: Callable[[RomSession], T]) -> T:
        return fn(self.session)

    def confirm(self, proposal: EditProposal) -> bool:
        return self.accept

    def applied(self, proposal: EditProposal) -> None:
        self.applied_maps.append(proposal.map_id)

    def show_map(self, map_id: str) -> None:
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _call(url: str, tool: str, args: dict[str, Any]) -> Any:
    async def run() -> Any:
        async with Client(url) as client:
            result = await client.call_tool(tool, args)
            assert not result.is_error, result.content
            return json.loads(result.content[0].text)  # type: ignore[union-attr]

    return asyncio.run(run())


@pytest.fixture
def served():
    servers: list[ServerThread] = []

    def start(host: FakeHost) -> str:
        server = ServerThread(host, _free_port())
        server.start()
        servers.append(server)
        return server.url

    yield start
    for server in servers:
        server.stop()


def test_mcp_without_rom(served):
    url = served(FakeHost(RomSession(EcuParser()), accept=False))
    assert _call(url, "rom_info", {}) == {"open": False}


def test_mcp_propose_edit_respects_user_answer(served, session: RomSession):
    before = session.load_table("Target Boost ").values[0][0]
    args = {"name": BOOST, "reason": "test", "cells": [{"row": 0, "col": 0, "value": before + 1}]}

    rejected = FakeHost(session, accept=False)
    result = _call(served(rejected), "propose_map_edit", args)
    assert result["applied"] is False and result["rejected_by_user"] is True
    assert session.load_table("Target Boost ").values[0][0] == before

    accepted = FakeHost(session, accept=True)
    result = _call(served(accepted), "propose_map_edit", args)
    assert result["applied"] is True and accepted.applied_maps == ["Target Boost "]
    assert session.load_table("Target Boost ").values[0][0] == pytest.approx(before + 1, abs=0.1)
