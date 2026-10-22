"""Где лежат логи прошивки: `<корень>/<имя прошивки>/`.

Корень по умолчанию — `ecu-editor-logs` рядом с файлом прошивки; пользователь может выбрать
свой («Папка логов…»), подпапка по имени прошивки добавляется всегда, чтобы логи разных
прошивок не перемешивались. Этим же правилом пользуется ИИ (list_logs).
"""

from __future__ import annotations

from pathlib import Path

LOGS_DIR_NAME = "ecu-editor-logs"
NO_ROM = "no-rom"


def default_logs_root(rom_path: str | Path | None) -> Path:
    if rom_path:
        return Path(rom_path).parent / LOGS_DIR_NAME
    return Path.home() / LOGS_DIR_NAME


def rom_log_dir(rom_path: str | Path | None, root: str | Path | None = None) -> Path:
    """Папка логов прошивки: `<root>/<имя файла прошивки без расширения>`."""
    base = Path(root) if root else default_logs_root(rom_path)
    return base / (Path(rom_path).stem if rom_path else NO_ROM)
