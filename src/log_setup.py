"""Настройка логирования: файл событий рядом с settings.ini и перехват необработанных
исключений (в потоке интерфейса и в фоновых потоках) — в сборке без консоли падения не
теряются, а попадают в `events.log`.

События адаптера (обрыв, попытки переподключения с причинами) пишутся сюда обычным
`logging`; байты обмена — в trace-файл той же папки. `events.log` ротируется по размеру.
"""

from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import cast

LOG_FILE = "events.log"
_MAX_BYTES = 5_000_000
_BACKUPS = 3

_configured = False


def setup_logging(log_dir: Path) -> None:
    """Настроить файл событий (идемпотентно) и перехват необработанных исключений."""
    global _configured
    if _configured:
        return
    _configured = True
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    def _hook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        logging.getLogger("uncaught").critical("необработанное исключение", exc_info=(exc_type, exc, tb))

    sys.excepthook = _hook

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        logging.getLogger("thread").critical(
            "необработанное исключение в потоке %s",
            args.thread.name if args.thread is not None else "?",
            exc_info=(args.exc_type, cast(BaseException, args.exc_value), args.exc_traceback),
        )

    threading.excepthook = _thread_hook
