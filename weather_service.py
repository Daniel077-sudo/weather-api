import asyncio
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from config import CRON_STATUS, CWA_API_KEY, MOENV_API_KEY, supabase
from data import CITY_7DAY_MAP, CITY_MAP, REPRESENTATIVE_DISTRICTS, TAIWAN_LOCATIONS
from gemini_service import call_gemini_raw
from utils import geocode_fallback, log_sync, parse_datetime, safe_int, safe_response, taipei_now

CWA_SSL_ERROR_MARKERS = ("CERTIFICATE_VERIFY_FAILED", "Missing Subject Key Identifier")
MOENV_AQI_URL = "https://data.moenv.gov.tw/api/v2/aqx_p_432"
CWA_RADAR_IMAGE_URL = "https://cwaopendata.s3.ap-northeast-1.amazonaws.com/Observation/O-A0058-006.png"


async def fetch_cwa_json(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch CWA JSON, retrying once for the CWA certificate-chain issue seen on Render."""
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, params=params, timeout=20.0)
            res.raise_for_status()
            return res.json()
    except httpx.TransportError as e:
        message = str(e)
        if message and not any(marker in message for marker in CWA_SSL_ERROR_MARKERS):
            raise
        async with httpx.AsyncClient(verify=False) as client:
            res = await client.get(url, params=params, timeout=20.0)
            res.raise_for_status()
            return res.json()


async def fetch_moenv_json(url: str, params: Dict[str, Any]) -> Any:
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, params=params, timeout=20.0)
            res.raise_for_status()
            return res.json()
    except httpx.TransportError as e:
        message = str(e)
        if not any(marker in message for marker in CWA_SSL_ERROR_MARKERS):
            raise
        async with httpx.AsyncClient(verify=False) as client:
            res = await client.get(url, params=params, timeout=20.0)
            res.raise_for_status()
            return res.json()


def find_district(data, target):
    if isinstance(data, dict):
        if data.get("locationName") == target or data.get("LocationName") == target: return data
        for k, v in data.items():
            found = find_district(v, target)
            if found: return found
    elif isinstance(data, list):
        for item in data:
            found = find_district(item, target)
            if found: return found
    return None


def split_city_district(city_name: str) -> Optional[Dict[str, str]]:
    for city in sorted(CITY_7DAY_MAP.keys(), key=len, reverse=True):
        if city_name.startswith(city):
            district = city_name[len(city):]
            if district:
                return {"city": city, "district": district}
    return None


def extract_element_value(values: Any) -> str:
    if isinstance(values, list) and values:
        first = values[0]
        if isinstance(first, dict):
            return str(next(iter(first.values()), ""))
        return str(first)
    if isinstance(values, dict):
        return str(next(iter(values.values()), ""))
    return ""


def normalize_element_values(values: Any) -> List[Dict[str, Any]]:
    if isinstance(values, list):
        return [item for item in values if isinstance(item, dict)]
    if isinstance(values, dict):
        return [values]
    return []


def extract_value_by_unit(values: Any, unit_keywords: List[str]) -> str:
    for item in normalize_element_values(values):
        unit = str(item.get("parameterUnit") or item.get("measures") or item.get("Measure") or item.get("unit") or "")
        if any(keyword in unit for keyword in unit_keywords):
            for key in ["value", "Value", "weather", "Weather", "WeatherDescription", "elementValue"]:
                if item.get(key) not in [None, ""]:
                    return str(item.get(key))
    return ""


def extract_named_value(values: Any, keys: List[str]) -> str:
    for item in normalize_element_values(values):
        for key in keys:
            if item.get(key) not in [None, ""]:
                return str(item.get(key))
    return extract_element_value(values)


def extract_exact_named_value(values: Any, keys: List[str]) -> str:
    for item in normalize_element_values(values):
        for key in keys:
            if item.get(key) not in [None, ""]:
                return str(item.get(key))
    return ""


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        text = str(value).strip().replace("＞", ">")
        if text.startswith(">"):
            text = text[1:].strip()
        number = float(text)
        if number <= -90:
            return default
        return number
    except (TypeError, ValueError):
        return default


def safe_optional_float(value: Any) -> Optional[float]:
    try:
        text = str(value).strip().replace("＞", ">")
        if not text or text in {"-", "--", "X", "x", "NA", "N/A"}:
            return None
        if text.startswith(">"):
            text = text[1:].strip()
        number = float(text)
        if number <= -90:
            return None
        return number
    except (TypeError, ValueError):
        return None


def normalize_tw_text(value: Any) -> str:
    return str(value or "").replace("台", "臺").replace(" ", "").strip()


def station_geo(station: Dict[str, Any]) -> Dict[str, Any]:
    geo = station.get("GeoInfo") or station.get("geoInfo") or {}
    return geo if isinstance(geo, dict) else {}


def station_county(station: Dict[str, Any]) -> str:
    geo = station_geo(station)
    return str(geo.get("CountyName") or geo.get("countyName") or geo.get("County") or station.get("CountyName") or "")


def station_town(station: Dict[str, Any]) -> str:
    geo = station_geo(station)
    return str(geo.get("TownName") or geo.get("townName") or geo.get("Town") or station.get("TownName") or "")


def station_coordinates(station: Dict[str, Any]) -> Optional[Dict[str, float]]:
    geo = station_geo(station)
    coordinates = geo.get("Coordinates") or geo.get("coordinates") or []
    if isinstance(coordinates, dict):
        coordinates = [coordinates]
    for coord in coordinates if isinstance(coordinates, list) else []:
        if not isinstance(coord, dict):
            continue
        name = str(coord.get("CoordinateName") or coord.get("coordinateName") or "")
        lat = safe_optional_float(coord.get("StationLatitude") or coord.get("stationLatitude") or coord.get("Latitude"))
        lng = safe_optional_float(coord.get("StationLongitude") or coord.get("stationLongitude") or coord.get("Longitude"))
        if lat is not None and lng is not None and ("WGS84" in name or not name):
            return {"lat": lat, "lng": lng}
    return None


def station_identity(station: Dict[str, Any]) -> Dict[str, Any]:
    coords = station_coordinates(station) or {}
    return {
        "station_name": station.get("StationName") or station.get("stationName") or "",
        "station_id": station.get("StationId") or station.get("stationId") or "",
        "station_county": station_county(station),
        "station_town": station_town(station),
        "station_lat": coords.get("lat"),
        "station_lng": coords.get("lng"),
    }


def station_distance_score(station: Dict[str, Any], lat: Optional[float], lng: Optional[float]) -> float:
    if lat is None or lng is None:
        return 0
    coords = station_coordinates(station)
    if not coords:
        return math.inf
    return (coords["lat"] - lat) ** 2 + (coords["lng"] - lng) ** 2


def station_name(station: Dict[str, Any]) -> str:
    return str(station.get("StationName") or station.get("stationName") or "")


def station_weather_element(station: Dict[str, Any]) -> Dict[str, Any]:
    element = station.get("WeatherElement") or station.get("weatherElement") or {}
    return element if isinstance(element, dict) else {}


def station_quality_score(station: Dict[str, Any], require_visibility: bool = False) -> int:
    name = station_name(station)
    town = station_town(station)
    score = 0
    if any(keyword in name for keyword in ["國道", "快速", "交流道"]) or re.search(r"[NS]\d+K|[A-Z]?\d+K", name):
        score += 100
    if any(keyword in name for keyword in ["陽明山", "鞍部", "竹子湖", "玉山", "合歡山", "阿里山"]) and "山" not in town:
        score += 30
    if require_visibility:
        element = station_weather_element(station)
        visibility = safe_optional_float(element.get("VisibilityDescription") or element.get("Visb") or element.get("Visibility"))
        if visibility is None:
            score += 1000
    return score


def select_best_station(
    stations: List[Dict[str, Any]],
    city: str,
    district: str = "",
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    require_visibility: bool = False,
) -> Optional[Dict[str, Any]]:
    city_key = normalize_tw_text(city)
    district_key = normalize_tw_text(district)
    valid = [station for station in stations if isinstance(station, dict)]
    if not valid:
        return None

    same_city = [station for station in valid if normalize_tw_text(station_county(station)) == city_key]
    same_district = [
        station for station in same_city
        if district_key and district_key in normalize_tw_text(station_town(station))
    ]
    prioritized: List[tuple] = []
    seen_ids = set()
    for priority, pool in enumerate([same_district, same_city, valid]):
        for station in pool:
            station_id = station.get("StationId") or station.get("stationId") or id(station)
            if station_id in seen_ids:
                continue
            seen_ids.add(station_id)
            prioritized.append((priority, station))
    if not prioritized:
        return None

    if lat is not None and lng is not None and any(station_coordinates(station) for _, station in prioritized):
        prioritized = [(priority, station) for priority, station in prioritized if station_coordinates(station)]

    if require_visibility:
        return min(
            prioritized,
            key=lambda item: (
                station_quality_score(item[1], require_visibility=True),
                item[0],
                station_distance_score(item[1], lat, lng),
            ),
        )[1]

    return min(
        prioritized,
        key=lambda item: (
            item[0],
            station_quality_score(item[1]),
            station_distance_score(item[1], lat, lng),
        ),
    )[1]


def extract_station_records(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    records = payload.get("records") or payload.get("Records") or {}
    stations = records.get("Station") or records.get("station") or []
    return stations if isinstance(stations, list) else []


async def fetch_cwa_station_dataset(dataset_id: str) -> List[Dict[str, Any]]:
    if not CWA_API_KEY:
        return []
    payload = await fetch_cwa_json(
        f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/{dataset_id}",
        {"Authorization": CWA_API_KEY, "format": "JSON"},
    )
    return extract_station_records(payload)


def normalize_observed_at(value: Any) -> Any:
    if not value:
        return value
    text = str(value)
    parsed = parse_datetime(text)
    if parsed:
        return parsed.astimezone(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    for fmt in ["%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"]:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone(timedelta(hours=8))).isoformat(timespec="seconds")
        except ValueError:
            continue
    return value


def extract_average_int(values: Any, keys: List[str]) -> int:
    nums = []
    for item in normalize_element_values(values):
        for key in keys:
            if item.get(key) not in [None, ""]:
                nums.append(safe_int(item.get(key)))
    if not nums:
        return 0
    return round(sum(nums) / len(nums))


def parse_weather_periods(dist_data: Optional[dict]) -> List[Dict[str, Any]]:
    """Normalize CWA weatherElement blocks into frontend-friendly forecast periods."""
    if not dist_data:
        return []

    elements = dist_data.get("weatherElement") or dist_data.get("WeatherElement") or []
    time_map: Dict[str, Dict[str, Any]] = {}

    for element in elements:
        element_name = element.get("elementName") or element.get("ElementName") or ""
        times = element.get("time") or element.get("Time") or []

        for item in times:
            start_time = item.get("startTime") or item.get("StartTime") or item.get("dataTime") or item.get("DataTime")
            if not start_time:
                continue

            period = time_map.setdefault(
                start_time,
                {
                    "time": start_time,
                    "start_time": start_time,
                    "end_time": item.get("endTime") or item.get("EndTime"),
                    "temp": 0,
                    "pop": 0,
                    "hum": 0,
                    "description": "未知",
                    "app_temp": 0,
                    "uvi": 0,
                    "wind_speed": "0",
                    "wind_ms": 0.0,
                    "wind_dir": "",
                    "_seen_fields": set(),
                },
            )

            raw_values = item.get("elementValue") or item.get("ElementValue") or []
            value = extract_named_value(raw_values, ["value", "Value", "weather", "Weather", "WeatherDescription", "elementValue"])
            if not value:
                continue

            app_temp = extract_average_int(raw_values, ["ApparentTemperature", "MaxApparentTemperature", "MinApparentTemperature"])
            if app_temp:
                period["app_temp"] = app_temp
                period["_seen_fields"].add("app_temp")

            wind_direction = extract_exact_named_value(raw_values, ["WindDirection"])
            if wind_direction:
                period["wind_dir"] = wind_direction
                period["_seen_fields"].add("wind_dir")

            wind_ms_value = extract_exact_named_value(raw_values, ["WindSpeed"])
            if wind_ms_value:
                period["wind_ms"] = safe_float(wind_ms_value)
                period["_seen_fields"].add("wind_ms")
            beaufort_value = extract_exact_named_value(raw_values, ["BeaufortScale"])
            if beaufort_value:
                period["wind_speed"] = beaufort_value
                period["_seen_fields"].add("wind_speed")

            uv_value = extract_exact_named_value(raw_values, ["UVIndex"])
            if uv_value:
                period["uvi"] = safe_int(uv_value)
                period["_seen_fields"].add("uvi")

            weather_value = extract_exact_named_value(raw_values, ["Weather"])
            pop_value = extract_exact_named_value(raw_values, ["ProbabilityOfPrecipitation"])
            temp_value = extract_average_int(raw_values, ["Temperature", "MaxTemperature", "MinTemperature"])
            hum_value = extract_exact_named_value(raw_values, ["RelativeHumidity"])
            if weather_value:
                period["description"] = weather_value
                period["_seen_fields"].add("description")
            elif pop_value:
                period["pop"] = safe_int(pop_value)
                period["_seen_fields"].add("pop")
            elif extract_average_int(raw_values, ["Temperature", "MaxTemperature", "MinTemperature"]):
                period["temp"] = temp_value
                period["_seen_fields"].add("temp")
            elif hum_value:
                period["hum"] = safe_int(hum_value)
                period["_seen_fields"].add("hum")
            elif element_name == "Wx" or "天氣現象" in element_name:
                period["description"] = value
                period["_seen_fields"].add("description")
            elif "PoP" in element_name or "降雨機率" in element_name:
                period["pop"] = safe_int(value)
                period["_seen_fields"].add("pop")
            elif element_name == "AT" or "體感溫度" in element_name:
                period["app_temp"] = safe_int(value)
                period["_seen_fields"].add("app_temp")
            elif element_name in ["T", "MaxT", "MinT"] or element_name == "溫度":
                period["temp"] = safe_int(value)
                period["_seen_fields"].add("temp")
            elif element_name == "RH" or "相對濕度" in element_name:
                period["hum"] = safe_int(value)
                period["_seen_fields"].add("hum")
            elif element_name == "UVI" or "紫外線" in element_name:
                period["uvi"] = safe_int(value)
                period["_seen_fields"].add("uvi")
            elif element_name == "WS" or "風速" in element_name:
                wind_ms = extract_value_by_unit(raw_values, ["公尺/秒", "m/s", "m／s"])
                if wind_ms:
                    period["wind_ms"] = safe_float(wind_ms)
                    period["_seen_fields"].add("wind_ms")
                else:
                    period["wind_speed"] = value
                    period["_seen_fields"].add("wind_speed")
            elif element_name == "WD" or "風向" in element_name:
                period["wind_dir"] = value
                period["_seen_fields"].add("wind_dir")

    periods = sorted(time_map.values(), key=lambda item: item["time"])
    carry: Dict[str, Any] = {}
    sparse_fields = ["description", "pop", "wind_dir", "wind_ms", "wind_speed"]
    for period in periods:
        seen_fields = period.get("_seen_fields") or set()
        for field in sparse_fields:
            if field in seen_fields:
                carry[field] = period.get(field)
            elif field in carry:
                period[field] = carry[field]
        period.pop("_seen_fields", None)
    return periods


def pick_current_weather(forecast: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not forecast:
        return {
            "time": None,
            "temp": 0,
            "pop": 0,
            "hum": 0,
            "description": "未知",
            "app_temp": 0,
            "uvi": 0,
            "wind_speed": "0",
            "wind_ms": 0.0,
            "wind_dir": "",
            "aqi": 0,
            "rain_mm_1h": 0.0,
            "rain_mm_3h": 0.0,
            "rain_mm_6h": 0.0,
            "rain_mm_12h": 0.0,
            "rain_mm_24h": 0.0,
            "wind_gust_ms": 0.0,
            "wind_gust_dir": "",
            "visibility_km": 0.0,
        }

    now = taipei_now()
    future = []
    for period in forecast:
        start = parse_datetime(period.get("start_time") or period.get("time"))
        end = parse_datetime(period.get("end_time"))
        if start and end and start <= now <= end:
            return period
        if start and start >= now:
            future.append(period)
    return future[0] if future else forecast[0]


def pick_weather_for_time(forecast: List[Dict[str, Any]], target_time: Optional[datetime]) -> Dict[str, Any]:
    if not target_time:
        return pick_current_weather(forecast)

    target = target_time.astimezone(timezone(timedelta(hours=8))) if target_time.tzinfo else target_time.replace(tzinfo=timezone(timedelta(hours=8)))
    candidates = []
    for period in forecast:
        start = parse_datetime(period.get("start_time") or period.get("time"))
        end = parse_datetime(period.get("end_time"))
        if start and end and start <= target <= end:
            return period
        if start:
            candidates.append((abs((start - target).total_seconds()), period))

    if candidates:
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]
    return pick_current_weather(forecast)


def analyze_weather_risk(weather: Dict[str, Any]) -> Dict[str, Any]:
    description = str(weather.get("description") or "")
    pop = safe_int(weather.get("pop"))
    uvi = safe_int(weather.get("uvi"))
    aqi = safe_int(weather.get("aqi"))
    app_temp = safe_int(weather.get("app_temp"))
    wind_ms = safe_float(weather.get("wind_ms"))
    wind_gust_ms = safe_float(weather.get("wind_gust_ms"))
    wind_speed = safe_int(weather.get("wind_speed"))
    rain_mm_1h = safe_float(weather.get("rain_mm_1h"))
    rain_mm_24h = safe_float(weather.get("rain_mm_24h"))
    visibility_km = safe_float(weather.get("visibility_km"))
    tags = []

    if pop >= 70 or rain_mm_1h >= 40 or rain_mm_24h >= 80 or any(keyword in description for keyword in ["大雨", "豪雨", "雷雨"]):
        tags.append("heavy_rain")
    if rain_mm_24h >= 200 or "豪雨" in description:
        tags.append("torrential_rain")
    if any(keyword in description for keyword in ["颱風", "強風"]):
        tags.append("strong_wind")
    if uvi >= 8:
        tags.append("high_uvi")
    if wind_ms >= 10 or wind_gust_ms >= 10.8 or wind_speed >= 10:
        tags.append("strong_wind")
    if 0 < visibility_km <= 1:
        tags.append("low_visibility")
    if aqi >= 151:
        tags.append("very_poor_air_quality")
    if aqi >= 101:
        tags.append("poor_air_quality")
    if app_temp >= 38:
        tags.append("extreme_heat")
    if app_temp >= 36:
        tags.append("heat_risk")

    high_tags = {"heavy_rain", "torrential_rain", "strong_wind", "low_visibility", "very_poor_air_quality", "extreme_heat"}
    if high_tags.intersection(tags):
        level = "high"
    elif tags or pop >= 40:
        level = "medium"
    else:
        level = "low"

    return {
        "risk_level": level,
        "risk_tags": sorted(set(tags)),
        "has_weather_risk": level != "low",
    }


def build_weather_suggestion(city: str, district: str, message: str, weather: Dict[str, Any], risk: Dict[str, Any]) -> str:
    location = f"{city}{district}"
    description = weather.get("description") or "天氣未知"
    pop = safe_int(weather.get("pop"))
    tags = risk.get("risk_tags") or []

    if "heavy_rain" in tags:
        return f"{location}降雨機率{pop}%，建議帶雨具並避開低窪、地下道。"
    if "strong_wind" in tags:
        return f"{location}可能有強風，外出請避開招牌、路樹與施工圍籬。"
    if "high_uvi" in tags:
        return f"{location}紫外線偏高，請補水並做好防曬。"
    if pop >= 40:
        return f"{location}有降雨機會，行程「{message}」建議預留交通緩衝。"
    return f"{location}目前{description}，行程「{message}」可照常，仍請留意最新天氣。"


async def fetch_cwa_forecast(city: str, district: str, seven_day: bool = True) -> Dict[str, Any]:
    dataset_map = CITY_7DAY_MAP if seven_day else CITY_MAP
    dataset_id = dataset_map.get(city)
    if not dataset_id:
        raise ValueError(f"目前尚不支援 {city} 的天氣查詢")
    if not CWA_API_KEY:
        raise ValueError("尚未設定 CWA_API_KEY")

    url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/{dataset_id}"
    params = {"Authorization": CWA_API_KEY, "format": "JSON"}
    payload = await fetch_cwa_json(url, params)
    dist_data = find_district(payload, district)

    forecast = parse_weather_periods(dist_data)
    if not forecast:
        raise ValueError(f"找不到 {city}{district} 的天氣資料")

    current = pick_current_weather(forecast)
    risk = analyze_weather_risk(current)
    return {
        "current": current,
        "forecast": forecast,
        **risk,
    }


def warning_matches_location(warning: Dict[str, Any], city: str, district: str) -> bool:
    text = json.dumps(warning, ensure_ascii=False)
    return bool(city and city in text) and (not district or district in text or city in str(warning.get("locationName") or ""))


def normalize_warning(raw: Dict[str, Any], city: str, district: str) -> Optional[Dict[str, Any]]:
    text = json.dumps(raw, ensure_ascii=False)
    if city not in text:
        return None
    if district and district not in text and city not in str(raw.get("locationName") or raw.get("LocationName") or ""):
        return None

    info = raw.get("info") or raw.get("Info") or raw
    phenomena = str(info.get("phenomena") or info.get("Phenomena") or raw.get("phenomena") or "")
    significance = str(info.get("significance") or info.get("Significance") or raw.get("significance") or "特報")
    title = str(info.get("headline") or info.get("event") or info.get("Event") or raw.get("title") or f"{phenomena}{significance}").strip()
    description = str(info.get("description") or info.get("Description") or info.get("instruction") or raw.get("description") or title)
    issued_at = info.get("effective") or info.get("sent") or info.get("issueTime") or raw.get("created_at") or taipei_now().isoformat()
    effective_at = info.get("onset") or info.get("effective") or raw.get("effective_at") or ""
    expires_at = info.get("expires") or info.get("Expires") or raw.get("expires_at") or ""
    level = "豪雨" if "豪雨" in text else "大雨" if "大雨" in text else "強風" if "強風" in text else significance
    warning_type = title if title and title != "特報" else f"{phenomena}{significance}".strip() or "天氣特報"
    return {
        "type": warning_type,
        "level": level,
        "issued_at": issued_at,
        "effective_at": effective_at,
        "expires_at": expires_at,
        "text": description,
        "source": "cwa",
    }


async def fetch_cwa_active_warnings(city: str, district: str) -> List[Dict[str, Any]]:
    if not CWA_API_KEY:
        return []

    warnings: List[Dict[str, Any]] = []
    seen = set()
    for dataset_id in ["W-C0033-001", "W-C0033-002"]:
        try:
            payload = await fetch_cwa_json(
                f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/{dataset_id}",
                {"Authorization": CWA_API_KEY, "format": "JSON"},
            )
            records = payload.get("records") or {}
            locations = records.get("location") or records.get("Location") or []
            for loc in locations if isinstance(locations, list) else []:
                if not warning_matches_location(loc, city, district):
                    continue
                hazards = (loc.get("hazardConditions") or {}).get("hazards") or []
                if not hazards and dataset_id == "W-C0033-001":
                    continue
                candidates = hazards if hazards else [loc]
                for item in candidates:
                    normalized = normalize_warning(item if isinstance(item, dict) else loc, city, district)
                    if not normalized:
                        continue
                    key = (normalized["type"], normalized["issued_at"], normalized["text"][:80])
                    if key in seen:
                        continue
                    seen.add(key)
                    warnings.append(normalized)
        except Exception as e:
            print(f"取得 CWA 特報失敗 {dataset_id}: {e}")
    return warnings


async def fetch_cwa_rain_observation(
    city: str,
    district: str = "",
    lat: Optional[float] = None,
    lng: Optional[float] = None,
) -> Dict[str, Any]:
    stations = await fetch_cwa_station_dataset("O-A0002-001")
    station = select_best_station(stations, city, district, lat, lng)
    if not station:
        return {}

    rainfall = station.get("RainfallElement") or station.get("rainfallElement") or {}
    identity = station_identity(station)
    return {
        "rain_mm_now": safe_float(rainfall.get("Now") or rainfall.get("now")),
        "rain_mm_10m": safe_float(rainfall.get("Past10Min") or rainfall.get("past10Min")),
        "rain_mm_1h": safe_float(rainfall.get("Past1hr") or rainfall.get("Past1Hr") or rainfall.get("past1hr")),
        "rain_mm_3h": safe_float(rainfall.get("Past3hr") or rainfall.get("Past3Hr") or rainfall.get("past3hr")),
        "rain_mm_6h": safe_float(rainfall.get("Past6Hr") or rainfall.get("Past6hr") or rainfall.get("past6Hr")),
        "rain_mm_12h": safe_float(rainfall.get("Past12hr") or rainfall.get("Past12Hr") or rainfall.get("past12hr")),
        "rain_mm_24h": safe_float(rainfall.get("Past24hr") or rainfall.get("Past24Hr") or rainfall.get("past24hr")),
        "rain_observed_at": normalize_observed_at((station.get("ObsTime") or {}).get("DateTime") if isinstance(station.get("ObsTime"), dict) else station.get("ObsTime")),
        "rain_station": identity.get("station_name"),
        "rain_station_id": identity.get("station_id"),
        "rain_station_county": identity.get("station_county"),
        "rain_station_town": identity.get("station_town"),
    }


def parse_visibility_km(value: Any) -> Optional[float]:
    text = str(value or "").replace("公里", "").replace("km", "").replace("KM", "").strip()
    return safe_optional_float(text)


async def fetch_cwa_weather_observation(
    city: str,
    district: str = "",
    lat: Optional[float] = None,
    lng: Optional[float] = None,
) -> Dict[str, Any]:
    stations = await fetch_cwa_station_dataset("O-A0003-001")
    station = select_best_station(stations, city, district, lat, lng, require_visibility=True)
    if not station:
        return {}

    element = station.get("WeatherElement") or station.get("weatherElement") or {}
    gust = element.get("GustInfo") or {}
    occurred_at = gust.get("Occurred_at") or gust.get("OccurredAt") or {}
    identity = station_identity(station)
    visibility = parse_visibility_km(element.get("VisibilityDescription") or element.get("Visb") or element.get("Visibility"))
    wind_gust_ms = safe_float(gust.get("PeakGustSpeed") or gust.get("peakGustSpeed"))
    wind_gust_dir = str(occurred_at.get("WindDirection") or occurred_at.get("windDirection") or "")
    return {
        "observed_temp": safe_float(element.get("AirTemperature") or element.get("airTemperature")),
        "observed_humidity": safe_int(element.get("RelativeHumidity") or element.get("relativeHumidity")),
        "observed_wind_ms": safe_float(element.get("WindSpeed") or element.get("windSpeed")),
        "observed_wind_dir": str(element.get("WindDirection") or element.get("windDirection") or ""),
        "wind_gust_ms": wind_gust_ms,
        "wind_gust_dir": wind_gust_dir,
        "wind_gust_observed_at": normalize_observed_at(occurred_at.get("DateTime") or occurred_at.get("dateTime")),
        "visibility_km": visibility,
        "observation_weather": element.get("Weather") or element.get("weather") or "",
        "observation_observed_at": normalize_observed_at((station.get("ObsTime") or {}).get("DateTime") if isinstance(station.get("ObsTime"), dict) else station.get("ObsTime")),
        "observation_station": identity.get("station_name"),
        "observation_station_id": identity.get("station_id"),
        "observation_station_county": identity.get("station_county"),
        "observation_station_town": identity.get("station_town"),
    }


async def fetch_cwa_uvi_observation(station_id: str = "") -> Dict[str, Any]:
    if not station_id:
        return {}
    payload = await fetch_cwa_json(
        "https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0005-001",
        {"Authorization": CWA_API_KEY, "format": "JSON"},
    )
    records = payload.get("records") or {}
    element = records.get("weatherElement") or {}
    locations = element.get("location") or []
    for item in locations if isinstance(locations, list) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("StationID") or item.get("stationId") or "") == str(station_id):
            return {
                "uvi": safe_int(item.get("UVIndex") or item.get("uvIndex")),
                "uvi_station_id": station_id,
                "uvi_observed_date": element.get("Date") or records.get("Date"),
            }
    return {}


def normalize_aqi_record(record: Dict[str, Any]) -> Dict[str, Any]:
    aqi = safe_int(record.get("aqi") or record.get("AQI"))
    observed_at = normalize_observed_at(record.get("publishtime") or record.get("PublishTime") or record.get("monitordate") or record.get("MonitorDate"))
    return {
        "aqi": aqi,
        "aqi_site": record.get("sitename") or record.get("SiteName") or "",
        "aqi_county": record.get("county") or record.get("County") or "",
        "aqi_status": record.get("status") or record.get("Status") or "",
        "aqi_pollutant": record.get("pollutant") or record.get("Pollutant") or "",
        "pm25": safe_int(record.get("pm2.5") or record.get("PM2.5") or record.get("pm25")),
        "pm10": safe_int(record.get("pm10") or record.get("PM10")),
        "o3": safe_int(record.get("o3") or record.get("O3")),
        "observed_at": observed_at,
    }


def aqi_record_coordinates(record: Dict[str, Any]) -> Optional[Dict[str, float]]:
    lat = safe_optional_float(record.get("latitude") or record.get("Latitude") or record.get("lat") or record.get("Lat"))
    lng = safe_optional_float(record.get("longitude") or record.get("Longitude") or record.get("lon") or record.get("lng") or record.get("Lng"))
    if lat is None or lng is None:
        return None
    return {"lat": lat, "lng": lng}


def aqi_distance_score(record: Dict[str, Any], lat: Optional[float], lng: Optional[float]) -> float:
    if lat is None or lng is None:
        return 0
    coords = aqi_record_coordinates(record)
    if not coords:
        return math.inf
    return (coords["lat"] - lat) ** 2 + (coords["lng"] - lng) ** 2


async def fetch_moenv_aqi(
    city: str,
    district: str = "",
    lat: Optional[float] = None,
    lng: Optional[float] = None,
) -> Dict[str, Any]:
    if not MOENV_API_KEY:
        return {}
    params = {
        "api_key": MOENV_API_KEY,
        "format": "json",
        "limit": 1000,
        "sort": "publishtime desc",
    }
    payload = await fetch_moenv_json(MOENV_AQI_URL, params)
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = payload.get("records") or payload.get("Records") or []
    else:
        records = []
    if not isinstance(records, list):
        return {}
    city_records = [
        record for record in records
        if isinstance(record, dict) and normalize_tw_text(record.get("county") or record.get("County")) == normalize_tw_text(city)
    ]
    if not city_records:
        return {}
    if lat is not None and lng is not None:
        with_coords = [record for record in city_records if aqi_record_coordinates(record)]
        if with_coords:
            return normalize_aqi_record(min(with_coords, key=lambda record: aqi_distance_score(record, lat, lng)))
    district_records = [
        record for record in city_records
        if district and district.replace("區", "") in str(record.get("sitename") or record.get("SiteName") or "")
    ]
    return normalize_aqi_record((district_records or city_records)[0])


async def build_live_weather_payload(
    city: str,
    district: str,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
) -> Dict[str, Any]:
    weather_payload = await fetch_cwa_forecast(city, district, seven_day=True)
    current = dict(weather_payload["current"])
    observed_at = current.get("time") or current.get("start_time")
    active_warnings: List[Dict[str, Any]] = []
    hourly: List[Dict[str, Any]] = []
    source_errors: List[Dict[str, str]] = []
    data_sources = {
        "cwa_forecast": "success",
        "cwa_3h_forecast": "not_checked",
        "cwa_warnings": "not_checked",
        "cwa_rain_observation": "not_checked",
        "cwa_weather_observation": "not_checked",
        "cwa_uvi_observation": "not_checked",
        "cwa_radar": "success",
        "moenv_aqi": "not_configured" if not MOENV_API_KEY else "not_found",
    }

    try:
        hourly_payload = await fetch_cwa_forecast(city, district, seven_day=False)
        hourly = hourly_payload.get("forecast") or []
        if hourly:
            current = dict(pick_current_weather(hourly))
            observed_at = current.get("time") or current.get("start_time") or observed_at
        data_sources["cwa_3h_forecast"] = "success" if hourly else "not_found"
    except Exception as e:
        data_sources["cwa_3h_forecast"] = "error"
        source_errors.append({"source": "cwa_3h_forecast", "message": str(e)})
        print(f"取得逐三小時預報失敗: {e}")

    try:
        aqi_payload = await fetch_moenv_aqi(city, district, lat, lng)
        if aqi_payload:
            data_sources["moenv_aqi"] = "success"
            current.update({
                "aqi": aqi_payload.get("aqi", 0),
                "aqi_site": aqi_payload.get("aqi_site", ""),
                "aqi_status": aqi_payload.get("aqi_status", ""),
                "aqi_pollutant": aqi_payload.get("aqi_pollutant", ""),
                "pm25": aqi_payload.get("pm25", 0),
                "pm10": aqi_payload.get("pm10", 0),
                "o3": aqi_payload.get("o3", 0),
            })
            observed_at = aqi_payload.get("observed_at") or observed_at
    except Exception as e:
        data_sources["moenv_aqi"] = "error"
        source_errors.append({"source": "moenv_aqi", "message": str(e)})
        print(f"取得 AQI 失敗: {e}")

    try:
        rain_payload = await fetch_cwa_rain_observation(city, district, lat, lng)
        if rain_payload:
            data_sources["cwa_rain_observation"] = "success"
            current.update(rain_payload)
            observed_at = rain_payload.get("rain_observed_at") or observed_at
        else:
            data_sources["cwa_rain_observation"] = "not_found"
    except Exception as e:
        data_sources["cwa_rain_observation"] = "error"
        source_errors.append({"source": "cwa_rain_observation", "message": str(e)})
        print(f"取得雨量觀測失敗: {e}")

    try:
        observation_payload = await fetch_cwa_weather_observation(city, district, lat, lng)
        if observation_payload:
            data_sources["cwa_weather_observation"] = "success"
            current.update(observation_payload)
            observed_at = observation_payload.get("observation_observed_at") or observed_at
            try:
                uvi_payload = await fetch_cwa_uvi_observation(str(observation_payload.get("observation_station_id") or ""))
                if uvi_payload:
                    data_sources["cwa_uvi_observation"] = "success"
                    if safe_int(uvi_payload.get("uvi")) > 0 or safe_int(current.get("uvi")) == 0:
                        current["uvi"] = safe_int(uvi_payload.get("uvi"))
                    current["uvi_station_id"] = uvi_payload.get("uvi_station_id", "")
                    current["uvi_observed_date"] = uvi_payload.get("uvi_observed_date", "")
                else:
                    data_sources["cwa_uvi_observation"] = "not_found"
            except Exception as uv_e:
                data_sources["cwa_uvi_observation"] = "error"
                source_errors.append({"source": "cwa_uvi_observation", "message": str(uv_e)})
                print(f"取得紫外線觀測失敗: {uv_e}")
        else:
            data_sources["cwa_weather_observation"] = "not_found"
            data_sources["cwa_uvi_observation"] = "not_found"
    except Exception as e:
        data_sources["cwa_weather_observation"] = "error"
        data_sources["cwa_uvi_observation"] = "error"
        source_errors.append({"source": "cwa_weather_observation", "message": str(e)})
        print(f"取得氣象觀測失敗: {e}")

    try:
        active_warnings = await fetch_cwa_active_warnings(city, district)
        data_sources["cwa_warnings"] = "success"
    except Exception as e:
        data_sources["cwa_warnings"] = "error"
        source_errors.append({"source": "cwa_warnings", "message": str(e)})
        print(f"取得 active_warnings 失敗: {e}")

    current_defaults = {
        "aqi": 0,
        "aqi_site": "",
        "aqi_status": "",
        "aqi_pollutant": "",
        "pm25": 0,
        "pm10": 0,
        "o3": 0,
        "rain_mm_now": 0.0,
        "rain_mm_10m": 0.0,
        "rain_mm_1h": 0.0,
        "rain_mm_3h": 0.0,
        "rain_mm_6h": 0.0,
        "rain_mm_12h": 0.0,
        "rain_mm_24h": 0.0,
        "wind_gust_ms": 0.0,
        "wind_gust_dir": "",
        "observed_temp": 0.0,
        "observed_humidity": 0,
        "observed_wind_ms": 0.0,
        "observed_wind_dir": "",
        "visibility_km": None,
    }
    for key, value in current_defaults.items():
        if key not in current or current.get(key) is None and value is not None:
            current[key] = value

    risk = analyze_weather_risk(current)
    if active_warnings:
        risk["has_weather_risk"] = True
        risk["risk_level"] = "high"
        warning_tags = []
        warning_text = json.dumps(active_warnings, ensure_ascii=False)
        if "大雨" in warning_text or "豪雨" in warning_text:
            warning_tags.append("heavy_rain")
        if "強風" in warning_text:
            warning_tags.append("strong_wind")
        if "濃霧" in warning_text:
            warning_tags.append("low_visibility")
        if not warning_tags:
            warning_tags.append("official_warning")
        risk["risk_tags"] = sorted(set((risk.get("risk_tags") or []) + warning_tags))

    forecast = weather_payload["forecast"]
    forecast[0] = {**forecast[0], **current} if forecast else current
    return {
        "current": current,
        "forecast": forecast,
        "active_warnings": active_warnings,
        "observed_at": observed_at,
        "hourly": hourly,
        "radar_image_url": CWA_RADAR_IMAGE_URL,
        "data_sources": data_sources,
        "source_errors": source_errors,
        **risk,
    }


async def build_weather_snapshot(city: str, district: str, event_time: Optional[datetime] = None) -> Dict[str, Any]:
    payload = await build_live_weather_payload(city, district)
    weather = pick_weather_for_time(payload.get("forecast") or [], event_time)
    risk = analyze_weather_risk(weather)
    if payload.get("active_warnings"):
        risk["has_weather_risk"] = True
        risk["risk_level"] = "high" if risk_rank(risk.get("risk_level")) < 2 else risk["risk_level"]
        risk["risk_tags"] = sorted(set((risk.get("risk_tags") or []) + (payload.get("risk_tags") or [])))
    return {
        "city": city,
        "district": district,
        "event_time": event_time.isoformat() if event_time else None,
        "weather": weather,
        "active_warnings": payload.get("active_warnings") or [],
        "observed_at": payload.get("observed_at"),
        **risk,
        "captured_at": taipei_now().isoformat(),
    }


def risk_rank(level: Optional[str]) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(level or "low", 0)


def compare_weather_snapshots(old_snapshot: Dict[str, Any], new_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    old_weather = old_snapshot.get("weather") or old_snapshot.get("current") or {}
    new_weather = new_snapshot.get("weather") or new_snapshot.get("current") or {}
    old_risk = old_snapshot.get("risk_level") or analyze_weather_risk(old_weather)["risk_level"]
    new_risk = new_snapshot.get("risk_level") or analyze_weather_risk(new_weather)["risk_level"]
    old_tags = set(old_snapshot.get("risk_tags") or [])
    new_tags = set(new_snapshot.get("risk_tags") or [])

    pop_delta = safe_int(new_weather.get("pop")) - safe_int(old_weather.get("pop"))
    temp_delta = safe_int(new_weather.get("temp")) - safe_int(old_weather.get("temp"))
    description_changed = (old_weather.get("description") or "") != (new_weather.get("description") or "")
    added_tags = sorted(new_tags - old_tags)
    reasons = []

    if risk_rank(new_risk) > risk_rank(old_risk):
        reasons.append(f"風險等級由 {old_risk} 升為 {new_risk}")
    if pop_delta >= 40:
        reasons.append(f"降雨機率增加 {pop_delta}%")
    if abs(temp_delta) >= 6:
        reasons.append(f"溫度變化 {temp_delta:+d} 度")
    if added_tags:
        reasons.append(f"新增風險: {', '.join(added_tags)}")
    if description_changed and risk_rank(new_risk) >= 1:
        reasons.append(f"天氣由「{old_weather.get('description', '未知')}」變為「{new_weather.get('description', '未知')}」")

    should_notify = bool(reasons) or (risk_rank(new_risk) == 2 and risk_rank(old_risk) < 2)
    return {
        "should_notify": should_notify,
        "severity": new_risk if should_notify else "low",
        "reasons": reasons,
        "diff": {
            "pop_delta": pop_delta,
            "temp_delta": temp_delta,
            "old_risk_level": old_risk,
            "new_risk_level": new_risk,
            "old_weather": old_weather,
            "new_weather": new_weather,
        },
    }


def resolve_event_location_parts(event: Dict[str, Any]) -> Dict[str, str]:
    city = event.get("city") or ""
    district = event.get("district") or ""
    location = event.get("location") or event.get("location_name") or ""

    if city and district:
        return {"city": city, "district": district}

    for known_city, districts in TAIWAN_LOCATIONS.items():
        if known_city in location:
            city = city or known_city
            for known_district in districts:
                if known_district in location:
                    district = district or known_district
                    break
            break

    geocoded = geocode_fallback(location or event.get("title") or "")
    return {
        "city": city or geocoded.get("city") or "臺北市",
        "district": district or geocoded.get("district") or REPRESENTATIVE_DISTRICTS.get(city or geocoded.get("city") or "臺北市", "中正區"),
    }


def build_alternative_location(city: str, district: str, risk_tags: List[str]) -> str:
    if "heavy_rain" in risk_tags:
        return f"{city}{district}附近的室內場館、百貨或捷運站周邊，避免低窪與地下道。"
    if "strong_wind" in risk_tags:
        return f"{city}{district}附近的室內空間，避免海邊、河堤、招牌與路樹旁。"
    if "high_uvi" in risk_tags:
        return f"{city}{district}附近有遮蔭或室內空調的地點。"
    return f"{city}{district}附近較安全的室內備案地點。"


async def build_weather_change_message(event: Dict[str, Any], comparison: Dict[str, Any], new_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    title = event.get("title") or "行程"
    city = new_snapshot.get("city") or ""
    district = new_snapshot.get("district") or ""
    risk_tags = new_snapshot.get("risk_tags") or []
    alternative = build_alternative_location(city, district, risk_tags)
    reasons_text = "、".join(comparison.get("reasons") or ["天氣風險上升"])
    local_message = f"「{title}」接近日期天氣變化明顯：{reasons_text}。建議改到{alternative}"
    prompt = (
        f"行程:{title}。地點:{city}{district}。天氣變化:{reasons_text}。"
        f"新天氣:{json.dumps(new_snapshot.get('weather') or {}, ensure_ascii=False)}。"
        f"請用60字內提醒使用者，並建議更換到更安全地點。"
    )
    ai_message = await call_gemini_raw(prompt)
    if not ai_message or ai_message.startswith("["):
        ai_message = local_message
    return {
        "message": ai_message,
        "suggested_location": alternative,
        "suggestion_source": "gemini" if ai_message != local_message else "local_fallback",
    }


async def _internal_sync(city: str, district: str):
    """內部背景核心同步邏輯 (加上單一縣市的錯誤捕捉)"""
    try:
        weather_payload = await build_live_weather_payload(city, district)
        now = taipei_now()
        db_payload = {
            "city_name": f"{city}{district}",
            "weather_data": {
                "current": weather_payload["current"],
                "forecast": weather_payload["forecast"],
                "schema_version": "weather_live_v3",
                "active_warnings": weather_payload.get("active_warnings", []),
                "hourly": weather_payload.get("hourly", []),
                "radar_image_url": weather_payload.get("radar_image_url", ""),
                "observed_at": weather_payload.get("observed_at"),
                "data_sources": weather_payload.get("data_sources", {}),
                "source_errors": weather_payload.get("source_errors", []),
                "risk_level": weather_payload["risk_level"],
                "risk_tags": weather_payload["risk_tags"],
                "has_weather_risk": weather_payload["has_weather_risk"],
            },
            "radar_image_url": weather_payload.get("radar_image_url", ""),
            "uvi": weather_payload["current"].get("uvi", 0),
            "aqi": weather_payload["current"].get("aqi", 0),
            "app_temp": weather_payload["current"].get("app_temp", 0),
            "wind_ms": weather_payload["current"].get("wind_ms", 0),
            "wind_dir": weather_payload["current"].get("wind_dir", ""),
            "rain_mm_1h": weather_payload["current"].get("rain_mm_1h", 0),
            "rain_mm_3h": weather_payload["current"].get("rain_mm_3h", 0),
            "rain_mm_6h": weather_payload["current"].get("rain_mm_6h", 0),
            "rain_mm_12h": weather_payload["current"].get("rain_mm_12h", 0),
            "rain_mm_24h": weather_payload["current"].get("rain_mm_24h", 0),
            "wind_gust_ms": weather_payload["current"].get("wind_gust_ms", 0),
            "wind_gust_dir": weather_payload["current"].get("wind_gust_dir", ""),
            "visibility_km": weather_payload["current"].get("visibility_km", 0),
            "observed_temp": weather_payload["current"].get("observed_temp", 0),
            "observed_humidity": weather_payload["current"].get("observed_humidity", 0),
            "observed_wind_ms": weather_payload["current"].get("observed_wind_ms", 0),
            "observed_wind_dir": weather_payload["current"].get("observed_wind_dir", ""),
            "aqi_site": weather_payload["current"].get("aqi_site", ""),
            "aqi_status": weather_payload["current"].get("aqi_status", ""),
            "aqi_pollutant": weather_payload["current"].get("aqi_pollutant", ""),
            "pm25": weather_payload["current"].get("pm25", 0),
            "pm10": weather_payload["current"].get("pm10", 0),
            "o3": weather_payload["current"].get("o3", 0),
            "active_warnings": weather_payload.get("active_warnings", []),
            "hourly": weather_payload.get("hourly", []),
            "observed_at": weather_payload.get("observed_at"),
            "data_sources": weather_payload.get("data_sources", {}),
            "source_errors": weather_payload.get("source_errors", []),
            "updated_at": now.isoformat(),
            "valid_until": (now + timedelta(hours=3)).isoformat()
        }
        try:
            supabase.table("weather_cache").upsert(db_payload, on_conflict="city_name").execute()
        except Exception:
            legacy_payload = {
                "city_name": db_payload["city_name"],
                "weather_data": db_payload["weather_data"],
                "updated_at": db_payload["updated_at"],
                "valid_until": db_payload["valid_until"],
            }
            supabase.table("weather_cache").upsert(legacy_payload, on_conflict="city_name").execute()
        print(f"[weather_sync] synced: {city}{district}")
        return {"success": True, "city_name": f"{city}{district}", "refreshed_at": now.isoformat()}
        
    except Exception as e:
        error_msg = str(e)
        print(f"[weather_sync] failed: {city} {error_msg}")
        try:
            supabase.table("sync_logs").insert({
                "task_name": f"weather_sync_{city}",
                "status": "error",
                "message": f"{city} 同步失敗: {error_msg}"
            }).execute()
        except Exception:
            pass
        return {"success": False, "city_name": f"{city}{district}", "message": error_msg}


async def _delayed_sync(city: str, district: str, delay_seconds: int):
    """延遲執行小幫手：保護 IP 不被氣象署封鎖"""
    await asyncio.sleep(delay_seconds)
    await _internal_sync(city, district)


async def _master_alert_and_log():
    """📍 最終任務：抓取真實氣象署警報並寫入日誌 (移除 delay_seconds，交由 orchestrator 控制)"""
    try:
        print("[weather_alerts] fetching CWA alerts")
        
        # 1. 抓取真實氣象署特報 (W-C0033-002)
        alert_url = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/W-C0033-002"
        alert_params = {"Authorization": CWA_API_KEY, "format": "JSON"}
        
        alert_res = await fetch_cwa_json(alert_url, alert_params)

        # 2. 解析警報資料 (防呆處理)
        records = alert_res.get("records", {})
        locations = records.get("location", [])

        active_alerts = []
        for loc in locations:
            loc_name = loc.get("locationName", "")
            hazard_conditions = loc.get("hazardConditions", {}).get("hazards", [])
            
            for hazard in hazard_conditions:
                info = hazard.get("info", {})
                phenomena = info.get("phenomena", "未知警報")
                significance = info.get("significance", "特報")

                is_high_severity = any(keyword in phenomena or keyword in significance for keyword in ["大", "豪", "警報", "颱風"])
                
                active_alerts.append({
                    "title": f"{loc_name}{phenomena}{significance}",
                    "severity": "high" if is_high_severity else "medium",
                    "description": f"氣象署發布：{loc_name}目前有{phenomena}{significance}，請注意防範。",
                    "created_at": datetime.now(timezone(timedelta(hours=8))).isoformat()
                })

        # 3. 寫入資料庫 (weather_alerts)
        if active_alerts:
            supabase.table("weather_alerts").insert(active_alerts).execute()
            print(f"[weather_alerts] inserted {len(active_alerts)} alerts")
        else:
            print("[weather_alerts] no active alerts")

        # 4. 寫入排程總結日誌 (翊翔的需求)
        supabase.table("sync_logs").insert({
            "task_name": "weather_update_all",
            "status": "success",
            "message": "全台 22 縣市天氣與真實警報排程執行完畢"
        }).execute()
        print("[weather_alerts] summary logged")

    except Exception as e:
        error_msg = str(e)
        print(f"[weather_alerts] failed: {error_msg}")
        
        supabase.table("sync_logs").insert({
            "task_name": "weather_update_all",
            "status": "error",
            "message": f"排程總結(含警報)執行失敗: {error_msg}"
        }).execute()


async def master_sync_orchestrator():
    """👨‍✈️ 總指揮官任務：確保所有縣市都跑完，再執行總結"""
    tasks = []
    delay = 0
    for city, district in REPRESENTATIVE_DISTRICTS.items():
        # 將每個縣市的同步任務加入清單，並依序增加延遲防封鎖
        tasks.append(_delayed_sync(city, district, delay))
        delay += 1 
        
    # 等待這 22 個縣市的任務 "全部" 執行完畢 (解決定時炸彈與競態條件)
    await asyncio.gather(*tasks)
    
    # 全部完成後，才安全地執行最後的警報與日誌統整
    await _master_alert_and_log()


async def refresh_expired_weather_cache(force: bool = False) -> Dict[str, Any]:
    now = taipei_now()
    CRON_STATUS["last_started_at"] = now.isoformat()
    CRON_STATUS["last_status"] = "running"
    refreshed = []
    skipped = []
    errors = []

    try:
        res = supabase.table("weather_cache").select("city_name,valid_until").execute()
        cache_rows = res.data or []
    except Exception as e:
        message = f"讀取 weather_cache 失敗: {e}"
        CRON_STATUS.update({
            "last_finished_at": taipei_now().isoformat(),
            "last_status": "error",
            "last_message": message,
            "last_refreshed_count": 0,
            "last_error_count": 1,
        })
        log_sync("refresh_weather_cache", "error", message, "cron", {"force": force})
        return safe_response("error", {"refreshed": [], "skipped": [], "errors": []}, message, "supabase")

    for row in cache_rows:
        city_name = row.get("city_name") or ""
        parts = split_city_district(city_name)
        if not parts:
            errors.append({"city_name": city_name, "message": "無法解析 city_name"})
            continue

        valid_until = parse_datetime(row.get("valid_until"))
        should_refresh = force or not valid_until or valid_until <= now
        if not should_refresh:
            skipped.append({"city_name": city_name, "valid_until": row.get("valid_until")})
            continue

        try:
            result = await _internal_sync(parts["city"], parts["district"])
            if result.get("success"):
                refreshed.append({"city_name": city_name, "refreshed_at": result.get("refreshed_at")})
            else:
                errors.append({"city_name": city_name, "message": result.get("message") or "同步失敗"})
        except Exception as e:
            errors.append({"city_name": city_name, "message": str(e)})

    status = "success" if not errors else "partial_success"
    payload = {
        "refreshed": refreshed,
        "skipped": skipped,
        "errors": errors,
        "checked": len(cache_rows),
    }
    message = "weather_cache refresh completed"
    CRON_STATUS.update({
        "last_finished_at": taipei_now().isoformat(),
        "last_status": status,
        "last_message": message,
        "last_refreshed_count": len(refreshed),
        "last_error_count": len(errors),
    })
    log_sync("refresh_weather_cache", status, message, "cron", payload)
    return safe_response(status, payload, message, "cron", errors)


async def refresh_weather_cache_city(city: str, district: str) -> Dict[str, Any]:
    try:
        result = await _internal_sync(city, district)
        if not result.get("success"):
            return safe_response(
                "error",
                {"city": city, "district": district, "city_name": result.get("city_name")},
                f"weather_cache refresh failed: {result.get('message') or '同步失敗'}",
                "cwa",
                [{"service": "cwa", "message": result.get("message") or "同步失敗"}],
            )
        city_name = f"{city}{district}"
        return safe_response(
            "success",
            {"city": city, "district": district, "city_name": city_name, "refreshed_at": result.get("refreshed_at")},
            f"{city_name} weather_cache refreshed",
            "cwa",
        )
    except Exception as e:
        return safe_response(
            "error",
            {"city": city, "district": district},
            f"weather_cache refresh failed: {e}",
            "cwa",
            [{"service": "cwa", "message": str(e)}],
        )


def summarize_weather_cache(limit: int = 30) -> Dict[str, Any]:
    now = taipei_now()
    try:
        res = (
            supabase.table("weather_cache")
            .select("city_name,updated_at,valid_until,weather_data")
            .order("valid_until")
            .limit(limit)
            .execute()
        )
        rows = res.data or []
    except Exception as e:
        return safe_response(
            "error",
            {"items": [], "total": 0, "expired_count": 0, "fresh_count": 0},
            f"讀取 weather_cache 失敗: {e}",
            "supabase",
            [{"service": "supabase", "message": str(e)}],
        )

    items = []
    expired_count = 0
    fresh_count = 0
    for row in rows:
        valid_until = parse_datetime(row.get("valid_until"))
        is_expired = not valid_until or valid_until <= now
        expired_count += 1 if is_expired else 0
        fresh_count += 0 if is_expired else 1
        weather_data = row.get("weather_data") or {}
        current = weather_data.get("current") or {}
        items.append({
            "city_name": row.get("city_name"),
            "updated_at": row.get("updated_at"),
            "valid_until": row.get("valid_until"),
            "is_expired": is_expired,
            "description": current.get("description"),
            "pop": current.get("pop"),
            "temp": current.get("temp"),
            "risk_level": weather_data.get("risk_level"),
            "risk_tags": weather_data.get("risk_tags") or [],
        })

    return safe_response(
        "success",
        {
            "items": items,
            "total": len(items),
            "expired_count": expired_count,
            "fresh_count": fresh_count,
            "checked_at": now.isoformat(),
        },
        "weather cache status loaded",
        "weather_cache",
    )


