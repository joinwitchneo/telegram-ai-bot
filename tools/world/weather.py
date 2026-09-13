"""天气：Open-Meteo 免费接口（无需 API Key），带 10 分钟缓存。

Weather API != AI API：这里只有 HTTP JSON，没有任何模型调用。
"""

from __future__ import annotations

from tools.core.result import ToolResult
from tools.world import http

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

WEATHER_CODES = {
    0: "晴", 1: "大致晴朗", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "小雨", 55: "中雨", 61: "小雨", 63: "中雨", 65: "大雨",
    71: "小雪", 73: "中雪", 75: "大雪", 80: "阵雨", 81: "强阵雨", 82: "暴雨",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷暴",
}


def geocode(city: str) -> dict | None:
    data = http.get_json(f"{GEOCODE_URL}?name={http.quote(city)}&count=1&language=zh&format=json")
    results = data.get("results") or []
    if not results:
        return None
    first = results[0]
    return {
        "name": first.get("name", city),
        "latitude": first.get("latitude"),
        "longitude": first.get("longitude"),
        "country": first.get("country", ""),
        "admin": first.get("admin1", ""),
    }


def fetch(city: str = "", *, latitude: float | None = None, longitude: float | None = None) -> ToolResult:
    http_calls = 0
    place = None
    if latitude is None or longitude is None:
        if not city:
            return ToolResult.failure("weather", "不知道查哪里的天气")
        place = geocode(city)
        http_calls += 1
        if not place:
            return ToolResult.failure("weather", f"没找到这个地点：{city}")
        latitude, longitude = place["latitude"], place["longitude"]

    url = (
        f"{FORECAST_URL}?latitude={latitude}&longitude={longitude}"
        "&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"
        "&timezone=auto&forecast_days=1"
    )
    data = http.get_json(url)
    http_calls += 1
    current = data.get("current") or {}
    daily = data.get("daily") or {}

    def first(block: dict, key: str, default=None):
        values = block.get(key) or []
        return values[0] if values else default

    code = int(current.get("weather_code", -1))
    label = WEATHER_CODES.get(code, "说不准")
    rain = first(daily, "precipitation_probability_max", 0) or 0
    name = (place or {}).get("name") or f"{latitude},{longitude}"
    text = (
        f"{name}现在 {current.get('temperature_2m')}℃，{label}，"
        f"体感 {current.get('apparent_temperature')}℃，湿度 {current.get('relative_humidity_2m')}%，"
        f"风 {current.get('wind_speed_10m')} km/h；"
        f"今天 {first(daily, 'temperature_2m_min')}~{first(daily, 'temperature_2m_max')}℃，"
        f"降水概率 {rain}%"
    )
    return ToolResult(
        name="weather",
        ok=True,
        text=text,
        data={
            "place": name,
            "current": current,
            "daily": daily,
            "source": "open-meteo",
        },
        http_requests=http_calls,
        source_type="weather",
    )
