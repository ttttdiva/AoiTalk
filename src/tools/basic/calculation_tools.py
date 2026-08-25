"""
Calculation tools for mathematical operations
"""

from ..bounded_calculator import (
    BoundedCalculatorError,
    evaluate_bounded_expression,
    format_bounded_result,
)
from ..core import tool


def calculate_impl(expression: str) -> str:
    """数式を計算します（純粋関数版）。基本的な四則演算、数学関数、定数が使用できます。
    
    使用可能な演算子: +, -, *, /, //, %, **, ()
    使用可能な関数: sin, cos, tan, log, log10, sqrt, abs, round, pow, etc.
    使用可能な定数: pi, e
    
    Args:
        expression: 計算したい数式（例: "2 + 3 * 4", "sin(pi/2)", "sqrt(16)"）
        
    Returns:
        計算結果の文字列
    """
    print(f"[Tool] calculate が呼び出されました: {expression}")
    
    try:
        normalized, result = evaluate_bounded_expression(expression)
        response = format_bounded_result(normalized, result)
        print(f"[Tool] calculate 結果: {response}")
        return response
        
    except BoundedCalculatorError as exc:
        error_msg = str(exc)
        print(f"[Tool] calculate エラー: {error_msg}")
        return error_msg
    except ZeroDivisionError:
        error_msg = f"計算エラー: ゼロで割ることはできません（{expression.strip()}）"
        print(f"[Tool] calculate エラー: {error_msg}")
        return error_msg
    except ValueError as exc:
        error_msg = (
            f"計算エラー: 値が範囲外または無効です（{expression.strip()}）"
        )
        print(f"[Tool] calculate エラー: {error_msg}")
        return error_msg
    except SyntaxError:
        error_msg = f"構文エラー: 式の形式が正しくありません（{expression.strip()}）"
        print(f"[Tool] calculate エラー: {error_msg}")
        return error_msg
    except NameError as exc:
        error_msg = f"計算エラー: 未知の変数または関数です（{expression.strip()}）"
        print(f"[Tool] calculate エラー: {error_msg}")
        return error_msg
    except OverflowError:
        error_msg = f"計算エラー: 結果が大きすぎます（{expression.strip()}）"
        print(f"[Tool] calculate エラー: {error_msg}")
        return error_msg
    except Exception:
        error_msg = "計算エラー: 式を評価できませんでした"
        print(f"[Tool] calculate エラー: {error_msg}")
        return error_msg


@tool
def calculate(expression: str) -> str:
    """数式を計算します。基本的な四則演算、数学関数、定数が使用できます。
    
    使用可能な演算子: +, -, *, /, //, %, **, ()
    使用可能な関数: sin, cos, tan, log, log10, sqrt, abs, round, pow, etc.
    使用可能な定数: pi, e
    
    Args:
        expression: 計算したい数式（例: "2 + 3 * 4", "sin(pi/2)", "sqrt(16)"）
        
    Returns:
        計算結果の文字列
    """
    return calculate_impl(expression)
