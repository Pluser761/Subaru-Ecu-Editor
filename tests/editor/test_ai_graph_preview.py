"""Предложение ИИ на графике: данные для окна графика и режим предпросмотра в EditorView."""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication

from src.presenters.ai_presenter import graph_preview
from src.ui.ai_edit_dialog import EditPreview
from src.ui.editor_view import EditorView


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def _vector_preview() -> EditPreview:
    return EditPreview(
        name="Vec",
        reason="",
        unit="deg",
        old=[[1.0], [2.0], [3.0]],
        new=[[1.0], [2.5], [3.0]],
        y_old=[10.0, 20.0, 30.0],
        y_new=[10.0, 20.0, 35.0],
    )


def test_graph_preview_data() -> None:
    data = graph_preview(_vector_preview())
    assert data["values"] == [[1.0], [2.5], [3.0]]
    assert data["reference"] == [[1.0], [2.0], [3.0]]
    assert data["y_axis"] == [10.0, 20.0, 35.0]
    assert data["selected"] == {(1, 0)}


def test_editor_view_preview_overrides_current_map(app: QApplication) -> None:
    view = EditorView()
    graph = view.graph_window
    view.show_graph_preview(graph_preview(_vector_preview()))
    assert graph.isVisible()
    # Прежние значения — второй серией на 2D-графике
    assert list(graph.curve._reference.yData) == [1.0, 2.0, 3.0]
    view.clear_graph_preview()
    # Открытых карт нет — после предпросмотра графику нечего показывать
    assert not graph.isVisible()
    assert graph.curve._reference.xData is None or len(graph.curve._reference.xData) == 0
