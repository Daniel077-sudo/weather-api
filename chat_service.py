import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from config import supabase
from gemini_service import call_gemini_json_cached
from utils import parse_datetime, taipei_now


ADD_KEYWORDS = ["新增", "加入", "建立", "安排", "排入", "新增行程", "加行程"]
DELETE_KEYWORDS = ["刪除", "取消", "移除", "刪掉", "不要了"]
FILLER_WORDS = [
    "幫我",
    "請幫我",
    "麻煩",
    "一個",
    "行程",
    "到行事曆",
    "同步至行事曆",
    "新增",
    "加入",
    "建立",
    "安排",
    "排入",
    "刪除",
    "取消",
    "移除",
    "刪掉",
    "去",
]

CHINESE_NUMBERS = {
    "零": 0,
    "〇": 0,
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
}


def empty_chat_response(reply: str = "我收到訊息了，目前沒有偵測到行程控制指令。") -> Dict[str, Any]:
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


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def chinese_number_to_int(value: str) -> Optional[int]:
    value = (value or "").strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    if value in CHINESE_NUMBERS:
        return CHINESE_NUMBERS[value]
    if "十" in value:
        if value == "十":
            return 10
        left, _, right = value.partition("十")
        tens = CHINESE_NUMBERS.get(left, 1 if left == "" else 0)
        ones = CHINESE_NUMBERS.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


def infer_action_type(message: str) -> str:
    if any(keyword in message for keyword in DELETE_KEYWORDS) and "行程" in message:
        return "DELETE_EVENT"
    if any(keyword in message for keyword in ADD_KEYWORDS) and ("行程" in message or "會議" in message or "開會" in message):
        return "ADD_EVENT"
    return "NONE"


def parse_event_datetime(message: str, now: Optional[datetime] = None) -> Optional[datetime]:
    now = now or taipei_now()
    message = normalize_text(message)

    iso_match = re.search(r"20\d{2}-\d{1,2}-\d{1,2}T\d{1,2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:\d{2})?", message)
    if iso_match:
        return parse_datetime(iso_match.group(0))

    target_date = now.date()
    if "明天" in message:
        target_date = (now + timedelta(days=1)).date()
    elif "後天" in message:
        target_date = (now + timedelta(days=2)).date()
    else:
        date_match = re.search(r"(?:(20\d{2})[/-])?(\d{1,2})\s*月\s*(\d{1,2})\s*(?:日|號)?", message)
        if date_match:
            year = int(date_match.group(1) or now.year)
            month = int(date_match.group(2))
            day = int(date_match.group(3))
            target_date = datetime(year, month, day, tzinfo=now.tzinfo).date()

    hour = None
    minute = 0
    time_match = re.search(r"([0-2]?\d|[零〇一二兩三四五六七八九十]{1,3})\s*[點:：]\s*([0-5]\d)?", message)
    if time_match:
        hour = chinese_number_to_int(time_match.group(1))
        if time_match.group(2):
            minute = int(time_match.group(2))
    elif "早上" in message or "上午" in message:
        hour = 9
    elif "中午" in message:
        hour = 12
    elif "下午" in message or "晚上" in message:
        hour = 14

    if hour is None:
        if "明天" in message or "後天" in message or re.search(r"\d{1,2}\s*月\s*\d{1,2}", message):
            hour = 9
        else:
            return None

    if ("下午" in message or "晚上" in message) and hour < 12:
        hour += 12
    if "中午" in message and hour < 11:
        hour += 12

    return datetime(target_date.year, target_date.month, target_date.day, hour, minute, tzinfo=now.tzinfo)


def strip_time_phrases(message: str) -> str:
    patterns = [
        r"20\d{2}-\d{1,2}-\d{1,2}T\d{1,2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:\d{2})?",
        r"(?:(20\d{2})[/-])?\d{1,2}\s*月\s*\d{1,2}\s*(?:日|號)?",
        r"(今天|明天|後天)",
        r"(早上|上午|中午|下午|晚上|晚間)",
        r"([0-2]?\d|[零〇一二兩三四五六七八九十]{1,3})\s*[點:：]\s*([0-5]\d)?",
    ]
    result = message
    for pattern in patterns:
        result = re.sub(pattern, " ", result)
    return normalize_text(result)


def extract_event_title(message: str, action_type: str) -> str:
    text = strip_time_phrases(message)
    for word in FILLER_WORDS:
        text = text.replace(word, " ")
    text = normalize_text(text)
    text = re.sub(r"^(的|在|於)\s*", "", text)
    text = re.sub(r"的$", "", text)
    if action_type == "DELETE_EVENT" and not text:
        return ""
    return text or "未命名行程"


def isoformat_utc_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_chat_response(raw: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
    action_type = str(raw.get("action_type") or fallback.get("action_type") or "NONE")
    if action_type not in {"ADD_EVENT", "DELETE_EVENT", "NONE"}:
        action_type = fallback.get("action_type") or "NONE"

    response = {
        **empty_chat_response(str(raw.get("reply") or fallback.get("reply") or "")),
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
            try:
                response[key] = isoformat_utc_z(parse_datetime(response[key]))
            except Exception:
                response[key] = fallback.get(key) or ""
    if action_type == "ADD_EVENT" and (not response["event_title"] or not response["event_start"]):
        return fallback
    return response


async def parse_chat_with_gemini(user_id: str, message: str, fallback: Dict[str, Any]) -> Dict[str, Any]:
    now = taipei_now()
    prompt = (
        "你是行程控制意圖解析器。只回 JSON，不要 markdown。\n"
        "請判斷使用者是否要新增行程、刪除行程，或只是一般聊天。\n"
        "時間請以 ISO8601 回傳；若使用者說的是台灣時間，請保留正確時區或轉成 UTC。\n"
        "JSON 欄位固定為 reply, has_alert, alert_title, alert_url, action_type, "
        "event_title, event_start, event_end, event_id_to_delete。\n"
        "action_type 只能是 ADD_EVENT, DELETE_EVENT, NONE。\n"
        f"現在時間(Asia/Taipei): {now.isoformat()}\n"
        f"user_id: {user_id}\n"
        f"message: {message}"
    )
    parsed = await call_gemini_json_cached(
        prompt,
        fallback,
        "chat_command_intent",
        user_id or "anonymous",
        {"message": message, "now": now.isoformat()},
    )
    return normalize_chat_response(parsed, fallback)


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


async def build_chat_command(user_id: str, message: str) -> Dict[str, Any]:
    message = normalize_text(message)
    action_type = infer_action_type(message)
    if action_type == "NONE":
        return empty_chat_response("我收到你的訊息了。這句話目前不像新增或刪除行程指令。")

    title = extract_event_title(message, action_type)

    if action_type == "DELETE_EVENT":
        event_id = find_event_id_to_delete(user_id, title)
        reply = f"我偵測到你想刪除「{title or '指定'}」行程。"
        if event_id:
            reply = f"好的，我偵測到你想刪除「{title}」行程，已回傳對應行程 ID 給 App 處理。"
        fallback = {
            **empty_chat_response(reply),
            "action_type": "DELETE_EVENT",
            "event_title": title,
            "event_id_to_delete": event_id,
        }
        return await parse_chat_with_gemini(user_id, message, fallback)

    start = parse_event_datetime(message)
    if not start:
        start = taipei_now() + timedelta(hours=1)
    end = start + timedelta(hours=2)
    start_iso = isoformat_utc_z(start)
    end_iso = isoformat_utc_z(end)
    reply = f"好的，我偵測到新增行程指令：{title}，時間是 {start.strftime('%Y-%m-%d %H:%M')}。"
    fallback = {
        **empty_chat_response(reply),
        "action_type": "ADD_EVENT",
        "event_title": title,
        "event_start": start_iso,
        "event_end": end_iso,
    }
    return await parse_chat_with_gemini(user_id, message, fallback)
