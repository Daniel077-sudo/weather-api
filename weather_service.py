import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from config import CRON_STATUS, CWA_API_KEY, supabase
from data import CITY_7DAY_MAP, CITY_MAP, REPRESENTATIVE_DISTRICTS, TAIWAN_LOCATIONS
from gemini_service import call_gemini_raw
from utils import geocode_fallback, log_sync, parse_datetime, safe_int, safe_response, taipei_now

CWA_SSL_ERROR_MARKERS = ("CERTIFICATE_VERIFY_FAILED", "Missing Subject Key Identifier")


async def fetch_cwa_json(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch CWA JSON, retrying once for the CWA certificate-chain issue seen on Render."""
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
                },
            )

            value = extract_element_value(item.get("elementValue") or item.get("ElementValue") or [])
            if not value:
                continue

            if element_name == "Wx" or "天氣現象" in element_name:
                period["description"] = value
            elif "PoP" in element_name or "降雨機率" in element_name:
                period["pop"] = safe_int(value)
            elif element_name in ["T", "MaxT", "MinT"] or "溫度" in element_name:
                period["temp"] = safe_int(value)
            elif element_name == "RH" or "相對濕度" in element_name:
                period["hum"] = safe_int(value)
            elif element_name == "AT" or "體感溫度" in element_name:
                period["app_temp"] = safe_int(value)
            elif element_name == "UVI" or "紫外線" in element_name:
                period["uvi"] = safe_int(value)
            elif element_name == "WS" or "風速" in element_name:
                period["wind_speed"] = value

    return sorted(time_map.values(), key=lambda item: item["time"])


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
    wind_speed = safe_int(weather.get("wind_speed"))
    tags = []

    if pop >= 70 or any(keyword in description for keyword in ["大雨", "豪雨", "雷雨"]):
        tags.append("heavy_rain")
    if any(keyword in description for keyword in ["颱風", "強風"]):
        tags.append("strong_wind")
    if uvi >= 8:
        tags.append("high_uvi")
    if wind_speed >= 10:
        tags.append("strong_wind")

    if "heavy_rain" in tags or "strong_wind" in tags:
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


async def build_weather_snapshot(city: str, district: str, event_time: Optional[datetime] = None) -> Dict[str, Any]:
    payload = await fetch_cwa_forecast(city, district, seven_day=True)
    weather = pick_weather_for_time(payload.get("forecast") or [], event_time)
    risk = analyze_weather_risk(weather)
    return {
        "city": city,
        "district": district,
        "event_time": event_time.isoformat() if event_time else None,
        "weather": weather,
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
        weather_payload = await fetch_cwa_forecast(city, district, seven_day=True)
        now = taipei_now()
        db_payload = {
            "city_name": f"{city}{district}",
            "weather_data": {
                "current": weather_payload["current"],
                "forecast": weather_payload["forecast"],
                "risk_level": weather_payload["risk_level"],
                "risk_tags": weather_payload["risk_tags"],
                "has_weather_risk": weather_payload["has_weather_risk"],
            },
            "updated_at": now.isoformat(),
            "valid_until": (now + timedelta(hours=3)).isoformat()
        }
        supabase.table("weather_cache").upsert(db_payload, on_conflict="city_name").execute()
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


