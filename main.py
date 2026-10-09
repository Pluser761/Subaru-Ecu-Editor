import sys

from PyQt6.QtCore import QLibraryInfo, QLocale, QTimer, QTranslator
from PyQt6.QtWidgets import QApplication

from src.core.parser import EcuParser
from src.i18n import Language, set_language
from src.presenters.main_presenter import MainPresenter
from src.ui.app_settings import AppSettings
from src.ui.main_window import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    settings = AppSettings()
    # Язык — до создания окон: подписи переводятся при построении виджетов
    language = settings.language()
    set_language(language)
    qt_translator = QTranslator(app)
    if language is Language.RU and qt_translator.load(
        "qtbase_ru", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    ):
        app.installTranslator(qt_translator)  # стандартные кнопки Qt («Да», «Отмена»)
    if language is Language.EN:
        # Числа в полях ввода — с точкой, даже если система русская
        QLocale.setDefault(QLocale(QLocale.Language.English, QLocale.Country.UnitedStates))

    window = MainWindow()
    presenter = MainPresenter(window, EcuParser(), settings=settings)
    window.show()
    # Прошлую прошивку открываем после показа окна: загрузка definitions занимает секунды
    QTimer.singleShot(0, presenter.restore_last_rom)
    QTimer.singleShot(0, presenter.restore_ai_server)
    app.aboutToQuit.connect(presenter.stop_ai_server)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
