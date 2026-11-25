import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from data import TAIWAN_LOCATIONS


TAIPEI_TZ = timezone(timedelta(hours=8))
WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
HOURS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
}
ACTIVITIES = [
    "騎腳踏車", "騎自行車", "腳踏車", "自行車", "騎車",
    "開會", "游泳", "遊泳", "爬山", "跑步", "路跑", "打球", "運動", "露營",
    "看牙醫", "看醫生", "看電影", "看展", "倒垃圾", "上班", "上課", "吃飯", "晨跑",
    "看診", "考試", "辦護照", "聚餐", "旅遊", "出遊", "買菜", "通勤", "吃冰", "玩",
    "交報告", "繳電費", "繳費", "洗衣服", "吃藥", "值班", "面試", "報到", "回家",
    "跨年",
]
WEATHER_WORDS = [
    "天氣", "下雨", "降雨", "帶傘", "氣溫", "回溫", "變暖", "變冷", "降溫", "寒流",
    "外套", "短袖", "濕度", "溼度", "潮濕", "很濕", "乾燥", "悶熱", "好悶",
    "體感", "露點", "日出", "日落", "天亮", "天黑", "太陽下山", "月出", "月落", "月亮",
    "氣壓", "雨量", "下了多少雨", "能見度", "視線", "起霧", "大霧", "詳細天氣", "天氣資料",
    "氣象資料", "觀測", "測站", "日照", "風速", "風向", "陣風", "最高溫", "最低溫", "紫外線", "防曬", "空氣品質", "口罩", "風大", "曬衣服", "洗車", "颱風", "台風", "特報",
    "雨什麼時候", "雨會下到", "雨要下多久", "什麼時候會下雨", "熱不熱", "冷不冷", "會不會下雨",
]
TIME_PERIODS = ["凌晨", "早上", "上午", "中午", "下午", "傍晚", "晚上", "今晚", "明晚", "今早", "明早", "半夜", "深夜", "晚"]
PLACE_ALIASES = {
    "小琉球": ("屏東縣", "琉球鄉"),
    "墾丁": ("屏東縣", "恆春鎮"),
    "綠島": ("臺東縣", "綠島鄉"),
    "蘭嶼": ("臺東縣", "蘭嶼鄉"),
    "日月潭": ("南投縣", "魚池鄉"),
    "清境": ("南投縣", "仁愛鄉"),
    "合歡山": ("南投縣", "仁愛鄉"),
    "武嶺": ("南投縣", "仁愛鄉"),
    "阿里山": ("嘉義縣", "阿里山鄉"),
    "太魯閣": ("花蓮縣", "秀林鄉"),
    "九份": ("新北市", "瑞芳區"),
    "小金門": ("金門縣", "烈嶼鄉"),
    "馬公": ("澎湖縣", "馬公市"),
    "成大": ("臺南市", None),
}
LOCATION_OPTIONAL_ACTIVITIES = {"洗衣服", "交報告", "繳電費", "繳費", "吃藥", "倒垃圾", "準備期中考"}
GENERIC_LOCATION_TERMS = [
    "我公司", "朋友家", "男友家", "女友家", "阿嬤家", "我們學校", "打工的地方", "補習班",
    "我家", "宿舍", "老家", "教室", "辦公室", "學校", "公司", "附近", "那邊", "這邊", "老地方",
    "外面", "市區", "樓下", "圖書館", "文化中心", "運動中心", "游泳池", "健身房", "百貨公司",
    "火車站", "高鐵站", "捷運站", "公車站", "停車場", "加油站", "大賣場", "電影院", "咖啡廳",
    "早餐店", "區公所", "警察局", "美術館", "博物館", "動物園", "體育館", "醫院", "診所",
    "牙醫", "藥局", "車站", "機場", "超商", "超市", "餐廳", "銀行", "郵局", "公園", "市場",
    "夜市", "海邊", "廟", "大潤發", "家樂福", "全聯", "好市多", "愛買", "新光三越", "遠百",
    "麥當勞", "肯德基", "星巴克", "摩斯", "7-11", "全家", "萊爾富", "OK",
]
VALID_INTENTS = {
    "CREATE_EVENT", "UPDATE_EVENT", "DELETE_EVENT", "QUERY_EVENT",
    "QUERY_WEATHER", "EVENT_WEATHER", "DISASTER_INFO", "GENERAL_CHAT",
}
VALID_MISSING_FIELDS = {"title", "date", "start_time", "city", "district", "target"}
AMBIGUOUS_DRAFT_TIMES: Dict[str, List[str]] = {}


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

    for match in re.finditer(r"明後天", message):
        span = match.span()
        occupied.append(span)
        found.append((span, (now + timedelta(days=1)).strftime("%Y-%m-%d")))
        found.append((span, (now + timedelta(days=2)).strftime("%Y-%m-%d")))

    for match in re.finditer(r"大後天|後天|明天|明早|明晚|今天|今早|今晚|tomorrow", message, re.IGNORECASE):
        token = match.group(0).lower()
        offset = {"今天": 0, "今早": 0, "今晚": 0, "明天": 1, "明早": 1, "明晚": 1, "後天": 2, "大後天": 3, "tomorrow": 1}[token]
        add(match, now + timedelta(days=offset))

    for match in re.finditer(r"(?:這|本)?(?:週末|周末)", message):
        monday = now - timedelta(days=now.weekday())
        add(match, monday + timedelta(days=5))

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
            has_next_year = message[max(0, match.start() - 2):match.start()] == "明年"
            candidate = now.replace(year=now.year + (1 if has_next_year else 0), month=month, day=day)
            if not has_next_year and candidate.date() < now.date():
                candidate = candidate.replace(year=candidate.year + 1)
            add(match, candidate)
        except ValueError:
            continue

    for match in re.finditer(r"這個月\s*(\d{1,2})\s*(?:日|號|号)", message):
        try:
            add(match, now.replace(day=int(match.group(1))))
        except ValueError:
            continue

    for match in re.finditer(r"(?<![\d/月])(\d{1,2})\s*(?:日|號|号)", message):
        try:
            add(match, now.replace(day=int(match.group(1))))
        except ValueError:
            continue

    return sorted(found, key=lambda item: item[0][0])


def _weekend_range(message: str, now: datetime) -> Optional[Tuple[str, str]]:
    if not any(word in message for word in ["週末", "周末"]):
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


def _parse_chinese_number(raw: str) -> Optional[int]:
    if not raw:
        return 0
    if raw.isdigit():
        return int(raw)
    if raw == "半":
        return 30
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if raw == "十":
        return 10
    if "十" in raw:
        left, right = raw.split("十", 1)
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    if all(char in digits for char in raw):
        return int("".join(str(digits[char]) for char in raw))
    return None


def _normalize_period(period: str, hour: int) -> str:
    if period in {"今晚", "明晚", "傍晚", "晚", "深夜"}:
        return "晚上"
    if period in {"今早", "明早"}:
        return "早上"
    if period == "半夜":
        return "凌晨" if hour <= 6 else "晚上"
    return period


def _clock_value(hour_raw: str, minute_raw: Optional[str], period: str = "") -> Optional[str]:
    hour = _parse_hour(hour_raw)
    if hour is None:
        return None
    minute = _parse_chinese_number(minute_raw or "")
    if minute is None or minute > 59:
        return None
    period = _normalize_period(period, hour)
    if period == "晚上" and hour == 12:
        hour = 0
    elif period in {"下午", "晚上", "pm"} and hour < 12:
        hour += 12
    elif period == "中午" and hour < 11:
        hour += 12
    elif period in {"凌晨", "am"} and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _clock_with_period(clock: str, period: str) -> Optional[str]:
    try:
        hour, minute = clock.split(":", 1)
    except ValueError:
        return None
    return _clock_value(str(int(hour)), str(int(minute)), period)


def _remember_ambiguous_times(draft_id: Optional[str], times: List[Tuple[Tuple[int, int], str]]) -> None:
    if not draft_id or not times:
        return
    AMBIGUOUS_DRAFT_TIMES[draft_id] = [value for _, value in times]
    while len(AMBIGUOUS_DRAFT_TIMES) > 1000:
        AMBIGUOUS_DRAFT_TIMES.pop(next(iter(AMBIGUOUS_DRAFT_TIMES)))


def _resolve_clock_for_date(clock: str, now: datetime, target_date: str) -> Optional[str]:
    hour, minute = (int(part) for part in clock.split(":", 1))
    if hour == 0 or hour >= 13:
        resolved_hour = hour
    else:
        try:
            target = datetime.strptime(target_date, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            target = now.date()
        if target != now.date():
            resolved_hour = hour + 12 if 1 <= hour <= 6 else hour
        else:
            candidates = [hour]
            if 1 <= hour <= 11:
                candidates.append(hour + 12)
            resolved_hour = next(
                (
                    candidate for candidate in candidates
                    if now.replace(hour=candidate, minute=minute, second=0, microsecond=0) > now
                ),
                -1,
            )
            if resolved_hour < 0:
                return None
    return f"{resolved_hour:02d}:{minute:02d}"


def _resolve_ambiguous_times(
    message: str,
    times: List[Tuple[Tuple[int, int], str]],
    now: datetime,
    target_date: str,
) -> Tuple[List[Tuple[Tuple[int, int], str]], bool, bool]:
    """Apply the LINE/App clock convention; return values, needs clarification, and past-time flag."""
    if not times:
        return times, _time_requires_clarification(message), False

    ambiguous = _time_requires_clarification(message)
    resolved = list(times)
    if ambiguous:
        converted = [_resolve_clock_for_date(value, now, target_date) for _, value in times]
        if any(value is None for value in converted):
            return times, True, True
        if len(converted) > 1:
            start_hour = int(str(converted[0]).split(":", 1)[0])
            end_hour = int(str(converted[1]).split(":", 1)[0])
            if end_hour < start_hour and start_hour >= 12:
                raw_end_hour = int(times[1][1].split(":", 1)[0])
                converted[1] = f"{raw_end_hour + 12:02d}:{times[1][1].split(':', 1)[1]}"
        resolved = [(times[index][0], str(value)) for index, value in enumerate(converted)]

    try:
        target = datetime.strptime(target_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        target = now.date()
    if target == now.date() and resolved:
        hour, minute = (int(part) for part in resolved[0][1].split(":", 1))
        start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if start <= now:
            return resolved, True, True
    return resolved, False, False


def _time_range_mentions(message: str) -> List[Tuple[Tuple[int, int], str]]:
    hour = r"十一|十二|十|[一二兩三四五六七八九]|\d{1,2}"
    period = r"凌晨|早上|上午|中午|下午|傍晚|晚上|今晚|明晚|今早|明早|半夜|深夜|晚"
    minute = r"半|[零〇一二兩三四五六七八九十]{1,3}|\d{1,2}(?!\s*樓)"
    pattern = re.compile(
        rf"(?:從\s*)?(?:({period})\s*)?({hour})\s*(?:點|时|時)?\s*({minute})?\s*(?:分)?"
        rf"\s*(?:到|至|~|～|－|-)\s*"
        rf"(?:({period})\s*)?({hour})\s*(?:點|时|時)\s*({minute})?\s*(?:分)?"
    )
    match = pattern.search(message)
    if not match:
        return []
    first_period = match.group(1) or match.group(4) or ""
    second_period = match.group(4) or match.group(1) or ""
    end_hour = _parse_hour(match.group(5))
    if (
        match.group(1) in {"晚上", "今晚", "明晚", "深夜", "晚"}
        and not match.group(4)
        and end_hour is not None
        and 1 <= end_hour <= 6
    ):
        # 「晚上十一點到一點」表示跨午夜，不是隔天下午一點。
        second_period = "凌晨"
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
        r"(?:(凌晨|早上|上午|中午|下午|傍晚|晚上|今晚|明晚|今早|明早|半夜|深夜|晚)\s*)?"
        r"(十一|十二|十|[一二兩三四五六七八九]|\d{1,2})\s*"
        r"(?:點|时|時|:|：)\s*(半|[零〇一二兩三四五六七八九十]{1,3}|\d{1,2}(?!\s*樓))?\s*(?:分)?"
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
        if len(re.findall(r"(?<!\d)(?:[01]\d|2[0-3])[:：][0-5]\d", range_text)) >= 2:
            return False
        hours = [int(value.split(":", 1)[0]) for _, value in range_values]
        return any(1 <= value <= 12 for value in hours)
    if re.search(r"\bat\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", message, re.IGNORECASE):
        return False
    pattern = re.compile(
        r"(?:(凌晨|早上|上午|中午|下午|傍晚|晚上|今晚|明晚|今早|明早|半夜|深夜|晚)\s*)?"
        r"(十一|十二|十|[一二兩三四五六七八九]|\d{1,2})\s*"
        r"(?:點|时|時|:|：)\s*(半|[零〇一二兩三四五六七八九十]{1,3}|\d{1,2}(?!\s*樓))?\s*(?:分)?"
    )
    matches = list(pattern.finditer(message))
    if not matches:
        return any(period in message for period in TIME_PERIODS)

    for match in matches:
        hour = _parse_hour(match.group(2))
        if re.match(r"(?:[01]\d|2[0-3])[:：]", match.group(0).strip()):
            continue
        if not match.group(1) and hour is not None and 1 <= hour <= 12:
            return True
    return False


def _duration_minutes(message: str) -> Optional[int]:
    match = re.search(r"(半|一|二|兩|三|四|五|六|\d+(?:\.5)?)\s*(?:個)?小時", message)
    if not match:
        return None
    raw = match.group(1)
    if raw == "半":
        return 30
    hours = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6}.get(raw)
    if hours is None:
        try:
            return int(float(raw) * 60)
        except ValueError:
            return None
    return hours * 60


def _add_minutes(clock: str, minutes: int) -> str:
    parsed = datetime.strptime(clock, "%H:%M") + timedelta(minutes=minutes)
    return parsed.strftime("%H:%M")


def _station_phrase(message: str) -> Optional[str]:
    cleaned = message
    spans = [span for span, _ in _date_mentions(message, datetime.now(TAIPEI_TZ))]
    spans.extend(span for span, _ in _time_mentions(message))
    for start, end in sorted(spans, reverse=True):
        cleaned = cleaned[:start] + " " + cleaned[end:]
    cleaned = re.sub(
        r"記得提醒我|幫我提醒|提醒我|提醒一下|叫我|記得|幫我|我要|我想|新增|加入|安排|建立|排行程",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"^(?:\s|我|我們|要|會|去|到|在|前往)+", "", cleaned)

    patterns = [
        r"((?:高鐵|臺鐵|台鐵|捷運|輕軌)[^\s，。！？!?]{1,20}?站)",
        r"([^\s，。！？!?]{1,20}?(?:火車站|高鐵站|捷運站|轉運站|車站))",
        r"([^\s，。！？!?]{1,16}?站)(?=$|[\s，。！？!?]|集合|搭車|開會|上車|下車|轉車)",
    ]
    for pattern in patterns:
        match = re.search(pattern, cleaned)
        if not match:
            continue
        candidate = _text(match.group(1)).strip(" \t\r\n，。！？!?：:")
        if candidate not in {"網站", "本站", "起點站", "終點站"}:
            return candidate[:120] or None
    return None


def _location_phrase(message: str) -> Optional[str]:
    explicit = re.search(r"(?:地點\s*(?:在|是|為|：|:)|位於|改在|[@＠])\s*([^，。！？!?]+)", message)
    if explicit:
        explicit_value = _text(explicit.group(1)).strip(" \t\r\n，。！？!?：:")
        return _station_phrase(explicit_value) or explicit_value[:120] or None

    for alias in sorted(PLACE_ALIASES, key=len, reverse=True):
        if alias in message:
            return alias

    station = _station_phrase(message)
    if station:
        return station

    suffix = r"文化中心|機場|大學|學院|高中|國中|國小|醫院|公園|老街|夜市|商場|百貨|廣場|體育館|圖書館"
    match = re.search(rf"(?:^|[\s，,]|去|到|在)([^\s，。！？!?]{{1,24}}(?:{suffix}))", message)
    if match:
        return _text(match.group(1))[:120] or None
    for term in sorted(GENERIC_LOCATION_TERMS, key=len, reverse=True):
        if term in message:
            return term
    return None


def _location(message: str, preferred_city: Optional[str] = None) -> Dict[str, Optional[str]]:
    normalized = message.replace("台", "臺")
    for alias, (alias_city, alias_district) in PLACE_ALIASES.items():
        if alias in normalized:
            return {"city": alias_city, "district": alias_district, "location": alias}

    city = None
    district = None
    raw_city = None
    raw_district = None
    shorthand_cities = {"新竹": "新竹市", "嘉義": "嘉義市", "臺北": "臺北市", "臺南": "臺南市", "臺中": "臺中市", "高雄": "高雄市"}
    exact_city = next((candidate for candidate in TAIWAN_LOCATIONS if candidate in normalized), None)
    if not exact_city:
        exact_city = next((full for short, full in shorthand_cities.items() if short in normalized), None)
    city_candidates = (
        [(exact_city, TAIWAN_LOCATIONS[exact_city])]
        if exact_city
        else list(TAIWAN_LOCATIONS.items())
    )
    for candidate, districts in city_candidates:
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

    location = _location_phrase(message)
    if city:
        location = location or (raw_city or city) + (raw_district or "")
    return {"city": city, "district": district, "location": location}


def extract_location_entities(message: str, preferred_city: Optional[str] = None) -> Dict[str, Optional[str]]:
    return _location(_text(message), preferred_city)


def _explicit_location_name(message: str) -> Optional[str]:
    return _location_phrase(message) if re.search(r"地點|位於|改在|[@＠]", message) else None


def _generic_location_name(message: str, title: Optional[str] = None) -> Optional[str]:
    phrase = _location_phrase(message)
    if phrase and phrase != title:
        return phrase
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
    return any(token in message for token in ["?", "？", "嗎", "呢", "幾點", "有沒有", "是不是", "會不會", "要不要", "能不能", "可不可以", "為什麼", "什麼", "哪", "怎麼", "覺得"])


def _is_event_query(message: str) -> bool:
    if any(token in message for token in ["幫我排", "幫我安排", "新增", "建立行程"]):
        return False
    if re.search(r"(?:^|我|你|今天|明天|後天|大後天|週末|周末|星期[一二三四五六日天]|週[一二三四五六日天]|禮拜[一二三四五六日天])(?:是不是)?有行程", message):
        return True
    if any(token in message for token in ["什麼行程", "什麼安排", "有什麼安排", "有空嗎", "有事嗎"]):
        return True
    return _is_question(message) and any(token in message for token in ["是不是要去", "要去臺北嗎", "要去台北嗎"])


def _has_weather_question(message: str) -> bool:
    return any(word in message for word in WEATHER_WORDS) or "適合" in message


def _is_draft_followup(message: str, now: datetime) -> bool:
    if any(word in message for word in ["心情", "很累", "好累", "很忙", "好忙", "放假"]):
        return False
    if _activity(message) or _time_mentions(message) or _location(message).get("city") or _location_phrase(message):
        return True
    if _explicit_location_name(message) or re.match(r"^(?:地點在|改在|在)\s*\S+", message):
        return True
    date_tokens = _date_mentions(message, now)
    if date_tokens:
        remainder = message
        for (start, end), _ in reversed(date_tokens):
            remainder = remainder[:start] + remainder[end:]
        remainder = re.sub(r"[\s，。！？!?的就改成改到是為]+", "", remainder)
        return not remainder
    if 1 < len(message) <= 20 and not _is_question(message) and not re.search(r"[，。！？!?]", message):
        return True
    return False


def _is_location_optional(title: Optional[str]) -> bool:
    text = str(title or "")
    return any(keyword in text for keyword in LOCATION_OPTIONAL_ACTIVITIES)


def _has_date_wording(message: str) -> bool:
    return bool(re.search(r"明年|今天|明天|後天|大後天|週末|周末|星期|禮拜|週|周|\d{1,2}\s*(?:/|月)\s*\d{1,2}|\d{1,2}\s*(?:日|號|号)", message))


def _is_recurring_request(message: str) -> bool:
    return bool(re.search(r"每天|每日|每週|每周|每星期|每禮拜|每個月|每月", message))


def _repeat_kind(message: str) -> str:
    if re.search(r"每天|每日", message):
        return "daily"
    if re.search(r"每週|每周|每星期|每禮拜", message):
        return "weekly"
    if re.search(r"每個月|每月", message):
        return "monthly"
    return "none"


def _advance_repeat_date(date_value: str, repeat: str) -> str:
    day = datetime.strptime(date_value, "%Y-%m-%d")
    if repeat == "daily":
        return (day + timedelta(days=1)).strftime("%Y-%m-%d")
    if repeat == "weekly":
        return (day + timedelta(days=7)).strftime("%Y-%m-%d")
    if repeat == "monthly":
        year = day.year + (1 if day.month == 12 else 0)
        month = 1 if day.month == 12 else day.month + 1
        while day.day > 28:
            try:
                return day.replace(year=year, month=month).strftime("%Y-%m-%d")
            except ValueError:
                day -= timedelta(days=1)
        return day.replace(year=year, month=month).strftime("%Y-%m-%d")
    return date_value


def _has_multiple_event_actions(message: str) -> bool:
    if not any(separator in message for separator in ["然後", "接著", "再去", "之後"]):
        return False
    return len({item for item in ACTIVITIES if item in message}) >= 2


def _is_usage_help(message: str) -> bool:
    if any(word in message for word in ["你會做什麼", "可以做什麼", "有哪些功能", "有什麼功能", "怎麼使用小藍", "小藍怎麼用", "怎麼用小藍", "可以幫我排行程嗎", "行程可以分享"]):
        return True
    has_how = any(word in message for word in ["怎麼", "如何", "怎樣", "怎麼用", "怎麼說"])
    has_feature = any(word in message for word in ["行程", "提醒", "草稿", "天氣", "地點", "時間", "防災", "警報", "小藍", "助理", "名稱"])
    has_operation = any(word in message for word in [
        "新增", "建立", "加入", "加行程", "安排", "修改", "更改", "編輯", "刪除", "取消", "查詢", "查", "查看",
        "確認", "儲存", "使用", "設定", "清除", "略過", "改名", "改", "問",
    ])
    return has_how and has_feature and has_operation


def _is_existing_event_location_update(message: str) -> bool:
    """Recognize wording that adds a location to an already-mentioned event."""
    return bool(re.search(
        r"(?:的?行程\s*(?:是在|在|的?地點(?:是|在|為))|行程的?地點\s*(?:是|在|為))",
        message,
    ))


def _usage_help_reply(message: str) -> str:
    if "行程可以分享" in message:
        return "可以，請先在 App 建立行程，再從行程詳情使用分享功能邀請家人；只有檢視權限的共享行程仍要由原建立者修改。"
    if "可以幫我排行程嗎" in message:
        return "可以。你可以直接說「明天下午三點在高雄開會」，小藍會整理成確認卡，等你確認後才加入行事曆。"
    if any(word in message for word in ["你會做什麼", "可以做什麼", "有哪些功能", "有什麼功能", "怎麼使用小藍", "小藍怎麼用", "怎麼用小藍"]):
        return "小藍可以幫你建立、查詢、修改與刪除行程，查天氣和行程風險，也能回答防災問題。直接用一句話描述需求即可，所有行程變更都會先讓你確認。"
    if any(word in message for word in ["改名", "名稱"]):
        return "助理名稱請在 App 的助理設定中修改；之後小藍會使用你設定的名稱自稱。"
    if "天氣" in message:
        return "你可以直接問「明天臺南會下雨嗎？」、「週五高雄幾度？」或「明天去爬山適合嗎？」。小藍會依日期、地點與活動查詢天氣。"
    if any(word in message for word in ["防災", "警報"]):
        return "你可以直接問「地震時怎麼辦？」、「颱風警報怎麼分？」或「淹水時要往哪裡避難？」。App 內也有地震小教室、火災小教室與颱風豪雨小教室；即時警報以中央氣象署及政府官方資訊為準。"
    if "草稿" in message or "確認" in message:
        return "小藍整理完行程後會顯示確認卡；按下確認才會寫入行事曆。若不需要，可以按取消或直接說「不用了」。"
    if any(word in message for word in ["清除", "略過"]) and "地點" in message:
        return "建立行程時可以略過行政區；要清除既有地點，請在行程編輯頁清除後儲存。小藍不會自行猜測或補上預設地點。"
    if any(word in message for word in ["修改", "更改", "編輯"]) and any(word in message for word in ["地點", "時間"]):
        return "你可以直接說「把明天下午三點的開會改到四點」或「把週五聚餐換到臺北車站」。小藍會先顯示修改內容，確認後才更新。"
    if any(word in message for word in ["刪除", "取消"]):
        return "你可以直接說「刪除明天下午三點的開會」。小藍會先找出符合的行程，等你確認後才刪除。"
    if any(word in message for word in ["修改", "更改", "編輯"]):
        return "你可以直接說「把明天下午三點的開會改到四點」。小藍會整理修改草稿，等你確認後才更新。"
    if any(word in message for word in ["查詢", "怎麼查", "查看", "查"]):
        return "你可以直接問「我明天有什麼行程？」或「後天下午有行程嗎？」。"
    return (
        "你可以直接說「明天下午三點在臺南火車站開會」。"
        "小藍會先整理成草稿，缺少資料時再追問，等你確認後才加入行事曆；沒說時間時會建立全天草稿。"
    )


def _infer_intent(message: str, intent_hint: Optional[str], now: datetime, has_draft: bool = False) -> str:
    if message in {"不用了", "算了", "取消", "取消好了", "先不用"}:
        return "GENERAL_CHAT"
    if _is_usage_help(message):
        return "GENERAL_CHAT"
    if _has_multiple_event_actions(message):
        return "CREATE_EVENT"
    if re.match(r"^(?:不要|不用)提醒", message):
        return "DELETE_EVENT"
    cancel_with_target = message.startswith("取消") and bool(
        _date_mentions(message, now) or _activity(message) or any(word in message for word in ["行程", "活動", "提醒"])
    )
    if cancel_with_target or any(word in message for word in ["刪掉", "取消行程", "移除行程", "不去了"]) or re.search(r"^(?:請)?刪除|^把.+(?:刪除|刪掉|取消)", message):
        return "DELETE_EVENT"
    if any(word in message for word in ["如果", "假如", "要是"]) and _has_weather_question(message):
        return "EVENT_WEATHER" if _activity(message) else "QUERY_WEATHER"
    draft_location = _location(message)
    draft_dates = _date_mentions(message, now)
    if has_draft and not _is_question(message) and (
        _time_mentions(message)
        or draft_location.get("city")
        or _location_phrase(message)
        or message.strip() in TIME_PERIODS
        or (draft_dates and _is_draft_followup(message, now))
    ):
        return "CREATE_EVENT"
    if any(word in message for word in [
        "改成", "改到", "改為", "改期", "改時間", "改地點", "改在", "修改", "更新行程",
        "延到", "延後", "延期", "提早", "提前", "挪到", "換到", "移到",
    ]):
        return "UPDATE_EVENT"
    if _is_existing_event_location_update(message):
        return "UPDATE_EVENT"
    if _is_event_query(message):
        return "QUERY_EVENT"
    if any(word in message for word in ["昨天", "前天", "上週", "上周", "剛剛", "已經", "之前"]):
        return "GENERAL_CHAT"
    if any(word in message for word in ["如果", "假如", "要是", "希望", "不要", "不用"]):
        return "GENERAL_CHAT"
    explicit_create_prefix = bool(re.match(
        r"^(?:可以|可不可以|能不能)?\s*(?:請)?\s*(?:記得提醒我|幫我提醒|提醒我|提醒一下|叫我|新增|加入|加行程|加一筆|排行程|安排|記下|幫我排|建立行程)",
        message,
    ))
    if explicit_create_prefix:
        return "CREATE_EVENT"
    if any(word in message for word in ["颱風", "台風", "地震", "防災", "淹水", "洪水", "避難", "豪雨"]) and any(
        wording in message for wording in ["怎麼分", "怎麼辦", "如何", "是什麼", "分幾種", "要做什麼", "可以去", "安全嗎", "注意什麼"]
    ):
        return "DISASTER_INFO"
    if _has_weather_question(message):
        return "EVENT_WEATHER" if _activity(message) else "QUERY_WEATHER"
    if any(word in message for word in ["很累", "好累", "很忙", "好忙", "放假", "心情"]) and not _activity(message):
        return "GENERAL_CHAT"
    if any(word in message for word in ["颱風", "台風", "地震", "防災", "淹水", "洪水", "避難", "豪雨"]):
        return "DISASTER_INFO"
    activity = _activity(message)
    explicit_create = bool(re.search(r"提醒我|幫我提醒|記得提醒我|提醒一下|叫我|新增|加入|加行程|加一筆|排行程|安排|記下|幫我排|建立行程", message))
    if _is_question(message) and not _time_mentions(message) and not re.match(r"^(?:記得提醒我|幫我提醒|提醒我|提醒一下|叫我)", message):
        return "GENERAL_CHAT"
    if explicit_create:
        return "CREATE_EVENT"
    create_signal = any(word in message for word in ["我要去", "想去", "要去", "幫我排", "安排", "新增", "建立", "提醒我"]) or bool(
        re.search(r"\bremind\s+me\s+to\b", message, re.IGNORECASE)
    )
    dates = _date_mentions(message, now)
    times = _time_mentions(message)
    location = _location(message)
    destination_signal = bool(re.search(r"(?:去|到|前往|回)\s*[^，。！？\s]+", message))
    social_signal = bool(activity and message.startswith("跟"))
    scheduled_statement = not _is_question(message) and bool(dates or times) and bool(activity or destination_signal or _draft_title(message))
    if create_signal or social_signal or destination_signal or (activity and not _is_question(message)) or scheduled_statement:
        return "CREATE_EVENT"
    if intent_hint == "CREATE_EVENT" and (
        (has_draft and _is_draft_followup(message, now)) or activity or times or location["city"]
    ):
        return "CREATE_EVENT"
    return "GENERAL_CHAT"


def _event_filter(message: str, now: datetime) -> Dict[str, Optional[str]]:
    dates = _date_mentions(message, now)
    times = _time_mentions(message)
    title = _activity(message)
    if not title:
        match = re.search(r"(?:把|將)?(?:今天|明天|後天|大後天|星期[一二三四五六日天]|週[一二三四五六日天]|禮拜[一二三四五六日天])?的?([^，。！？]+?)(?:取消|刪掉|刪除|改成|改到|改為)", message)
        title = _text(match.group(1)) if match else None
    if title in {"那個", "那筆", "這個", "這筆", "它"}:
        title = None
    return {
        "date": dates[0][1] if dates else None,
        "title_keyword": title,
        "time": times[0][1] if times and not any(word in message for word in ["改成", "改到", "改為"]) else None,
    }


def _draft_title(message: str) -> Optional[str]:
    activity = _activity(message)
    if activity:
        return activity
    return_place = re.search(r"(?:^|要|會|想|[，,\s])回\s*([^，。！？!?\s]+)", message)
    if return_place:
        return f"回{return_place.group(1)}"
    place = _location_phrase(message)
    if place and re.search(r"(?:去|到|前往|回)\s*" + re.escape(place), message):
        title_place = place[1:] if place.startswith("我") and len(place) > 1 else place
        return f"回{title_place}" if re.search(r"回\s*" + re.escape(place), message) else f"去{title_place}"
    text = message
    text = re.sub(r"記得提醒我|幫我提醒|可以?幫我|請幫我|提醒一下|提醒我|叫我|記得|幫我|我要|我該|我得|我想|想要|想|安排|新增|加入|建立|排行程|排|等一下|待會", " ", text)
    text = re.sub(r"明年|明後天|大後天|後天|明天|明早|明晚|今天|今早|今晚|等一下|這個月|(?:下|這|本)?(?:星期|禮拜|週|周)[一二三四五六日天]|這週末|本週末", " ", text)
    text = re.sub(r"\btomorrow\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\d{1,2}\s*(?:/|月)\s*\d{1,2}\s*(?:日|號|号)?", " ", text)
    text = re.sub(r"(?<!\d)\d{1,2}\s*(?:日|號|号)", " ", text)
    text = re.sub(
        r"(?:從\s*)?(?:(?:早上|上午|中午|下午|晚上|凌晨|傍晚|半夜|深夜)\s*)?(?:十一|十二|十|[一二兩三四五六七八九]|\d{1,2})\s*(?:點|時)?\s*(?:半|[零〇一二兩三四五六七八九十]{1,3}|\d{1,2})?\s*(?:分)?\s*(?:到|至|~|～|－|-|—)\s*(?:(?:早上|上午|中午|下午|晚上|凌晨|傍晚|半夜|深夜)\s*)?(?:十一|十二|十|[一二兩三四五六七八九]|\d{1,2})\s*(?:點|時)\s*(?:半|[零〇一二兩三四五六七八九十]{1,3}|\d{1,2})?\s*(?:分)?",
        " ", text,
    )
    text = re.sub(r"(?:早上|上午|中午|下午|晚上|凌晨|傍晚|半夜|深夜)?\s*(?:十一|十二|十|[一二兩三四五六七八九]|\d{1,2})\s*(?:點|時|:|：)\s*(?:半|[零〇一二兩三四五六七八九十]{1,3}|\d{1,2})?\s*(?:分)?", " ", text)
    text = re.sub(r"\bat\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", " ", text, flags=re.IGNORECASE)
    location = _location(message)
    for value in [location.get("location"), location.get("district"), location.get("city")]:
        if value:
            text = text.replace(value, " ").replace(value.replace("臺", "台"), " ")
            if value.endswith(("市", "縣")):
                text = text.replace(value[:-1], " ")
    if place:
        text = text.replace(place, " ")
    text = re.sub(r"有沒有|是不是|會不會|要不要|能不能|可不可以|為什麼|什麼|幾點|要去|前往|到|去|在|的|嗎|呢|[？?！!，。]", " ", text)
    text = re.sub(r"(^|\s)要(?=\S)", r"\1", text)
    text = re.sub(r"^[\s「『\"']*(?:我|我們)[\s「『\"']*", "", text)
    text = text.strip(" \t\r\n「」『』\"'，。！？!?：:")
    title = _text(text) or None
    if title and len(title.replace(" ", "")) <= 1:
        return None
    return title


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
        "time_guessed": False,
        "repeat": "none",
        "events": None,
    }


def validate_chat_v2_response(result: Dict[str, Any], assistant_name: str = "小藍") -> Dict[str, Any]:
    """Enforce the public v2 contract before a response reaches mobile clients."""
    errors: List[str] = []
    required = {
        "status", "contract_version", "intent", "is_question", "needs_clarification",
        "missing_fields", "reply", "draft_id",
    }
    if required - set(result):
        errors.append("missing_required_fields")
    if result.get("status") != "success":
        errors.append("invalid_status")
    if result.get("contract_version") != 2:
        errors.append("invalid_contract_version")

    intent = result.get("intent")
    if intent not in VALID_INTENTS:
        errors.append("invalid_intent")
    if result.get("repeat", "none") not in {"none", "daily", "weekly", "monthly"}:
        errors.append("invalid_repeat")
    if result.get("events") is not None and not isinstance(result.get("events"), list):
        errors.append("invalid_events")

    missing = result.get("missing_fields")
    if not isinstance(missing, list) or any(field not in VALID_MISSING_FIELDS for field in missing):
        errors.append("invalid_missing_fields")
        missing = []
    result["missing_fields"] = missing
    result["needs_clarification"] = bool(missing)

    if not _text(result.get("reply")):
        errors.append("empty_reply")

    attachment_keys = {"draft_event", "event_filter", "changes", "entities"}
    allowed_attachments = {
        "CREATE_EVENT": {"draft_event"},
        "UPDATE_EVENT": {"event_filter", "changes"},
        "DELETE_EVENT": {"event_filter"},
        "QUERY_EVENT": {"entities"},
        "QUERY_WEATHER": {"entities"},
        "EVENT_WEATHER": {"entities"},
        "DISASTER_INFO": set(),
        "GENERAL_CHAT": set(),
    }.get(intent, set())
    if any(result.get(key) is not None for key in attachment_keys - allowed_attachments):
        errors.append("unexpected_attachment")
    if intent == "CREATE_EVENT" and not isinstance(result.get("draft_event"), dict):
        errors.append("missing_draft_event")
    if intent in {"UPDATE_EVENT", "DELETE_EVENT"} and not isinstance(result.get("event_filter"), dict):
        errors.append("missing_event_filter")
    if intent in {"QUERY_EVENT", "QUERY_WEATHER", "EVENT_WEATHER"} and not isinstance(result.get("entities"), dict):
        errors.append("missing_entities")

    draft = result.get("draft_event")
    if isinstance(draft, dict):
        for field in ["title", "date", "start_time", "end_time", "city", "district", "location"]:
            if draft.get(field) in ("", "null", "無", "未知"):
                draft[field] = None
        date_value = draft.get("date")
        if date_value:
            try:
                datetime.strptime(str(date_value), "%Y-%m-%d")
            except ValueError:
                errors.append("invalid_date")
        for field in ["end_date"]:
            value = draft.get(field)
            if value:
                try:
                    datetime.strptime(str(value), "%Y-%m-%d")
                except ValueError:
                    errors.append(f"invalid_{field}")
        if "dates" in draft:
            values = draft.get("dates")
            if not isinstance(values, list):
                errors.append("invalid_dates")
            else:
                for value in values:
                    try:
                        datetime.strptime(str(value), "%Y-%m-%d")
                    except ValueError:
                        errors.append("invalid_dates")
                        break
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
                district = draft.get("district")
                if district and district not in TAIWAN_LOCATIONS[normalized_city]:
                    errors.append("invalid_district")

    entities = result.get("entities")
    if isinstance(entities, dict):
        for field in ["start_date", "end_date"]:
            value = entities.get(field)
            if value:
                try:
                    datetime.strptime(str(value), "%Y-%m-%d")
                except ValueError:
                    errors.append(f"invalid_entities_{field}")
        entity_time = entities.get("time")
        if entity_time and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(entity_time)):
            errors.append("invalid_entities_time")
        entity_city = entities.get("city")
        if entity_city:
            normalized_city = str(entity_city).replace("台", "臺")
            if normalized_city not in TAIWAN_LOCATIONS:
                errors.append("invalid_entities_city")
            else:
                entities["city"] = normalized_city

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


def _entity_dates(message: str, now: datetime, default_today: bool = False) -> Tuple[Optional[str], Optional[str]]:
    weekend = _weekend_range(message, now)
    if weekend:
        return weekend
    day_count_match = re.search(r"(?:未來|接下來|往後)?\s*([2-8二兩三四五六七八])\s*天", message)
    if day_count_match:
        day_count = {
            "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8,
        }.get(day_count_match.group(1), int(day_count_match.group(1)) if day_count_match.group(1).isdigit() else 3)
        return now.strftime("%Y-%m-%d"), (now + timedelta(days=day_count - 1)).strftime("%Y-%m-%d")
    if any(word in message for word in [
        "這週", "本週", "一週", "未來一週", "七天", "未來七天", "未來幾天",
        "雨什麼時候", "雨會下到", "雨要下多久", "什麼時候會下雨",
        "什麼時候回溫", "什麼時候會回溫", "下次變冷", "下次降溫", "下次寒流",
    ]):
        return now.strftime("%Y-%m-%d"), (now + timedelta(days=6)).strftime("%Y-%m-%d")
    dates = _date_mentions(message, now)
    value = dates[0][1] if dates else (now.strftime("%Y-%m-%d") if default_today else None)
    return value, value


def build_chat_v2_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    message = _text(payload.get("message"))
    assistant_name = " ".join(str(payload.get("assistant_name") or "").split())[:24] or "小藍"
    now = _client_now(payload.get("client_now"))
    existing_draft = dict(payload.get("draft_event") or payload.get("pending_card") or {})
    intent = _infer_intent(message, payload.get("intent_hint"), now, bool(existing_draft))
    draft_id = payload.get("draft_id") or (f"draft-{uuid.uuid4().hex}" if intent == "CREATE_EVENT" else None)
    response = _base_response(intent, message, draft_id)
    response["assistant_name"] = assistant_name

    if intent == "CREATE_EVENT":
        if _has_multiple_event_actions(message):
            response.update({
                "draft_event": {
                    "title": None,
                    "date": None,
                    "start_time": None,
                    "end_time": None,
                    "all_day": False,
                    "is_reminder": False,
                    "city": None,
                    "district": None,
                    "location": None,
                    "location_optional": True,
                    "location_requires_city": False,
                },
                "events": [],
                "missing_fields": ["title"],
                "needs_clarification": True,
                "reply": "我聽到兩件行程，為了避免漏掉時間或地點，請一次告訴我一件事。",
            })
            return response
        existing = existing_draft
        repeat = _repeat_kind(message)
        response["repeat"] = repeat
        message_activity = _activity(message)
        if existing.get("title") and message_activity and message_activity not in str(existing.get("title") or ""):
            existing = {}
            draft_id = f"draft-{uuid.uuid4().hex}"
            response["draft_id"] = draft_id
        dates = _date_mentions(message, now)
        times = _time_mentions(message)
        if dates:
            draft_date = dates[0][1]
        elif existing.get("date"):
            draft_date = existing.get("date")
        elif _has_date_wording(message):
            draft_date = None
        else:
            draft_date = now.strftime("%Y-%m-%d")
        if repeat != "none" and draft_date:
            while datetime.strptime(str(draft_date), "%Y-%m-%d").date() < now.date():
                draft_date = _advance_repeat_date(str(draft_date), repeat)
        time_needs_clarification = _time_requires_clarification(message)
        ambiguous_time_in_original_message = bool(times) and time_needs_clarification
        past_time = False
        period_only = _normalize_period(message.strip(), 1) if message.strip() in TIME_PERIODS else ""
        remembered_times = list(payload.get("_ambiguous_times") or AMBIGUOUS_DRAFT_TIMES.get(str(draft_id or ""), []))
        if existing and period_only and remembered_times:
            resolved_times = [_clock_with_period(value, period_only) for value in remembered_times]
            times = [((0, len(message)), value) for value in resolved_times if value]
            times, time_needs_clarification, past_time = _resolve_ambiguous_times(
                message,
                times,
                now,
                str(draft_date or now.date().isoformat()),
            )
            AMBIGUOUS_DRAFT_TIMES.pop(str(draft_id), None)
        else:
            times, time_needs_clarification, past_time = _resolve_ambiguous_times(
                message,
                times,
                now,
                str(draft_date or now.date().isoformat()),
            )
            if time_needs_clarification and times:
                _remember_ambiguous_times(draft_id, times)
                response["_ambiguous_times"] = [value for _, value in times]
        if repeat != "none" and past_time and draft_date:
            draft_date = _advance_repeat_date(str(draft_date), repeat)
            times, time_needs_clarification, past_time = _resolve_ambiguous_times(
                message,
                _time_mentions(message),
                now,
                str(draft_date),
            )
        response["time_guessed"] = bool(
            ambiguous_time_in_original_message
            and times
            and not time_needs_clarification
            and not past_time
        )
        has_time_wording = bool(times) or time_needs_clarification
        is_reminder = bool(existing.get("is_reminder")) or bool(re.search(r"提醒我|幫我提醒|記得提醒我|提醒一下|叫我", message)) or bool(
            re.search(r"\bremind\s+me\s+to\b", message, re.IGNORECASE)
        )
        current_location = _location(_text(payload.get("current_location"))) if payload.get("current_location") else {}
        preferred_city = existing.get("city") or current_location.get("city")
        location = _location(message, preferred_city)
        location_source = "message" if any(location.get(key) for key in ["city", "district", "location"]) else None
        explicit_location = _explicit_location_name(message)
        title = _draft_title(message)
        generic_location = _generic_location_name(message, title)
        if (
            existing.get("title")
            and not _activity(message)
            and not dates
            and _is_draft_followup(message, now)
            and (
                times
                or period_only
                or location.get("city")
                or location.get("location")
                or payload.get("intent_hint") == "CREATE_EVENT"
            )
        ):
            title = None
        if explicit_location:
            location["location"] = explicit_location
        elif generic_location:
            location["location"] = generic_location
        elif existing and not dates and not times and not _activity(message) and not any(period in message for period in TIME_PERIODS) and _is_draft_followup(message, now):
            location["location"] = message
            location_source = "message"
        if (
            not any(location.get(key) for key in ["city", "district", "location"])
            and not any(existing.get(key) for key in ["city", "district", "location"])
            and current_location.get("city")
        ):
            location = {
                "city": current_location.get("city"),
                "district": current_location.get("district"),
                "location": _text(payload.get("current_location")) or current_location.get("location"),
            }
            location_source = "current_location"
        existing_start = existing.get("start_time")
        if times and not time_needs_clarification:
            start_time = times[0][1]
            duration = _duration_minutes(message)
            end_time = (
                times[1][1]
                if len(times) > 1
                else (_add_minutes(start_time, duration) if duration else existing.get("end_time"))
            )
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
            "date": draft_date,
            "start_time": start_time,
            "end_time": end_time,
            "all_day": all_day,
            "is_reminder": is_reminder,
            "city": location["city"] if location["city"] is not None else existing.get("city"),
            "district": location["district"] if location["city"] is not None else existing.get("district"),
            "location": location["location"] if location["location"] is not None else existing.get("location"),
            "location_source": location_source or existing.get("location_source"),
        }
        unique_dates = list(dict.fromkeys(value for _, value in dates))
        if len(unique_dates) > 1:
            date_spans = sorted(dates, key=lambda item: item[0][0])
            has_range_separator = "明後天" in message or any(
                re.search(r"(?:到|至|~|～|－|-)", message[left[0][1]:right[0][0]])
                for left, right in zip(date_spans, date_spans[1:])
            )
            if has_range_separator:
                draft["end_date"] = unique_dates[-1]
            else:
                draft["dates"] = unique_dates
        draft["location_optional"] = True
        draft["location_requires_city"] = False
        missing = [key for key in ["title", "date"] if not draft.get(key)]
        if (has_time_wording or existing_start) and not draft.get("start_time"):
            missing.append("start_time")
        if missing:
            first_missing = missing[0]
            if first_missing == "title":
                reply = "要記成什麼行程呢？"
            elif first_missing == "date":
                reply = "哪一天呢？"
            elif first_missing == "start_time" and past_time:
                reply = "這個時間今天已經過了，要改成幾點呢？"
            elif first_missing == "start_time" and times and time_needs_clarification:
                reply = "要改成幾點呢？"
            elif first_missing == "start_time":
                period = next((period for period in TIME_PERIODS if period in message), "")
                reply = f"{period}幾點呢？" if period else "幾點呢？"
            elif first_missing == "city" and draft.get("location"):
                reply = f"是哪一間 {draft['location']} 呢？在哪個縣市、哪一區？"
            else:
                reply = "要去哪裡呢？（縣市、行政區或地標都可以）"
        else:
            reply = "我幫你整理好了，確認後就會加入行事曆。"
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
        elif dates:
            change_marker = max((message.rfind(word) for word in ["改到", "改成", "改為", "改期", "延期", "延到", "挪到", "移到"]), default=-1)
            if change_marker >= 0 and dates[0][0][0] > change_marker:
                changes["date"] = dates[0][1]
                event_filter["date"] = None
        if times:
            target_date = changes.get("date") or event_filter.get("date") or (now + timedelta(days=1)).strftime("%Y-%m-%d")
            resolved_times, needs_time, past_time = _resolve_ambiguous_times(message, [times[-1]], now, target_date)
            if not needs_time and resolved_times:
                changes["start_time"] = resolved_times[0][1]
            elif past_time:
                response["missing_fields"] = ["start_time"]
                response["reply"] = "這個時間今天已經過了，要改成幾點呢？"
        title_change = re.search(r"(?:名稱|標題)\s*(?:改成|改為|改到)\s*([^，。！？!?]+)", message)
        if title_change:
            changes["title"] = _text(title_change.group(1)).strip(" \"'「」『』") or None
        if _is_existing_event_location_update(message) or any(word in message for word in ["地點", "改在", "換到", "移到"]):
            for key, value in change_location.items():
                if value is not None:
                    changes[key] = value
        if not any(event_filter.values()):
            response["missing_fields"] = ["target"]
            response["needs_clarification"] = True
            response["reply"] = "你想修改哪一筆行程呢？"
        elif not response.get("missing_fields"):
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
        start_date, end_date = _entity_dates(message, now, default_today=True)
        location = _location(message)
        activity = _activity(message) if intent in {"QUERY_EVENT", "EVENT_WEATHER"} else None
        times = _time_mentions(message)
        response["entities"] = {
            "start_date": start_date,
            "end_date": end_date,
            "time": times[0][1] if times else None,
            "city": location["city"],
            "district": location["district"],
            "activity": activity,
        }
        response["reply"] = {
            "QUERY_EVENT": "我正在查行程，請稍等一下。",
            "QUERY_WEATHER": "我正在查天氣，請稍等一下。",
            "EVENT_WEATHER": "我正在比對行程和天氣，請稍等一下。",
        }[intent]
        return response

    if intent == "DISASTER_INFO":
        if "地震" in message:
            response["reply"] = "地震時先趴下、掩護、穩住，遠離玻璃與高櫃；搖晃停止後再依官方指示疏散。"
        elif "颱風" in message or "台風" in message:
            if any(word in message for word in ["海邊", "看浪", "觀浪", "戲水"]):
                response["reply"] = "颱風期間不要前往海邊、港口、堤防或河口看浪、戲水。長浪與突發大浪可能把人捲入海中，請留在安全室內並遵守海岸封閉與地方政府警戒。"
            else:
                response["reply"] = (
                    "中央氣象署的颱風警報分為海上與陸上兩種：預測颱風七級風暴風範圍可能在 24 小時內侵襲"
                    "臺灣本島、澎湖、金門或馬祖 100 公里內海域時，發布海上颱風警報並列出警戒海域；"
                    "預測可能在 18 小時內侵襲上述地區陸地時，發布陸上颱風警報並列出警戒縣市。"
                    "警報發布後仍要持續查看氣象署更新、地方政府停班停課與疏散通知。"
                )
        else:
            response["reply"] = "請先確認官方警報，避開危險區域並準備飲水、藥品、證件與行動電源。"
        return response

    if _is_usage_help(message):
        response["reply"] = _usage_help_reply(message)
        response["_skip_gemini"] = True
    else:
        response["reply"] = (
            "好的，這次不建立草稿。"
            if "不用了" in message or message in {"算了", "取消", "先不用"}
            else "我沒聽懂，可以換個說法嗎？例如：明天下午三點在臺南火車站開會"
        )
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
