"""Окно MAF-скейлинга: параметры в опросе, точки, кривая и запись новых значений."""

from __future__ import annotations

from PyQt6.QtCore import Qt

from src.logger.core.maf import MafTable
from src.logger.core.session import Sample
from src.logger.presenter import LoggerPresenter, RomInfo
from src.logger.ui.dashboard import TILE_GAP, TILE_WIDTH
from src.logger.ui.window import LoggerWindow


class FakePoller:
    def __init__(self) -> None:
        self.selection: list[tuple[str, str | None]] = []


class FakeRunner:
    """Заглушка потока опроса: только набор параметров."""

    def __init__(self) -> None:
        self.poller = FakePoller()

    def select(self, selection: list[tuple[str, str | None]]) -> None:
        self.poller.selection = list(selection)

    def stop_recording(self) -> None:
        return None

    def stop(self) -> None:
        return None


def _sample(seq: int, volts: float, trim: float) -> Sample:
    """Опрос раз в 10 с — медленный подъём напряжения проходит фильтр скорости.

    A/F Correction #1 и A/F Learning #1 в сумме дают trim.
    """
    values = {
        "P18": volts,
        "P12": 40.0,
        "P3": trim / 2,
        "P4": trim / 2,
        "P8": 2000.0,
        "P2": 90.0,
        "P11": 30.0,
        "E3": 8.0,
        "E23": 0.0,
    }
    return Sample(float(seq) * 10000.0, values, 0)


def _presenter(qapp) -> LoggerPresenter:  # type: ignore[no-untyped-def]
    presenter = LoggerPresenter(
        LoggerWindow(), None, lambda: RomInfo(), find_ports=lambda: []
    )
    presenter.runner = FakeRunner()  # type: ignore[assignment]
    return presenter


def test_maf_is_separate_window(qapp) -> None:  # type: ignore[no-untyped-def]
    presenter = _presenter(qapp)
    assert presenter.maf_window.isWindow()
    assert presenter.maf_window.windowTitle()
    assert presenter.maf_window.parent() is None


def test_record_adds_hidden_params(qapp) -> None:  # type: ignore[no-untyped-def]
    presenter = _presenter(qapp)
    # Галочка в окне MAF — через его сигнал, как у пользователя
    presenter.maf_window._record.setChecked(True)  # type: ignore[attr-defined]
    ids = [pid for pid, _units in presenter.runner.poller.selection]  # type: ignore[union-attr]
    assert {"P18", "P3", "P4", "P8", "P2", "P11", "E3", "E33", "E23", "E54"} <= set(ids)
    presenter.maf_window._record.setChecked(False)  # type: ignore[attr-defined]
    assert presenter.runner.poller.selection == []  # скрытые параметры убраны из опроса  # type: ignore[union-attr]


def test_close_window_stops_record(qapp) -> None:  # type: ignore[no-untyped-def]
    presenter = _presenter(qapp)
    presenter.maf_window._record.setChecked(True)  # type: ignore[attr-defined]
    assert presenter.runner.poller.selection  # type: ignore[union-attr]
    presenter.maf_window.close()
    assert presenter.runner.poller.selection == []  # type: ignore[union-attr]
    assert not presenter.maf_window._record.isChecked()  # type: ignore[attr-defined]


def test_points_curve_and_apply(qapp) -> None:  # type: ignore[no-untyped-def]
    applied: dict[int, float] = {}

    def table() -> MafTable:
        return MafTable("MAF Sensor Scaling", (1.0, 2.0, 3.0), (10.0, 20.0, 30.0))

    def apply(name: str, values: dict[int, float]) -> int:
        applied.update(values)
        return len(values)

    presenter = LoggerPresenter(
        LoggerWindow(), None, lambda: RomInfo(), find_ports=lambda: [],
        maf_table=table, maf_apply=apply,
    )
    presenter.runner = FakeRunner()  # type: ignore[assignment]
    presenter._on_maf_record(True)
    for seq in range(4):
        presenter._on_sample(_sample(seq, 1.5 + seq * 0.5, 2.0 + seq * 3.0))
    presenter._refresh_maf()
    xs, ys = presenter.maf_window._scatter.getData()  # type: ignore[attr-defined]
    assert list(xs) == [1.5, 2.0, 2.5, 3.0]
    assert list(ys) == [2.0, 5.0, 8.0, 11.0]

    presenter.interpolate_maf(3)
    curve_x, curve_y = presenter.maf_window._curve.getData()  # type: ignore[attr-defined]
    assert len(curve_x) == 200 and len(curve_y) == 200

    presenter.apply_maf(1.9, 2.6)
    assert sorted(applied) == [1]  # строки 1.0 и 3.0 В вне диапазона
    assert applied[1] == 20.0 * (1.0 + 5.0 / 100.0)
    presenter.disconnect()
    presenter.maf_window.close()


def test_apply_without_curve_is_error(qapp) -> None:  # type: ignore[no-untyped-def]
    presenter = LoggerPresenter(LoggerWindow(), None, lambda: RomInfo(), find_ports=lambda: [])
    presenter.apply_maf(1.2, 2.6)
    assert "Интерполировать" in presenter.maf_window._info.text()  # type: ignore[attr-defined]
    presenter.disconnect()
    presenter.maf_window.close()


def test_dashboard_grid_size(qapp) -> None:  # type: ignore[no-untyped-def]
    presenter = _presenter(qapp)
    dashboard = presenter.window.dashboard
    width, height, columns = dashboard.grid_size(4)
    assert columns == 2
    assert width == 2 * TILE_WIDTH + TILE_GAP
    assert height > 0
    assert dashboard.grid_size(5)[2] == 3  # 3×2 — компактнее, чем 5 в ряд


def test_menu_action_opens_maf_window(qapp) -> None:  # type: ignore[no-untyped-def]
    from src.presenters.main_presenter import MainPresenter
    from src.ui.main_window import MainWindow

    window = MainWindow()
    presenter = MainPresenter(window)
    assert window.maf_action.shortcutContext() == Qt.ShortcutContext.ApplicationShortcut
    window.maf_action.trigger()
    assert presenter._logger is not None
    assert presenter._logger.maf_window.isVisible()


def test_logger_window_fits_dashboard(qapp) -> None:  # type: ignore[no-untyped-def]
    from PyQt6.QtCore import QRect

    from src.logger.ui.window import LiveRow

    class Screen:
        def availableGeometry(self) -> QRect:
            return QRect(0, 0, 1600, 1000)

    window = LoggerWindow()
    window.screen = lambda: Screen()  # type: ignore[assignment]
    window.show()
    qapp.processEvents()

    def rows(n: int) -> list[LiveRow]:
        return [LiveRow(f"P{i}", f"Param {i}", "rpm") for i in range(n)]

    window.set_live_rows(rows(2))
    qapp.processEvents()
    narrow = window.width()
    window.set_live_rows(rows(12))
    qapp.processEvents()
    assert window.width() > narrow  # 12 плиток — шире, чем 2

    # Ручной размер уважаем: дальше окно не подгоняется
    manual = narrow + 40
    window.resize(manual, 600)
    qapp.processEvents()
    window.set_live_rows(rows(2))
    qapp.processEvents()
    assert window.width() == manual
