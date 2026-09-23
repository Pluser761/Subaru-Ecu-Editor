import sys

from PyQt6.QtWidgets import QApplication

from src.core.parser import EcuParser
from src.presenters.main_presenter import MainPresenter
from src.ui.main_window import MainWindow


def main() -> None:
    app = QApplication(sys.argv)

    window = MainWindow()
    MainPresenter(window, EcuParser())
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
