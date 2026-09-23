from __future__ import annotations

import ast
import math
import operator
from functools import lru_cache
from typing import Any

import numpy as np


_BIN_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
}

_UNARY_OPS: dict[type, Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def eval_expression(expression: str, x: float) -> float:
    """Безопасный eval scaling-выражения RomRaider (только арифметика и x)."""
    expr = expression.strip()
    if not expr:
        return x
    tree = _parse(expr)
    try:
        return float(_eval_node(tree.body, {"x": float(x)}))
    except ZeroDivisionError:
        # «96560.6/x» при x=0: RomRaider показывает Infinity — делаем так же,
        # чтобы одна ячейка не роняла загрузку всей таблицы
        return math.inf


def eval_expression_array(expression: str, x: np.ndarray) -> np.ndarray:
    """То же выражение сразу для массива x (numpy); деление на 0 даёт inf/nan."""
    expr = expression.strip()
    values = np.asarray(x, dtype=float)
    if not expr:
        return values.copy()
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        result = _eval_node(_parse(expr).body, {"x": values})
    return np.broadcast_to(np.asarray(result, dtype=float), values.shape).copy()


@lru_cache(maxsize=512)
def _parse(expr: str) -> ast.Expression:
    return ast.parse(expr, mode="eval")


# Узлы работают и со скалярами, и с numpy-массивами: приводит к float вызывающий
def _eval_node(node: ast.AST, env: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, env)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id not in env:
            raise ValueError(f"Unknown name in expression: {node.id}")
        return env[node.id]
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
        return op(_eval_node(node.left, env), _eval_node(node.right, env))
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"Unsupported unary: {type(node.op).__name__}")
        return op(_eval_node(node.operand, env))
    if isinstance(node, ast.Call):
        raise ValueError("Function calls are not allowed in scaling expressions")
    raise ValueError(f"Unsupported expression node: {type(node).__name__}")
