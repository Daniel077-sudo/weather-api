from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from config import supabase
from utils import taipei_now


TABLE_COLUMNS: Dict[str, set[str]] = {}
CITY_COLUMNS = ("city", "city_name", "county", "county_name")
DISTRICT_COLUMNS = ("district", "district_name", "town", "town_name")
DATE_COLUMNS = ("date", "forecast_date", "weather_date", "start_date", "target_date")
CITY_WEATHER_DEFAULT_DISTRICTS = {
    "臺北市": "中正區", "新北市": "板橋區", "基隆市": "仁愛區", "宜蘭縣": "宜蘭市",
    "桃園市": "桃園區", "新竹縣": "竹北市", "新竹市": "東區", "苗栗縣": "苗栗市",
    "臺中市": "西屯區", "彰化縣": "彰化市", "南投縣": "南投市", "雲林縣": "斗六市",
    "嘉義縣": "太保市", "嘉義市": "東區", "臺南市": "安平區", "高雄市": "苓雅區",
    "屏東縣": "屏東市", "花蓮縣": "花蓮市", "臺東縣": "臺東市", "澎湖縣": "馬公市",
    "金門縣": "金城鎮", "連江縣": "南竿鄉",
}


def weather_default_district(city: str, district: str = "") -> str:
    return district or CITY_WEATHER_DEFAULT_DISTRICTS.get(city.replace("台", "臺"), "")


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
    if value is None:
        return ""
    return str(value).strip().removesuffix("%").strip()


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
            if city_column == "city_name" and not district_column:
                combined_name = city_value + district if include_district and district else ""
                query = (
                    query.eq(city_column, combined_name)
                    if combined_name
                    else query.ilike(city_column, f"{city_value}%")
                )
            else:
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
    effective_district = weather_default_district(city, district)
    cache_rows = _load_rows("weather_cache_own", city, effective_district, 3, client)
    daily_rows = _load_rows("weather_daily_own", city, effective_district, 14, client)
    return cache_rows, daily_rows


def _date_text(row: Dict[str, Any]) -> str:
    value = _value((row, _dict(row.get("weather_data"))), DATE_COLUMNS)
    return str(value or "")[:10]


def _daily_payload(item: Dict[str, Any]) -> Dict[str, Any]:
    nested = _dict(item.get("weather_data"))
    objects = (item, nested)
    return {
        "date": _date_text(item),
        "description": _value(objects, ("description", "wx", "weather", "weather_text")),
        "temp_min": _value(objects, ("min_t", "temp_min", "min_temp", "t_min")),
        "temp_max": _value(objects, ("max_t", "temp_max", "max_temp", "t_max")),
        "rh": _value(objects, ("rh", "humidity", "relative_humidity", "relativeHumidity")),
        "app_min": _value(objects, ("app_min", "apparent_min", "apparent_temp_min", "apparent_temperature_min")),
        "app_max": _value(objects, ("app_max", "apparent_max", "apparent_temp_max", "apparent_temperature_max")),
        "pop": _value(objects, ("pop", "rain_probability", "probability_of_precipitation")),
    }


def _daily_payloads(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for row in rows:
        weather_data = _dict(row.get("weather_data"))
        nested_daily = row.get("daily")
        if not isinstance(nested_daily, list):
            nested_daily = weather_data.get("daily")
        if isinstance(nested_daily, list):
            items.extend(item for item in nested_daily if isinstance(item, dict))
        else:
            items.append(row)
    return [_daily_payload(item) for item in items]


def _parse_observed_time(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=taipei_now().tzinfo)
        return parsed.astimezone(taipei_now().tzinfo)
    except (TypeError, ValueError):
        return None


def _row_freshness(row: Dict[str, Any], max_age: timedelta, valid_until_key: str = "") -> str:
    if not row:
        return "missing"
    now = taipei_now()
    if valid_until_key:
        valid_until = _parse_observed_time(row.get(valid_until_key))
        if valid_until:
            return "fresh" if now <= valid_until else "stale"
    updated_at = _parse_observed_time(row.get("updated_at"))
    if not updated_at:
        return "unknown_time"
    return "fresh" if now - updated_at <= max_age else "stale"


def _fresh_observation(weather_data: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
    obs = _dict(weather_data.get("obs"))
    if not obs:
        return {}, "missing"
    observed_at = _parse_observed_time(obs.get("time"))
    if not observed_at:
        return {}, "unknown_time"
    if observed_at and taipei_now() - observed_at > timedelta(minutes=90):
        return {}, "stale"
    invalid_sentinels = {-99, -999, -9999, "-99", "-99.0", "-999", "-999.0", "-9999"}
    cleaned = {
        key: value
        for key, value in obs.items()
        if value not in invalid_sentinels and value not in (None, "")
    }
    return cleaned, "fresh"


def _clock(value: Any) -> str:
    text = str(value or "").strip()
    if "T" in text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%H:%M")
        except ValueError:
            pass
    return text[:5] if len(text) >= 5 else text


def _humidity_level(value: Any) -> str:
    try:
        humidity = float(_percent_text(value))
    except ValueError:
        return ""
    if humidity >= 90:
        return "非常潮濕"
    if humidity >= 80:
        return "潮濕"
    if humidity <= 40:
        return "乾燥"
    if humidity <= 55:
        return "偏乾"
    return "舒適"


def _number(value: Any) -> Optional[float]:
    try:
        return float(str(value).replace(">", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _temperature_advice(value: Any) -> str:
    temp = _number(value)
    if temp is None:
        return ""
    if temp >= 36:
        return "體感炎熱，請補充水分並避免長時間曝曬"
    if temp >= 30:
        return "體感偏熱，外出請補水並做好防曬"
    if temp <= 15:
        return "體感偏冷，外出建議加件保暖衣物"
    if temp <= 20:
        return "早晚稍涼，建議攜帶薄外套"
    return "體感大致舒適"


def _humidity_advice(value: Any) -> str:
    level = _humidity_level(value)
    return {
        "非常潮濕": "非常潮濕，衣物不易乾，室內可以開除濕機",
        "潮濕": "偏潮濕，室內請留意通風或除濕",
        "乾燥": "空氣乾燥，請適時補水並注意保濕",
        "偏乾": "空氣偏乾，建議適時補水",
        "舒適": "濕度在一般舒適範圍",
    }.get(level, "")


def _wind_advice(wind_ms: Any, gust_ms: Any) -> str:
    wind = _number(wind_ms) or 0
    gust = _number(gust_ms) or 0
    strongest = max(wind, gust)
    if strongest >= 10.8:
        return "風勢或陣風偏強，請避開招牌、路樹及海邊"
    if strongest >= 7.9:
        return "風勢較強，騎車請減速並固定戶外物品"
    return "目前風勢不強" if wind_ms not in (None, "") or gust_ms not in (None, "") else ""


def _visibility_advice(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(">"):
        return "能見度良好"
    first = _number(text.split("-", 1)[0])
    if first is not None and first <= 1:
        return "能見度很低，行車請開近光燈或霧燈並大幅減速"
    if first is not None and first <= 6:
        return "能見度偏低，行車請減速並加大安全距離"
    return "能見度尚可"


def _pressure_advice(value: Any) -> str:
    pressure = _number(value)
    if pressure is None:
        return ""
    if pressure < 1000:
        return "氣壓偏低，鋒面或低壓系統接近時常見，請留意最新天氣變化"
    if pressure >= 1025:
        return "氣壓偏高，天氣通常較穩定，但仍請以最新預報為準"
    return "氣壓在常見範圍"


def _rainfall_advice(value: Any) -> str:
    rainfall = _number(value)
    if rainfall is None:
        return ""
    if rainfall >= 80:
        return "今天累積雨量偏高，請留意低窪地區積淹水與山區落石"
    if rainfall >= 40:
        return "今天累積雨量較多，外出請避開積水路段"
    if rainfall > 0:
        return "今天已有降雨，路面可能濕滑，外出請留意"
    return "今天目前尚無累積雨量"


def _uvi_advice(value: Any) -> str:
    uvi = _number(value)
    if uvi is None:
        return ""
    if uvi >= 11:
        return "紫外線達危險級，上午 10 點至下午 2 點盡量避免戶外曝曬"
    if uvi >= 8:
        return "紫外線過量，外出請使用防曬、帽子及遮陽衣物"
    if uvi >= 6:
        return "紫外線偏高，外出建議做好防曬"
    return "紫外線目前不高，長時間戶外活動仍建議基本防曬"


def _aqi_advice(value: Any) -> str:
    aqi = _number(value)
    if aqi is None:
        return ""
    if aqi >= 301:
        return "空氣品質達危害等級，建議留在室內並關好門窗"
    if aqi >= 201:
        return "空氣品質非常不健康，請避免戶外活動"
    if aqi >= 151:
        return "空氣品質對所有族群不健康，請減少戶外活動並視需要戴口罩"
    if aqi >= 101:
        return "敏感族群請減少長時間或劇烈戶外活動"
    if aqi >= 51:
        return "空氣品質普通，敏感族群若不適可減少戶外活動"
    return "空氣品質良好，適合一般戶外活動"


def _detail_type(message: str) -> str:
    checks = [
        ("detail", ("詳細天氣", "天氣資料", "氣象資料", "觀測", "測站", "日照")),
        ("rain_probability", ("降雨機率", "下雨機率", "會下雨", "可能下雨")),
        ("uvi", ("紫外線", "防曬")),
        ("air_quality", ("空氣品質", "AQI", "PM2.5", "口罩")),
        ("feels_like", ("體感",)),
        ("dew", ("露點",)),
        ("humidity", ("濕度", "溼度", "潮濕", "很濕", "乾燥", "悶熱", "好悶")),
        ("sun", ("日出", "日落", "天亮", "天黑", "太陽下山")),
        ("moon", ("月出", "月落", "月亮")),
        ("pressure", ("氣壓",)),
        ("rainfall", ("雨量", "下了多少雨")),
        ("visibility", ("能見度", "視線", "起霧", "大霧")),
        ("wind", ("風速", "風向", "陣風", "風大")),
    ]
    for detail_type, keywords in checks:
        if any(keyword in message for keyword in keywords):
            return detail_type
    return "general"


def _hourly_values(hourly: List[Dict[str, Any]], keys: Iterable[str]) -> List[float]:
    values: List[float] = []
    for item in hourly:
        value = _value((item,), keys)
        try:
            values.append(float(str(value).replace("%", "").strip()))
        except (TypeError, ValueError):
            continue
    return values


def _active_hourly_period(hourly: List[Dict[str, Any]]) -> Dict[str, Any]:
    now = taipei_now()
    for item in hourly:
        starts_at = _parse_observed_time(item.get("time"))
        if starts_at and starts_at <= now < starts_at + timedelta(hours=3):
            return item
    return {}


def _range_text(values: List[float], suffix: str = "") -> str:
    if not values:
        return ""
    low, high = min(values), max(values)
    low_text = f"{low:g}"
    high_text = f"{high:g}"
    return f"{low_text}{suffix}" if low == high else f"{low_text}–{high_text}{suffix}"


def _rain_action(pop_value: Any) -> str:
    if pop_value in (None, ""):
        return "出發前請再確認最新降雨預報"
    try:
        pop = float(_percent_text(pop_value))
    except (TypeError, ValueError):
        return "出發前請再確認最新降雨預報"
    if pop >= 50:
        return "很可能下雨，帶傘或雨衣"
    if pop >= 30:
        return "可能會下雨，帶把傘備用"
    return "降雨機會較低"


def _daily_rain_advice(pop_value: Any) -> str:
    if pop_value in (None, ""):
        return "降雨機率暫無資料，出發前請再確認最新預報"
    return f"降雨機率 {_percent_text(pop_value)}%，{_rain_action(pop_value)}"


def _no_data_reply(location: str, detail_type: str) -> str:
    labels = {
        "humidity": "濕度",
        "feels_like": "體感溫度",
        "dew": "露點",
        "sun": "日出日落",
        "moon": "月出月落",
        "pressure": "氣壓",
        "rainfall": "累積雨量",
        "rain_probability": "降雨機率",
        "uvi": "紫外線",
        "air_quality": "空氣品質",
        "visibility": "能見度",
        "wind": "風速風向",
        "detail": "詳細天氣",
        "general": "天氣",
    }
    return f"目前讀不到{location}的{labels.get(detail_type, '天氣')}資料。"


def build_own_weather_answer(
    city: str,
    district: str,
    message: str,
    start_date: str,
    end_date: str,
    cache_rows: List[Dict[str, Any]],
    daily_rows: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    location = f"{city}{district}" or "查詢地點"
    detail_type = _detail_type(message)
    cache_row = cache_rows[0] if cache_rows else {}
    cache_status = _row_freshness(cache_row, timedelta(minutes=130), "valid_until")
    daily_status = _row_freshness(daily_rows[0] if daily_rows else {}, timedelta(hours=7))
    weather_data = _dict(cache_row.get("weather_data"))
    if cache_status == "stale":
        weather_data = {}
    current = _dict(weather_data.get("current"))
    obs, observation_status = _fresh_observation(weather_data)
    astro = _dict(weather_data.get("astro"))
    tomorrow_astro = _dict(astro.get("tomorrow"))
    hourly = weather_data.get("hourly_3h")
    if not isinstance(hourly, list):
        hourly = []
    hourly = [item for item in hourly if isinstance(item, dict)]
    usable_cache_row = cache_row if cache_status != "stale" else {}
    current_hourly = _active_hourly_period(hourly)
    objects = (obs, current_hourly, current, weather_data, usable_cache_row)
    daily = _daily_payloads(daily_rows)
    if daily_status == "stale":
        daily = []
    daily = [item for item in daily if not start_date or not item["date"] or start_date <= item["date"] <= (end_date or start_date)]
    daily.sort(key=lambda item: item.get("date") or "9999-12-31")

    temp = _value(objects, ("temp", "temperature", "air_temperature", "observed_temp"))
    humidity = _value(objects, ("rh", "humidity", "relative_humidity", "relativeHumidity", "observed_humidity", "hum"))
    humidity_source = "observation" if _value((obs,), ("hum", "humidity", "rh", "relative_humidity")) not in (None, "") else "forecast"
    app_temp = _value(objects, ("app_temp", "apparent_temperature", "apparentTemperature", "apparent_temp"))
    description = _value(objects, ("weather", "description", "wx", "weather_text"))
    pop = _value(objects, ("pop", "rain_probability", "probability_of_precipitation"))
    dew = _value((current_hourly, current), ("dew", "dew_point", "dewPoint"))
    comfort_desc = _value((current_hourly, current), ("comfort_desc", "comfort", "comfort_description"))
    uvi = _value((current, usable_cache_row), ("uvi",))
    aqi = _value((current, usable_cache_row), ("aqi",))
    aqi_status = _value((current, usable_cache_row), ("aqi_status",))
    fetched_at = cache_row.get("updated_at") or current.get("time")

    first_daily = daily[0] if daily else {}
    if humidity in (None, ""):
        humidity = first_daily.get("rh")
        if humidity not in (None, ""):
            humidity_source = "daily"

    today = taipei_now().date().isoformat()
    tomorrow = (taipei_now().date() + timedelta(days=1)).isoformat()
    requested_astro = astro
    if start_date == tomorrow:
        requested_astro = tomorrow_astro
    elif start_date and start_date not in {today, str(astro.get("date") or "")[:10]}:
        requested_astro = {}
    sunrise = _value((requested_astro,), ("sunrise", "sunrise_time", "sunriseTime"))
    sunset = _value((requested_astro,), ("sunset", "sunset_time", "sunsetTime"))
    dawn = _value((requested_astro,), ("dawn",))
    dusk = _value((requested_astro,), ("dusk",))
    moonrise = _value((requested_astro,), ("moonrise", "moonrise_time"))
    moonset = _value((requested_astro,), ("moonset", "moonset_time"))
    station = str(obs.get("station") or "").strip()
    observed_clock = _clock(obs.get("time"))
    station_note = ""
    if station or observed_clock:
        station_note = f"（{'、'.join(part for part in [station + '測站' if station else '', observed_clock + ' 觀測' if observed_clock else ''] if part)}）"
    observation_label = f"{station}測站" if station else f"{location}測站"
    time_note = f"（{observed_clock} 觀測）" if observed_clock else ""

    hourly_humidity = _hourly_values(hourly, ("humidity", "hum", "rh"))
    hourly_app = _hourly_values(hourly, ("app_temp", "apparent_temperature"))
    hourly_dew = _hourly_values(hourly, ("dew_point", "dew"))
    multi_day = bool(start_date and end_date and start_date != end_date)

    if multi_day:
        if daily:
            lines = []
            for item in daily:
                date_text = str(item.get("date") or "")
                label = date_text[5:].replace("-", "/") if len(date_text) >= 10 else date_text
                parts = [str(item.get("description") or "天氣現象暫無資料")]
                if item.get("temp_min") not in (None, "") and item.get("temp_max") not in (None, ""):
                    parts.append(f"氣溫 {item['temp_min']}–{item['temp_max']}°C")
                if item.get("app_min") not in (None, "") and item.get("app_max") not in (None, ""):
                    parts.append(f"體感 {item['app_min']}–{item['app_max']}°C")
                if item.get("rh") not in (None, ""):
                    parts.append(f"平均濕度 {_percent_text(item['rh'])}%")
                parts.append(_daily_rain_advice(item.get("pop")))
                lines.append(f"{label}：" + "；".join(parts) + "。")
            range_label = f"{start_date} 至 {end_date}"
            reply = f"{location}{range_label}每日天氣與降雨提醒：\n" + "\n".join(lines)
        else:
            reply = f"目前讀不到{location}{start_date}至{end_date}的每日預報資料。"
    elif detail_type == "humidity":
        parts = []
        if humidity not in (None, ""):
            source_note = station_note if humidity_source == "observation" else "（預報）"
            parts.append(f"{location}現在相對濕度 {_percent_text(humidity)}%{source_note}")
            if _humidity_advice(humidity):
                parts.append(_humidity_advice(humidity))
        if hourly_humidity:
            parts.append(f"今天接下來預報 {_range_text(hourly_humidity, '%')}")
        reply = "。".join(parts) + ("。" if parts else "")
    elif detail_type == "feels_like":
        parts = []
        if app_temp not in (None, ""):
            parts.append(f"{location}現在體感 {app_temp}°C" + (f"（氣溫 {temp}°C）" if temp not in (None, "") else ""))
        if hourly_app:
            parts.append(f"今天接下來體感 {_range_text(hourly_app, '°C')}")
        if _temperature_advice(app_temp):
            parts.append(_temperature_advice(app_temp))
        reply = "。".join(parts) + ("。" if parts else "")
    elif detail_type == "dew":
        if dew not in (None, ""):
            difference = None
            if _number(temp) is not None and _number(dew) is not None:
                difference = _number(temp) - _number(dew)
            note = "露點越接近氣溫，空氣越潮濕"
            if difference is not None and difference <= 2:
                note += "；目前兩者很接近，體感可能較悶"
            reply = f"{location}現在露點 {dew}°C（預報）" + (f"，氣溫 {temp}°C" if temp not in (None, "") else "") + f"。{note}。"
        elif hourly_dew:
            reply = f"{location}今天接下來露點 {_range_text(hourly_dew, '°C')}（預報）。"
        else:
            reply = ""
    elif detail_type == "sun":
        if not requested_astro:
            reply = f"目前只有{city}今天和明天的日出日落資料，查不到更後面的日期。"
        elif ("日出" in message and "日落" in message) or ("天亮" in message and "天黑" in message):
            parts = []
            if sunrise:
                parts.append(f"日出 {sunrise}" + (f"（天亮 {dawn}）" if dawn else ""))
            if sunset:
                parts.append(f"日落 {sunset}" + (f"（天黑 {dusk}）" if dusk else ""))
            reply = f"{city}{start_date or today}" + "、".join(parts) + "。" if parts else ""
        elif any(word in message for word in ["日出", "天亮"]):
            reply = f"{city}{start_date or today}日出 {sunrise}" + (f"（天亮 {dawn}）" if dawn else "") + "。" if sunrise else ""
        elif any(word in message for word in ["日落", "天黑", "太陽下山"]):
            reply = f"{city}{start_date or today}日落 {sunset}" + (f"（天黑 {dusk}）" if dusk else "") + "。" if sunset else ""
        else:
            reply = ""
    elif detail_type == "moon":
        if not requested_astro:
            reply = f"目前只有{city}今天和明天的月出月落資料，查不到更後面的日期。"
        else:
            date_label = f"{city}{start_date or today}"
            asks_moonrise = "月出" in message and "月落" not in message
            asks_moonset = "月落" in message and "月出" not in message
            if asks_moonrise:
                reply = f"{date_label}月出 {moonrise}。" if moonrise else f"{date_label}沒有月出。"
            elif asks_moonset:
                reply = f"{date_label}月落 {moonset}。" if moonset else f"{date_label}沒有月落。"
            else:
                moon_parts = []
                moon_parts.append(f"月出 {moonrise}" if moonrise else "沒有月出")
                moon_parts.append(f"月落 {moonset}" if moonset else "沒有月落")
                reply = date_label + "、".join(moon_parts) + "。"
    elif detail_type == "pressure":
        pressure = obs.get("pressure")
        reply = (
            f"{observation_label}氣壓 {pressure} hPa{time_note}。{_pressure_advice(pressure)}。"
            if pressure not in (None, "") else ""
        )
    elif detail_type == "rainfall":
        rain_today = obs.get("rain_today")
        reply = (
            f"{observation_label}今天（0 點起）累積雨量 {rain_today} mm{time_note}。{_rainfall_advice(rain_today)}。"
            if rain_today not in (None, "") else ""
        )
    elif detail_type == "visibility":
        visibility = obs.get("visibility")
        vis_station = str(obs.get("vis_station") or station or location)
        reply = (
            f"{vis_station}測站能見度 {visibility} 公里{time_note}。{_visibility_advice(visibility)}。"
            if visibility not in (None, "") else ""
        )
    elif detail_type == "wind":
        wind_ms = obs.get("wind_ms")
        gust_ms = obs.get("gust_ms")
        wind_dir = obs.get("wind_dir")
        parts = []
        if wind_ms not in (None, ""):
            parts.append(f"{wind_dir or '風速'} {wind_ms} m/s")
        elif wind_dir:
            parts.append(str(wind_dir))
        if gust_ms not in (None, ""):
            parts.append(f"陣風 {gust_ms} m/s")
        if parts:
            reply = f"{observation_label}" + "、".join(parts) + time_note + "。"
            if _wind_advice(wind_ms, gust_ms):
                reply += _wind_advice(wind_ms, gust_ms) + "。"
        else:
            reply = ""
    elif detail_type == "uvi":
        if uvi not in (None, ""):
            reply = f"{location}目前紫外線指數 {uvi}。{_uvi_advice(uvi)}。"
        else:
            reply = ""
    elif detail_type == "air_quality":
        if aqi not in (None, ""):
            status_text = f"（{aqi_status}）" if aqi_status not in (None, "") else ""
            reply = f"{location}目前 AQI {aqi}{status_text}。{_aqi_advice(aqi)}。"
        else:
            reply = ""
    elif detail_type == "detail":
        observed_parts = []
        for label, key, unit in [
            ("氣溫", "temp", "°C"), ("濕度", "hum", "%"), ("氣壓", "pressure", " hPa"),
            ("今天雨量", "rain_today", " mm"), ("風速", "wind_ms", " m/s"), ("陣風", "gust_ms", " m/s"),
            ("能見度", "visibility", " 公里"), ("日照", "sunshine", " 小時"),
        ]:
            value = obs.get(key)
            if value not in (None, ""):
                observed_parts.append(f"{label} {value}{unit}")
        if obs.get("wind_dir"):
            observed_parts.append(str(obs["wind_dir"]))
        if obs.get("high") not in (None, "") or obs.get("low") not in (None, ""):
            high_low = []
            if obs.get("high") not in (None, ""):
                high_low.append(f"最高 {obs['high']}°C" + (f"（{_clock(obs.get('high_time'))}）" if obs.get("high_time") else ""))
            if obs.get("low") not in (None, ""):
                high_low.append(f"最低 {obs['low']}°C" + (f"（{_clock(obs.get('low_time'))}）" if obs.get("low_time") else ""))
            observed_parts.append("今日" + "、".join(high_low))
        forecast_parts = []
        for label, value, unit in [
            ("天氣", description, ""), ("溫度", _value((current,), ("temp", "temperature")), "°C"),
            ("體感", app_temp, "°C"), ("濕度", _value((current,), ("humidity", "hum", "rh")), "%"),
            ("露點", dew, "°C"), ("降雨機率", pop, "%"), ("舒適度", comfort_desc, ""),
        ]:
            if value not in (None, ""):
                forecast_parts.append(f"{label} {value}{unit}")
        sections = []
        if observed_parts:
            sections.append(f"測站觀測{station_note}：" + "、".join(observed_parts))
        if forecast_parts:
            sections.append("未來 3 小時預報：" + "、".join(forecast_parts))
        astro_parts = [part for part in [f"日出 {astro.get('sunrise')}" if astro.get("sunrise") else "", f"日落 {astro.get('sunset')}" if astro.get("sunset") else "", f"月出 {astro.get('moonrise')}" if astro.get("moonrise") else "", f"月落 {astro.get('moonset')}" if astro.get("moonset") else ""] if part]
        if astro_parts:
            sections.append("、".join(astro_parts))
        advice = [
            item for item in [
                _rain_action(pop) if pop not in (None, "") else "",
                _temperature_advice(app_temp),
                _humidity_advice(humidity),
                _wind_advice(obs.get("wind_ms"), obs.get("gust_ms")),
            ]
            if item
        ]
        if advice:
            sections.append("提醒：" + "；".join(dict.fromkeys(advice)))
        reply = "。\n".join(sections) + ("。" if sections else "")
    elif first_daily and start_date and first_daily.get("date") == start_date and start_date != today:
        parts = [f"{location}{start_date}預報"]
        if first_daily.get("description"):
            parts.append(str(first_daily["description"]))
        if first_daily.get("temp_min") not in (None, "") and first_daily.get("temp_max") not in (None, ""):
            parts.append(f"氣溫 {first_daily['temp_min']}–{first_daily['temp_max']}°C")
        if first_daily.get("app_min") not in (None, "") and first_daily.get("app_max") not in (None, ""):
            parts.append(f"體感 {first_daily['app_min']}–{first_daily['app_max']}°C")
        if first_daily.get("rh") not in (None, ""):
            parts.append(f"相對濕度約 {_percent_text(first_daily['rh'])}%")
        parts.append(_daily_rain_advice(first_daily.get("pop")))
        daily_advice = _temperature_advice(first_daily.get("app_max"))
        if daily_advice:
            parts.append(daily_advice)
        reply = "；".join(parts) + "。"
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
        reply = parts[0] + "：" + "，".join(parts[1:]) + "。" if len(parts) > 1 else ""
        advice = [
            item for item in [
                _rain_action(pop) if pop not in (None, "") else "",
                _temperature_advice(app_temp),
                _humidity_advice(humidity),
                _wind_advice(obs.get("wind_ms"), obs.get("gust_ms")),
            ]
            if item
        ]
        if advice:
            reply += "提醒：" + "；".join(dict.fromkeys(advice)) + "。"
        if astro.get("sunrise") and astro.get("sunset"):
            reply += f"日出 {astro['sunrise']}、日落 {astro['sunset']}。"
        if astro.get("sunset") and tomorrow_astro.get("sunrise") and _clock(taipei_now().isoformat()) > _clock(astro.get("sunset")):
            reply += f"明天日出 {tomorrow_astro['sunrise']}。"

    if not reply:
        reply = _no_data_reply(location if detail_type not in {"sun", "moon"} else city, detail_type)

    normalized_current = {
        "description": description,
        "temp": temp,
        "app_temp": app_temp,
        "hum": humidity,
        "rh": humidity,
        "humidity_source": humidity_source if humidity not in (None, "") else None,
        "pop": pop,
        "dew": dew,
        "comfort_desc": comfort_desc,
        "fetched_at": fetched_at,
        "uvi": uvi,
        "aqi": aqi,
        "aqi_status": aqi_status,
        "pressure": obs.get("pressure"),
        "rain_today": obs.get("rain_today"),
        "wind_ms": obs.get("wind_ms"),
        "wind_dir": obs.get("wind_dir"),
        "gust_ms": obs.get("gust_ms"),
        "high": obs.get("high"),
        "high_time": obs.get("high_time"),
        "low": obs.get("low"),
        "low_time": obs.get("low_time"),
        "visibility": obs.get("visibility"),
        "vis_station": obs.get("vis_station"),
        "sunshine": obs.get("sunshine"),
        "station": obs.get("station"),
        "observed_at": obs.get("time"),
        "station_distance_km": obs.get("dist_km"),
    }
    normalized_current = {key: value for key, value in normalized_current.items() if value not in (None, "")}

    return {
        "reply": reply,
        "weather_summary": {
            "source": "weather_cache_own+weather_daily_own",
            "city": city,
            "district": district or None,
            "start_date": start_date or None,
            "end_date": end_date or None,
            "observation_status": observation_status,
            "cache_status": cache_status,
            "daily_status": daily_status,
            "updated_at": cache_row.get("updated_at"),
            "valid_until": cache_row.get("valid_until"),
            "data_sources": cache_row.get("data_sources") or weather_data.get("data_sources") or {},
            "source_errors": cache_row.get("source_errors") or weather_data.get("source_errors") or [],
            "current": normalized_current,
            "obs": obs,
            "astro": astro,
            "hourly_3h": hourly,
            "current_hourly": current_hourly,
            "daily": daily,
        },
    }


def get_own_weather_answer(city: str, district: str, message: str, start_date: str, end_date: str) -> Optional[Dict[str, Any]]:
    cache_rows, daily_rows = load_own_weather_rows(city, district)
    result = build_own_weather_answer(city, district, message, start_date, end_date, cache_rows, daily_rows)
    if result:
        result["weather_summary"]["data_district"] = weather_default_district(city, district) or None
    return result
