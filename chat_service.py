import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from config import supabase
from data import GAME_QUESTIONS, TAIWAN_LOCATIONS
from disaster_service import get_active_disaster_alerts
from event_service import create_memory_event, enrich_event_payload_with_risk, normalize_event, persist_event_risk_fields
from gemini_service import call_gemini_json_cached
from utils import parse_datetime, safe_response, taipei_now
from weather_service import build_weather_snapshot, build_weather_suggestion, resolve_event_location_parts


CHAT_ACTIONS = {
    "ADD_EVENT",
    "CREATE_EVENT",
    "UPDATE_EVENT",
    "DELETE_EVENT",
    "EVENT_SYNCED",
    "CLARIFY",
    "WEATHER_QUERY",
    "DISASTER_GUIDE",
    "GAME_START",
    "NONE",
}
XIAOLAN_PERSONA = (
    "你是小藍，智行天氣與防災專用助理。回答要簡短、自然、主動提醒風險；"
    "不得編造天氣、警報、交通或資料庫結果，缺資料時要追問。"
)
TAIPEI_TZ = timezone(timedelta(hours=8))
MEMORY_MAX_CHARS = 4000
CHAT_LOGS_TABLE = "chat_logs"
LOCAL_PENDING_EVENTS: Dict[str, Dict[str, Any]] = {}
LOCAL_CHAT_HISTORY: Dict[str, List[Dict[str, Any]]] = {}
TIME_HINTS = ["今天", "明天", "後天", "下週", "下周", "下星期", "週末", "周末", "星期", "禮拜", "上午", "早上", "下午", "晚上", "中午", "點"]
GO_HINTS = ["要去", "我要去", "會去", "去", "前往", "到"]
ACTIVITY_HINTS = ["跑步", "游泳", "遊泳", "打球", "爬山", "露營", "開會", "上課", "買菜", "看診", "旅遊", "出遊", "考試", "聚餐", "通勤"]
WEATHER_QUERY_HINTS = ["會下雨", "天氣", "氣溫", "降雨", "熱不熱", "冷不冷", "適合去", "適合騎車", "適合出門"]
GAME_HINTS = ["小遊戲", "遊戲", "測驗", "quiz", "答題"]
UPDATE_EVENT_HINTS = ["改成", "改到", "改為", "改一下", "更新行程", "修改行程", "地點改", "時間改"]
DISTRICT_SUFFIXES = ("區", "鄉", "鎮", "市")
CHINESE_HOUR_MAP = {
    "一": 1,
    "二": 2,
    "兩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
}


def empty_chat_response(reply: str = "收到，我可以協助你查天氣、整理災防提醒，或解析新增/刪除行程。") -> Dict[str, Any]:
    return {
        "status": "success",
        "reply": reply,
        "has_alert": False,
        "alert_title": "",
        "alert_url": "",
        "action_type": "NONE",
        "missing_slots": [],
        "clarify_slot": "",
        "event_created": {},
        "event_updated": {},
        "weather_summary": {},
        "guideline": {},
        "game": {},
        "assistant_alerts": [],
        "event_title": "",
        "event_start": "",
        "event_end": "",
        "event_id": "",
        "event_city": "",
        "event_district": "",
        "event_location": "",
        "event_id_to_delete": "",
    }


def is_valid_uuid(value: str) -> bool:
    if not value:
        return False
    try:
        uuid.UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False


def db_user_id(user_id: str) -> Optional[str]:
    return user_id if is_valid_uuid(user_id) else None


def friendly_db_unavailable_reply(title: str) -> str:
    return f"小藍已理解要建立「{title}」，但目前資料庫暫時無法寫入。請稍後再試。"


def persist_local_chat_turn(user_id: str, message: str, response: Dict[str, Any], now: str):
    if not user_id:
        return
    rows = LOCAL_CHAT_HISTORY.setdefault(user_id, [])
    rows.append({
        "id": f"local-user-{len(rows) + 1}",
        "user_id": user_id,
        "role": "user",
        "content": message,
        "created_at": now,
    })
    rows.append({
        "id": f"local-assistant-{len(rows) + 1}",
        "user_id": user_id,
        "role": "assistant",
        "content": response.get("reply") or "",
        "response_payload": response,
        "action_type": response.get("action_type") or "NONE",
        "created_at": now,
    })
    del rows[:-80]

    if response.get("action_type") == "CLARIFY" and response.get("pending_event"):
        LOCAL_PENDING_EVENTS[user_id] = response["pending_event"]
    else:
        LOCAL_PENDING_EVENTS.pop(user_id, None)


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
    persist_local_chat_turn(user_id, message, response, now)
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
            "pending_event": response.get("pending_event") if response.get("action_type") == "CLARIFY" else None,
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
        rows = []
        fetch_limit = min(max(limit * 3, limit), 100)
        try:
            res = (
                supabase.table(CHAT_LOGS_TABLE)
                .select("*")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(fetch_limit)
                .execute()
            )
            rows = res.data or []
        except Exception:
            rows = []
        if not rows:
            try:
                legacy = (
                    supabase.table(CHAT_LOGS_TABLE)
                    .select("*")
                    .ilike("user_input", f"[{user_id}]%")
                    .limit(fetch_limit)
                    .execute()
                )
                rows = legacy.data or []
            except Exception:
                rows = []
        normalized_rows: List[Dict[str, Any]] = []
        for row in reversed(rows):
            normalized_rows.extend(expand_chat_history_row(row, user_id))
        local_rows = [normalize_chat_history_row(row, user_id) for row in LOCAL_CHAT_HISTORY.get(user_id, [])]
        if local_rows:
            seen = {(item.get("sender"), item.get("message"), item.get("created_at")) for item in normalized_rows}
            for item in local_rows:
                key = (item.get("sender"), item.get("message"), item.get("created_at"))
                if key not in seen:
                    normalized_rows.append(item)
        return safe_response("success", dedupe_chat_history(normalized_rows)[-limit:], "chat history loaded", CHAT_LOGS_TABLE)
    except Exception as e:
        return safe_response("error", [], str(e), CHAT_LOGS_TABLE, [{"service": "supabase", "message": str(e)}])


def expand_chat_history_row(row: Dict[str, Any], requested_user_id: str = "") -> List[Dict[str, Any]]:
    role = row.get("role") or ""
    raw_user_input = str(row.get("user_input") or "")
    ai_response = str(row.get("ai_response") or "")
    if not role and raw_user_input and ai_response:
        user_message = raw_user_input
        legacy_match = re.match(r"^\[([^\]]+)\]\s*(.*)$", raw_user_input)
        if legacy_match:
            user_message = legacy_match.group(2)
        user_row = {**row, "role": "user", "content": user_message, "ai_response": ""}
        assistant_row = {**row, "role": "assistant", "content": ai_response, "user_input": "", "ai_response": ai_response}
        return [
            normalize_chat_history_row(user_row, requested_user_id),
            normalize_chat_history_row(assistant_row, requested_user_id),
        ]
    return [normalize_chat_history_row(row, requested_user_id)]


def normalize_chat_history_row(row: Dict[str, Any], requested_user_id: str = "") -> Dict[str, Any]:
    role = row.get("role") or ""
    raw_user_input = str(row.get("user_input") or "")
    legacy_user_id = ""
    legacy_match = re.match(r"^\[([^\]]+)\]\s*(.*)$", raw_user_input)
    if legacy_match:
        legacy_user_id = legacy_match.group(1)
        raw_user_input = legacy_match.group(2)

    is_ai = role == "assistant" or bool(row.get("ai_response") and not row.get("content"))
    message = row.get("content") or (row.get("ai_response") if is_ai else raw_user_input) or ""
    sender = "assistant" if is_ai else "user"
    normalized_user_id = row.get("user_id") or requested_user_id or legacy_user_id

    return {
        "id": str(row.get("id") or ""),
        "user_id": str(normalized_user_id or ""),
        "sender": sender,
        "message": str(message or ""),
        "is_ai": is_ai,
        "created_at": row.get("created_at"),
        "action_type": row.get("action_type") or "",
        "response_payload": row.get("response_payload") or {},
    }


def dedupe_chat_history(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped = []
    seen: Dict[Any, int] = {}
    for row in rows:
        message = normalize_text(row.get("message") or "")
        sender = row.get("sender") or ""
        if not message or sender not in {"user", "assistant"}:
            continue
        response_payload = row.get("response_payload") or {}
        action_type = row.get("action_type") or response_payload.get("action_type") or ""
        if sender == "assistant":
            key = (row.get("user_id") or "", sender, message)
        else:
            key = (row.get("user_id") or "", sender, message, row.get("created_at") or "")
        if key in seen:
            existing_index = seen[key]
            existing = deduped[existing_index]
            existing_payload = existing.get("response_payload") or {}
            existing_action = existing.get("action_type") or existing_payload.get("action_type") or ""
            if sender == "assistant" and not existing_action and action_type:
                deduped[existing_index] = {**row, "message": message}
            continue
        seen[key] = len(deduped)
        deduped.append({**row, "message": message})
    return deduped


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


def ensure_taipei_iso(value: Any) -> str:
    parsed = parse_datetime(str(value)) if value else None
    return taipei_iso(parsed) if parsed else str(value or "")


def next_weekday(now: datetime, weekday: int) -> datetime:
    days = (weekday - now.weekday()) % 7
    if days == 0:
        days = 7
    return now + timedelta(days=days)


def parse_weekday_hint(message: str) -> Optional[int]:
    match = re.search(r"(?:星期|禮拜|週|周)([一二三四五六日天])", message)
    if not match:
        return None
    return {
        "一": 0,
        "二": 1,
        "三": 2,
        "四": 3,
        "五": 4,
        "六": 5,
        "日": 6,
        "天": 6,
    }.get(match.group(1))


def infer_action_type(message: str) -> str:
    delete_keywords = ["刪除", "取消", "移除", "不要去了", "刪掉行程"]
    add_keywords = ["新增", "加入", "排入", "安排", "建立", "幫我記", "幫我排", "行程"]
    if any(keyword in message for keyword in delete_keywords):
        return "DELETE_EVENT"
    if any(keyword in message for keyword in add_keywords):
        return "ADD_EVENT"
    if has_destination_intent(message) and (has_location_hint(message) or has_activity_hint(message)):
        return "ADD_EVENT"
    if has_time_hint(message) and has_location_hint(message) and has_trip_or_activity_hint(message):
        return "ADD_EVENT"
    if has_time_hint(message) and has_trip_or_activity_hint(message):
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
    normalized = message.replace("台", "臺")
    for city, districts in TAIWAN_LOCATIONS.items():
        if city in normalized:
            matched_district = ""
            for district in districts:
                short_district = district[:-1] if district.endswith(DISTRICT_SUFFIXES) else district
                if district in normalized or (len(short_district) >= 2 and short_district in normalized):
                    matched_district = district
                    break
            return {"city": city, "district": matched_district}
    for keyword, parts in location_map.items():
        if keyword in message:
            city = parts.get("city") or ""
            district = parts.get("district") or ""
            for candidate in TAIWAN_LOCATIONS.get(city, []):
                short_candidate = candidate[:-1] if candidate.endswith(DISTRICT_SUFFIXES) else candidate
                if candidate in normalized or (len(short_candidate) >= 2 and short_candidate in normalized):
                    district = candidate
                    break
            return {"city": city, "district": district}
    return {"city": "", "district": ""}


def has_time_hint(message: str) -> bool:
    if any(hint in message for hint in TIME_HINTS):
        return True
    return bool(re.search(r"(\d{1,2}|十[一二]?|[一二兩三四五六七八九])\s*[點:：]\s*\d{0,2}", message))


def has_location_hint(message: str) -> bool:
    location = infer_location(message)
    return bool(location.get("city") or location.get("district"))


def has_trip_or_activity_hint(message: str) -> bool:
    return any(hint in message for hint in GO_HINTS + ACTIVITY_HINTS)


def has_destination_intent(message: str) -> bool:
    return any(hint in message for hint in GO_HINTS)


def has_activity_hint(message: str) -> bool:
    return any(hint in message for hint in ACTIVITY_HINTS)


def has_weather_query_hint(message: str) -> bool:
    return any(hint in message for hint in WEATHER_QUERY_HINTS)


def is_weather_question(message: str) -> bool:
    return has_weather_query_hint(message) and any(token in message for token in ["嗎", "?", "？", "會", "適合", "如何", "怎樣"])


def has_game_hint(message: str) -> bool:
    return any(hint in message.lower() for hint in GAME_HINTS)


def has_update_event_hint(message: str) -> bool:
    return any(hint in message for hint in UPDATE_EVENT_HINTS)


def infer_disaster_type(message: str) -> str:
    if "地震" in message:
        return "earthquake"
    if any(keyword in message for keyword in ["豪雨", "大雨"]):
        return "heavy_rain"
    if any(keyword in message for keyword in ["淹水", "洪水"]):
        return "flood"
    if "颱風" in message or "台風" in message:
        return "typhoon"
    if "火災" in message:
        return "fire"
    if "濃霧" in message:
        return "fog"
    if "強風" in message:
        return "strong_wind"
    return ""


def infer_game_type(message: str) -> str:
    disaster_type = infer_disaster_type(message)
    if disaster_type in GAME_QUESTIONS:
        return disaster_type
    return "flood"


def build_game_start_response(message: str) -> Dict[str, Any]:
    game_type = infer_game_type(message)
    questions = GAME_QUESTIONS.get(game_type) or GAME_QUESTIONS.get("flood", [])
    return {
        **empty_chat_response(f"小藍已準備好 {game_type} 防災測驗，共 {len(questions)} 題。"),
        "action_type": "GAME_START",
        "game": {
            "game_type": game_type,
            "questions": [
                {
                    "id": question["id"],
                    "question": question["question"],
                    "choices": question["choices"],
                }
                for question in questions
            ],
        },
    }


def build_disaster_guide_response(message: str) -> Optional[Dict[str, Any]]:
    if not any(keyword in message for keyword in ["地震", "颱風", "豪雨", "大雨", "火災", "淹水", "停電", "土石流"]):
        return None
    if not any(keyword in message for keyword in ["怎麼辦", "怎麼做", "應該", "處理", "準備", "注意"]):
        return None

    disaster_type = infer_disaster_type(message) or "general"
    user_activity = "driving" if any(keyword in message for keyword in ["開車", "騎車", "通勤", "路上"]) else "general"
    if "地震" in message:
        reply = "地震時先趴下、掩護、穩住，遠離玻璃與櫃子；搖晃停止後再關火源、穿鞋、帶手機與避難包移動。"
    elif "火災" in message:
        reply = "火災時先低姿勢避煙，摸門把確認熱度，能逃就往安全出口走；不能逃就關門塞縫、到窗邊求救。"
    elif any(keyword in message for keyword in ["豪雨", "大雨", "淹水"]):
        reply = "豪雨或淹水時避免地下道、河堤與低窪路段，不涉水通行；先確認氣象警特報並準備雨具、行動電源。"
    elif "颱風" in message:
        reply = "颱風前先固定門窗與陽台物品，準備飲水、食物、手電筒與行動電源；期間避免外出與靠近海邊河岸。"
    else:
        reply = "先確認官方警報，備妥手機、行動電源、飲水、藥品與證件；若所在地有風險，提早往安全地點移動。"
    return {
        **empty_chat_response(reply),
        "action_type": "DISASTER_GUIDE",
        "guideline": {
            "disaster_type": disaster_type,
            "user_activity": user_activity,
            "instruction": reply,
            "priority": "high" if disaster_type in ["flood", "typhoon", "fire"] else "medium",
        },
    }


async def build_weather_query_response(message: str, current_location: Optional[str] = None) -> Dict[str, Any]:
    location = infer_location(message)
    if not (location.get("city") or location.get("district")) and current_location:
        location = infer_location(current_location)
    city = location.get("city") or "臺南市"
    district = location.get("district") or "東區"
    event_time = infer_event_time(message)["event_start"] if has_time_hint(message) else ""
    parsed_time = parse_datetime(event_time)

    try:
        snapshot = await build_weather_snapshot(city, district, parsed_time)
        weather = snapshot.get("weather") or {}
        suggestion = build_weather_suggestion(city, district, "天氣查詢", weather, snapshot)
        weather_summary = {
            "city": city,
            "district": district,
            "temp": weather.get("temp"),
            "pop": weather.get("pop"),
            "wx": weather.get("description") or weather.get("wx") or "未知",
            "risk_level": snapshot.get("risk_level") or "low",
            "risk_tags": snapshot.get("risk_tags") or [],
            "has_weather_risk": bool(snapshot.get("has_weather_risk")),
            "target_time": event_time,
        }
        reply = f"小藍查到{city}{district}天氣「{weather_summary['wx']}」，降雨機率 {weather_summary['pop']}%。{suggestion}"
    except Exception as e:
        weather_summary = {"city": city, "district": district, "error": str(e), "target_time": event_time}
        reply = f"小藍目前查不到{city}{district}的即時天氣資料，請稍後再試或確認地點。"

    return {
        **empty_chat_response(reply),
        "action_type": "WEATHER_QUERY",
        "weather_summary": weather_summary,
    }


def infer_event_time(message: str, now: Optional[datetime] = None) -> Dict[str, str]:
    now = now or taipei_now()
    base = now.astimezone(TAIPEI_TZ)

    iso_match = re.search(r"20\d{2}-\d{1,2}-\d{1,2}T\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?", message)
    if iso_match:
        start = parse_datetime(iso_match.group(0))
        end = start + timedelta(hours=2) if start else None
        return {"event_start": taipei_iso(start), "event_end": taipei_iso(end)}

    weekday = parse_weekday_hint(message)
    if weekday is not None:
        start = next_weekday(base, weekday).replace(hour=9, minute=0, second=0, microsecond=0)
    elif "週末" in message or "周末" in message:
        saturday = next_weekday(base, 5).replace(hour=9, minute=0, second=0, microsecond=0)
        sunday = saturday + timedelta(days=1, hours=8)
        return {"event_start": taipei_iso(saturday), "event_end": taipei_iso(sunday)}
    elif "下週" in message or "下周" in message or "下星期" in message:
        start = (base + timedelta(days=7)).replace(hour=9, minute=0, second=0, microsecond=0)
    elif "後天" in message:
        start = (base + timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
    elif "明天" in message:
        start = (base + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    elif "今天" in message:
        start = base.replace(hour=max(base.hour + 1, 9), minute=0, second=0, microsecond=0)
    else:
        start = (base + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

    time_match = re.search(r"(上午|早上|下午|晚上|中午)?\s*(\d{1,2}|十[一二]?|[一二兩三四五六七八九])\s*[點:：]\s*(\d{1,2})?", message)
    if time_match:
        period = time_match.group(1) or ""
        hour_text = time_match.group(2)
        hour = int(hour_text) if hour_text.isdigit() else CHINESE_HOUR_MAP.get(hour_text, 9)
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
    for token in ["幫我", "請", "新增", "加入", "排入", "安排", "建立", "行程", "一個", "一筆", "一項", "我", "想要", "想"]:
        text = text.replace(token, " ")
    text = re.sub(r"(今天|明天|後天|下週|下周|下星期|這週末|本週末|週末|周末|星期[一二三四五六日天]|禮拜[一二三四五六日天]|週[一二三四五六日天]|周[一二三四五六日天]|上午|早上|下午|晚上|中午|(\d{1,2}|十[一二]?|[一二兩三四五六七八九])\s*[點:：]\s*\d{0,2})", " ", text)
    location = infer_location(message)
    normalized_city = (location.get("city") or "").replace("臺", "台")
    district = location.get("district", "")
    short_district = district[:-1] if district.endswith(DISTRICT_SUFFIXES) else district
    for token in [
        location.get("city", ""),
        normalized_city,
        district,
        short_district,
        "台南市",
        "臺南市",
        "台南",
        "臺南",
        "的公園",
        "公園",
    ]:
        if token:
            text = text.replace(token, " ")
    text = re.sub(r"(要去|我要去|會去|前往|到|去)", " ", text)
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


def find_event_to_update(user_id: str, title: str = "") -> Dict[str, Any]:
    try:
        query = supabase.table("events").select("*").order("start_time", desc=True).limit(5)
        if user_id:
            query = query.eq("user_id", user_id)
        res = query.execute()
        rows = res.data or []
        if not rows:
            return {}
        if title:
            for row in rows:
                if title in str(row.get("title") or "") or str(row.get("title") or "") in title:
                    return row
        return rows[0]
    except Exception:
        return {}


async def update_event_from_chat(user_id: str, message: str, current_location: Optional[str] = None) -> Dict[str, Any]:
    title = infer_event_title(message, "ADD_EVENT")
    existing = find_event_to_update(user_id, title)
    if not existing:
        return {
            **empty_chat_response("小藍找不到可修改的行程，請提供行程名稱或先建立行程。"),
            "action_type": "CLARIFY",
            "missing_slots": ["event"],
            "clarify_slot": "event",
        }

    update_payload: Dict[str, Any] = {}
    if has_time_hint(message):
        event_time = infer_event_time(message)
        update_payload["start_time"] = event_time["event_start"]
        update_payload["end_time"] = event_time["event_end"]

    location = infer_location(message)
    if not (location.get("city") or location.get("district")) and current_location and "地點" in message:
        location = infer_location(current_location)
    if location.get("city") or location.get("district"):
        update_payload["city"] = location.get("city") or existing.get("city")
        update_payload["district"] = location.get("district") or existing.get("district")
        update_payload["location"] = "".join(part for part in [update_payload.get("city"), update_payload.get("district")] if part)

    if not update_payload:
        return {
            **empty_chat_response("要把這個行程的時間、地點或標題改成什麼？"),
            "action_type": "CLARIFY",
            "missing_slots": ["update_fields"],
            "clarify_slot": "update_fields",
        }

    if update_payload.get("city") or update_payload.get("start_time"):
        try:
            event_for_weather = {**existing, **update_payload}
            parts = resolve_event_location_parts(event_for_weather)
            event_time = parse_datetime(event_for_weather.get("start_time"))
            snapshot = await build_weather_snapshot(parts["city"], parts["district"], event_time)
            suggestion = build_weather_suggestion(
                parts["city"],
                parts["district"],
                event_for_weather.get("title") or "行程",
                snapshot.get("weather") or {},
                snapshot,
            )
            update_payload.update({
                "city": parts["city"],
                "district": parts["district"],
                "weather_snapshot": snapshot,
                "weather_checked_at": snapshot.get("captured_at"),
                "risk_level": snapshot.get("risk_level"),
                "risk_tags": snapshot.get("risk_tags"),
                "has_weather_risk": snapshot.get("has_weather_risk"),
                "recommended_action": suggestion,
                "ai_suggestion": suggestion,
            })
        except Exception:
            pass

    event_id = existing.get("id")
    try:
        res = supabase.table("events").update(update_payload).eq("id", event_id).execute()
    except Exception:
        legacy_payload = {
            key: value
            for key, value in update_payload.items()
            if key in {"start_time", "end_time", "location", "has_weather_risk", "ai_suggestion"}
        }
        res = supabase.table("events").update(legacy_payload).eq("id", event_id).execute()

    updated = normalize_event(res.data[0]) if res.data else normalize_event({**existing, **update_payload})
    event_updated = {
        "id": str(updated.get("id") or ""),
        "title": updated.get("title") or "",
        "start_time": ensure_taipei_iso(updated.get("start_time")),
        "end_time": ensure_taipei_iso(updated.get("end_time")),
        "city": updated.get("city") or "",
        "district": updated.get("district") or "",
        "location": updated.get("location") or "",
        "has_weather_risk": bool(updated.get("has_weather_risk")),
        "risk_level": updated.get("risk_level") or "low",
    }
    return {
        **empty_chat_response(f"小藍已更新行程：{event_updated['title']}。"),
        "action_type": "UPDATE_EVENT",
        "event_updated": event_updated,
        "event_id": event_updated["id"],
        "event_title": event_updated["title"],
        "event_start": event_updated["start_time"],
        "event_end": event_updated["end_time"],
        "event_city": event_updated["city"],
        "event_district": event_updated["district"],
        "event_location": event_updated["location"],
    }


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


def get_pending_event(user_id: str) -> Dict[str, Any]:
    if not user_id:
        return {}
    if user_id in LOCAL_PENDING_EVENTS:
        return LOCAL_PENDING_EVENTS[user_id]
    memory = get_user_memory(user_id)
    summary = memory.get("summary_json") or {}
    pending = summary.get("pending_event") or {}
    if isinstance(pending, dict) and pending:
        return pending

    try:
        res = (
            supabase.table(CHAT_LOGS_TABLE)
            .select("response_payload,action_type,created_at")
            .eq("user_id", user_id)
            .eq("role", "assistant")
            .order("created_at", desc=True)
            .limit(5)
            .execute()
        )
        rows = res.data or []
        if rows:
            latest = rows[0]
            if latest.get("action_type") != "CLARIFY":
                return {}
            payload = latest.get("response_payload") or {}
            pending = payload.get("pending_event") or {}
            return pending if isinstance(pending, dict) else {}
    except Exception:
        pass
    return {}


def infer_event_slots(message: str, pending: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    pending = pending or {}
    action_type = "ADD_EVENT"
    location = infer_location(message)
    event_start = pending.get("event_start") or ""
    event_end = pending.get("event_end") or ""
    if has_time_hint(message):
        event_time = infer_event_time(message)
        event_start = event_time["event_start"]
        event_end = event_time["event_end"]

    event_city = pending.get("event_city") or location.get("city") or ""
    event_district = pending.get("event_district") or location.get("district") or ""
    event_location = pending.get("event_location") or "".join(part for part in [event_city, event_district] if part)
    if not event_location and location.get("city"):
        event_location = "".join(part for part in [location.get("city"), location.get("district")] if part)

    event_title = pending.get("event_title") or ""
    inferred_title = infer_event_title(message, action_type)
    if not event_title and inferred_title and inferred_title != "新行程":
        event_title = inferred_title

    return {
        "event_title": event_title,
        "event_start": event_start,
        "event_end": event_end,
        "event_city": event_city,
        "event_district": event_district,
        "event_location": event_location,
    }


def missing_event_slots(slots: Dict[str, Any]) -> List[str]:
    missing = []
    if not slots.get("event_title"):
        missing.append("title")
    if not slots.get("event_start") or not slots.get("event_end"):
        missing.append("time")
    if not (slots.get("event_city") or slots.get("event_location")):
        missing.append("location")
    return missing


def build_clarify_response(slots: Dict[str, Any], missing: List[str]) -> Dict[str, Any]:
    if "location" in missing and "time" in missing:
        reply = "要去哪個縣市或地點？大概幾點到幾點？"
        clarify_slot = "location,time"
    elif "location" in missing:
        reply = "這個行程要去哪個縣市、行政區或地點？"
        clarify_slot = "location"
    elif "time" in missing:
        reply = "這個行程大概什麼時候開始、什麼時候結束？"
        clarify_slot = "time"
    else:
        reply = "這個行程的事由或標題要填什麼？"
        clarify_slot = "title"

    return {
        **empty_chat_response(reply),
        "action_type": "CLARIFY",
        "missing_slots": missing,
        "clarify_slot": clarify_slot,
        "event_title": slots.get("event_title") or "",
        "event_start": slots.get("event_start") or "",
        "event_end": slots.get("event_end") or "",
        "event_city": slots.get("event_city") or "",
        "event_district": slots.get("event_district") or "",
        "event_location": slots.get("event_location") or "",
        "pending_event": slots,
    }


async def create_event_from_chat(user_id: str, slots: Dict[str, Any]) -> Dict[str, Any]:
    event_payload: Dict[str, Any] = {
        "user_id": db_user_id(user_id),
        "title": slots.get("event_title") or "新行程",
        "start_time": slots.get("event_start"),
        "end_time": slots.get("event_end"),
        "city": slots.get("event_city") or None,
        "district": slots.get("event_district") or None,
        "location": slots.get("event_location") or None,
        "external_source": "chat",
    }
    location_parts = resolve_event_location_parts(event_payload)
    event_payload["city"] = event_payload.get("city") or location_parts["city"]
    event_payload["district"] = event_payload.get("district") or location_parts["district"]
    event_payload["location"] = event_payload.get("location") or f"{event_payload['city']}{event_payload['district']}"

    weather_summary: Dict[str, Any] = {}
    weather_text = "目前天氣資料暫時無法取得，已先建立行程。"
    try:
        event_payload = await enrich_event_payload_with_risk(event_payload, log_prefix="聊天建立行程")
        snapshot = event_payload.get("weather_snapshot") or {}
        weather = snapshot.get("weather") or {}
        weather_summary = {
            "temp": weather.get("temp"),
            "pop": weather.get("pop"),
            "wx": weather.get("description") or weather.get("wx") or "未知",
            "risk_level": event_payload.get("risk_level") or "low",
            "risk_tags": event_payload.get("risk_tags") or [],
            "has_weather_risk": bool(event_payload.get("has_weather_risk")),
        }
        weather_text = f"那天{event_payload['city']}{event_payload['district']}天氣「{weather.get('description', '未知')}」。{event_payload.get('ai_suggestion') or event_payload.get('recommended_action') or ''}"
    except Exception as e:
        event_payload["has_weather_risk"] = False
        event_payload["risk_level"] = "low"
        event_payload["risk_tags"] = []
        event_payload["weather_alert_status"] = "weather_update_failed"
        event_payload["ai_suggestion"] = weather_text
        event_payload["recommended_action"] = weather_text
        print(f"聊天建立行程天氣查詢失敗: {e}")

    try:
        duplicate_query = supabase.table("events").select("*").eq("title", event_payload["title"]).eq("start_time", event_payload["start_time"]).limit(1)
        if event_payload.get("user_id"):
            duplicate_query = duplicate_query.eq("user_id", event_payload["user_id"])
        duplicate = duplicate_query.execute()
        if duplicate.data:
            res = duplicate
        else:
            try:
                res = supabase.table("events").insert(event_payload).execute()
            except Exception:
                event_payload["location_name"] = event_payload.get("location")
                compatible_keys = {
                    "user_id", "city", "district", "title", "start_time", "end_time",
                    "location_name", "has_weather_risk", "ai_suggestion",
                    "risk_level", "risk_tags", "recommended_action", "weather_snapshot",
                    "weather_checked_at", "weather_alert_status", "external_source",
                }
                compatible_payload = {key: value for key, value in event_payload.items() if key in compatible_keys}
                try:
                    res = supabase.table("events").insert(compatible_payload).execute()
                except Exception:
                    legacy_keys = {
                        "user_id", "title", "start_time", "end_time",
                        "location_name", "has_weather_risk", "ai_suggestion", "external_source",
                    }
                    legacy_payload = {key: value for key, value in event_payload.items() if key in legacy_keys}
                    res = supabase.table("events").insert(legacy_payload).execute()
    except Exception:
        memory_payload = {**event_payload, "user_id": user_id or None}
        created = normalize_event(create_memory_event(memory_payload))
        created_city = created.get("city") or event_payload.get("city") or ""
        created_district = created.get("district") or event_payload.get("district") or ""
        created_location = created.get("location") or event_payload.get("location") or ""
        event_created = {
            "id": str(created.get("id") or ""),
            "title": created.get("title") or "",
            "start_time": ensure_taipei_iso(created.get("start_time")),
            "end_time": ensure_taipei_iso(created.get("end_time")),
            "city": created_city,
            "district": created_district,
            "location": created_location,
            "has_weather_risk": bool(created.get("has_weather_risk") or event_payload.get("has_weather_risk") or False),
            "risk_level": created.get("risk_level") or event_payload.get("risk_level") or "low",
        }
        return {
            **empty_chat_response(friendly_db_unavailable_reply(event_payload["title"])),
            "status": "partial_success",
            "action_type": "CREATE_EVENT",
            "event_created": event_created,
            "event_id": event_created["id"],
            "event_title": event_created["title"],
            "event_start": event_created["start_time"],
            "event_end": event_created["end_time"],
            "event_city": created_city,
            "event_district": created_district,
            "event_location": created_location,
            "weather_summary": weather_summary,
            "pending_event": None,
        }

    created_row = res.data[0] if res.data else event_payload
    persisted_risk = persist_event_risk_fields(created_row.get("id"), event_payload)
    created = normalize_event({**created_row, **event_payload, **persisted_risk})
    reply = f"已幫你加入行程到行事曆了：{created.get('title')}。{weather_text}"
    created_city = created.get("city") or event_payload.get("city") or ""
    created_district = created.get("district") or event_payload.get("district") or ""
    created_location = created.get("location") or created.get("location_name") or event_payload.get("location") or ""
    event_created = {
        "id": str(created.get("id") or ""),
        "title": created.get("title") or "",
        "start_time": ensure_taipei_iso(created.get("start_time")),
        "end_time": ensure_taipei_iso(created.get("end_time")),
        "city": created_city,
        "district": created_district,
        "location": created_location,
        "has_weather_risk": bool(created.get("has_weather_risk") or event_payload.get("has_weather_risk") or False),
        "risk_level": created.get("risk_level") or event_payload.get("risk_level") or "low",
    }
    return {
        **empty_chat_response(reply),
        "action_type": "CREATE_EVENT",
        "event_created": event_created,
        "weather_summary": weather_summary,
        "event_id": str(created.get("id") or ""),
        "event_title": created.get("title") or "",
        "event_start": event_created["start_time"],
        "event_end": event_created["end_time"],
        "event_city": created_city,
        "event_district": created_district,
        "event_location": created_location,
        "pending_event": None,
    }


def build_local_fallback(user_id: str, message: str, current_location: Optional[str] = None) -> Dict[str, Any]:
    disaster_qa = build_disaster_guide_response(message)
    if disaster_qa:
        return disaster_qa

    if has_game_hint(message):
        return build_game_start_response(message)

    pending = get_pending_event(user_id)
    if pending:
        if current_location and not (pending.get("event_city") or pending.get("event_location")) and not has_location_hint(message) and not has_destination_intent(message):
            current_parts = infer_location(current_location)
            if current_parts.get("city") or current_parts.get("district"):
                pending = {
                    **pending,
                    "event_city": current_parts.get("city") or "",
                    "event_district": current_parts.get("district") or "",
                    "event_location": "".join(
                        part for part in [current_parts.get("city"), current_parts.get("district")] if part
                    ),
                }
        slots = infer_event_slots(message, pending)
        missing = missing_event_slots(slots)
        if missing:
            return build_clarify_response(slots, missing)
        return {
            **empty_chat_response("行程資訊已補齊，準備建立行程。"),
            "action_type": "ADD_EVENT",
            **slots,
            "pending_event": slots,
        }

    action_type = infer_action_type(message)
    if action_type == "NONE" and current_location and has_time_hint(message) and has_trip_or_activity_hint(message) and not has_destination_intent(message):
        action_type = "ADD_EVENT"
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
    location_defaults: Dict[str, Any] = {}
    if current_location and not has_location_hint(message) and not has_destination_intent(message):
        current_parts = infer_location(current_location)
        if current_parts.get("city") or current_parts.get("district"):
            location_defaults = {
                "event_city": current_parts.get("city") or "",
                "event_district": current_parts.get("district") or "",
                "event_location": "".join(
                    part for part in [current_parts.get("city"), current_parts.get("district")] if part
                ),
            }
    slots = infer_event_slots(message, {
        "event_title": title,
        "event_start": event_time["event_start"] if has_time_hint(message) else "",
        "event_end": event_time["event_end"] if has_time_hint(message) else "",
        **location_defaults,
    })
    missing = missing_event_slots(slots)
    if missing:
        return build_clarify_response(slots, missing)

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
        "event_city": slots.get("event_city") or "",
        "event_district": slots.get("event_district") or "",
        "event_location": slots.get("event_location") or "",
        "event_id_to_delete": "",
    }


def normalize_chat_response(raw: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
    action_type = str(raw.get("action_type") or fallback.get("action_type") or "NONE").upper()
    if action_type not in CHAT_ACTIONS:
        action_type = fallback.get("action_type") or "NONE"
    if action_type == "CREATE_EVENT":
        action_type = "ADD_EVENT"
    if fallback.get("action_type") in ["ADD_EVENT", "DELETE_EVENT", "CLARIFY"] and action_type == "NONE":
        action_type = fallback.get("action_type") or "NONE"

    response = {
        "status": str(raw.get("status") or fallback.get("status") or "success"),
        "reply": str(raw.get("reply") or fallback.get("reply") or ""),
        "has_alert": bool(raw.get("has_alert") or fallback.get("has_alert") or False),
        "alert_title": str(raw.get("alert_title") or fallback.get("alert_title") or ""),
        "alert_url": str(raw.get("alert_url") or fallback.get("alert_url") or ""),
        "action_type": action_type,
        "missing_slots": raw.get("missing_slots") or fallback.get("missing_slots") or [],
        "clarify_slot": str(raw.get("clarify_slot") or fallback.get("clarify_slot") or ""),
        "event_created": raw.get("event_created") or fallback.get("event_created") or {},
        "event_updated": raw.get("event_updated") or fallback.get("event_updated") or {},
        "weather_summary": raw.get("weather_summary") or fallback.get("weather_summary") or {},
        "guideline": raw.get("guideline") or fallback.get("guideline") or {},
        "game": raw.get("game") or fallback.get("game") or {},
        "assistant_alerts": raw.get("assistant_alerts") or fallback.get("assistant_alerts") or [],
        "event_title": str(raw.get("event_title") or fallback.get("event_title") or ""),
        "event_start": str(raw.get("event_start") or fallback.get("event_start") or ""),
        "event_end": str(raw.get("event_end") or fallback.get("event_end") or ""),
        "event_id": str(raw.get("event_id") or fallback.get("event_id") or ""),
        "event_city": str(raw.get("event_city") or fallback.get("event_city") or ""),
        "event_district": str(raw.get("event_district") or fallback.get("event_district") or ""),
        "event_location": str(raw.get("event_location") or fallback.get("event_location") or ""),
        "event_id_to_delete": str(raw.get("event_id_to_delete") or fallback.get("event_id_to_delete") or ""),
        "pending_event": raw.get("pending_event") if "pending_event" in raw else fallback.get("pending_event"),
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
            "event_city": fallback.get("event_city", ""),
            "event_district": fallback.get("event_district", ""),
            "event_location": fallback.get("event_location", ""),
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
        "action_type 只能是 ADD_EVENT、UPDATE_EVENT、DELETE_EVENT、WEATHER_QUERY、DISASTER_GUIDE、GAME_START、CLARIFY、NONE。不要輸出 CREATE_EVENT 或 EVENT_SYNCED，CREATE_EVENT 由後端建立 DB 後產生。\n"
        "若 ADD_EVENT，請填 event_title、event_start、event_end、event_city、event_district、event_location。時間必須是 Asia/Taipei 的 ISO8601，例如 2026-07-25T09:00:00+08:00。\n"
        "若缺少新增行程必填 slot，請輸出 CLARIFY，並填 missing_slots 與 clarify_slot。\n"
        "若 DELETE_EVENT，請填 event_title；event_id_to_delete 若不知道請留空字串。\n"
        "若 NONE，行程欄位留空。\n"
        "has_alert/alert_title/alert_url 若無法確認，請沿用 fallback 或 false/空字串。\n"
        "必須包含欄位：status, reply, has_alert, alert_title, alert_url, action_type, missing_slots, clarify_slot, event_created, event_updated, weather_summary, guideline, game, assistant_alerts, event_title, event_start, event_end, event_id, event_city, event_district, event_location, event_id_to_delete。\n"
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


async def build_chat_command(user_id: str, message: str, current_location: Optional[str] = None) -> Dict[str, Any]:
    normalized = normalize_text(message)

    if has_game_hint(normalized):
        response = build_game_start_response(normalized)
        persist_chat_turn(user_id, normalized, response)
        return response

    disaster_guide = build_disaster_guide_response(normalized)
    if disaster_guide:
        persist_chat_turn(user_id, normalized, disaster_guide)
        return disaster_guide

    if has_update_event_hint(normalized):
        response = await update_event_from_chat(user_id, normalized, current_location)
        persist_chat_turn(user_id, normalized, response)
        return response

    if is_weather_question(normalized) or (has_weather_query_hint(normalized) and infer_action_type(normalized) == "NONE"):
        response = await build_weather_query_response(normalized, current_location)
        persist_chat_turn(user_id, normalized, response)
        return response

    fallback = build_local_fallback(user_id, normalized, current_location)
    response = await parse_chat_with_gemini(user_id, normalized, fallback)

    if response.get("action_type") == "WEATHER_QUERY":
        response = await build_weather_query_response(normalized, current_location)
    elif response.get("action_type") == "DISASTER_GUIDE":
        response = build_disaster_guide_response(normalized) or response
    elif response.get("action_type") == "GAME_START":
        response = build_game_start_response(normalized)
    elif response.get("action_type") == "UPDATE_EVENT":
        response = await update_event_from_chat(user_id, normalized, current_location)

    if response.get("action_type") == "ADD_EVENT":
        slots = {
            "event_title": response.get("event_title") or fallback.get("event_title") or "",
            "event_start": response.get("event_start") or fallback.get("event_start") or "",
            "event_end": response.get("event_end") or fallback.get("event_end") or "",
            "event_city": response.get("event_city") or fallback.get("event_city") or "",
            "event_district": response.get("event_district") or fallback.get("event_district") or "",
            "event_location": response.get("event_location") or fallback.get("event_location") or "",
        }
        missing = missing_event_slots(slots)
        if missing:
            response = build_clarify_response(slots, missing)
        else:
            response = await create_event_from_chat(user_id, slots)

    persist_chat_turn(user_id, normalized, response)
    return response
