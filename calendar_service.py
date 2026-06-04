from typing import Any, Dict, Optional

import httpx

from config import TIMETREE_ACCESS_TOKEN, TIMETREE_CALENDAR_ID, TIMETREE_EVENTS_URL, supabase
from utils import log_sync, safe_response, taipei_now

def normalize_timetree_event(raw_event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    attributes = raw_event.get("attributes") if isinstance(raw_event.get("attributes"), dict) else raw_event
    event_id = raw_event.get("id") or attributes.get("id")
    title = attributes.get("title") or attributes.get("summary") or "TimeTree 行程"
    start_time = (
        attributes.get("start_at")
        or attributes.get("startAt")
        or attributes.get("start_time")
        or attributes.get("start")
    )
    end_time = (
        attributes.get("end_at")
        or attributes.get("endAt")
        or attributes.get("end_time")
        or attributes.get("end")
        or start_time
    )
    if not event_id or not start_time:
        return None
    location = attributes.get("location") or attributes.get("place") or ""
    return {
        "title": title,
        "start_time": start_time,
        "end_time": end_time,
        "location": location,
        "description": attributes.get("description") or attributes.get("memo") or "",
        "external_source": "timetree",
        "external_event_id": str(event_id),
        "last_synced_at": taipei_now().isoformat(),
    }


async def fetch_timetree_events() -> Dict[str, Any]:
    if not TIMETREE_ACCESS_TOKEN:
        return safe_response("not_configured", {"events": []}, "TIMETREE_ACCESS_TOKEN is missing", "timetree")
    if not TIMETREE_EVENTS_URL:
        return safe_response("not_configured", {"events": []}, "TIMETREE_EVENTS_URL is missing", "timetree")
    events_url = TIMETREE_EVENTS_URL.format(calendar_id=TIMETREE_CALENDAR_ID or "")
    headers = {"Authorization": f"Bearer {TIMETREE_ACCESS_TOKEN}"}
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(events_url, headers=headers, timeout=20.0)
            if res.status_code in [401, 403]:
                return safe_response("partial_success", {"events": []}, "TimeTree authorization failed", "timetree", [{"status_code": res.status_code}])
            res.raise_for_status()
            payload = res.json()
    except Exception as e:
        return safe_response("partial_success", {"events": []}, f"TimeTree sync failed: {e}", "timetree", [{"message": str(e)}])

    raw_events = payload.get("data") or payload.get("events") or []
    if isinstance(raw_events, dict):
        raw_events = raw_events.get("events") or raw_events.get("data") or []
    events = []
    for item in raw_events:
        if isinstance(item, dict):
            normalized = normalize_timetree_event(item)
            if normalized:
                events.append(normalized)
    return safe_response("success", {"events": events}, "TimeTree events loaded", "timetree")


async def sync_timetree_event_payloads() -> Dict[str, Any]:
    fetched = await fetch_timetree_events()
    if fetched["status"] != "success":
        return fetched

    created = []
    updated = []
    errors = []
    for event_payload in fetched["data"].get("events", []):
        try:
            existing = supabase.table("events").select("id").eq("external_source", "timetree").eq("external_event_id", event_payload["external_event_id"]).limit(1).execute()
            if existing.data:
                event_id = existing.data[0]["id"]
                supabase.table("events").update(event_payload).eq("id", event_id).execute()
                updated.append(event_payload["external_event_id"])
            else:
                supabase.table("events").insert(event_payload).execute()
                created.append(event_payload["external_event_id"])
        except Exception as e:
            errors.append({"external_event_id": event_payload.get("external_event_id"), "message": str(e)})

    status = "success" if not errors else "partial_success"
    payload = {"created": created, "updated": updated, "errors": errors}
    log_sync("timetree_sync", status, "TimeTree sync completed", "timetree", payload)
    return safe_response(status, payload, "TimeTree sync completed", "timetree", errors)
