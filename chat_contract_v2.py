import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from data import TAIWAN_LOCATIONS


TAIPEI_TZ = timezone(timedelta(hours=8))
WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
HOURS = {
    "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
}
ACTIVITIES = [
    "騎腳踏車", "騎自行車", "腳踏車", "自行車", "騎車",
    "開會", "游泳", "遊泳", "爬山", "跑步", "路跑", "打球", "運動", "露營",
    "看牙醫", "看醫生", "看電影", "上班", "上課", "吃飯", "晨跑",
    "看診", "考試", "辦護照", "聚餐", "旅遊", "出遊", "買菜", "通勤", "吃冰", "玩",
]
WEATHER_WORDS = ["天氣", "下雨", "降雨", "帶傘", "氣溫", "外套", "熱不熱", "冷不冷", "會不會下雨"]
TIME_PERIODS = ["早上", "上午", "中午", "下午", "晚上", "凌晨"]
VAGUE_LOCATION_NAMES = {"全聯", "學校", "成大", "小西門"}
VALID_INTENTS = {
    "CREATE_EVENT", "UPDATE_EVENT", "DELETE_EVENT", "QUERY_EVENT",
    "QUERY_WEATHER", "EVENT_WEATHER", "DISASTER_INFO", "GENERAL_CHAT",
}
VALID_MISSING_FIELDS = {"title", "date", "start_time", "city", "district", "target"}


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _client_now(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(TAIPEI_TZ)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TAIPEI_TZ)
        return parsed.astimezone(TAIPEI_TZ)
    except (TypeError, ValueError) as exc:
        raise ValueError("client_now must be a valid ISO8601 datetime") from exc


def _date_mentions(message: str, now: datetime) -> List[Tuple[Tuple[int, int], str]]:
    found: List[Tuple[Tuple[int, int], str]] = []
    occupied: List[Tuple[int, int]] = []

    def add(match: re.Match, day: datetime):
        span = match.span()
        if any(span[0] < end and span[1] > start for start, end in occupied):
            return
        occupied.append(span)
        found.append((span, day.strftime("%Y-%m-%d")))

    for match in re.finditer(r"大後天|後天|明天|今天|tomorrow", message, re.IGNORECASE):
        token = match.group(0).lower()
        offset = {"今天": 0, "明天": 1, "後天": 2, "大後天": 3, "tomorrow": 1}[token]
        add(match, now + timedelta(days=offset))

    for match in re.finditer(r"(下|這|本)?(?:星期|禮拜|週|周)([一二三四五六日天])", message):
        target = WEEKDAYS[match.group(2)]
        if match.group(1) == "下":
            next_monday = now + timedelta(days=(7 - now.weekday()))
            day = next_monday + timedelta(days=target)
        else:
            day = now + timedelta(days=(target - now.weekday()) % 7)
        add(match, day)

    for match in re.finditer(r"(?<!\d)(\d{1,2})\s*(?:/|月)\s*(\d{1,2})\s*(?:日|號|号)?", message):
        month, day = int(match.group(1)), int(match.group(2))
        try:
            candidate = now.replace(month=month, day=day)
            if candidate.date() < now.date():
                candidate = candidate.replace(year=candidate.year + 1)
            add(match, candidate)
        except ValueError:
            continue

    return sorted(found, key=lambda item: item[0][0])


def _weekend_range(message: str, now: datetime) -> Optional[Tuple[str, str]]:
    if not any(word in message for word in ["這週末", "本週末", "這周末", "本周末"]):
        return None
    monday = now - timedelta(days=now.weekday())
    saturday = monday + timedelta(days=5)
    sunday = monday + timedelta(days=6)
    return saturday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")


def _parse_hour(raw: str) -> Optional[int]:
    if raw.isdigit():
        value = int(raw)
        return value if 0 <= value <= 23 else None
    return HOURS.get(raw)


def _clock_value(hour_raw: str, minute_raw: Optional[str], period: str = "") -> Optional[str]:
    hour = _parse_hour(hour_raw)
    if hour is None:
        return None
    minute = 30 if minute_raw == "半" else int(minute_raw or "0")
    if minute > 59:
        return None
    if period in {"下午", "晚上", "pm"} and hour < 12:
        hour += 12
    elif period == "中午" and hour < 11:
        hour += 12
    elif period in {"凌晨", "am"} and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _time_range_mentions(message: str) -> List[Tuple[Tuple[int, int], str]]:
    hour = r"十一|十二|十|[一二兩三四五六七八九]|\d{1,2}"
    pattern = re.compile(
        rf"(?:從\s*)?(?:(早上|上午|中午|下午|晚上|凌晨)\s*)?({hour})\s*(?:點|时|時)?\s*(半|\d{{1,2}})?\s*(?:分)?"
        rf"\s*(?:到|至|~|～|－|-)\s*"
        rf"(?:(早上|上午|中午|下午|晚上|凌晨)\s*)?({hour})\s*(?:點|时|時)\s*(半|\d{{1,2}})?\s*(?:分)?"
    )
    match = pattern.search(message)
    if not match:
        return []
    first_period = match.group(1) or match.group(4) or ""
    second_period = match.group(4) or match.group(1) or ""
    start = _clock_value(match.group(2), match.group(3), first_period)
    end = _clock_value(match.group(5), match.group(6), second_period)
    if not start or not end:
        return []
    return [(match.span(), start), (match.span(), end)]


def _time_mentions(message: str) -> List[Tuple[Tuple[int, int], str]]:
    range_values = _time_range_mentions(message)
    if range_values:
        return range_values
    pattern = re.compile(
        r"(?:(早上|上午|中午|下午|晚上|凌晨)\s*)?"
        r"(十一|十二|十|[一二兩三四五六七八九]|\d{1,2})\s*"
        r"(?:點|时|時|:|：)\s*(半|\d{1,2})?\s*(?:分)?"
    )
    found = []
    for match in pattern.finditer(message):
        period = match.group(1) or ""
        value = _clock_value(match.group(2), match.group(3), period)
        if value:
            found.append((match.span(), value))

    english = re.compile(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)
    for match in english.finditer(message):
        value = _clock_value(match.group(1), match.group(2), match.group(3).lower())
        if value:
            found.append((match.span(), value))
    found.sort(key=lambda item: item[0][0])
    return found


def _time_requires_clarification(message: str) -> bool:
    """Return true when the user mentioned time but did not provide a precise clock time."""
    range_values = _time_range_mentions(message)
    if range_values:
        range_text = message[range_values[0][0][0]:range_values[0][0][1]]
        if any(period in range_text for period in TIME_PERIODS):
            return False
        hours = [int(value.split(":", 1)[0]) for _, value in range_values]
        return any(1 <= value <= 12 for value in hours)
    if re.search(r"\bat\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", message, re.IGNORECASE):
        return False
    pattern = re.compile(
        r"(?:(早上|上午|中午|下午|晚上|凌晨)\s*)?"
        r"(十一|十二|十|[一二兩三四五六七八九]|\d{1,2})\s*"
        r"(?:點|时|時|:|：)\s*(半|\d{1,2})?\s*(?:分)?"
    )
    matches = list(pattern.finditer(message))
    if not matches:
        return any(period in message for period in TIME_PERIODS)

    for match in matches:
        hour = _parse_hour(match.group(2))
        if not match.group(1) and hour is not None and 1 <= hour <= 12:
            return True
    return False


def _location(message: str, preferred_city: Optional[str] = None) -> Dict[str, Optional[str]]:
    normalized = message.replace("台", "臺")
    city = None
    district = None
    raw_city = None
    raw_district = None
    for candidate, districts in TAIWAN_LOCATIONS.items():
        short_city = candidate.replace("臺", "台")
        aliases = {candidate, short_city}
        if candidate.endswith(("市", "縣")):
            aliases.update({candidate[:-1], short_city[:-1]})
        raw_city = next((alias for alias in sorted(aliases, key=len, reverse=True) if alias and alias in message), None)
        if raw_city or candidate in normalized:
            city = candidate
            for item in districts:
                short = item[:-1] if item.endswith(("區", "鄉", "鎮", "市")) else item
                if item in normalized:
                    district = item
                    raw_district = item if item in message else item.replace("臺", "台")
                    break
                if len(short) >= 2 and short in normalized:
                    district = item
                    raw_district = short if short in message else short.replace("臺", "台")
                    break
            break

    if not city and preferred_city in TAIWAN_LOCATIONS:
        for item in TAIWAN_LOCATIONS[preferred_city]:
            short = item[:-1] if item.endswith(("區", "鄉", "鎮", "市")) else item
            if item in normalized or (len(short) >= 2 and short in normalized):
                city, district = preferred_city, item
                raw_district = item if item in message else short
                break

    location = None
    if city:
        location = (raw_city or city) + (raw_district or "")
    return {"city": city, "district": district, "location": location}


def _explicit_location_name(message: str) -> Optional[str]:
    match = re.search(r"(?:地點\s*(?:在|是|為|：|:)|位於)\s*([^，。！？!?]+)", message)
    if not match:
        return None
    value = _text(match.group(1)).strip(" \t\r\n，。！？!?：:")
    return value[:120] or None


def _generic_location_name(message: str, title: Optional[str] = None) -> Optional[str]:
    if _location(message).get("city"):
        return None
    patterns = [
        r"(?:去|前往)\s*([^，。！？!?\s]+)",
        r"(?:地點\s*(?:在|是|為|：|:)|位於|在)\s*([^，。！？!?\s]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, message)
        if not match:
            continue
        value = _text(match.group(1))
        if title and value.endswith(title) and value != title:
            value = value[:-len(title)]
        if value and value not in ACTIVITIES:
            return value[:120]
    return None


def _activity(message: str) -> Optional[str]:
    english = re.search(r"\bremind\s+me\s+to\s+(.+?)(?:\s+tomorrow|\s+at\s+\d|$)", message, re.IGNORECASE)
    if english:
        return _text(english.group(1)) or None
    if message.startswith("跟") and "吃飯" in message:
        return _text(message)
    for item in ACTIVITIES:
        if item in message:
            if item == "遊泳":
                return "游泳"
            if item in {"騎自行車", "腳踏車", "自行車", "騎車"}:
                return "騎腳踏車"
            return item
    return None


def _is_question(message: str) -> bool:
    return any(token in message for token in ["?", "？", "嗎", "有沒有", "是不是", "會不會", "什麼", "怎麼", "覺得"])


def _is_event_query(message: str) -> bool:
    if any(token in message for token in ["幫我排", "幫我安排", "新增", "建立行程"]):
        return False
    if any(token in message for token in ["有行程", "什麼行程", "什麼安排", "有什麼安排", "有空嗎", "有事嗎"]):
        return True
    return _is_question(message) and any(token in message for token in ["是不是要去", "要去臺北嗎", "要去台北嗎"])


def _has_weather_question(message: str) -> bool:
    return any(word in message for word in WEATHER_WORDS) or "適合" in message


def _infer_intent(message: str, intent_hint: Optional[str], now: datetime, has_draft: bool = False) -> str:
    if message in {"不用了", "算了", "取消好了", "先不用"}:
        return "GENERAL_CHAT"
    if any(word in message for word in ["刪掉", "刪除", "取消行程", "移除行程"]) or (
        "取消" in message and any(word in message for word in ["開會", "行程", "安排"])
    ):
        return "DELETE_EVENT"
    if any(word in message for word in ["改成", "改到", "改為", "修改", "更新行程"]):
        return "UPDATE_EVENT"
    if _is_event_query(message):
        return "QUERY_EVENT"
    if _has_weather_question(message):
        return "EVENT_WEATHER" if _activity(message) else "QUERY_WEATHER"
    if any(word in message for word in ["颱風", "台風", "地震", "防災", "淹水", "洪水", "避難", "豪雨"]):
        return "DISASTER_INFO"
    activity = _activity(message)
    create_signal = any(word in message for word in ["我要去", "想去", "要去", "幫我排", "安排", "新增", "建立", "提醒我"]) or bool(
        re.search(r"\bremind\s+me\s+to\b", message, re.IGNORECASE)
    )
    dates = _date_mentions(message, now)
    times = _time_mentions(message)
    location = _location(message)
    destination_signal = bool(re.search(r"(?:去|到|前往)\s*[^，。！？\s]+", message))
    social_signal = bool(activity and message.startswith("跟"))
    if create_signal or social_signal or (dates and destination_signal) or (activity and (dates or times or location["city"])) or (dates and times and location["city"]):
        return "CREATE_EVENT"
    if intent_hint == "CREATE_EVENT" and (has_draft or activity or dates or times or location["city"]):
        return "CREATE_EVENT"
    return "GENERAL_CHAT"


def _event_filter(message: str, now: datetime) -> Dict[str, Optional[str]]:
    dates = _date_mentions(message, now)
    times = _time_mentions(message)
    title = _activity(message)
    if not title:
        match = re.search(r"(?:把|將)?(?:今天|明天|後天|大後天|星期[一二三四五六日天]|週[一二三四五六日天]|禮拜[一二三四五六日天])?的?([^，。！？]+?)(?:取消|刪掉|刪除|改成|改到|改為)", message)
        title = _text(match.group(1)) if match else None
    return {
        "date": dates[0][1] if dates else None,
        "title_keyword": title,
        "time": times[0][1] if times and not any(word in message for word in ["改成", "改到", "改為"]) else None,
    }


def _draft_title(message: str) -> Optional[str]:
    activity = _activity(message)
    if activity:
        return activity
    text = message
    text = re.sub(r"可以?幫我|請幫我|幫我|提醒我|我要|我想|想要|想|安排|新增|建立|排", " ", text)
    text = re.sub(r"大後天|後天|明天|今天|等一下|(?:下|這|本)?(?:星期|禮拜|週|周)[一二三四五六日天]|這週末|本週末", " ", text)
    text = re.sub(r"\btomorrow\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\d{1,2}\s*(?:/|月)\s*\d{1,2}\s*(?:日|號|号)?", " ", text)
    text = re.sub(
        r"(?:從\s*)?(?:(?:早上|上午|中午|下午|晚上|凌晨)\s*)?(?:十一|十二|十|[一二兩三四五六七八九]|\d{1,2})\s*(?:點|時)?\s*(?:半|\d{1,2})?\s*(?:分)?\s*(?:到|至|~|～|－|-)\s*(?:(?:早上|上午|中午|下午|晚上|凌晨)\s*)?(?:十一|十二|十|[一二兩三四五六七八九]|\d{1,2})\s*(?:點|時)\s*(?:半|\d{1,2})?\s*(?:分)?",
        " ", text,
    )
    text = re.sub(r"(?:早上|上午|中午|下午|晚上|凌晨)?\s*(?:十一|十二|十|[一二兩三四五六七八九]|\d{1,2})\s*(?:點|時|:|：)\s*(?:半|\d{1,2})?\s*(?:分)?", " ", text)
    text = re.sub(r"\bat\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", " ", text, flags=re.IGNORECASE)
    location = _location(message)
    for value in [location.get("city"), location.get("district"), location.get("location")]:
        if value:
            text = text.replace(value, " ").replace(value.replace("臺", "台"), " ")
            if value.endswith(("市", "縣")):
                text = text.replace(value[:-1], " ")
    text = re.sub(r"要去|前往|到|去|在|的|嗎|呢|[？?！!，。]", " ", text)
    text = re.sub(r"^[\s「『\"']*(?:我|我們)[\s「『\"']*", "", text)
    text = text.strip(" \t\r\n「」『』\"'，。！？!?：:")
    return _text(text) or None


def _base_response(intent: str, message: str, draft_id: Optional[str]) -> Dict[str, Any]:
    return {
        "status": "success",
        "contract_version": 2,
        "intent": intent,
        "is_question": _is_question(message),
        "needs_clarification": False,
        "missing_fields": [],
        "reply": "收到。",
        "draft_id": draft_id,
        "draft_event": None,
        "event_filter": None,
        "changes": None,
        "entities": None,
    }


def validate_chat_v2_response(result: Dict[str, Any], assistant_name: str = "小藍") -> Dict[str, Any]:
    """Enforce the public v2 contract before a response reaches mobile clients."""
    errors: List[str] = []
    intent = result.get("intent")
    if intent not in VALID_INTENTS:
        errors.append("invalid_intent")

    missing = result.get("missing_fields")
    if not isinstance(missing, list) or any(field not in VALID_MISSING_FIELDS for field in missing):
        errors.append("invalid_missing_fields")
        missing = []
    result["missing_fields"] = missing
    result["needs_clarification"] = bool(missing)

    if not _text(result.get("reply")):
        errors.append("empty_reply")

    draft = result.get("draft_event")
    if isinstance(draft, dict):
        date_value = draft.get("date")
        if date_value:
            try:
                datetime.strptime(str(date_value), "%Y-%m-%d")
            except ValueError:
                errors.append("invalid_date")
        for field in ["start_time", "end_time"]:
            value = draft.get(field)
            if value and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(value)):
                errors.append(f"invalid_{field}")
        city = draft.get("city")
        if city:
            normalized_city = str(city).replace("台", "臺")
            if normalized_city not in TAIWAN_LOCATIONS:
                errors.append("invalid_city")
            else:
                draft["city"] = normalized_city

    if not errors:
        return result

    safe = _base_response("GENERAL_CHAT", "", result.get("draft_id"))
    safe.update({
        "status": "success",
        "assistant_name": assistant_name,
        "reply": "我沒聽清楚，可以再說一次嗎？",
        "validation_errors": errors,
        "_skip_gemini": True,
    })
    return safe


def _entity_dates(message: str, now: datetime) -> Tuple[Optional[str], Optional[str]]:
    weekend = _weekend_range(message, now)
    if weekend:
        return weekend
    dates = _date_mentions(message, now)
    value = dates[0][1] if dates else None
    return value, value


def build_chat_v2_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    message = _text(payload.get("message"))
    assistant_name = " ".join(str(payload.get("assistant_name") or "").split())[:24] or "小藍"
    now = _client_now(payload.get("client_now"))
    existing_draft = dict(payload.get("draft_event") or {})
    intent = _infer_intent(message, payload.get("intent_hint"), now, bool(existing_draft))
    draft_id = payload.get("draft_id") or (f"draft-{uuid.uuid4().hex}" if intent == "CREATE_EVENT" else None)
    response = _base_response(intent, message, draft_id)
    response["assistant_name"] = assistant_name

    if intent == "CREATE_EVENT":
        existing = existing_draft
        dates = _date_mentions(message, now)
        times = _time_mentions(message)
        time_needs_clarification = _time_requires_clarification(message)
        has_time_wording = bool(times) or time_needs_clarification
        is_reminder = bool(existing.get("is_reminder")) or "提醒我" in message or bool(
            re.search(r"\bremind\s+me\s+to\b", message, re.IGNORECASE)
        )
        preferred_city = existing.get("city")
        location = _location(message, preferred_city)
        explicit_location = _explicit_location_name(message)
        title = _draft_title(message)
        generic_location = _generic_location_name(message, title)
        if explicit_location:
            location["location"] = explicit_location
        elif generic_location:
            location["location"] = generic_location
        existing_start = existing.get("start_time")
        if times and not time_needs_clarification:
            start_time = times[0][1]
            end_time = times[1][1] if len(times) > 1 else existing.get("end_time")
            all_day = False
        elif time_needs_clarification:
            start_time = existing_start
            end_time = existing.get("end_time")
            all_day = False
        elif existing_start:
            start_time = existing_start
            end_time = existing.get("end_time")
            all_day = bool(existing.get("all_day", False))
        else:
            start_time = None
            end_time = None
            all_day = True
        draft = {
            "title": title if title is not None else existing.get("title"),
            "date": dates[0][1] if dates else existing.get("date") or now.strftime("%Y-%m-%d"),
            "start_time": start_time,
            "end_time": end_time,
            "all_day": all_day,
            "is_reminder": is_reminder,
            "city": location["city"] if location["city"] is not None else existing.get("city"),
            "district": location["district"] if location["city"] is not None else existing.get("district"),
            "location": location["location"] if location["location"] is not None else existing.get("location"),
        }
        location_requires_city = bool(existing.get("location_requires_city")) or bool(
            generic_location and generic_location in VAGUE_LOCATION_NAMES
        )
        if location.get("city"):
            location_requires_city = False
        location_optional = is_reminder or draft.get("title") == "辦護照"
        draft["location_optional"] = location_optional
        draft["location_requires_city"] = location_requires_city
        missing = [key for key in ["title", "date"] if not draft.get(key)]
        if not location_optional and (location_requires_city or not (draft.get("city") or draft.get("location"))):
            missing.append("city")
        if (has_time_wording or existing_start) and not draft.get("start_time"):
            missing.append("start_time")
        if missing == ["start_time"] and time_needs_clarification:
            reply = "你說的時間是上午還是下午？請告訴我完整時段。"
        else:
            reply = ("還需要告訴我" + "、".join({"title": "要做什麼", "date": "日期", "start_time": "時間", "city": "地點"}[key] for key in missing) + "。") if missing else "我幫你整理好了，確認後就會加入行事曆。"
        response.update({
            "draft_event": draft,
            "missing_fields": missing,
            "needs_clarification": bool(missing),
            "reply": reply,
        })
        return response

    if intent == "UPDATE_EVENT":
        event_filter = _event_filter(message, now)
        dates = _date_mentions(message, now)
        times = _time_mentions(message)
        change_location = _location(message)
        changes: Dict[str, Any] = {}
        if len(dates) > 1:
            changes["date"] = dates[-1][1]
        if times:
            changes["start_time"] = times[-1][1]
        if change_location["city"]:
            changes.update(change_location)
        if not any(event_filter.values()):
            response["missing_fields"] = ["target"]
            response["needs_clarification"] = True
            response["reply"] = "你想修改哪一筆行程呢？"
        else:
            response["reply"] = "我已整理好要修改的內容，確認後才會更新。"
        response["event_filter"] = event_filter
        response["changes"] = changes or None
        return response

    if intent == "DELETE_EVENT":
        event_filter = _event_filter(message, now)
        response["event_filter"] = event_filter
        if not any(event_filter.values()):
            response["missing_fields"] = ["target"]
            response["needs_clarification"] = True
            response["reply"] = "你想刪除哪一筆行程呢？"
        else:
            response["reply"] = "我找到你描述的行程條件，確認後才會刪除。"
        return response

    if intent in {"QUERY_EVENT", "QUERY_WEATHER", "EVENT_WEATHER"}:
        start_date, end_date = _entity_dates(message, now)
        location = _location(message)
        activity = _activity(message) if intent in {"QUERY_EVENT", "EVENT_WEATHER"} else None
        response["entities"] = {
            "start_date": start_date,
            "end_date": end_date,
            "time": _time_mentions(message)[0][1] if _time_mentions(message) else None,
            "city": location["city"],
            "district": location["district"],
            "activity": activity,
        }
        if intent == "QUERY_EVENT":
            response["reply"] = "我幫你看看這段時間的行程。"
        elif intent == "QUERY_WEATHER":
            response["reply"] = "我幫你整理要查詢的天氣條件。"
        else:
            response["reply"] = f"我幫你看看天氣是否適合{activity or '這個活動'}。"
        return response

    if intent == "DISASTER_INFO":
        if "地震" in message:
            response["reply"] = "地震時先趴下、掩護、穩住，遠離玻璃與高櫃；搖晃停止後再依官方指示疏散。"
        elif "颱風" in message or "台風" in message:
            response["reply"] = (
                "中央氣象署的颱風警報分為海上與陸上兩種：預測颱風七級風暴風範圍可能在 24 小時內侵襲"
                "臺灣本島、澎湖、金門或馬祖 100 公里內海域時，發布海上颱風警報並列出警戒海域；"
                "預測可能在 18 小時內侵襲上述地區陸地時，發布陸上颱風警報並列出警戒縣市。"
                "警報發布後仍要持續查看氣象署更新、地方政府停班停課與疏散通知。"
            )
        else:
            response["reply"] = "請先確認官方警報，避開危險區域並準備飲水、藥品、證件與行動電源。"
        return response

    response["reply"] = "不用了" in message and "好的，這次不建立草稿。" or f"嗨，我是{assistant_name}。需要查行程、天氣或防災資訊都可以告訴我。"
    return response


def draft_to_legacy_response(v2: Dict[str, Any]) -> Dict[str, Any]:
    intent = v2.get("intent") or "GENERAL_CHAT"
    result: Dict[str, Any] = {
        "status": v2.get("status") or "success",
        "reply": v2.get("reply") or "收到。",
        "action_type": "NONE",
        "event_created": None,
        "event_updated": None,
        "event_id": None,
        "event_id_to_delete": None,
    }
    if intent == "CREATE_EVENT":
        draft = v2.get("draft_event") or {}
        missing = v2.get("missing_fields") or []
        action = "CLARIFY" if missing else "CREATE_EVENT"
        start = _draft_datetime(draft.get("date"), draft.get("start_time"))
        end = _draft_datetime(draft.get("date"), draft.get("end_time"))
        if start and not end:
            end = (datetime.fromisoformat(start) + timedelta(hours=1)).isoformat(timespec="seconds")
        result.update({
            "action_type": action,
            "reply": v2.get("reply") if missing else "我幫你整理好了，確認後就會加入行事曆。",
            "missing_slots": missing,
            "clarify_slot": missing[0] if missing else "",
            "event_title": draft.get("title") or "",
            "event_start": start or "",
            "event_end": end or "",
            "event_city": draft.get("city") or "",
            "event_district": draft.get("district") or "",
            "event_location": draft.get("location") or "",
        })
    elif intent in {"DELETE_EVENT", "UPDATE_EVENT"}:
        event_filter = v2.get("event_filter") or {}
        changes = v2.get("changes") or {}
        target_date = changes.get("date") or event_filter.get("date")
        target_time = changes.get("start_time") or event_filter.get("time")
        result.update({
            "action_type": intent if not v2.get("needs_clarification") else "CLARIFY",
            "event_title": event_filter.get("title_keyword") or "",
            "event_start": _draft_datetime(target_date, target_time) or "",
            "event_city": changes.get("city") or "",
            "event_district": changes.get("district") or "",
            "event_location": changes.get("location") or "",
            "event_updated": changes or None,
            "missing_slots": v2.get("missing_fields") or [],
        })
    elif intent == "QUERY_WEATHER" or intent == "EVENT_WEATHER":
        result["action_type"] = "WEATHER_QUERY"
    elif intent == "DISASTER_INFO":
        result["action_type"] = "DISASTER_GUIDE"
    return result


def _draft_datetime(date_value: Optional[str], time_value: Optional[str]) -> Optional[str]:
    if not date_value or not time_value:
        return None
    try:
        return datetime.fromisoformat(f"{date_value}T{time_value}:00+08:00").isoformat(timespec="seconds")
    except ValueError:
        return None
