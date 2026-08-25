"""Bounded AST-based calculator used by tool surfaces."""

from __future__ import annotations

import ast
import math
import operator
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

MAX_EXPRESSION_LENGTH = 512
MAX_AST_NODES = 200
MAX_AST_DEPTH = 20
MAX_FACTORIAL_ARG = 20
MAX_ABS_INT_DIGITS = 10000
MAX_RESULT_TEXT_LENGTH = 256
MAX_EVALUATION_SECONDS = 1.0


class BoundedCalculatorError(ValueError):
    """Raised when an expression exceeds calculator safety limits."""


_DANGEROUS_PATTERNS = [
    r"import\s+",
    r"__",
    r"exec",
    r"eval",
    r"open",
    r"file",
    r"input",
    r"raw_input",
    r"compile",
    r"globals",
    r"locals",
    r"vars",
    r"dir",
    r"hasattr",
    r"getattr",
    r"setattr",
    r"delattr",
]

_MAX_POW_EXPONENT = 64
_MAX_INT_BIT_LENGTH = 4096

_BINOPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_UNARYOPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _check_numeric_magnitude(value: Any) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        if value.bit_length() > _MAX_INT_BIT_LENGTH:
            raise BoundedCalculatorError("数値が大きすぎます")
    elif isinstance(value, float) and math.isfinite(value):
        if abs(value) > float(10**min(MAX_ABS_INT_DIGITS, 308)):
            raise BoundedCalculatorError("数値が大きすぎます")


def _bounded_factorial(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BoundedCalculatorError("factorial は非負整数のみ受け付けます")
    if value < 0:
        raise ValueError("factorial の引数は非負である必要があります")
    if value > MAX_FACTORIAL_ARG:
        raise BoundedCalculatorError(
            f"factorial の引数は {MAX_FACTORIAL_ARG} 以下である必要があります"
        )
    result = math.factorial(value)
    _check_numeric_magnitude(result)
    return result


def _bounded_cbrt(value: Any) -> float:
    _check_numeric_magnitude(value)
    result = float(value) ** (1 / 3)
    _check_numeric_magnitude(result)
    return result


def _bounded_pow(base: Any, exponent: Any) -> Any:
    if isinstance(exponent, bool) or not isinstance(exponent, (int, float)):
        raise BoundedCalculatorError("pow の指数が無効です")
    if isinstance(exponent, float) and not exponent.is_integer():
        raise BoundedCalculatorError("pow の指数は整数である必要があります")
    exponent_value = int(exponent)
    if abs(exponent_value) > _MAX_POW_EXPONENT:
        raise BoundedCalculatorError("pow の指数が大きすぎます")
    _check_numeric_magnitude(base)
    result = pow(base, exponent_value)
    _check_numeric_magnitude(result)
    return result


_SAFE_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "sinh": math.sinh,
    "cosh": math.cosh,
    "tanh": math.tanh,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "ln": math.log,
    "sqrt": math.sqrt,
    "cbrt": _bounded_cbrt,
    "exp": math.exp,
    "floor": math.floor,
    "ceil": math.ceil,
    "degrees": math.degrees,
    "radians": math.radians,
    "factorial": _bounded_factorial,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "pow": _bounded_pow,
    "int": int,
    "float": float,
}

_SAFE_CONSTANTS: dict[str, Any] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
    "inf": math.inf,
    "nan": math.nan,
}


@dataclass
class _AstLimits:
    node_count: int = 0
    max_depth: int = 0


def _validate_ast_limits(tree: ast.AST) -> None:
    limits = _AstLimits()

    def visit(node: ast.AST, depth: int) -> None:
        limits.node_count += 1
        limits.max_depth = max(limits.max_depth, depth)
        if limits.node_count > MAX_AST_NODES:
            raise BoundedCalculatorError("式が複雑すぎます")
        if limits.max_depth > MAX_AST_DEPTH:
            raise BoundedCalculatorError("式のネストが深すぎます")
        for child in ast.iter_child_nodes(node):
            visit(child, depth + 1)

    visit(tree, 1)


def _normalize_expression(expression: str) -> str:
    normalized = expression.strip()
    if not normalized:
        raise BoundedCalculatorError("計算式が入力されていません")
    if len(normalized) > MAX_EXPRESSION_LENGTH:
        raise BoundedCalculatorError(
            f"計算式は {MAX_EXPRESSION_LENGTH} 文字以下である必要があります"
        )
    for pattern in _DANGEROUS_PATTERNS:
        if re.search(pattern, normalized, re.IGNORECASE):
            raise BoundedCalculatorError(
                f"安全上の理由により、この式は計算できません: {normalized}"
            )
    normalized = normalized.replace("^", "**")
    normalized = normalized.replace("×", "*")
    normalized = normalized.replace("÷", "/")
    normalized = re.sub(r"(\d)([a-zA-Z])", r"\1*\2", normalized)
    normalized = re.sub(r"(\d)\(", r"\1*(", normalized)
    normalized = re.sub(r"\)(\d)", r")*\1", normalized)
    normalized = re.sub(r"\)([a-zA-Z])", r")*\1", normalized)
    normalized = re.sub(r"([a-zA-Z])\(", r"\1(", normalized)
    return normalized


class _SafeEvaluator(ast.NodeVisitor):
    def __init__(self, *, deadline: float) -> None:
        self._deadline = deadline

    def _check_budget(self) -> None:
        if time.monotonic() >= self._deadline:
            raise BoundedCalculatorError("計算時間が上限を超えました")

    def visit(self, node: ast.AST) -> Any:
        self._check_budget()
        if not isinstance(
            node,
            (
                ast.Expression,
                ast.BinOp,
                ast.UnaryOp,
                ast.Call,
                ast.Name,
                ast.Constant,
                ast.Load,
            ),
        ):
            raise BoundedCalculatorError("許可されていない式です")
        return super().visit(node)

    def visit_Expression(self, node: ast.Expression) -> Any:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Any:
        if isinstance(node.value, (int, float)):
            if isinstance(node.value, int) and len(str(abs(node.value))) > MAX_ABS_INT_DIGITS:
                raise BoundedCalculatorError("数値リテラルが大きすぎます")
            return node.value
        raise BoundedCalculatorError("許可されていない定数です")

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id in _SAFE_CONSTANTS:
            return _SAFE_CONSTANTS[node.id]
        raise BoundedCalculatorError(f"未知の変数または関数です: {node.id}")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        operator_fn = _UNARYOPS.get(type(node.op))
        if operator_fn is None:
            raise BoundedCalculatorError("許可されていない単項演算子です")
        return operator_fn(self.visit(node.operand))

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        left = self.visit(node.left)
        right = self.visit(node.right)
        if isinstance(node.op, ast.Pow):
            return _bounded_pow(left, right)
        operator_fn = _BINOPS.get(type(node.op))
        if operator_fn is None:
            raise BoundedCalculatorError("許可されていない二項演算子です")
        result = operator_fn(left, right)
        _check_numeric_magnitude(result)
        return result

    def visit_Call(self, node: ast.Call) -> Any:
        if node.keywords:
            raise BoundedCalculatorError("キーワード引数は使用できません")
        if not isinstance(node.func, ast.Name):
            raise BoundedCalculatorError("許可されていない関数呼び出しです")
        function = _SAFE_FUNCTIONS.get(node.func.id)
        if function is None:
            raise BoundedCalculatorError(f"未知の変数または関数です: {node.func.id}")
        args = [self.visit(arg) for arg in node.args]
        result = function(*args)
        _check_numeric_magnitude(result)
        return result


def evaluate_bounded_expression(expression: str) -> tuple[str, Any]:
    """Evaluate a math expression under bounded resource limits."""

    normalized = _normalize_expression(expression)
    tree = ast.parse(normalized, mode="eval")
    _validate_ast_limits(tree)
    result = _SafeEvaluator(deadline=time.monotonic() + MAX_EVALUATION_SECONDS).visit(tree)
    return normalized, result


def format_bounded_result(normalized_expression: str, result: Any) -> str:
    _check_numeric_magnitude(result)
    if isinstance(result, (int, float)):
        if math.isnan(result):
            return f"{normalized_expression} = NaN（非数）"
        if math.isinf(result):
            return f"{normalized_expression} = ∞（無限大）"
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        elif isinstance(result, float):
            if abs(result) < 1e-10:
                result = 0
            else:
                result = round(result, 12)
    rendered = f"{normalized_expression} = {result}"
    if len(rendered) > MAX_RESULT_TEXT_LENGTH:
        raise BoundedCalculatorError("結果の表示サイズが上限を超えています")
    return rendered
