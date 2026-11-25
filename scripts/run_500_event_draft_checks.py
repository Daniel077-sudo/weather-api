import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_contract_v2 import build_chat_v2_response, validate_chat_v2_response
from data import TAIWAN_LOCATIONS


CLIENT_NOW = "2026-11-25T14:00:00+08:00"
ACTIVITIES = [
    "看牙醫", "開會", "上課", "上班", "吃飯", "看電影", "游泳", "跑步", "爬山", "打球",
    "看展", "辦護照", "聚餐", "買菜", "交報告", "繳電費", "吃藥", "值班", "面試", "報到",
]
PATTERNS = [
    ("明天去{place}{activity}", "2026-11-26", None, None, True, False),
    ("後天下午三點在{place}{activity}", "2026-11-27", "15:00", None, False, False),
    ("提醒我明天早上九點去{place}{activity}", "2026-11-26", "09:00", None, False, True),
    ("明天晚上七點到九點在{place}{activity}", "2026-11-26", "19:00", "21:00", False, False),
]
SPECIAL_PLACES = [
    "臺北火車站", "板橋火車站", "南港火車站", "桃園火車站", "中壢火車站", "新竹火車站",
    "苗栗火車站", "臺中火車站", "彰化火車站", "斗六火車站", "嘉義火車站", "臺南火車站",
    "高雄火車站", "屏東火車站", "宜蘭火車站", "花蓮火車站", "臺東火車站", "瑞芳火車站",
    "南港高鐵站", "臺北高鐵站", "板橋高鐵站", "桃園高鐵站", "新竹高鐵站", "苗栗高鐵站",
    "臺中高鐵站", "彰化高鐵站", "雲林高鐵站", "嘉義高鐵站", "臺南高鐵站", "左營高鐵站",
    "西門捷運站", "臺北車站捷運站", "忠孝復興捷運站", "市政府捷運站", "淡水捷運站",
    "新埔捷運站", "景安捷運站", "頂溪捷運站", "大安森林公園捷運站", "象山捷運站",
    "小琉球", "墾丁", "綠島", "蘭嶼", "日月潭", "清境", "合歡山", "武嶺", "阿里山",
    "太魯閣", "九份", "小金門", "馬公", "成大", "臺南新光三越西門店", "臺北101",
    "高雄展覽館", "國立臺南大學", "臺中國家歌劇院", "臺北市立動物園", "高雄駁二藝術特區",
    "臺南市中西區全聯", "臺北市信義區星巴克", "高雄市左營區新光三越", "新北市板橋區郵局",
    "桃園市桃園區圖書館", "新竹市東區文化中心",
]


def make_case(message, activity, expected_date, start_time, end_time, all_day, is_reminder, **extra):
    return {
        "message": message,
        "activity": activity,
        "date": expected_date,
        "start_time": start_time,
        "end_time": end_time,
        "all_day": all_day,
        "is_reminder": is_reminder,
        **extra,
    }


def build_cases():
    cases = []
    index = 0
    for city, districts in TAIWAN_LOCATIONS.items():
        for district in districts:
            activity = ACTIVITIES[index % len(ACTIVITIES)]
            pattern, date, start, end, all_day, reminder = PATTERNS[index % len(PATTERNS)]
            place = f"{city}{district}"
            cases.append(make_case(
                pattern.format(place=place, activity=activity), activity, date, start, end, all_day, reminder,
                city=city, district=district, location=place, category="district",
            ))
            index += 1

    special_index = 0
    while len(cases) < 500:
        place = SPECIAL_PLACES[special_index % len(SPECIAL_PLACES)]
        activity = ACTIVITIES[(special_index + 7) % len(ACTIVITIES)]
        pattern, date, start, end, all_day, reminder = PATTERNS[(special_index + 1) % len(PATTERNS)]
        cases.append(make_case(
            pattern.format(place=place, activity=activity), activity, date, start, end, all_day, reminder,
            location=place, category="named_place",
        ))
        special_index += 1
    return cases[:500]


def check_case(case):
    payload = {
        "message": case["message"],
        "contract_version": 2,
        "client_now": CLIENT_NOW,
        "draft_event": None,
        "pending_card": None,
    }
    result = validate_chat_v2_response(build_chat_v2_response(payload), "小藍")
    draft = result.get("draft_event") or {}
    errors = []
    if result.get("intent") != "CREATE_EVENT":
        errors.append(f"intent={result.get('intent')}")
    if not draft:
        errors.append("missing draft_event")
        return errors, result
    if draft.get("title") != case["activity"]:
        errors.append(f"title={draft.get('title')!r}, expected={case['activity']!r}")
    for key in ["date", "start_time", "end_time", "all_day", "is_reminder"]:
        if draft.get(key) != case[key]:
            errors.append(f"{key}={draft.get(key)!r}, expected={case[key]!r}")
    if case["category"] == "district":
        for key in ["city", "district"]:
            if draft.get(key) != case[key]:
                errors.append(f"{key}={draft.get(key)!r}, expected={case[key]!r}")
    location = str(draft.get("location") or "")
    if case["category"] == "named_place" and case["location"] not in location:
        errors.append(f"location={location!r}, expected to preserve={case['location']!r}")
    if result.get("needs_clarification") or result.get("missing_fields"):
        errors.append(f"unexpected clarification={result.get('missing_fields')}")
    reply = str(result.get("reply") or "")
    if "確認後" not in reply:
        errors.append(f"reply={reply!r}")
    if any(text in reply for text in ["已加入", "已新增", "記好了", "設定好了"]):
        errors.append(f"false success reply={reply!r}")
    return errors, result


def main():
    cases = build_cases()
    failures = []
    categories = Counter(case["category"] for case in cases)
    for index, case in enumerate(cases, 1):
        errors, result = check_case(case)
        if errors:
            failures.append({"index": index, "case": case, "errors": errors, "result": result})

    summary = {
        "total": len(cases),
        "passed": len(cases) - len(failures),
        "failed": len(failures),
        "categories": dict(categories),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if failures:
        print("\nFirst 20 failures:")
        print(json.dumps(failures[:20], ensure_ascii=False, indent=2))
        raise SystemExit(1)

    sample_indexes = [0, 99, 199, 299, 399, 499]
    print("\nSamples:")
    for index in sample_indexes:
        case = cases[index]
        _, result = check_case(case)
        print(json.dumps({
            "message": case["message"],
            "reply": result.get("reply"),
            "draft_event": result.get("draft_event"),
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
