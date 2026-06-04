import hashlib
import json
import math
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from config import CRON_SECRET, supabase
from data import DISASTER_CODE_ALIASES, GEOCODE_FALLBACKS, RISK_KEYWORDS

def taipei_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def safe_response(status: str, data: Any = None, message: str = "", source: str = "backend", errors: Optional[List[Any]] = None) -> Dict[str, Any]:
    return {
        "status": status,
        "data": data if data is not None else {},
        "message": message,
        "source": source,
        "errors": errors or [],
    }


def require_cron_secret(x_cron_secret: Optional[str]) -> Optional[Dict[str, Any]]:
    if not CRON_SECRET:
        return None
    if x_cron_secret != CRON_SECRET:
        return safe_response("error", {}, "Invalid cron secret", "auth", [{"code": "invalid_cron_secret"}])
    return None


def stable_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def log_sync(task_name: str, status: str, message: str, source: str = "backend", payload: Optional[Dict[str, Any]] = None):
    try:
        supabase.table("sync_logs").insert({
            "task_name": task_name,
            "status": status,
            "message": message,
            "source": source,
            "payload": payload or {},
            "finished_at": taipei_now().isoformat(),
        }).execute()
    except Exception:
        pass


def parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def maps_url(lat: float, lng: float) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"


def normalize_shelter(shelter: Dict[str, Any], origin_lat: Optional[float] = None, origin_lng: Optional[float] = None) -> Dict[str, Any]:
    lat = float(shelter.get("lat") or shelter.get("latitude") or 0)
    lng = float(shelter.get("lng") or shelter.get("longitude") or 0)
    result = {
        "id": shelter.get("id"),
        "name": shelter.get("name") or "",
        "city": shelter.get("city") or "",
        "district": shelter.get("district") or "",
        "address": shelter.get("address") or "",
        "lat": lat,
        "lng": lng,
        "capacity": shelter.get("capacity"),
        "shelter_type": shelter.get("shelter_type") or "shelter",
        "maps_url": maps_url(lat, lng),
    }
    if origin_lat is not None and origin_lng is not None:
        result["distance_km"] = round(haversine_km(origin_lat, origin_lng, lat, lng), 2)
    return result


def geocode_fallback(query: str) -> Dict[str, Any]:
    if query in GEOCODE_FALLBACKS:
        return GEOCODE_FALLBACKS[query]

    for name, data in GEOCODE_FALLBACKS.items():
        if query in name or name in query:
            return data

    return {
        "name": query,
        "lat": 25.0478,
        "lng": 121.5170,
        "city": "台北市",
        "district": "中正區",
        "note": "fallback_default_location",
    }


def normalize_disaster_code(disaster: Optional[str]) -> str:
    if not disaster:
        return ""
    return DISASTER_CODE_ALIASES.get(disaster, disaster)


def analyze_text_risk(text: str) -> Dict[str, Any]:
    lowered = text.lower()
    tags = []
    for tag, keywords in RISK_KEYWORDS.items():
        if any(keyword.lower() in lowered for keyword in keywords):
            tags.append(tag)

    if any(tag in tags for tag in ["flood", "typhoon"]):
        level = "high"
    elif tags:
        level = "medium"
    else:
        level = "low"

    return {
        "has_weather_risk": level != "low",
        "risk_level": level,
        "risk_tags": tags,
    }


def build_recommended_action(risk_level: str, risk_tags: List[str], location: str = "") -> str:
    if risk_level == "low":
        return "目前未偵測到明顯天氣風險，仍建議出門前確認最新預報。"
    if "flood" in risk_tags:
        return f"{location}可能有淹水或積水風險，請避開地下道、河堤與低窪路段。"
    if "heavy_rain" in risk_tags:
        return f"{location}可能有大雨風險，建議提早出門並攜帶雨具。"
    if "strong_wind" in risk_tags:
        return f"{location}可能有強風風險，請避開招牌、路樹與施工圍籬。"
    if "fog" in risk_tags:
        return f"{location}可能有濃霧或低能見度，交通移動請放慢速度。"
    if "typhoon" in risk_tags:
        return f"{location}可能受颱風影響，非必要請減少外出並確認交通異動。"
    return f"{location}有天氣風險，請保留彈性時間並注意官方警報。"


