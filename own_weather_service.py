from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from config import supabase
from utils import taipei_now


TABLE_COLUMNS: Dict[str, set[str]] = {}
CITY_COLUMNS = ("city", "city_name", "county", "county_name")
DISTRICT_COLUMNS = ("district", "district_name", "town", "town_name")
DATE_COLUMNS = ("date", "forecast_date", "weather_date", "start_date", "target_date")


def _dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return value if isinstance(value, dict) else {}


def _value(objects: Iterable[Dict[str, Any]], keys: Iterable[str]) -> Any:
    for obj in objects:
        for key in keys:
            value = obj.get(key)
            if value not in (None, ""):
                return value
    return None


def _percent_text(value: Any) -> str:
    return str(value or "").strip().removesuffix("%").strip()


def _column(columns: set[str], candidates: Iterable[str]) -> Optional[str]:
    return next((name for name in candidates if name in columns), None)


def _probe_columns(table: str, client: Any) -> set[str]:
    if table in TABLE_COLUMNS:
        return TABLE_COLUMNS[table]
    try:
        result = client.table(table).select("*").limit(1).execute()
        rows = result.data or []
        columns = set(rows[0].keys()) if rows else set()
    except Exception:
        columns = set()
    if columns:
        TABLE_COLUMNS[table] = columns
    return columns


def _load_rows(table: str, city: str, district: str, limit: int, client: Any) -> List[Dict[str, Any]]:
    if not city:
        return []
    columns = _probe_columns(table, client)
    if not columns:
        return []
    city_column = _column(columns, CITY_COLUMNS)
    district_column = _column(columns, DISTRICT_COLUMNS)
    order_candidates = (
        ("updated_at", "created_at", "forecast_date", "date")
        if table == "weather_cache_own"
        else ("forecast_date", "date", "target_date", "updated_at", "created_at")
    )
    order_column = _column(columns, order_candidates)

    def execute(city_value: str, include_district: bool) -> List[Dict[str, Any]]:
        query = client.table(table).select("*")
        if city_column:
            query = query.eq(city_column, city_value)
        if include_district and district and district_column:
            query = query.eq(district_column, district)
        if order_column:
            query = query.order(order_column, desc=order_column in {"updated_at", "created_at"})
        result = query.limit(limit if city_column else max(limit, 200)).execute()
        return [row for row in (result.data or []) if isinstance(row, dict)]

    try:
        rows = execute(city, True)
        if not rows and city_column and "臺" in city:
            rows = execute(city.replace("臺", "台"), True)
        if not rows and district and district_column:
            rows = execute(city, False)
            if not rows and city_column and "臺" in city:
                rows = execute(city.replace("臺", "台"), False)
    except Exception:
        return []

    if city_column:
        return rows
    filtered = []
    for row in rows:
        weather_data = _dict(row.get("weather_data"))
        row_city = _value((row, weather_data), CITY_COLUMNS)
        row_district = _value((row, weather_data), DISTRICT_COLUMNS)
        if str(row_city or "").replace("台", "臺") != city.replace("台", "臺"):
            continue
        if district and row_district and str(row_district) != district:
            continue
        filtered.append(row)
    return filtered[:limit]


def load_own_weather_rows(
    city: str,
    district: str = "",
    client: Any = supabase,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    cache_rows = _load_rows("weather_cache_own", city, district, 3, client)
    daily_rows = _load_rows("weather_daily_own", city, district, 14, client)
    return cache_rows, daily_rows


def _date_text(row: Dict[str, Any]) -> str:
    value = _value((row, _dict(row.get("weather_data"))), DATE_COLUMNS)
    return str(value or "")[:10]


def _daily_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    nested = _dict(row.get("weather_data"))
    objects = (row, nested)
    return {
        "date": _date_text(row),
        "description": _value(objects, ("description", "wx", "weather", "weather_text")),
        "temp_min": _value(objects, ("temp_min", "min_temp", "min_t", "t_min")),
        "temp_max": _value(objects, ("temp_max", "max_temp", "max_t", "t_max")),
        "rh": _value(objects, ("rh", "humidity", "relative_humidity", "relativeHumidity")),
        "app_min": _value(objects, ("app_min", "apparent_min", "apparent_temp_min", "apparent_temperature_min")),
        "app_max": _value(objects, ("app_max", "apparent_max", "apparent_temp_max", "apparent_temperature_max")),
        "pop": _value(objects, ("pop", "rain_probability", "probability_of_precipitation")),
    }


def build_own_weather_answer(
    city: str,
    district: str,
    message: str,
    start_date: str,
    end_date: str,
    cache_rows: List[Dict[str, Any]],
    daily_rows: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if not cache_rows and not daily_rows:
        return None

    cache_row = cache_rows[0] if cache_rows else {}
    weather_data = _dict(cache_row.get("weather_data"))
    current = _dict(weather_data.get("current"))
    obs = _dict(weather_data.get("obs"))
    astro = _dict(weather_data.get("astro"))
    objects = (obs, current, weather_data, cache_row)
    daily = [_daily_payload(row) for row in daily_rows]
    daily = [item for item in daily if not start_date or not item["date"] or start_date <= item["date"] <= (end_date or start_date)]

    temp = _value(objects, ("temp", "temperature", "air_temperature", "observed_temp"))
    humidity = _value(objects, ("rh", "humidity", "relative_humidity", "relativeHumidity", "observed_humidity", "hum"))
    app_temp = _value(objects, ("app_temp", "apparent_temperature", "apparentTemperature", "apparent_temp"))
    description = _value(objects, ("description", "wx", "weather", "weather_text"))
    pop = _value(objects, ("pop", "rain_probability", "probability_of_precipitation"))
    sunrise = _value((astro,), ("sunrise", "sunrise_time", "sunriseTime"))
    sunset = _value((astro,), ("sunset", "sunset_time", "sunsetTime"))

    first_daily = daily[0] if daily else {}
    if humidity in (None, ""):
        humidity = first_daily.get("rh")
    location = f"{city}{district}" or "查詢地點"
    lower_message = message.lower()

    if any(word in message for word in ["濕度", "潮濕"]):
        reply = f"{location}目前相對濕度約 {_percent_text(humidity)}%。" if humidity not in (None, "") else ""
    elif any(word in lower_message for word in ["日出", "sunrise"]):
        reply = f"{location}日出時間為 {sunrise}。" if sunrise else ""
    elif any(word in lower_message for word in ["日落", "sunset"]):
        reply = f"{location}日落時間為 {sunset}。" if sunset else ""
    elif first_daily and start_date and first_daily.get("date") == start_date and start_date != taipei_now().date().isoformat():
        parts = [f"{location}{start_date}預報"]
        if first_daily.get("description"):
            parts.append(str(first_daily["description"]))
        if first_daily.get("temp_min") not in (None, "") and first_daily.get("temp_max") not in (None, ""):
            parts.append(f"氣溫 {first_daily['temp_min']}–{first_daily['temp_max']}°C")
        if first_daily.get("app_min") not in (None, "") and first_daily.get("app_max") not in (None, ""):
            parts.append(f"體感 {first_daily['app_min']}–{first_daily['app_max']}°C")
        if first_daily.get("rh") not in (None, ""):
            parts.append(f"相對濕度約 {_percent_text(first_daily['rh'])}%")
        reply = "，".join(parts) + "。"
    else:
        parts = [f"{location}目前"]
        if description:
            parts.append(str(description))
        if temp not in (None, ""):
            parts.append(f"氣溫 {temp}°C")
        if app_temp not in (None, ""):
            parts.append(f"體感 {app_temp}°C")
        if humidity not in (None, ""):
            parts.append(f"相對濕度 {_percent_text(humidity)}%")
        if pop not in (None, ""):
            parts.append(f"降雨機率 {_percent_text(pop)}%")
        reply = "，".join(parts) + "。" if len(parts) > 1 else ""

    return {
        "reply": reply,
        "weather_summary": {
            "source": "weather_cache_own+weather_daily_own",
            "city": city,
            "district": district or None,
            "start_date": start_date or None,
            "end_date": end_date or None,
            "current": {
                "description": description,
                "temp": temp,
                "app_temp": app_temp,
                "rh": humidity,
                "pop": pop,
            },
            "obs": obs,
            "astro": astro,
            "daily": daily,
        },
    }


def get_own_weather_answer(city: str, district: str, message: str, start_date: str, end_date: str) -> Optional[Dict[str, Any]]:
    cache_rows, daily_rows = load_own_weather_rows(city, district)
    return build_own_weather_answer(city, district, message, start_date, end_date, cache_rows, daily_rows)
