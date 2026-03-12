"""
Calculation tools for mathematical operations
"""
import math
import re
from ..core import tool as function_tool


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
    
    # 入力の前処理
    expression = expression.strip()
    if not expression:
        return "計算式が入力されていません。"
    
    try:
        # 危険な文字列をチェック
        dangerous_patterns = [
            r'import\s+', r'__', r'exec', r'eval', r'open', r'file',
            r'input', r'raw_input', r'compile', r'globals', r'locals',
            r'vars', r'dir', r'hasattr', r'getattr', r'setattr', r'delattr'
        ]
        
        for pattern in dangerous_patterns:
            if re.search(pattern, expression, re.IGNORECASE):
                return f"安全上の理由により、この式は計算できません: {expression}"
        
        # 安全な名前空間を作成
        safe_dict = {
            "__builtins__": {},
            # 基本演算子（evalで自動的に利用可能）
            # 数学関数
            "sin": math.sin, "cos": math.cos, "tan": math.tan,
            "asin": math.asin, "acos": math.acos, "atan": math.atan,
            "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
            "log": math.log, "log10": math.log10, "log2": math.log2,
            "ln": math.log,  # 自然対数の別名
            "sqrt": math.sqrt, "cbrt": lambda x: x ** (1/3),  # 立方根
            "exp": math.exp,
            "floor": math.floor, "ceil": math.ceil,
            "degrees": math.degrees, "radians": math.radians,
            "factorial": math.factorial,
            # 基本関数
            "abs": abs, "round": round, "min": min, "max": max,
            "sum": sum, "pow": pow,
            "int": int, "float": float,
            # 数学定数
            "pi": math.pi, "e": math.e, "tau": math.tau,
            "inf": math.inf, "nan": math.nan,
        }
        
        # 式の前処理（一般的な数学記法をPython記法に変換）
        expression = expression.replace("^", "**")  # べき乗記法
        expression = expression.replace("×", "*")   # 掛け算記号
        expression = expression.replace("÷", "/")   # 割り算記号
        
        # 暗黙的な掛け算を明示的に（例: "2pi" -> "2*pi", "3(4+5)" -> "3*(4+5)"）
        expression = re.sub(r'(\d)([a-zA-Z])', r'\1*\2', expression)  # 数字+文字
        expression = re.sub(r'(\d)\(', r'\1*(', expression)          # 数字+(
        expression = re.sub(r'\)(\d)', r')*\1', expression)          # )+数字
        expression = re.sub(r'\)([a-zA-Z])', r')*\1', expression)    # )+文字
        expression = re.sub(r'([a-zA-Z])\(', r'\1(', expression)     # 関数名+( はそのまま
        
        # 計算実行
        result = eval(expression, safe_dict, {})
        
        # 結果の後処理
        if isinstance(result, (int, float)):
            if math.isnan(result):
                return f"{expression} = NaN（非数）"
            elif math.isinf(result):
                return f"{expression} = ∞（無限大）"
            elif isinstance(result, float) and result.is_integer():
                result = int(result)  # 整数の場合は整数表示
            elif isinstance(result, float):
                # 小数点以下が多い場合は丸める
                if abs(result) < 1e-10:
                    result = 0  # 非常に小さい値は0として扱う
                else:
                    result = round(result, 12)  # 精度を制限
        
        response = f"{expression} = {result}"
        print(f"[Tool] calculate 結果: {response}")
        return response
        
    except ZeroDivisionError:
        error_msg = f"計算エラー: ゼロで割ることはできません（{expression}）"
        print(f"[Tool] calculate エラー: {error_msg}")
        return error_msg
    except ValueError as e:
        error_msg = f"計算エラー: 値が範囲外または無効です（{expression}）- {str(e)}"
        print(f"[Tool] calculate エラー: {error_msg}")
        return error_msg
    except SyntaxError:
        error_msg = f"構文エラー: 式の形式が正しくありません（{expression}）"
        print(f"[Tool] calculate エラー: {error_msg}")
        return error_msg
    except NameError as e:
        error_msg = f"計算エラー: 未知の変数または関数です（{expression}）- {str(e)}"
        print(f"[Tool] calculate エラー: {error_msg}")
        return error_msg
    except OverflowError:
        error_msg = f"計算エラー: 結果が大きすぎます（{expression}）"
        print(f"[Tool] calculate エラー: {error_msg}")
        return error_msg
    except Exception as e:
        error_msg = f"計算エラー: {str(e)}（{expression}）"
        print(f"[Tool] calculate エラー: {error_msg}")
        return error_msg


@function_tool
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
