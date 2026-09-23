import sys

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from src.core.parser import EcuParser
from src.presenters.main_presenter import MainPresenter
from src.ui.app_settings import AppSettings
from src.ui.main_window import MainWindow


def main() -> None:
    app = QApplication(sys.argv)

    window = MainWindow()
    presenter = MainPresenter(window, EcuParser(), settings=AppSettings())
    window.show()
    # Прошлую прошивку открываем после показа окна: загрузка definitions занимает секунды
    QTimer.singleShot(0, presenter.restore_last_rom)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
