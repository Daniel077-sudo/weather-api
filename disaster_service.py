import hashlib
from datetime import timedelta
from typing import Any, Dict, List, Optional

import httpx

from config import CWA_API_KEY, supabase
from utils import log_sync, parse_datetime, safe_response, taipei_now


def _alert_hash(payload: Dict[str, Any]) -> str:
    key = "|".join(str(payload.get(part) or "") for part in ["source", "type", "city", "district", "title", "started_at"])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _severity_from_text(text: str) -> str:
    high_keywords = ["豪雨", "大豪雨", "超大豪雨", "颱風", "強風", "淹水", "土石流", "停班", "停課"]
    medium_keywords = ["大雨", "雷雨", "濃霧", "低溫", "高溫", "陸上強風"]
    if any(keyword in text for keyword in high_keywords):
        return "high"
    if any(keyword in text for keyword in medium_keywords):
        return "medium"
    return "low"


def _type_from_text(text: str) -> str:
    if any(keyword in text for keyword in ["豪雨", "大雨", "雷雨", "雨"]):
        return "rain"
    if any(keyword in text for keyword in ["淹水", "水位"]):
        return "flood"
    if any(keyword in text for keyword in ["颱風", "強風", "風"]):
        return "wind"
    if "高溫" in text:
        return "heat"
    if "低溫" in text:
        return "cold"
    return "weather"


def normalize_cwa_alert(location_name: str, hazard: Dict[str, Any], raw: Dict[str, Any]) -> Dict[str, Any]:
    info = hazard.get("info") or {}
    phenomena = str(info.get("phenomena") or "天氣警特報")
    significance = str(info.get("significance") or "")
    title = f"{location_name}{phenomena}{significance}"
    description = str(info.get("description") or info.get("instruction") or title)
    started_at = info.get("effectiveTime") or info.get("onset") or taipei_now().isoformat()
    expires_at = info.get("expires") or info.get("expiresTime") or (taipei_now() + timedelta(hours=6)).isoformat()
    combined = f"{title} {description}"
    payload = {
        "source": "cwa",
        "type": _type_from_text(combined),
        "city": location_name,
        "district": "",
        "title": title,
        "description": description,
        "severity": _severity_from_text(combined),
        "started_at": started_at,
        "expires_at": expires_at,
        "source_url": "https://www.cwa.gov.tw/V8/C/P/Warning/W26.html",
        "raw_payload": {"location": raw, "hazard": hazard},
        "updated_at": taipei_now().isoformat(),
    }
    payload["alert_hash"] = _alert_hash(payload)
    return payload


async def refresh_disaster_alerts() -> Dict[str, Any]:
    if not CWA_API_KEY:
        return safe_response("not_configured", {"inserted": 0, "alerts": []}, "CWA_API_KEY is missing", "cwa")

    try:
        url = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/W-C0033-002"
        params = {"Authorization": CWA_API_KEY, "format": "JSON"}
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=20.0)
            response.raise_for_status()
            payload = response.json()

        alerts: List[Dict[str, Any]] = []
        for location in payload.get("records", {}).get("location", []) or []:
            location_name = location.get("locationName") or ""
            hazards = location.get("hazardConditions", {}).get("hazards", []) or []
            for hazard in hazards:
                alerts.append(normalize_cwa_alert(location_name, hazard, location))

        inserted = 0
        errors = []
        for alert in alerts:
            try:
                supabase.table("disaster_alerts").upsert(alert, on_conflict="alert_hash").execute()
                inserted += 1
            except Exception as e:
                errors.append({"title": alert.get("title"), "message": str(e)})

        status = "success" if not errors else "partial_success"
        data = {"inserted": inserted, "alerts": alerts, "errors": errors}
        log_sync("refresh_disaster_alerts", status, f"refreshed {inserted} disaster alerts", "cwa", data)
        return safe_response(status, data, "disaster alerts refreshed", "cwa", errors)
    except Exception as e:
        error = {"service": "cwa", "message": str(e)}
        log_sync("refresh_disaster_alerts", "error", str(e), "cwa", {"errors": [error]})
        return safe_response("error", {"inserted": 0, "alerts": []}, str(e), "cwa", [error])


def get_active_disaster_alerts(city: Optional[str] = None, district: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
    now = taipei_now().isoformat()
    try:
        query = (
            supabase.table("disaster_alerts")
            .select("*")
            .gte("expires_at", now)
            .order("severity", desc=True)
            .order("started_at", desc=True)
            .limit(limit)
        )
        if city:
            query = query.eq("city", city)
        if district:
            query = query.or_(f"district.eq.{district},district.eq.")
        res = query.execute()
        return safe_response("success", res.data or [], "active disaster alerts loaded", "disaster_alerts")
    except Exception as e:
        return safe_response("error", [], str(e), "disaster_alerts", [{"service": "supabase", "message": str(e)}])


def cleanup_expired_disaster_alerts() -> Dict[str, Any]:
    now = taipei_now().isoformat()
    try:
        deleted = supabase.table("disaster_alerts").delete().lt("expires_at", now).execute()
        rows = deleted.data or []
        data = {"deleted_count": len(rows), "deleted": rows}
        log_sync("cleanup_disaster_alerts", "success", f"deleted {len(rows)} expired disaster alerts", "disaster_alerts", data)
        return safe_response("success", data, "expired disaster alerts cleaned", "disaster_alerts")
    except Exception as e:
        error = {"service": "supabase", "message": str(e)}
        log_sync("cleanup_disaster_alerts", "error", str(e), "disaster_alerts", {"errors": [error]})
        return safe_response("error", {"deleted_count": 0}, str(e), "disaster_alerts", [error])


def summarize_disaster_alert_risk(alerts: List[Dict[str, Any]]) -> Dict[str, Any]:
    severities = [str(alert.get("severity") or "low") for alert in alerts]
    if "high" in severities:
        level = "high"
    elif "medium" in severities:
        level = "medium"
    else:
        level = "low"
    return {
        "risk_level": level,
        "has_disaster_risk": bool(alerts),
        "alert_count": len(alerts),
        "risk_tags": sorted({str(alert.get("type") or "alert") for alert in alerts}),
    }


def monitor_watch_areas(limit: int = 500) -> Dict[str, Any]:
    try:
        areas_res = (
            supabase.table("user_watch_areas")
            .select("*")
            .eq("is_active", True)
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        )
        watch_areas = areas_res.data or []
    except Exception as e:
        return safe_response("error", {"checked": 0, "created": 0}, str(e), "user_watch_areas", [{"service": "supabase", "message": str(e)}])

    checked = 0
    created = 0
    skipped = 0
    errors: List[Dict[str, Any]] = []
    notifications: List[Dict[str, Any]] = []
    alerts_cache: Dict[str, Dict[str, Any]] = {}

    for area in watch_areas:
        checked += 1
        user_id = area.get("user_id")
        watch_area_id = area.get("id")
        city = area.get("city")
        district = area.get("district") or ""
        if not user_id or not city:
            skipped += 1
            continue

        cache_key = f"{city}|{district}"
        if cache_key not in alerts_cache:
            alerts_cache[cache_key] = get_active_disaster_alerts(city, district, 10)
        alerts_response = alerts_cache[cache_key]
        if alerts_response.get("status") != "success":
            errors.extend(alerts_response.get("errors") or [])
            continue

        for alert in alerts_response.get("data") or []:
            alert_hash = alert.get("alert_hash")
            if not alert_hash:
                skipped += 1
                continue
            notification = {
                "user_id": user_id,
                "watch_area_id": watch_area_id,
                "alert_hash": alert_hash,
                "city": city,
                "district": district,
                "title": alert.get("title") or "災防告警",
                "message": alert.get("description") or alert.get("title") or "",
                "severity": alert.get("severity") or "low",
                "source": alert.get("source") or "unknown",
                "source_url": alert.get("source_url") or "",
                "status": "unread",
                "created_at": taipei_now().isoformat(),
            }
            try:
                supabase.table("area_alert_notifications").upsert(
                    notification,
                    on_conflict="user_id,watch_area_id,alert_hash",
                ).execute()
                created += 1
                notifications.append(notification)
            except Exception as e:
                errors.append({"watch_area_id": watch_area_id, "alert_hash": alert_hash, "message": str(e)})

    status = "success" if not errors else "partial_success"
    data = {
        "checked": checked,
        "created": created,
        "skipped": skipped,
        "area_cache_entries": len(alerts_cache),
        "notifications": notifications,
        "errors": errors,
    }
    log_sync("monitor_watch_areas", status, f"checked {checked} watch areas, created {created} notifications", "watch_areas", data)
    return safe_response(status, data, "watch areas monitored", "watch_areas", errors)
