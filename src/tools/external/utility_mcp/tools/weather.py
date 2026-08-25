"""天気情報取得ツール"""

from __future__ import annotations

import datetime
import random
from typing import TYPE_CHECKING

import requests
from .....services.outbound_privacy_service import (
    OutboundPrivacyGateway,
    PrivacyError,
    get_privacy_policy_context,
)
from .....services.turn_context import get_turn_context

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP, api_key: str):
    """天気ツールを MCP サーバーに登録する。"""

    def _gateway() -> OutboundPrivacyGateway:
        try:
            from .....config import Config

            config = Config()
        except Exception as exc:
            raise PrivacyError("天気APIのプライバシー設定を解決できません") from exc
        try:
            turn = get_turn_context()
        except Exception:
            turn = None
        inherited = get_privacy_policy_context()
        return OutboundPrivacyGateway(
            config,
            user_id=str(getattr(turn, "user_id", None) or ""),
            session_id=str(getattr(turn, "session_id", None) or ""),
            session_context=inherited.session_context,
            project_metadata=inherited.project_metadata,
        )

    @mcp.tool()
    async def get_weather_info(location: str = "東京", when: str = "今") -> str:
        """指定された場所の天気情報を取得します。

        Args:
            location: 天気を調べたい場所
            when: いつの天気か（"今", "明日", "今日"など）
        """
        if not api_key:
            weather_types = ["晴れ", "曇り", "小雨", "晴れ時々曇り"]
            weather = random.choice(weather_types)
            temp = random.randint(15, 28)
            humidity = random.randint(40, 80)

            if when in ["明日", "あした"]:
                return f"{location}の明日の天気は{weather}の予報です。予想気温は{temp}度、湿度は{humidity}%です。"
            elif when in ["今日", "きょう"]:
                return f"{location}の今日の天気は{weather}です。気温は{temp}度、湿度は{humidity}%です。"
            else:
                return f"{location}の現在の天気は{weather}です。気温は{temp}度、湿度は{humidity}%です。"

        try:
            if when in ["明日", "あした"]:
                base_url = "http://api.openweathermap.org/data/2.5/forecast"
                is_forecast = True
            else:
                base_url = "http://api.openweathermap.org/data/2.5/weather"
                is_forecast = False

            location_clean = location.strip()
            main_cities = ["東京", "大阪", "名古屋", "札幌", "福岡", "神戸", "京都", "横浜", "千葉", "さいたま"]
            location_to_try = [location_clean]

            for city in main_cities:
                if city in location_clean and city != location_clean:
                    location_to_try.append(city)
                    break

            if location_clean.endswith("区") or location_clean.endswith("市"):
                for city in main_cities:
                    if location_clean.startswith(city):
                        location_to_try.append(city)
                        break

            seen = set()
            unique_locations = []
            for loc in location_to_try:
                if loc not in seen:
                    seen.add(loc)
                    unique_locations.append(loc)
            location_to_try = unique_locations

            response = None
            for loc in location_to_try:
                try:
                    protected = _gateway().protect_sync(
                        {"q": loc},
                        provider="openweather",
                        base_url=base_url,
                        source_kind="weather_query_mcp",
                    )
                    safe_location = (
                        str(protected.payload.get("q") or loc)
                        if isinstance(protected.payload, dict)
                        else loc
                    )
                except Exception as exc:
                    return f"天気情報の取得はプライバシーポリシーにより停止しました: {exc}"
                params = {
                    "q": safe_location,
                    "appid": api_key,
                    "units": "metric",
                    "lang": "ja"
                }
                response = requests.get(base_url, params=params, timeout=5)
                if response.status_code == 200:
                    break

            if response is None or response.status_code != 200:
                try:
                    protected = _gateway().protect_sync(
                        {"q": f"{location_to_try[0]},JP"},
                        provider="openweather",
                        base_url=base_url,
                        source_kind="weather_query_mcp",
                    )
                    last_try = (
                        str(protected.payload.get("q") or f"{location_to_try[0]},JP")
                        if isinstance(protected.payload, dict)
                        else f"{location_to_try[0]},JP"
                    )
                except Exception as exc:
                    return f"天気情報の取得はプライバシーポリシーにより停止しました: {exc}"
                params["q"] = last_try
                response = requests.get(base_url, params=params, timeout=5)

            response.raise_for_status()
            data = response.json()

            if is_forecast:
                tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
                tomorrow_noon = tomorrow.replace(hour=12, minute=0, second=0)

                best_forecast = None
                min_diff = float('inf')

                for forecast in data.get('list', []):
                    forecast_time = datetime.datetime.fromtimestamp(forecast['dt'])
                    diff = abs((forecast_time - tomorrow_noon).total_seconds())
                    if diff < min_diff:
                        min_diff = diff
                        best_forecast = forecast

                if best_forecast:
                    weather_desc = best_forecast["weather"][0]["description"]
                    temp = round(best_forecast["main"]["temp"])
                    humidity = best_forecast["main"]["humidity"]
                    wind_speed = round(best_forecast["wind"]["speed"], 1)

                    result = f"{location}の明日の天気は{weather_desc}の予報です。"
                    result += f"予想気温は{temp}度、"
                    result += f"湿度は{humidity}%、風速は{wind_speed}メートル毎秒です。"
                else:
                    result = f"{location}の明日の天気予報を取得できませんでした。"
            else:
                weather_desc = data["weather"][0]["description"]
                temp = round(data["main"]["temp"])
                feels_like = round(data["main"]["feels_like"])
                humidity = data["main"]["humidity"]
                wind_speed = round(data["wind"]["speed"], 1)

                if when in ["今日", "きょう"]:
                    result = f"{location}の今日の天気は{weather_desc}です。"
                else:
                    result = f"{location}の現在の天気は{weather_desc}です。"

                result += f"気温は{temp}度（体感温度{feels_like}度）、"
                result += f"湿度は{humidity}%、風速は{wind_speed}メートル毎秒です。"

            return result

        except requests.exceptions.RequestException:
            return f"{location}の天気情報の取得に失敗しました。"
        except KeyError:
            return f"{location}の天気情報が見つかりませんでした。"
        except Exception:
            return "天気情報の取得中にエラーが発生しました。"
