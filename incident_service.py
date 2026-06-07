import re
from typing import Any, Dict, Iterable, List, Optional

from config import (
    INCIDENT_NOTIFY_CONFIDENCE,
    supabase,
    THREADS_SCAN_KEYWORDS,
    THREADS_SCAN_MAX_KEYWORDS,
    THREADS_SCAN_POST_LIMIT,
    THREADS_SCAN_REPLY_LIMIT,
    THREADS_PROVIDER,
)
from threads_service import fetch_threads_replies, search_threads_keyword, threads_is_configured
from utils import safe_response, stable_hash, taipei_now


INCIDENT_TYPE_KEYWORDS = {
    "fire": ["火災", "濃煙", "爆炸", "消防車", "瓦斯外洩"],
    "traffic_accident": ["車禍", "事故", "警車", "救護車", "塞車"],
    "flood": ["淹水", "積水", "道路封閉"],
    "power_outage": ["停電"],
}

CITY_DISTRICT_PATTERN = re.compile(
    r"(?:台北|臺北|新北|桃園|台中|臺中|台南|臺南|高雄|基隆|新竹|苗栗|彰化|南投|雲林|嘉義|屏東|宜蘭|花蓮|台東|臺東|澎湖|金門|連江)[市縣]"
    r"[\u4e00-\u9fff]{1,4}(?:區|鄉|鎮|市)|"
    r"(?:台北|臺北|新北|桃園|台中|臺中|台南|臺南|高雄|基隆|新竹|苗栗|彰化|南投|雲林|嘉義|屏東|宜蘭|花蓮|台東|臺東|澎湖|金門|連江)[市縣]"
)
LANDMARK_PATTERN = re.compile(
    r"[\u4e00-\u9fff]{2,12}(?:路|街|大道|巷|橋|交流道|車站|捷運站|大學|高中|國小|商圈|夜市)"
)


def incident_keywords() -> List[str]:
    return [item.strip() for item in THREADS_SCAN_KEYWORDS.split(",") if item.strip()][:THREADS_SCAN_MAX_KEYWORDS]


def classify_incident_type(text: str) -> str:
    for incident_type, keywords in INCIDENT_TYPE_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return incident_type
    return "unknown"


def extract_locations(texts: Iterable[str]) -> List[str]:
    locations: List[str] = []
    seen = set()
    for text in texts:
        for pattern in (CITY_DISTRICT_PATTERN, LANDMARK_PATTERN):
            for match in pattern.finditer(text or ""):
                value = match.group(0).strip()
                if value and value not in seen:
                    seen.add(value)
                    locations.append(value)
    return locations


def build_confidence(post_text: str, replies: List[Dict[str, Any]], locations: List[str]) -> float:
    evidence_count = 1 + len([reply for reply in replies if reply.get("text")])
    confidence = 0.35
    confidence += min(len(locations), 4) * 0.1
    confidence += min(evidence_count, 8) * 0.03
    if classify_incident_type(post_text) != "unknown":
        confidence += 0.15
    if len(locations) >= 2 and evidence_count >= 3:
        confidence += 0.1
    return min(round(confidence, 2), 0.98)


def build_summary(incident_type: str, locations: List[str], keyword: str) -> str:
    location_text = "、".join(locations[:3]) if locations else "尚未確認地點"
    type_text = {
        "fire": "疑似火災/濃煙",
        "traffic_accident": "疑似交通事故",
        "flood": "疑似淹水/道路受阻",
        "power_outage": "疑似停電",
    }.get(incident_type, f"疑似災情: {keyword}")
    return f"{type_text}，留言與貼文提到：{location_text}。"


def _find_existing(source_url: str, source_id: str) -> Optional[Dict[str, Any]]:
    try:
        query = supabase.table("incident_reports").select("id,status").limit(1)
        if source_url:
            query = query.eq("source_url", source_url)
        else:
            query = query.eq("source_id", source_id)
        res = query.execute()
        return (res.data or [None])[0]
    except Exception:
        return None


def _store_incident(report: Dict[str, Any], evidence: List[Dict[str, Any]]) -> Dict[str, Any]:
    existing = _find_existing(report.get("source_url") or "", report.get("source_id") or "")
    if existing:
        report_id = existing["id"]
        supabase.table("incident_reports").update(report).eq("id", report_id).execute()
    else:
        inserted = supabase.table("incident_reports").insert(report).execute()
        report_id = inserted.data[0]["id"] if inserted.data else None

    for item in evidence:
        item["incident_id"] = report_id
    if evidence and report_id:
        supabase.table("incident_evidence").insert(evidence).execute()

    if report.get("confidence", 0) >= INCIDENT_NOTIFY_CONFIDENCE and report_id:
        supabase.table("incident_notifications").insert({
            "incident_id": report_id,
            "status": "queued",
            "created_at": taipei_now().isoformat(),
        }).execute()

    return {**report, "id": report_id}


async def scan_threads_incidents() -> Dict[str, Any]:
    response_source = "bing" if THREADS_PROVIDER == "bing" else "threads"
    if not threads_is_configured():
        return safe_response(
            "not_configured",
            {"incidents": [], "keywords": incident_keywords()},
            "THREADS_ACCESS_TOKEN is missing or THREADS_PROVIDER is not official",
            response_source,
        )

    incidents = []
    errors = []
    checked_posts = 0

    for keyword in incident_keywords():
        search = await search_threads_keyword(keyword, THREADS_SCAN_POST_LIMIT)
        if search["status"] != "success":
            errors.extend(search.get("errors") or [{"keyword": keyword, "message": search.get("message")}])
            continue

        for post in search["data"]:
            checked_posts += 1
            if isinstance(post.get("_replies"), list):
                replies = post["_replies"][:THREADS_SCAN_REPLY_LIMIT]
            else:
                replies_result = await fetch_threads_replies(post.get("source_id") or "", THREADS_SCAN_REPLY_LIMIT)
                replies = replies_result.get("data") or []
                if replies_result["status"] == "error":
                    errors.extend(replies_result.get("errors") or [])

            texts = [post.get("text") or ""] + [reply.get("text") or "" for reply in replies]
            locations = extract_locations(texts)
            incident_type = classify_incident_type(" ".join(texts))
            confidence = build_confidence(post.get("text") or "", replies, locations)
            if confidence < 0.55:
                continue

            source_url = post.get("source_url") or ""
            source_id = post.get("source_id") or stable_hash(post)
            report = {
                "source": "threads",
                "source_id": source_id,
                "source_url": source_url,
                "incident_type": incident_type,
                "title": build_summary(incident_type, locations, keyword),
                "summary": build_summary(incident_type, locations, keyword),
                "location_text": locations[0] if locations else "",
                "locations": locations,
                "confidence": confidence,
                "evidence_count": 1 + len(replies),
                "status": "confirmed" if confidence >= INCIDENT_NOTIFY_CONFIDENCE else "candidate",
                "raw": {"post": post, "reply_count": len(replies), "keyword": keyword},
                "last_seen_at": taipei_now().isoformat(),
            }
            evidence = [
                {
                    "source": "threads_post",
                    "source_id": source_id,
                    "source_url": source_url,
                    "text": post.get("text") or "",
                    "extracted_locations": locations,
                    "raw": post.get("raw") or {},
                    "created_at": taipei_now().isoformat(),
                }
            ]
            evidence.extend([
                {
                    "source": "threads_reply",
                    "source_id": reply.get("source_id") or "",
                    "source_url": source_url,
                    "text": reply.get("text") or "",
                    "extracted_locations": extract_locations([reply.get("text") or ""]),
                    "raw": reply.get("raw") or {},
                    "created_at": taipei_now().isoformat(),
                }
                for reply in replies[:THREADS_SCAN_REPLY_LIMIT]
            ])

            try:
                incidents.append(_store_incident(report, evidence))
            except Exception as e:
                errors.append({"service": "supabase", "message": str(e), "source_url": source_url})

    status = "success" if not errors else "partial_success"
    return safe_response(
        status,
        {
            "incidents": incidents,
            "keywords": incident_keywords(),
            "checked_posts": checked_posts,
            "created_or_updated": len(incidents),
        },
        "incident scan completed",
        response_source,
        errors,
    )


def list_incidents(limit: int = 20, city: Optional[str] = None, incident_type: Optional[str] = None) -> Dict[str, Any]:
    try:
        query = supabase.table("incident_reports").select("*").order("last_seen_at", desc=True).limit(limit)
        if incident_type:
            query = query.eq("incident_type", incident_type)
        if city:
            query = query.ilike("location_text", f"%{city}%")
        res = query.execute()
        return safe_response("success", res.data or [], "incidents loaded", "incident_reports")
    except Exception as e:
        return safe_response("error", [], str(e), "incident_reports", [{"service": "supabase", "message": str(e)}])


def get_incident(incident_id: int) -> Dict[str, Any]:
    try:
        report = supabase.table("incident_reports").select("*").eq("id", incident_id).limit(1).execute()
        evidence = supabase.table("incident_evidence").select("*").eq("incident_id", incident_id).order("created_at", desc=True).execute()
        data = {"report": (report.data or [None])[0], "evidence": evidence.data or []}
        return safe_response("success", data, "incident loaded", "incident_reports")
    except Exception as e:
        return safe_response("error", {"id": incident_id}, str(e), "incident_reports", [{"service": "supabase", "message": str(e)}])
