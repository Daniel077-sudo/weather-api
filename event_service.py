import json
from datetime import timedelta
from typing import Any, Dict

from config import supabase
from schemas import AIIntentSuggestion, EventRiskCheckRequest
from data import TAIWAN_LOCATIONS
from gemini_service import call_gemini_json_cached
from transport_service import build_transport_links, build_traffic_risk_async, determine_transport_type
from utils import analyze_text_risk, build_recommended_action, geocode_fallback, parse_datetime, safe_response, taipei_now
from weather_service import (
    analyze_weather_risk, build_alternative_location, build_weather_change_message,
    build_weather_snapshot, build_weather_suggestion, compare_weather_snapshots,
    pick_current_weather, resolve_event_location_parts, risk_rank,
)

def normalize_event(event: dict) -> dict:
    """Return the exact JSON shape expected by the iOS frontend."""
    ai_text = event.get("ai_suggestion")
    if isinstance(ai_text, dict):
        reason = ai_text.get("reason") or ""
        alternative = ai_text.get("alternative_location") or ""
        ai_text = " ".join(part for part in [reason, alternative] if part)
    elif ai_text is None:
        ai_text = ""

    event_url = event.get("url") or event.get("transport_ticket_link") or ""
    transport_type = event.get("transport_type") or determine_transport_type(event_url)

    risk_tags = event.get("risk_tags") or []
    if isinstance(risk_tags, str):
        risk_tags = [tag.strip() for tag in risk_tags.split(",") if tag.strip()]

    return {
        "id": event.get("id"),
        "title": event.get("title") or "",
        "start_time": event.get("start_time"),
        "end_time": event.get("end_time"),
        "location": event.get("location") or event.get("location_name") or "",
        "city": event.get("city") or "",
        "district": event.get("district") or "",
        "url": event_url,
        "transport_type": transport_type or "",
        "has_weather_risk": bool(event.get("has_weather_risk", False)),
        "ai_suggestion": str(ai_text),
        "risk_level": event.get("risk_level") or ("medium" if event.get("has_weather_risk") else "low"),
        "risk_tags": risk_tags,
        "recommended_action": event.get("recommended_action") or str(ai_text),
        "weather_alert_status": event.get("weather_alert_status") or "",
        "external_source": event.get("external_source") or "",
        "external_event_id": event.get("external_event_id") or "",
        "last_synced_at": event.get("last_synced_at"),
    }


async def build_event_risk(payload: EventRiskCheckRequest) -> Dict[str, Any]:
    location = payload.location or "".join(part for part in [payload.city, payload.district] if part) or "目的地"
    weather_text = ""
    alert_text = ""
    weather_payload: Dict[str, Any] = {}

    try:
        if payload.city and payload.district:
            cache = supabase.table("weather_cache").select("*").eq("city_name", f"{payload.city}{payload.district}").execute()
            if cache.data:
                weather_data = cache.data[0].get("weather_data") or {}
                current = weather_data.get("current") or {}
                weather_payload = {
                    "weather": current,
                    **analyze_weather_risk(current),
                }
                weather_text = json.dumps(weather_data, ensure_ascii=False)
    except Exception as e:
        weather_text = f"weather_cache unavailable: {e}"

    try:
        alerts = supabase.table("weather_alerts").select("*").order("created_at", desc=True).limit(5).execute()
        if alerts.data:
            alert_text = json.dumps(alerts.data, ensure_ascii=False)
    except Exception as e:
        alert_text = f"weather_alerts unavailable: {e}"

    combined_text = " ".join([
        payload.title or "",
        location,
        payload.activity or "",
        payload.transport_type or "",
        weather_text,
        alert_text,
    ])
    risk = analyze_text_risk(combined_text)
    action = build_recommended_action(risk["risk_level"], risk["risk_tags"], location)
    if weather_payload:
        risk = {
            "has_weather_risk": weather_payload["has_weather_risk"] or risk["has_weather_risk"],
            "risk_level": weather_payload["risk_level"] if risk_rank(weather_payload["risk_level"]) >= risk_rank(risk["risk_level"]) else risk["risk_level"],
            "risk_tags": sorted(set(weather_payload["risk_tags"] + risk["risk_tags"])),
        }
        action = build_weather_suggestion(payload.city or "", payload.district or "", payload.title or payload.activity or "行程", weather_payload["weather"], risk)

    fallback_ai = {
        "intent": payload.activity or "commuting",
        "risk_summary": action,
        "recommended_action": action,
        "alternative_location": build_alternative_location(payload.city or "", payload.district or "", risk["risk_tags"]),
        "confidence": 0.65,
    }
    prompt = (
        "你是防災行程助理。請只回傳 JSON，不要 markdown。\n"
        f"行程:{payload.title or ''}\n"
        f"時間:{payload.start_time or ''} 到 {payload.end_time or ''}\n"
        f"地點:{location}\n"
        f"活動:{payload.activity or ''}\n"
        f"交通:{payload.transport_type or ''}\n"
        f"天氣:{weather_text}\n"
        f"警報:{alert_text}\n"
        "JSON 欄位: intent, risk_summary, recommended_action, alternative_location, confidence。"
    )
    ai_raw = await call_gemini_json_cached(
        prompt,
        fallback_ai,
        "event_risk_intent",
        payload.event_id or payload.title or location,
        {
            "event": payload.model_dump(),
            "weather": weather_payload,
            "risk": risk,
        },
    )
    try:
        ai_structured = AIIntentSuggestion(**ai_raw).model_dump()
        ai_structured["cache_hit"] = bool(ai_raw.get("cache_hit"))
    except Exception:
        ai_structured = fallback_ai
        ai_structured["cache_hit"] = False
    traffic_risk = await build_traffic_risk_async(weather_payload or risk, payload.transport_type)

    return {
        "event": {
            "title": payload.title or "",
            "start_time": payload.start_time,
            "end_time": payload.end_time,
            "location": location,
            "activity": payload.activity,
            "transport_type": payload.transport_type,
        },
        **risk,
        "recommended_action": ai_structured.get("recommended_action") or action,
        "ai_suggestion": ai_structured.get("risk_summary") or action,
        "ai_intent": ai_structured,
        "traffic_risk": traffic_risk,
        "booking_links": build_transport_links("", location, payload.transport_type),
        "tdx_status": traffic_risk.get("tdx_status"),
        "errors": [] if traffic_risk.get("tdx_status") in ["success", "not_configured"] else [{"service": "tdx", "message": traffic_risk.get("tdx_message")}],
        "sources": {
            "weather_cache_used": bool(weather_text and "unavailable" not in weather_text),
            "weather_alerts_used": bool(alert_text and "unavailable" not in alert_text),
            "gemini_used": ai_structured.get("suggestion_source") != "local_fallback" and ai_structured.get("risk_summary") != fallback_ai["risk_summary"],
            "ai_cache_hit": bool(ai_structured.get("cache_hit")),
            "tdx_used": traffic_risk.get("tdx_status") == "success",
        },
    }


async def monitor_event_weather_window(hours_ahead: int = 36) -> Dict[str, Any]:
    now = taipei_now()
    window_end = now + timedelta(hours=hours_ahead)
    result = {
        "checked": 0,
        "initialized_snapshots": 0,
        "notifications": [],
        "errors": [],
        "window": {
            "from": now.isoformat(),
            "to": window_end.isoformat(),
        },
    }

    try:
        res = supabase.table("events").select("*").gte("start_time", now.isoformat()).lte("start_time", window_end.isoformat()).execute()
        events = res.data or []
    except Exception as e:
        return {"status": "error", "message": f"讀取行程失敗: {str(e)}", **result}

    for event in events:
        result["checked"] += 1
        event_id = event.get("id")
        title = event.get("title") or "行程"
        try:
            event_time = parse_datetime(event.get("start_time"))
            location_parts = resolve_event_location_parts(event)
            new_snapshot = await build_weather_snapshot(location_parts["city"], location_parts["district"], event_time)
            old_snapshot = event.get("weather_snapshot") or {}

            if not old_snapshot:
                try:
                    supabase.table("events").update({
                        "city": location_parts["city"],
                        "district": location_parts["district"],
                        "weather_snapshot": new_snapshot,
                        "weather_checked_at": taipei_now().isoformat(),
                    }).eq("id", event_id).execute()
                except Exception as update_e:
                    result["errors"].append({"event_id": event_id, "message": f"初始化天氣快照失敗: {update_e}"})
                result["initialized_snapshots"] += 1
                continue

            comparison = compare_weather_snapshots(old_snapshot, new_snapshot)
            if not comparison["should_notify"]:
                try:
                    supabase.table("events").update({
                        "weather_checked_at": taipei_now().isoformat(),
                    }).eq("id", event_id).execute()
                except Exception:
                    pass
                continue

            reminder = await build_weather_change_message(event, comparison, new_snapshot)
            traffic_risk = await build_traffic_risk_async(new_snapshot, event.get("transport_type"))
            notification = {
                "event_id": event_id,
                "title": title,
                "start_time": event.get("start_time"),
                "location": event.get("location") or f"{location_parts['city']}{location_parts['district']}",
                "severity": comparison["severity"],
                "reasons": comparison["reasons"],
                "message": reminder["message"],
                "suggested_location": reminder["suggested_location"],
                "suggestion_source": reminder["suggestion_source"],
                "traffic_risk": traffic_risk,
                "booking_links": build_transport_links("", event.get("location") or f"{location_parts['city']}{location_parts['district']}", event.get("transport_type")),
                "tdx_status": traffic_risk.get("tdx_status"),
                "old_weather": comparison["diff"]["old_weather"],
                "new_weather": comparison["diff"]["new_weather"],
                "created_at": taipei_now().isoformat(),
            }
            result["notifications"].append(notification)

            try:
                supabase.table("event_weather_alerts").insert({
                    "event_id": str(event_id) if event_id is not None else None,
                    "title": title,
                    "message": notification["message"],
                    "severity": notification["severity"],
                    "change_summary": {
                        "reasons": notification["reasons"],
                        "old_weather": notification["old_weather"],
                        "new_weather": notification["new_weather"],
                        "traffic_risk": notification["traffic_risk"],
                        "booking_links": notification["booking_links"],
                    },
                    "suggested_location": notification["suggested_location"],
                    "created_at": notification["created_at"],
                    "status": "unread",
                }).execute()
            except Exception as insert_e:
                result["errors"].append({"event_id": event_id, "message": f"提醒寫入失敗: {insert_e}"})

            try:
                supabase.table("events").update({
                    "weather_snapshot": new_snapshot,
                    "weather_checked_at": taipei_now().isoformat(),
                    "weather_alert_status": "notified",
                    "has_weather_risk": True,
                    "risk_level": new_snapshot["risk_level"],
                    "risk_tags": new_snapshot["risk_tags"],
                    "recommended_action": notification["message"],
                    "ai_suggestion": notification["message"],
                }).eq("id", event_id).execute()
            except Exception as update_e:
                result["errors"].append({"event_id": event_id, "message": f"行程提醒狀態更新失敗: {update_e}"})

        except Exception as event_e:
            result["errors"].append({"event_id": event_id, "message": f"{title} 監測失敗: {event_e}"})

    return {"status": "success", **result}


