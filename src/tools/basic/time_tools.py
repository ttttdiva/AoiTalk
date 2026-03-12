"""
Time-related tools
"""
import datetime
from ..core import tool as function_tool


def get_current_time_impl() -> str:
    """ユーザーが現在の時刻や日付を尋ねた場合にのみ使用する関数（純粋関数版）"""
    print("[Tool] get_current_time が呼び出されました")
    now = datetime.datetime.now()
    # Voice-friendly format
    result = now.strftime("%Y年%m月%d日 %H時%M分")
    print(f"[Tool] get_current_time 結果: {result}")
    return result


@function_tool
def get_current_time() -> str:
    """ユーザーが現在の時刻や日付を尋ねた場合にのみ使用する関数"""
    return get_current_time_impl()
