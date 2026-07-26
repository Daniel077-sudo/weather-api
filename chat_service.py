import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from config import supabase
from disaster_service import get_active_disaster_alerts
from gemini_service import call_gemini_json_cached
from utils import parse_datetime, safe_response, taipei_now


CHAT_ACTIONS = {"ADD_EVENT", "DELETE_EVENT", "NONE"}
TAIPEI_TZ = timezone(timedelta(hours=8))
MEMORY_MAX_CHARS = 4000
CHAT_LOGS_TABLE = "chat_logs"


def empty_chat_response(reply: str = "收到，我可以協助你查天氣、整理災防提醒，或解析新增/刪除行程。") -> Dict[str, Any]:
    return {
        "reply": reply,
        "has_alert": False,
        "alert_title": "",
        "alert_url": "",
        "action_type": "NONE",
        "event_title": "",
        "event_start": "",
        "event_end": "",
        "event_id_to_delete": "",
    }


def get_user_memory(user_id: str) -> Dict[str, Any]:
    if not user_id:
        return {"memory_markdown": "", "summary_json": {}}
    try:
        res = supabase.table("user_memory_profiles").select("*").eq("user_id", user_id).limit(1).execute()
        if res.data:
            return res.data[0]
    except Exception:
        pass
    return {"memory_markdown": "", "summary_json": {}}


def compact_memory(existing_markdown: str, message: str, response: Dict[str, Any]) -> str:
    existing = existing_markdown or ""
    now = taipei_now().isoformat(timespec="seconds")
    action = response.get("action_type") or "NONE"
    event_title = response.get("event_title") or ""
    event_start = response.get("event_start") or ""
    alert_title = response.get("alert_title") or ""
    facts = []
    if event_title:
        facts.append(f"行程: {event_title}")
    if event_start:
        facts.append(f"時間: {event_start}")
    if alert_title:
        facts.append(f"告警: {alert_title}")
    fact_text = "；".join(facts) if facts else "一般對話"
    entry = f"- {now}：使用者說「{message[:120]}」；判斷 {action}；{fact_text}"
    if not existing.strip():
        memory = "# 使用者記憶摘要\n\n## 最近互動\n" + entry
    else:
        memory = existing.rstrip() + "\n" + entry
    if len(memory) > MEMORY_MAX_CHARS:
        header = "# 使用者記憶摘要\n\n## 最近互動\n"
        tail = memory[-(MEMORY_MAX_CHARS - len(header)) :]
        memory = header + tail.lstrip()
    return memory


def persist_chat_turn(user_id: str, message: str, response: Dict[str, Any]):
    if not user_id:
        return
    now = taipei_now().isoformat()
    assistant_payload = {
        "user_id": user_id,
        "role": "assistant",
        "content": response.get("reply") or "",
        "response_payload": response,
        "action_type": response.get("action_type") or "NONE",
        "event_title": response.get("event_title") or "",
        "event_start": response.get("event_start") or None,
        "event_end": response.get("event_end") or None,
        "has_alert": bool(response.get("has_alert")),
        "alert_title": response.get("alert_title") or "",
        "alert_url": response.get("alert_url") or "",
        "user_input": message,
        "ai_response": response.get("reply") or "",
        "created_at": now,
    }
    try:
        supabase.table(CHAT_LOGS_TABLE).insert({
            "user_id": user_id,
            "role": "user",
            "content": message,
            "user_input": message,
            "ai_response": "",
            "created_at": now,
        }).execute()
        supabase.table(CHAT_LOGS_TABLE).insert(assistant_payload).execute()
    except Exception:
        try:
            supabase.table(CHAT_LOGS_TABLE).insert({
                "user_input": f"[{user_id}] {message}",
                "ai_response": response.get("reply") or "",
            }).execute()
        except Exception:
            pass

    try:
        current_memory = get_user_memory(user_id)
        memory_markdown = compact_memory(current_memory.get("memory_markdown") or "", message, response)
        summary_json = {
            "last_action_type": response.get("action_type") or "NONE",
            "last_event_title": response.get("event_title") or "",
            "last_event_start": response.get("event_start") or "",
            "last_has_alert": bool(response.get("has_alert")),
            "last_alert_title": response.get("alert_title") or "",
        }
        supabase.table("user_memory_profiles").upsert({
            "user_id": user_id,
            "memory_markdown": memory_markdown,
            "summary_json": summary_json,
            "last_interaction_at": now,
            "updated_at": now,
        }, on_conflict="user_id").execute()
    except Exception:
        pass


def get_chat_history(user_id: str, limit: int = 30) -> Dict[str, Any]:
    try:
        res = (
            supabase.table(CHAT_LOGS_TABLE)
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return safe_response("success", res.data or [], "chat history loaded", CHAT_LOGS_TABLE)
    except Exception as e:
        return safe_response("error", [], str(e), CHAT_LOGS_TABLE, [{"service": "supabase", "message": str(e)}])


def get_user_memory_response(user_id: str) -> Dict[str, Any]:
    try:
        memory = get_user_memory(user_id)
        return safe_response("success", memory, "user memory loaded", "user_memory_profiles")
    except Exception as e:
        return safe_response("error", {}, str(e), "user_memory_profiles", [{"service": "supabase", "message": str(e)}])


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def taipei_iso(value: Optional[datetime]) -> str:
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=TAIPEI_TZ)
    return value.astimezone(TAIPEI_TZ).isoformat(timespec="seconds")


def next_weekday(now: datetime, weekday: int) -> datetime:
    days = (weekday - now.weekday()) % 7
    if days == 0:
        days = 7
    return now + timedelta(days=days)


def infer_action_type(message: str) -> str:
    delete_keywords = ["刪除", "取消", "移除", "不要去了", "刪掉行程"]
    add_keywords = ["新增", "加入", "排入", "安排", "建立", "想去", "想要去", "週末", "周末", "露營", "行程"]
    if any(keyword in message for keyword in delete_keywords):
        return "DELETE_EVENT"
    if any(keyword in message for keyword in add_keywords):
        return "ADD_EVENT"
    return "NONE"


def infer_location(message: str) -> Dict[str, str]:
    location_map = {
        "阿里山": {"city": "嘉義縣", "district": "阿里山鄉"},
        "臺南": {"city": "臺南市", "district": ""},
        "台南": {"city": "臺南市", "district": ""},
        "高雄": {"city": "高雄市", "district": ""},
        "臺北": {"city": "臺北市", "district": ""},
        "台北": {"city": "臺北市", "district": ""},
        "新北": {"city": "新北市", "district": ""},
        "臺中": {"city": "臺中市", "district": ""},
        "台中": {"city": "臺中市", "district": ""},
        "花蓮": {"city": "花蓮縣", "district": ""},
        "宜蘭": {"city": "宜蘭縣", "district": ""},
        "嘉義": {"city": "嘉義縣", "district": ""},
    }
    for keyword, parts in location_map.items():
        if keyword in message:
            return parts
    return {"city": "", "district": ""}


def infer_event_time(message: str, now: Optional[datetime] = None) -> Dict[str, str]:
    now = now or taipei_now()
    base = now.astimezone(TAIPEI_TZ)

    iso_match = re.search(r"20\d{2}-\d{1,2}-\d{1,2}T\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?", message)
    if iso_match:
        start = parse_datetime(iso_match.group(0))
        end = start + timedelta(hours=2) if start else None
        return {"event_start": taipei_iso(start), "event_end": taipei_iso(end)}

    if "週末" in message or "周末" in message:
        saturday = next_weekday(base, 5).replace(hour=9, minute=0, second=0, microsecond=0)
        sunday = saturday + timedelta(days=1, hours=8)
        return {"event_start": taipei_iso(saturday), "event_end": taipei_iso(sunday)}

    if "明天" in message:
        start = (base + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    elif "今天" in message:
        start = base.replace(hour=max(base.hour + 1, 9), minute=0, second=0, microsecond=0)
    else:
        start = (base + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

    time_match = re.search(r"(上午|早上|下午|晚上|中午)?\s*(\d{1,2})\s*[點:：]\s*(\d{1,2})?", message)
    if time_match:
        period = time_match.group(1) or ""
        hour = int(time_match.group(2))
        minute = int(time_match.group(3) or 0)
        if period in ["下午", "晚上"] and hour < 12:
            hour += 12
        if period == "中午" and hour < 11:
            hour += 12
        start = start.replace(hour=hour, minute=minute)

    duration = timedelta(days=1, hours=8) if any(word in message for word in ["兩天一夜", "2天1夜"]) else timedelta(hours=2)
    return {"event_start": taipei_iso(start), "event_end": taipei_iso(start + duration)}


def infer_event_title(message: str, action_type: str) -> str:
    text = normalize_text(message)
    for token in ["幫我", "請", "新增", "加入", "排入", "安排", "建立", "行程", "我", "想要", "想"]:
        text = text.replace(token, " ")
    text = re.sub(r"(今天|明天|這週末|本週末|週末|周末|上午|早上|下午|晚上|中午|\d{1,2}\s*[點:：]\s*\d{0,2})", " ", text)
    text = normalize_text(text).strip("，。！! ")
    if not text and action_type == "DELETE_EVENT":
        return ""
    if "阿里山" in message and "露營" in message:
        if any(word in message for word in ["兩天一夜", "2天1夜", "週末", "周末"]):
            return "阿里山兩天一夜露營"
        return "阿里山露營"
    return text or "新行程"


def find_event_id_to_delete(user_id: str, title: str) -> str:
    if not title:
        return ""
    try:
        query = supabase.table("events").select("id,title,start_time").order("start_time", desc=True).limit(1)
        if user_id:
            query = query.eq("user_id", user_id)
        exact = query.eq("title", title).execute()
        if exact.data:
            return str(exact.data[0].get("id") or "")
    except Exception:
        return ""
    return ""


def lookup_alert(message: str) -> Dict[str, Any]:
    location = infer_location(message)
    if not location["city"]:
        return {"has_alert": False, "alert_title": "", "alert_url": ""}
    try:
        response = get_active_disaster_alerts(location["city"], location.get("district") or None, 1)
        alerts = response.get("data") if response.get("status") == "success" else []
        if isinstance(alerts, list) and alerts:
            alert = alerts[0]
            return {
                "has_alert": True,
                "alert_title": str(alert.get("title") or "官方災防告警"),
                "alert_url": str(alert.get("source_url") or "https://www.ncdr.nat.gov.tw"),
            }
    except Exception:
        pass
    return {"has_alert": False, "alert_title": "", "alert_url": ""}


def build_local_fallback(user_id: str, message: str) -> Dict[str, Any]:
    action_type = infer_action_type(message)
    if action_type == "NONE":
        return empty_chat_response("收到。這則訊息我判斷是一般對話，目前不會更動行事曆。")

    title = infer_event_title(message, action_type)
    alert = lookup_alert(message)

    if action_type == "DELETE_EVENT":
        event_id = find_event_id_to_delete(user_id, title)
        reply = f"收到，已偵測到刪除行程指令：{title or '未指定標題'}。"
        return {
            **empty_chat_response(reply),
            **alert,
            "action_type": "DELETE_EVENT",
            "event_title": title,
            "event_id_to_delete": event_id,
        }

    event_time = infer_event_time(message)
    reply = f"收到，已偵測到新增行程【{title}】。"
    if alert["has_alert"]:
        reply += f" 同時偵測到官方告警：{alert['alert_title']}，請注意安全。"
    return {
        **empty_chat_response(reply),
        **alert,
        "action_type": "ADD_EVENT",
        "event_title": title,
        "event_start": event_time["event_start"],
        "event_end": event_time["event_end"],
        "event_id_to_delete": "",
    }


def normalize_chat_response(raw: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
    action_type = str(raw.get("action_type") or fallback.get("action_type") or "NONE").upper()
    if action_type not in CHAT_ACTIONS:
        action_type = fallback.get("action_type") or "NONE"

    response = {
        "reply": str(raw.get("reply") or fallback.get("reply") or ""),
        "has_alert": bool(raw.get("has_alert") or fallback.get("has_alert") or False),
        "alert_title": str(raw.get("alert_title") or fallback.get("alert_title") or ""),
        "alert_url": str(raw.get("alert_url") or fallback.get("alert_url") or ""),
        "action_type": action_type,
        "event_title": str(raw.get("event_title") or fallback.get("event_title") or ""),
        "event_start": str(raw.get("event_start") or fallback.get("event_start") or ""),
        "event_end": str(raw.get("event_end") or fallback.get("event_end") or ""),
        "event_id_to_delete": str(raw.get("event_id_to_delete") or fallback.get("event_id_to_delete") or ""),
    }

    for key in ["event_start", "event_end"]:
        if response[key]:
            parsed = parse_datetime(response[key])
            if parsed:
                response[key] = taipei_iso(parsed)
            else:
                response[key] = fallback.get(key) or ""

    if action_type == "ADD_EVENT" and (not response["event_title"] or not response["event_start"] or not response["event_end"]):
        response.update({
            "event_title": fallback.get("event_title", ""),
            "event_start": fallback.get("event_start", ""),
            "event_end": fallback.get("event_end", ""),
        })
    if action_type != "DELETE_EVENT":
        response["event_id_to_delete"] = ""
    return response


async def parse_chat_with_gemini(user_id: str, message: str, fallback: Dict[str, Any]) -> Dict[str, Any]:
    now = taipei_now()
    memory = get_user_memory(user_id)
    memory_markdown = memory.get("memory_markdown") or ""
    prompt = (
        "你是 FastAPI 後端的行事曆與災防助理。請只回傳 JSON object，不要 markdown。\n"
        "任務：解析使用者是否要新增行程、刪除行程，或只是一般聊天。\n"
        "action_type 只能是 ADD_EVENT、DELETE_EVENT、NONE。\n"
        "若 ADD_EVENT，請填 event_title、event_start、event_end。時間必須是 Asia/Taipei 的 ISO8601，例如 2026-07-25T09:00:00+08:00。\n"
        "若 DELETE_EVENT，請填 event_title；event_id_to_delete 若不知道請留空字串。\n"
        "若 NONE，行程欄位留空。\n"
        "has_alert/alert_title/alert_url 若無法確認，請沿用 fallback 或 false/空字串。\n"
        "必須包含欄位：reply, has_alert, alert_title, alert_url, action_type, event_title, event_start, event_end, event_id_to_delete。\n"
        f"現在時間 Asia/Taipei: {now.isoformat()}\n"
        f"user_id: {user_id}\n"
        f"user_memory_markdown: {memory_markdown}\n"
        f"message: {message}\n"
        f"fallback_json: {fallback}"
    )
    parsed = await call_gemini_json_cached(
        prompt,
        fallback,
        "chat_command_v2",
        user_id or "anonymous",
        {"message": message, "now": now.isoformat(), "fallback": fallback},
    )
    return normalize_chat_response(parsed, fallback)


async def build_chat_command(user_id: str, message: str) -> Dict[str, Any]:
    normalized = normalize_text(message)
    fallback = build_local_fallback(user_id, normalized)
    response = await parse_chat_with_gemini(user_id, normalized, fallback)
    persist_chat_turn(user_id, normalized, response)
    return response
