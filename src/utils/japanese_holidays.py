"""日本の国民の祝日を計算で求める。

frontend/src/lib/japanese-holidays.ts と同じ規則（固定日・ハッピーマンデー・
春分/秋分・振替休日・国民の休日）を実装する。日付をハードコードしないため、
年を跨いでも祝日スキップが無反応にならない。

春分/秋分の近似式の有効範囲に合わせて 1980-2099 年を対象とし、範囲外の年は
祝日なしとして扱う（誤った日をスキップするより安全側）。
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
from typing import Optional

SUPPORTED_YEAR_MIN = 1980
SUPPORTED_YEAR_MAX = 2099

_SUNDAY = 6
_SATURDAY = 5


def _nth_monday(year: int, month: int, nth: int) -> int:
    """指定した月の第 nth 月曜日の日を返す（ハッピーマンデー用）。"""
    first_weekday = date(year, month, 1).weekday()  # 0=月曜
    offset = (0 - first_weekday) % 7
    return 1 + offset + (nth - 1) * 7


def _vernal_equinox_day(year: int) -> int:
    """春分の日（1980-2099 用の近似式）。"""
    return int(20.8431 + 0.242194 * (year - 1980) - (year - 1980) // 4)


def _autumnal_equinox_day(year: int) -> int:
    """秋分の日（1980-2099 用の近似式）。"""
    return int(23.2488 + 0.242194 * (year - 1980) - (year - 1980) // 4)


def _base_holidays(year: int) -> dict[date, str]:
    """祝日法に定められた「国民の祝日」本体（振替休日・国民の休日を含まない）。"""
    return {
        date(year, 1, 1): "元日",
        date(year, 1, _nth_monday(year, 1, 2)): "成人の日",
        date(year, 2, 11): "建国記念の日",
        date(year, 2, 23): "天皇誕生日",
        date(year, 3, _vernal_equinox_day(year)): "春分の日",
        date(year, 4, 29): "昭和の日",
        date(year, 5, 3): "憲法記念日",
        date(year, 5, 4): "みどりの日",
        date(year, 5, 5): "こどもの日",
        date(year, 7, _nth_monday(year, 7, 3)): "海の日",
        date(year, 8, 11): "山の日",
        date(year, 9, _nth_monday(year, 9, 3)): "敬老の日",
        date(year, 9, _autumnal_equinox_day(year)): "秋分の日",
        date(year, 10, _nth_monday(year, 10, 2)): "スポーツの日",
        date(year, 11, 3): "文化の日",
        date(year, 11, 23): "勤労感謝の日",
    }


@lru_cache(maxsize=256)
def _year_holidays(year: int) -> dict[date, str]:
    """振替休日と国民の休日を加えた、その年の休日全体。"""
    holidays = _base_holidays(year)

    # 振替休日: 祝日が日曜に当たるとき、その後の最初の平日を休日にする。
    for day in sorted(holidays):
        if day.weekday() != _SUNDAY:
            continue
        substitute = day + timedelta(days=1)
        while substitute in holidays:
            substitute += timedelta(days=1)
        if substitute.year == year:
            holidays[substitute] = "振替休日"

    # 国民の休日: 前後がともに祝日である平日（日曜を除く）を休日にする。
    extra: dict[date, str] = {}
    current = date(year, 1, 1)
    last = date(year, 12, 31)
    while current <= last:
        if (
            current not in holidays
            and current.weekday() != _SUNDAY
            and (current - timedelta(days=1)) in holidays
            and (current + timedelta(days=1)) in holidays
        ):
            extra[current] = "国民の休日"
        current += timedelta(days=1)
    holidays.update(extra)

    return holidays


def japanese_holiday_name(value: date) -> Optional[str]:
    """指定日の休日名。休日でなければ None。"""
    if value.year < SUPPORTED_YEAR_MIN or value.year > SUPPORTED_YEAR_MAX:
        return None
    return _year_holidays(value.year).get(value)


def is_japanese_holiday(value: date) -> bool:
    """指定日が日本の休日（国民の祝日・振替休日・国民の休日）かどうか。

    土日は祝日扱いしない。土日の除外は skip_weekend の担当。
    """
    return japanese_holiday_name(value) is not None


def is_weekend(value: date) -> bool:
    """土曜または日曜かどうか。"""
    return value.weekday() >= _SATURDAY


def list_japanese_holidays(year: int) -> list[date]:
    """指定年の休日を昇順で返す（検証・テスト用）。"""
    if year < SUPPORTED_YEAR_MIN or year > SUPPORTED_YEAR_MAX:
        return []
    return sorted(_year_holidays(year))
