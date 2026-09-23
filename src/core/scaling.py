from __future__ import annotations

import ast
import math
import operator
from typing import Any


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
    tree = ast.parse(expr, mode="eval")
    try:
        return float(_eval_node(tree.body, {"x": float(x)}))
    except ZeroDivisionError:
        # «96560.6/x» при x=0: RomRaider показывает Infinity — делаем так же,
        # чтобы одна ячейка не роняла загрузку всей таблицы
        return math.inf


def _eval_node(node: ast.AST, env: dict[str, float]) -> float:
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
        return float(op(_eval_node(node.left, env), _eval_node(node.right, env)))
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"Unsupported unary: {type(node.op).__name__}")
        return float(op(_eval_node(node.operand, env)))
    if isinstance(node, ast.Call):
        raise ValueError("Function calls are not allowed in scaling expressions")
    raise ValueError(f"Unsupported expression node: {type(node).__name__}")
