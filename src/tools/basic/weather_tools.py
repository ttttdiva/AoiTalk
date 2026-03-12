"""
Weather-related tools
"""
import os
import random
import datetime
import requests
from ..core import tool as function_tool


def get_weather_info_impl(location: str = "東京", when: str = "今") -> str:
    """指定された場所の天気情報を取得する（純粋関数版）
    
    Args:
        location: 天気を調べたい場所
        when: いつの天気か（"今", "明日", "今日"など）
    """
    print(f"[Tool] get_weather_info が呼び出されました: {location} ({when})")
    
    # OpenWeatherMap API key from environment variable
    api_key = os.getenv("OPENWEATHER_API_KEY")
    
    if not api_key:
        # If no API key, use a mock weather service
        weather_types = ["晴れ", "曇り", "小雨", "晴れ時々曇り"]
        weather = random.choice(weather_types)
        temp = random.randint(15, 28)
        humidity = random.randint(40, 80)
        
        if when in ["明日", "あした"]:
            result = f"{location}の明日の天気は{weather}の予報です。予想気温は{temp}度、湿度は{humidity}%です。"
        elif when in ["今日", "きょう"]:
            result = f"{location}の今日の天気は{weather}です。気温は{temp}度、湿度は{humidity}%です。"
        else:
            result = f"{location}の現在の天気は{weather}です。気温は{temp}度、湿度は{humidity}%です。"
        
        print(f"[Tool] get_weather_info 結果（モック）: {result}")
        return result
    
    try:
        # Determine API endpoint based on when
        if when in ["明日", "あした"]:
            # Use forecast API for tomorrow
            base_url = "http://api.openweathermap.org/data/2.5/forecast"
            is_forecast = True
        else:
            # Use current weather API
            base_url = "http://api.openweathermap.org/data/2.5/weather"
            is_forecast = False
        
        # Clean up location string (remove extra spaces, normalize)
        location_clean = location.strip()
        
        # Handle common Japanese location patterns
        # Pattern 1: 東京大田区 -> 東京
        # Pattern 2: 大阪市中央区 -> 大阪
        
        # Try to extract main city from compound names
        main_cities = ["東京", "大阪", "名古屋", "札幌", "福岡", "神戸", "京都", "横浜", "千葉", "さいたま"]
        location_to_try = [location_clean]
        
        for city in main_cities:
            if city in location_clean and city != location_clean:
                location_to_try.append(city)
                break
        
        # Also try removing common suffixes
        if location_clean.endswith("区") or location_clean.endswith("市"):
            # Try to find city name before 区/市
            for city in main_cities:
                if location_clean.startswith(city):
                    location_to_try.append(city)
                    break
        
        # Remove duplicates while preserving order
        seen = set()
        unique_locations = []
        for loc in location_to_try:
            if loc not in seen:
                seen.add(loc)
                unique_locations.append(loc)
        location_to_try = unique_locations
        
        # Debug: print locations to try
        print(f"[Tool] get_weather_info 試行する地名: {location_to_try}")
        
        # Try each location variant
        response = None
        for loc in location_to_try:
            params = {
                "q": loc,
                "appid": api_key,
                "units": "metric",
                "lang": "ja"
            }
            
            print(f"[Tool] get_weather_info APIコール試行: {loc}")
            response = requests.get(base_url, params=params, timeout=5)
            if response.status_code == 200:
                print(f"[Tool] get_weather_info 成功: {loc}")
                break
            else:
                print(f"[Tool] get_weather_info 失敗: {loc} (status: {response.status_code})")
        
        if response is None or response.status_code != 200:
            # Last resort: try with "Japan" suffix
            last_try = f"{location_to_try[0]},JP"
            params["q"] = last_try
            print(f"[Tool] get_weather_info 最終試行: {last_try}")
            response = requests.get(base_url, params=params, timeout=5)
            if response.status_code == 200:
                print(f"[Tool] get_weather_info 成功: {last_try}")
        
        response.raise_for_status()
        
        data = response.json()
        
        if is_forecast:
            # Extract tomorrow's weather from forecast (around noon)
            # Forecast API returns data every 3 hours, find tomorrow around 12:00
            tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
            tomorrow_noon = tomorrow.replace(hour=12, minute=0, second=0)
            
            # Find the closest forecast to tomorrow noon
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
            # Extract current weather information
            weather_desc = data["weather"][0]["description"]
            temp = round(data["main"]["temp"])
            feels_like = round(data["main"]["feels_like"])
            humidity = data["main"]["humidity"]
            wind_speed = round(data["wind"]["speed"], 1)
            
            # Build response based on when
            if when in ["今日", "きょう"]:
                result = f"{location}の今日の天気は{weather_desc}です。"
            else:
                result = f"{location}の現在の天気は{weather_desc}です。"
            
            result += f"気温は{temp}度（体感温度{feels_like}度）、"
            result += f"湿度は{humidity}%、風速は{wind_speed}メートル毎秒です。"
        
        print(f"[Tool] get_weather_info 結果: {result}")
        return result
        
    except requests.exceptions.RequestException as e:
        error_msg = f"{location}の天気情報の取得に失敗しました。"
        print(f"[Tool] get_weather_info エラー: {e}")
        return error_msg
    except KeyError as e:
        error_msg = f"{location}の天気情報が見つかりませんでした。"
        print(f"[Tool] get_weather_info エラー: {e}")
        return error_msg
    except Exception as e:
        error_msg = f"天気情報の取得中にエラーが発生しました。"
        print(f"[Tool] get_weather_info エラー: {e}")
        return error_msg


@function_tool
def get_weather_info(location: str = "東京", when: str = "今") -> str:
    """指定された場所の天気情報を取得する
    
    Args:
        location: 天気を調べたい場所
        when: いつの天気か（"今", "明日", "今日"など）
    """
    return get_weather_info_impl(location, when)
