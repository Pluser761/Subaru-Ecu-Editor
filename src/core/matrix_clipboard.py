from __future__ import annotations

from src.i18n import tr

def format_matrix(values: list[list[float]], decimals: int = 2) -> str:
    """TSV-матрица с decimals знаками после точки (удобно для Excel)."""
    lines: list[str] = []
    for row in values:
        lines.append("\t".join(f"{v:.{decimals}f}" for v in row))
    return "\n".join(lines)


def format_axis(values: list[float], decimals: int = 2) -> str:
    return "\t".join(f"{v:.{decimals}f}" for v in values)


def parse_number(text: str) -> float:
    normalized = text.strip().replace(",", ".")
    if not normalized:
        raise ValueError("empty")
    return float(normalized)


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if not line:
        return []
    if "\t" in line:
        return [cell.strip() for cell in line.split("\t")]
    if ";" in line:
        return [cell.strip() for cell in line.split(";")]
    # Несколько запятых → CSV; одна запятая → десятичный разделитель
    if line.count(",") > 1:
        return [cell.strip() for cell in line.split(",")]
    return [line]


def parse_matrix(text: str) -> list[list[float]]:
    """Разбор TSV/CSV из буфера. Пустые строки пропускаются."""
    raw = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not raw.strip():
        return []

    matrix: list[list[float]] = []
    for line in raw.split("\n"):
        cells = _split_row(line)
        if not cells:
            continue
        row: list[float] = []
        for cell in cells:
            if cell.strip() == "":
                continue
            row.append(parse_number(cell))
        if row:
            matrix.append(row)
    return matrix


def parse_axis(text: str) -> list[float]:
    """Одна линия или столбец значений оси."""
    matrix = parse_matrix(text)
    if not matrix:
        return []
    if len(matrix) == 1:
        return matrix[0]
    # столбец значений
    if all(len(row) == 1 for row in matrix):
        return [row[0] for row in matrix]
    # берём первую строку
    return matrix[0]


def matrix_shape(matrix: list[list[float]]) -> tuple[int, int]:
    if not matrix:
        return 0, 0
    return len(matrix), len(matrix[0])


def validate_rectangular(matrix: list[list[float]]) -> str | None:
    """None если матрица прямоугольная, иначе текст ошибки."""
    if not matrix:
        return tr("Буфер обмена пуст или не содержит чисел")
    width = len(matrix[0])
    if width == 0:
        return tr("Пустая строка в буфере обмена")
    for i, row in enumerate(matrix):
        if len(row) != width:
            return tr(
                "Матрица непрямоугольная: строка 1 = {width} кол., строка {row} = {count} кол.",
                width=width,
                row=i + 1,
                count=len(row),
            )
    return None


def validate_paste_size(
    paste_rows: int,
    paste_cols: int,
    *,
    sel_rows: int,
    sel_cols: int,
) -> str | None:
    """Буфер обязан точно совпасть с размером выделения."""
    if paste_rows <= 0 or paste_cols <= 0:
        return tr("Буфер обмена пуст или не содержит чисел")

    if paste_rows != sel_rows or paste_cols != sel_cols:
        return tr(
            "Размер не совпадает: буфер {rows}×{cols}, выделение {sel_rows}×{sel_cols}",
            rows=paste_rows,
            cols=paste_cols,
            sel_rows=sel_rows,
            sel_cols=sel_cols,
        )
    return None


def validate_axis_paste(value_count: int, *, target_count: int) -> str | None:
    """Буфер оси обязан точно совпасть с числом выбранных секций."""
    if value_count <= 0:
        return tr("Буфер обмена пуст или не содержит чисел")
    if value_count != target_count:
        return tr("Размер оси не совпадает: буфер {count}, выделение {selected}", count=value_count, selected=target_count)
    return None
