"""計算ツール"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP):
    """計算ツールを MCP サーバーに登録する。"""

    @mcp.tool()
    async def calculate(expression: str) -> str:
        """数式を計算します。基本的な四則演算、数学関数、定数が使用できます。

        使用可能な演算子: +, -, *, /, //, %, **, ()
        使用可能な関数: sin, cos, tan, log, log10, sqrt, abs, round, pow, etc.
        使用可能な定数: pi, e

        Args:
            expression: 計算したい数式（例: "2 + 3 * 4", "sin(pi/2)", "sqrt(16)"）
        """
        expression = expression.strip()
        if not expression:
            return "計算式が入力されていません。"

        try:
            dangerous_patterns = [
                r'import\s+', r'__', r'exec', r'eval', r'open', r'file',
                r'input', r'raw_input', r'compile', r'globals', r'locals',
                r'vars', r'dir', r'hasattr', r'getattr', r'setattr', r'delattr'
            ]

            for pattern in dangerous_patterns:
                if re.search(pattern, expression, re.IGNORECASE):
                    return f"安全上の理由により、この式は計算できません: {expression}"

            safe_dict = {
                "__builtins__": {},
                "sin": math.sin, "cos": math.cos, "tan": math.tan,
                "asin": math.asin, "acos": math.acos, "atan": math.atan,
                "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
                "log": math.log, "log10": math.log10, "log2": math.log2,
                "ln": math.log,
                "sqrt": math.sqrt, "cbrt": lambda x: x ** (1 / 3),
                "exp": math.exp,
                "floor": math.floor, "ceil": math.ceil,
                "degrees": math.degrees, "radians": math.radians,
                "factorial": math.factorial,
                "abs": abs, "round": round, "min": min, "max": max,
                "sum": sum, "pow": pow,
                "int": int, "float": float,
                "pi": math.pi, "e": math.e, "tau": math.tau,
                "inf": math.inf, "nan": math.nan,
            }

            expression = expression.replace("^", "**")
            expression = expression.replace("×", "*")
            expression = expression.replace("÷", "/")

            expression = re.sub(r'(\d)([a-zA-Z])', r'\1*\2', expression)
            expression = re.sub(r'(\d)\(', r'\1*(', expression)
            expression = re.sub(r'\)(\d)', r')*\1', expression)
            expression = re.sub(r'\)([a-zA-Z])', r')*\1', expression)
            expression = re.sub(r'([a-zA-Z])\(', r'\1(', expression)

            result = eval(expression, safe_dict, {})

            if isinstance(result, (int, float)):
                if math.isnan(result):
                    return f"{expression} = NaN（非数）"
                elif math.isinf(result):
                    return f"{expression} = ∞（無限大）"
                elif isinstance(result, float) and result.is_integer():
                    result = int(result)
                elif isinstance(result, float):
                    if abs(result) < 1e-10:
                        result = 0
                    else:
                        result = round(result, 12)

            return f"{expression} = {result}"

        except ZeroDivisionError:
            return f"計算エラー: ゼロで割ることはできません（{expression}）"
        except ValueError as e:
            return f"計算エラー: 値が範囲外または無効です（{expression}）- {str(e)}"
        except SyntaxError:
            return f"構文エラー: 式の形式が正しくありません（{expression}）"
        except NameError as e:
            return f"計算エラー: 未知の変数または関数です（{expression}）- {str(e)}"
        except OverflowError:
            return f"計算エラー: 結果が大きすぎます（{expression}）"
        except Exception as e:
            return f"計算エラー: {str(e)}（{expression}）"
