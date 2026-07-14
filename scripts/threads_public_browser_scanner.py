import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx

from data import TAIWAN_LOCATIONS
from gemini_service import call_gemini_vision
from incident_service import (
    build_confidence,
    build_summary,
    classify_incident_type,
    cleanup_expired_incidents,
    extract_locations,
    infer_city_district,
    incident_expires_at,
    incident_keywords,
    _store_incident,
)
from threads_service import _is_threads_url, _normalize_duckduckgo_url, _search_bing_keyword, _search_duckduckgo_keyword
from config import INCIDENT_NOTIFY_CONFIDENCE, THREADS_SCAN_POST_LIMIT
from utils import safe_response, stable_hash, taipei_now


THREADS_SEARCH_URL = "https://www.threads.com/search?q={query}"
THREADS_AUTH_STATE_PATH = Path(os.getenv("THREADS_AUTH_STATE_PATH", "storage/threads_auth_state.json"))
ABSOLUTE_DATE_PATTERN = re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})")
MONTH_DAY_PATTERN = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日")
RECENT_TIME_PATTERN = re.compile(r"(\d+)\s*(秒|分鐘|分|小時|天|hours?|hrs?|minutes?|mins?|days?)")


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def auth_state_path() -> Optional[str]:
    path = THREADS_AUTH_STATE_PATH
    if path.exists():
        return str(path)
    return None


async def new_threads_context(browser):
    kwargs = {
        "locale": "zh-TW",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
    }
    state_path = auth_state_path()
    if state_path:
        kwargs["storage_state"] = state_path
    return await browser.new_context(**kwargs)


def has_city_or_district_keyword(text: str) -> bool:
    text = clean_text(text)
    if not text:
        return False
    locations = extract_locations([text])
    location_parts = infer_city_district([text], locations)
    return bool(location_parts.get("city") or location_parts.get("district"))


def city_search_term(city: str) -> str:
    city = clean_text(city)
    return city.replace("臺", "台").removesuffix("市").removesuffix("縣") or city


def location_terms(city: str = "", district: str = "") -> List[str]:
    terms = []
    if city:
        terms.extend([city, city.replace("臺", "台"), city_search_term(city)])
    if district:
        terms.append(district)
    return [term for index, term in enumerate(terms) if term and term not in terms[:index]]


def title_has_required_location(title: str, city: str = "", district: str = "") -> bool:
    title = clean_text(title)
    if not title:
        return False
    terms = location_terms(city, district)
    if terms and any(term in title for term in terms):
        return True
    return has_city_or_district_keyword(title)


def infer_author_location(raw: Dict[str, Any]) -> Dict[str, Any]:
    candidates = [
        clean_text(raw.get("search_title") or ""),
        clean_text(raw.get("title") or ""),
        clean_text(raw.get("meta_description") or ""),
    ]
    profile_text = clean_text(raw.get("profile_text") or raw.get("author_location") or "")
    if profile_text:
        candidates.insert(0, profile_text)
    locations = extract_locations(candidates)
    parts = infer_city_district(candidates, locations)
    return {
        "city": parts.get("city") or "",
        "district": parts.get("district") or "",
        "locations": locations,
        "source_text": next((item for item in candidates if item), ""),
    }


def build_scoped_keywords(city: str = "", district: str = "", keywords: Optional[List[str]] = None) -> List[str]:
    base_keywords = keywords or incident_keywords()
    place = city_search_term(city) if city else clean_text(district)
    if not place:
        return base_keywords
    places = [place]
    if city and "臺" in city:
        places.append(city.replace("市", "").replace("縣", ""))
    return [
        f"{place_name} {keyword}"
        for keyword in base_keywords
        for place_name in places
        if place_name
    ]


def batch_items(items: List[Any], batch_index: int = 0, batch_total: int = 1) -> List[Any]:
    if batch_total <= 1:
        return items
    batch_index = max(0, batch_index) % batch_total
    return [item for index, item in enumerate(items) if index % batch_total == batch_index]


def build_scan_jobs(
    city: str = "",
    district: str = "",
    keywords: Optional[List[str]] = None,
    scan_scope: str = "single",
    batch_index: int = 0,
    batch_total: int = 1,
) -> List[Dict[str, Any]]:
    base_keywords = keywords or incident_keywords()
    scan_scope = (scan_scope or "single").lower()

    if scan_scope == "keywords":
        selected_keywords = batch_items(base_keywords, batch_index, batch_total)
        return [{
            "city": city,
            "district": district,
            "keywords": build_scoped_keywords(city, district, selected_keywords),
        }]

    if scan_scope == "regions" and not city:
        selected_cities = batch_items(list(TAIWAN_LOCATIONS.keys()), batch_index, batch_total)
        return [
            {
                "city": target_city,
                "district": "",
                "keywords": build_scoped_keywords(target_city, "", base_keywords),
            }
            for target_city in selected_cities
        ]

    return [{
        "city": city,
        "district": district,
        "keywords": build_scoped_keywords(city, district, base_keywords),
    }]


def infer_post_date(text: str, now: Optional[datetime] = None) -> Tuple[Optional[str], str]:
    text = clean_text(text)
    now = now or taipei_now()
    today = now.date()
    if not text:
        return None, "no_text"

    absolute = ABSOLUTE_DATE_PATTERN.search(text)
    if absolute:
        year, month, day = [int(part) for part in absolute.groups()]
        try:
            return datetime(year, month, day).date().isoformat(), "absolute_date"
        except ValueError:
            return None, "invalid_absolute_date"

    month_day = MONTH_DAY_PATTERN.search(text)
    if month_day:
        month, day = [int(part) for part in month_day.groups()]
        try:
            return datetime(today.year, month, day).date().isoformat(), "month_day"
        except ValueError:
            return None, "invalid_month_day"

    recent = RECENT_TIME_PATTERN.search(text)
    if recent:
        amount = int(recent.group(1))
        unit = recent.group(2)
        if unit in {"秒", "分鐘", "分", "minute", "minutes", "min", "mins"}:
            return today.isoformat(), "relative_minutes"
        if unit in {"小時", "hour", "hours", "hr", "hrs"} and amount <= 24:
            return today.isoformat(), "relative_hours"
        if unit in {"天", "day", "days"}:
            return (today - timedelta(days=amount)).isoformat(), "relative_days"
        return None, "relative_time_not_today"

    if "今天" in text or "剛剛" in text:
        return today.isoformat(), "relative_today"
    if "昨天" in text:
        return (today - timedelta(days=1)).isoformat(), "relative_yesterday"

    return None, "unknown_date"


def is_today_post(text: str, now: Optional[datetime] = None) -> Tuple[bool, Optional[str], str]:
    now = now or taipei_now()
    post_date, reason = infer_post_date(text, now)
    if not post_date:
        return False, post_date, reason
    return post_date == now.date().isoformat(), post_date, reason


def infer_post_age_minutes(text: str, now: Optional[datetime] = None) -> Tuple[Optional[int], Optional[str], str]:
    text = clean_text(text)
    now = now or taipei_now()
    if not text:
        return None, None, "no_text"
    if "剛剛" in text:
        return 0, now.date().isoformat(), "relative_now"
    recent = RECENT_TIME_PATTERN.search(text)
    if recent:
        amount = int(recent.group(1))
        unit = recent.group(2)
        if unit in {"秒"}:
            return 0, now.date().isoformat(), "relative_seconds"
        if unit in {"分鐘", "分", "minute", "minutes", "min", "mins"}:
            return amount, now.date().isoformat(), "relative_minutes"
        if unit in {"小時", "hour", "hours", "hr", "hrs"}:
            return amount * 60, now.date().isoformat(), "relative_hours"
        if unit in {"天", "day", "days"}:
            post_date = (now.date() - timedelta(days=amount)).isoformat()
            return amount * 24 * 60, post_date, "relative_days"
    post_date, reason = infer_post_date(text, now)
    return None, post_date, reason


def passes_time_filter(
    primary_text: str,
    fallback_text: str = "",
    only_today: bool = True,
    max_age_minutes: Optional[int] = None,
) -> Tuple[bool, Optional[str], str, Optional[int]]:
    age_minutes, post_date, reason = infer_post_age_minutes(primary_text)
    if age_minutes is None and reason in {"no_text", "unknown_date"} and fallback_text:
        age_minutes, post_date, reason = infer_post_age_minutes(fallback_text)

    if max_age_minutes is not None:
        if age_minutes is None:
            return False, post_date, reason, age_minutes
        return age_minutes <= max_age_minutes, post_date, reason, age_minutes

    is_today = bool(post_date and post_date == taipei_now().date().isoformat())
    if only_today:
        return is_today, post_date, reason, age_minutes
    return True, post_date, reason, age_minutes


async def extract_post_detail(page, source_url: str) -> Dict[str, Any]:
    await page.goto(source_url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(6000)
    data = await page.evaluate(
        """
        () => ({
          title: document.title || '',
          og: document.querySelector('meta[property="og:description"]')?.content || '',
          desc: document.querySelector('meta[name="description"]')?.content || '',
          body: document.body?.innerText || '',
          imageUrls: [
            document.querySelector('meta[property="og:image"]')?.content || '',
            ...Array.from(document.images || []).map((img) => img.currentSrc || img.src || '')
          ].filter(Boolean)
        })
        """
    )
    title = clean_text(data.get("title") or "")
    og = clean_text(data.get("og") or "")
    desc = clean_text(data.get("desc") or "")
    body = clean_text(data.get("body") or "")
    text = og or desc or title or body
    if len(text) < 20 and body:
        text = body
    return {
        "text": text[:1800],
        "title": title,
        "meta_description": og or desc,
        "body_sample": body[:1800],
        "image_urls": list(dict.fromkeys(data.get("imageUrls") or []))[:5],
    }


def profile_url_from_post_url(source_url: str) -> str:
    try:
        parts = [part for part in urlparse(source_url).path.split("/") if part]
    except Exception:
        return ""
    if not parts or not parts[0].startswith("@"):
        return ""
    return f"https://www.threads.com/{parts[0]}"


def normalize_threads_post_url(source_url: str) -> str:
    return (source_url or "").removesuffix("/media")


async def extract_author_profile_text(page, source_url: str) -> Dict[str, str]:
    profile_url = profile_url_from_post_url(source_url)
    if not profile_url:
        return {"profile_url": "", "profile_text": ""}
    try:
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(3500)
        data = await page.evaluate(
            """
            () => ({
              title: document.title || '',
              desc: document.querySelector('meta[name="description"]')?.content || '',
              body: document.body?.innerText || ''
            })
            """
        )
        profile_text = clean_text(" ".join([
            data.get("title") or "",
            data.get("desc") or "",
            data.get("body") or "",
        ]))
        return {"profile_url": profile_url, "profile_text": profile_text[:400]}
    except Exception:
        return {"profile_url": profile_url, "profile_text": ""}


async def analyze_image_urls(image_urls: List[str], text: str) -> Dict[str, Any]:
    if not image_urls:
        return {"status": "no_image", "image_urls": [], "analysis": {}}
    prompt = (
        "你是台灣即時災情輔助判讀助手。請根據 Threads 貼文圖片與主旨判斷是否有災情或交通風險。"
        "只回 JSON，不要 markdown。欄位: visible_scene, incident_signals(陣列), location_clues(陣列),"
        "risk_level(low|medium|high), confidence(0到1), notes。"
        f"貼文主旨或文字:{text[:500]}"
    )
    errors = []
    for image_url in image_urls[:2]:
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                response = await client.get(image_url, timeout=20.0)
                response.raise_for_status()
            mime_type = response.headers.get("content-type", "image/jpeg").split(";")[0]
            if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
                continue
            analysis = await call_gemini_vision(response.content, mime_type, prompt)
            if analysis:
                return {"status": "success", "image_urls": image_urls, "analysis": analysis, "source_url": image_url}
        except Exception as e:
            errors.append({"image_url": image_url, "message": str(e)})
    return {"status": "unavailable", "image_urls": image_urls, "analysis": {}, "errors": errors}


async def extract_search_posts(
    page,
    keyword: str,
    limit: int,
    city: str = "",
    district: str = "",
    only_today: bool = True,
    max_age_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    await page.goto(THREADS_SEARCH_URL.format(query=quote_plus(keyword)), wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(8000)

    for _ in range(2):
        await page.mouse.wheel(0, 1400)
        await page.wait_for_timeout(1500)

    items = await page.evaluate(
        """
        (limit) => {
          const anchors = Array.from(document.querySelectorAll('a[href*="/@"][href*="/post/"]'));
          const results = [];
          const seen = new Set();
          for (const anchor of anchors) {
            const href = new URL(anchor.getAttribute('href'), location.origin).href;
            if (href.includes('/media')) continue;
            if (seen.has(href)) continue;
            seen.add(href);
            let node = anchor;
            let bestText = (anchor.innerText || '').replace(/\\s+/g, ' ').trim();
            for (let i = 0; i < 14 && node && node.parentElement; i++) {
              const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
              if (text.length > 20 && text.length < 700 && /\\d+\\s*(秒|分鐘|分|小時|天)|20\\d{2}/.test(text)) {
                bestText = text;
                break;
              }
              if (text.length > bestText.length && text.length < 500) bestText = text;
              node = node.parentElement;
            }
            const text = bestText;
            if (!text || text.length < 8) continue;
            results.push({ source_url: href, source_id: href, text, raw: { provider: 'public_browser' } });
            if (results.length >= limit) break;
          }
          return results;
        }
        """,
        limit,
    )

    normalized = []
    skipped_no_location_title = 0
    skipped_not_today = 0
    for item in items:
        source_url = item.get("source_url")
        if not source_url:
            continue
        source_url = normalize_threads_post_url(source_url)
        search_title = clean_text(item.get("text") or "")
        if not title_has_required_location(search_title, city, district):
            skipped_no_location_title += 1
            continue
        try:
            detail = await extract_post_detail(page, source_url)
            detail.update(await extract_author_profile_text(page, source_url))
        except Exception:
            detail = {"text": search_title, "title": "", "meta_description": "", "body_sample": ""}
        text = clean_text(detail.get("text") or item.get("text") or "")
        body_sample = clean_text(detail.get("body_sample") or "")
        combined_text = f"{text} {body_sample}"
        within_window, post_date, date_reason, age_minutes = passes_time_filter(
            search_title,
            combined_text,
            only_today,
            max_age_minutes,
        )
        if not within_window:
            skipped_not_today += 1
            continue
        if not text:
            continue
        if keyword not in combined_text and classify_incident_type(combined_text) == "unknown":
            continue
        normalized.append({
            "source": "threads",
            "source_id": source_url,
            "source_url": source_url,
            "keyword": keyword,
            "text": text,
            "author": None,
            "created_at": post_date,
            "raw": {
                **(item.get("raw") or {}),
                **detail,
                "search_title": search_title,
                "post_date": post_date,
                "date_reason": date_reason,
                "is_today": bool(post_date and post_date == taipei_now().date().isoformat()),
                "age_minutes": age_minutes,
                "max_age_minutes": max_age_minutes,
            },
        })
    return {
        "posts": normalized,
        "candidate_posts": len(items),
        "skipped_no_location_title": skipped_no_location_title,
        "skipped_not_today": skipped_not_today,
    }


async def extract_bing_fallback_posts(
    page,
    keyword: str,
    limit: int,
    city: str = "",
    district: str = "",
    only_today: bool = True,
    max_age_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    search_limit = max(limit * 4, 12)
    search = await _search_bing_keyword(keyword, search_limit)
    posts = search.get("data") or []
    normalized = []
    skipped_no_location_title = 0
    skipped_not_today = 0
    errors = search.get("errors") or []

    for item in posts:
        source_url = item.get("source_url")
        if not source_url:
            continue
        source_url = normalize_threads_post_url(source_url)
        search_title = clean_text(
            item.get("raw", {}).get("title")
            or item.get("raw", {}).get("meta_description")
            or item.get("text")
            or ""
        )
        try:
            detail = await extract_post_detail(page, source_url)
            detail.update(await extract_author_profile_text(page, source_url))
        except Exception as e:
            errors.append({"service": "threads_public_browser", "message": str(e), "source_url": source_url})
            detail = {
                "text": clean_text(item.get("text") or search_title),
                "title": search_title,
                "meta_description": item.get("raw", {}).get("meta_description") or "",
                "body_sample": "",
                "image_urls": [],
            }
        text = clean_text(detail.get("text") or item.get("text") or search_title)
        detail_title = clean_text(detail.get("title") or detail.get("meta_description") or search_title)
        if not title_has_required_location(detail_title, city, district):
            skipped_no_location_title += 1
            continue
        body_sample = clean_text(detail.get("body_sample") or "")
        combined_text = f"{search_title} {text} {body_sample}"
        within_window, post_date, date_reason, age_minutes = passes_time_filter(
            search_title or detail_title,
            combined_text,
            only_today,
            max_age_minutes,
        )
        if not within_window:
            skipped_not_today += 1
            continue
        if keyword not in combined_text and classify_incident_type(combined_text) == "unknown":
            continue
        normalized.append({
            "source": "threads",
            "source_id": source_url,
            "source_url": source_url,
            "keyword": keyword,
            "text": text,
            "author": item.get("author"),
            "created_at": post_date,
            "raw": {
                **(item.get("raw") or {}),
                **detail,
                "provider": "bing_fallback",
                "search_title": search_title,
                "post_date": post_date,
                "date_reason": date_reason,
                "is_today": bool(post_date and post_date == taipei_now().date().isoformat()),
                "age_minutes": age_minutes,
                "max_age_minutes": max_age_minutes,
            },
        })

    return {
        "posts": normalized,
        "candidate_posts": len(posts),
        "skipped_no_location_title": skipped_no_location_title,
        "skipped_not_today": skipped_not_today,
        "errors": errors,
    }


async def extract_web_search_fallback_posts(
    page,
    keyword: str,
    limit: int,
    city: str = "",
    district: str = "",
    only_today: bool = True,
    max_age_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    result = await extract_bing_fallback_posts(page, keyword, limit, city, district, only_today, max_age_minutes)
    result["provider"] = "bing"
    if result["posts"] or result["candidate_posts"]:
        return result

    search_limit = max(limit * 4, 12)
    search = await _search_duckduckgo_keyword(keyword, search_limit)
    posts = search.get("data") or []
    normalized = []
    skipped_no_location_title = 0
    skipped_not_today = 0
    skipped_non_post_url = 0
    errors = search.get("errors") or []

    for item in posts:
        source_url = item.get("source_url")
        if not source_url or "/post/" not in source_url:
            skipped_non_post_url += 1
            continue
        source_url = normalize_threads_post_url(source_url)
        search_title = clean_text(
            item.get("raw", {}).get("title")
            or item.get("raw", {}).get("meta_description")
            or item.get("text")
            or ""
        )
        try:
            detail = await extract_post_detail(page, source_url)
            detail.update(await extract_author_profile_text(page, source_url))
        except Exception as e:
            errors.append({"service": "threads_public_browser", "message": str(e), "source_url": source_url})
            detail = {
                "text": clean_text(item.get("text") or search_title),
                "title": search_title,
                "meta_description": item.get("raw", {}).get("meta_description") or "",
                "body_sample": "",
                "image_urls": [],
            }
        text = clean_text(detail.get("text") or item.get("text") or search_title)
        detail_title = clean_text(detail.get("title") or detail.get("meta_description") or search_title)
        if not title_has_required_location(detail_title, city, district):
            skipped_no_location_title += 1
            continue
        body_sample = clean_text(detail.get("body_sample") or "")
        combined_text = f"{search_title} {text} {body_sample}"
        within_window, post_date, date_reason, age_minutes = passes_time_filter(
            search_title or detail_title,
            combined_text,
            only_today,
            max_age_minutes,
        )
        if not within_window:
            skipped_not_today += 1
            continue
        if keyword not in combined_text and classify_incident_type(combined_text) == "unknown":
            continue
        normalized.append({
            "source": "threads",
            "source_id": source_url,
            "source_url": source_url,
            "keyword": keyword,
            "text": text,
            "author": item.get("author"),
            "created_at": post_date,
            "raw": {
                **(item.get("raw") or {}),
                **detail,
                "provider": "duckduckgo_fallback",
                "search_title": search_title,
                "post_date": post_date,
                "date_reason": date_reason,
                "is_today": bool(post_date and post_date == taipei_now().date().isoformat()),
                "age_minutes": age_minutes,
                "max_age_minutes": max_age_minutes,
            },
        })

    return {
        "provider": "duckduckgo",
        "posts": normalized,
        "candidate_posts": len(posts),
        "skipped_no_location_title": skipped_no_location_title,
        "skipped_not_today": skipped_not_today,
        "skipped_non_post_url": skipped_non_post_url,
        "errors": errors,
    }


async def extract_browser_duckduckgo_posts(
    page,
    keyword: str,
    limit: int,
    city: str = "",
    district: str = "",
    only_today: bool = True,
    max_age_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    search_limit = max(limit * 4, 12)
    search_url = f"https://duckduckgo.com/?q={quote_plus(f'site:threads.com {keyword}')}"
    await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(5000)
    for _ in range(2):
        await page.mouse.wheel(0, 1200)
        await page.wait_for_timeout(1000)

    urls = await page.evaluate(
        """
        (limit) => {
          const urls = [];
          const seen = new Set();
          for (const anchor of Array.from(document.querySelectorAll('a[href]'))) {
            const href = anchor.href || anchor.getAttribute('href') || '';
            if (!href || seen.has(href)) continue;
            seen.add(href);
            urls.push(href);
            if (urls.length >= limit) break;
          }
          return urls;
        }
        """,
        search_limit * 3,
    )

    source_urls = []
    seen = set()
    for raw_url in urls:
        url = _normalize_duckduckgo_url(raw_url)
        url = normalize_threads_post_url(url)
        if not _is_threads_url(url) or "/post/" not in url:
            continue
        if url in seen:
            continue
        seen.add(url)
        source_urls.append(url)
        if len(source_urls) >= search_limit:
            break

    normalized = []
    skipped_no_location_title = 0
    skipped_not_today = 0
    errors = []
    for source_url in source_urls:
        try:
            detail = await extract_post_detail(page, source_url)
            detail.update(await extract_author_profile_text(page, source_url))
        except Exception as e:
            errors.append({"service": "threads_public_browser", "message": str(e), "source_url": source_url})
            continue

        search_title = clean_text(detail.get("title") or detail.get("meta_description") or "")
        text = clean_text(detail.get("text") or search_title)
        if not title_has_required_location(search_title or text, city, district):
            skipped_no_location_title += 1
            continue

        body_sample = clean_text(detail.get("body_sample") or "")
        combined_text = f"{search_title} {text} {body_sample}"
        within_window, post_date, date_reason, age_minutes = passes_time_filter(
            search_title or text,
            combined_text,
            only_today,
            max_age_minutes,
        )
        if not within_window:
            skipped_not_today += 1
            continue
        if keyword not in combined_text and classify_incident_type(combined_text) == "unknown":
            continue

        normalized.append({
            "source": "threads",
            "source_id": source_url,
            "source_url": source_url,
            "keyword": keyword,
            "text": text,
            "author": None,
            "created_at": post_date,
            "raw": {
                **detail,
                "provider": "browser_duckduckgo_fallback",
                "search_title": search_title,
                "post_date": post_date,
                "date_reason": date_reason,
                "is_today": bool(post_date and post_date == taipei_now().date().isoformat()),
                "age_minutes": age_minutes,
                "max_age_minutes": max_age_minutes,
            },
        })

    return {
        "provider": "browser_duckduckgo",
        "posts": normalized,
        "candidate_posts": len(source_urls),
        "skipped_no_location_title": skipped_no_location_title,
        "skipped_not_today": skipped_not_today,
        "errors": errors,
    }


def infer_scoped_location(texts: List[str], locations: List[str], city: str = "", district: str = "") -> Dict[str, str]:
    inferred = infer_city_district(texts, locations)
    haystack = " ".join([*(texts or []), *(locations or [])])
    if city and any(term in haystack for term in location_terms(city, district)):
        districts = TAIWAN_LOCATIONS.get(city, [])
        matched_district = district if district in districts else ""
        if not matched_district:
            matched_district = next((item for item in districts if item in haystack), "")
        return {"city": city, "district": matched_district}
    return inferred


def merge_author_location(
    location_parts: Dict[str, str],
    author_location: Dict[str, Any],
    scoped_city: str = "",
) -> Tuple[Dict[str, str], str]:
    source = "post_text"
    merged = dict(location_parts)
    author_city = author_location.get("city") or ""
    author_district = author_location.get("district") or ""
    if not author_city and not author_district:
        return merged, source

    if scoped_city and author_city and author_city != scoped_city:
        return merged, source

    if not merged.get("city") and author_city:
        merged["city"] = author_city
        source = "author_profile"
    if not merged.get("district") and author_district:
        merged["district"] = author_district
        source = "author_profile"
    return merged, source


async def build_incident_from_post(
    post: Dict[str, Any],
    keyword: str,
    store: bool = True,
    city: str = "",
    district: str = "",
) -> Dict[str, Any]:
    texts = [post.get("text") or "", post.get("raw", {}).get("search_title") or ""]
    locations = extract_locations(texts)
    location_parts = infer_scoped_location(texts, locations, city, district)
    author_location = infer_author_location(post.get("raw") or {})
    location_parts, location_source = merge_author_location(location_parts, author_location, city)
    for value in [location_parts.get("city"), location_parts.get("district")]:
        if value and value not in locations:
            locations.insert(0, value)
    incident_type = classify_incident_type(" ".join(texts))
    confidence = build_confidence(post.get("text") or "", [], locations)
    image_analysis = await analyze_image_urls(post.get("raw", {}).get("image_urls") or [], post.get("text") or "")
    if image_analysis.get("analysis"):
        confidence = min(round(confidence + 0.08, 2), 0.98)

    source_url = normalize_threads_post_url(post.get("source_url") or "")
    source_id = post.get("source_id") or stable_hash(post)
    summary = build_summary(incident_type, locations, keyword)
    report = {
        "source": "threads",
        "source_id": source_id,
        "source_url": source_url,
        "incident_type": incident_type,
        "title": summary,
        "summary": summary,
        "city": location_parts["city"],
        "district": location_parts["district"],
        "location_text": locations[0] if locations else "",
        "locations": locations,
        "confidence": confidence,
        "evidence_count": 1,
        "status": "confirmed" if confidence >= INCIDENT_NOTIFY_CONFIDENCE else "candidate",
        "raw": {"post": post, "keyword": keyword, "provider": "public_browser", "image_analysis": image_analysis},
        "last_seen_at": taipei_now().isoformat(),
        "expires_at": incident_expires_at(),
    }
    evidence = [{
        "source": "threads_post",
        "source_id": source_id,
        "source_url": source_url,
        "text": post.get("text") or "",
        "extracted_locations": locations,
        "raw": post.get("raw") or {},
        "created_at": taipei_now().isoformat(),
    }]
    stored = None
    if store:
        stored = _store_incident(report, evidence)
    return {
        "report": stored or report,
        "evidence": evidence,
        "analysis": {
            "title_text": post.get("raw", {}).get("search_title") or post.get("raw", {}).get("title") or "",
            "post_text": post.get("text") or "",
            "image_analysis": image_analysis,
            "locations": locations,
            "city": location_parts["city"],
            "district": location_parts["district"],
            "location_source": location_source,
            "author_location": author_location,
            "incident_type": incident_type,
            "confidence": confidence,
            "should_notify": confidence >= INCIDENT_NOTIFY_CONFIDENCE,
            "post_date": post.get("raw", {}).get("post_date"),
            "date_reason": post.get("raw", {}).get("date_reason"),
            "is_today": post.get("raw", {}).get("is_today"),
            "age_minutes": post.get("raw", {}).get("age_minutes"),
            "max_age_minutes": post.get("raw", {}).get("max_age_minutes"),
        },
    }


async def analyze_threads_url(
    source_url: str,
    store: bool = False,
    only_today: bool = True,
    max_age_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    from playwright.async_api import async_playwright

    errors = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await new_threads_context(browser)
        page = await context.new_page()
        try:
            detail = await extract_post_detail(page, source_url)
            detail.update(await extract_author_profile_text(page, source_url))
            text = clean_text(detail.get("text") or detail.get("title") or "")
            within_window, post_date, date_reason, age_minutes = passes_time_filter(
                text,
                "",
                only_today,
                max_age_minutes,
            )
            post = {
                "source": "threads",
                "source_id": source_url,
                "source_url": source_url,
                "keyword": "manual_url",
                "text": text,
                "author": None,
                "created_at": post_date,
                "raw": {
                    **detail,
                    "search_title": clean_text(detail.get("title") or ""),
                    "post_date": post_date,
                    "date_reason": date_reason,
                    "is_today": bool(post_date and post_date == taipei_now().date().isoformat()),
                    "age_minutes": age_minutes,
                    "max_age_minutes": max_age_minutes,
                },
            }
            result = await build_incident_from_post(post, "manual_url", store=store and within_window)
            result["time_filter"] = {
                "only_today": only_today,
                "max_age_minutes": max_age_minutes,
                "within_window": within_window,
                "is_today": bool(post_date and post_date == taipei_now().date().isoformat()),
                "post_date": post_date,
                "age_minutes": age_minutes,
                "reason": date_reason,
                "stored": bool(store and within_window),
            }
        except Exception as e:
            errors.append({"service": "threads_public_browser", "message": str(e), "source_url": source_url})
            result = {}
        await context.close()
        await browser.close()

    return safe_response(
        "success" if not errors else "error",
        result,
        "threads url analyzed",
        "threads_public_browser",
        errors,
    )


async def scan_public_threads_browser(
    city: str = "",
    district: str = "",
    keywords: Optional[List[str]] = None,
    store: bool = True,
    only_today: bool = True,
    max_age_minutes: Optional[int] = None,
    scan_scope: str = "single",
    batch_index: int = 0,
    batch_total: int = 1,
) -> Dict[str, Any]:
    from playwright.async_api import async_playwright

    incidents = []
    analyses = []
    errors = []
    checked_posts = 0
    candidate_posts = 0
    skipped_no_location_title = 0
    skipped_not_today = 0
    bing_candidate_posts = 0
    bing_used_keywords = []
    duckduckgo_candidate_posts = 0
    duckduckgo_used_keywords = []
    browser_duckduckgo_candidate_posts = 0
    browser_duckduckgo_used_keywords = []
    skipped_non_post_url = 0
    jobs = build_scan_jobs(city, district, keywords, scan_scope, batch_index, batch_total)
    executed_keywords = []
    limit = max(1, THREADS_SCAN_POST_LIMIT)
    cleanup_result = cleanup_expired_incidents() if store else None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await new_threads_context(browser)
        page = await context.new_page()
        for job in jobs:
            job_city = job.get("city") or ""
            job_district = job.get("district") or ""
            for keyword in job.get("keywords") or []:
                executed_keywords.append(keyword)
                try:
                    search_result = await extract_search_posts(page, keyword, limit, job_city, job_district, only_today, max_age_minutes)
                except Exception as e:
                    errors.append({"service": "threads_public_browser", "keyword": keyword, "city": job_city, "message": str(e)})
                    continue
                posts = search_result["posts"]
                candidate_posts += search_result["candidate_posts"]
                skipped_no_location_title += search_result["skipped_no_location_title"]
                skipped_not_today += search_result["skipped_not_today"]

                if not posts:
                    try:
                        fallback_result = await extract_web_search_fallback_posts(page, keyword, limit, job_city, job_district, only_today, max_age_minutes)
                        posts = fallback_result["posts"]
                        if fallback_result.get("provider") == "duckduckgo":
                            duckduckgo_candidate_posts += fallback_result["candidate_posts"]
                            skipped_non_post_url += fallback_result.get("skipped_non_post_url", 0)
                        else:
                            bing_candidate_posts += fallback_result["candidate_posts"]
                        skipped_no_location_title += fallback_result["skipped_no_location_title"]
                        skipped_not_today += fallback_result["skipped_not_today"]
                        errors.extend(fallback_result.get("errors") or [])
                        if fallback_result["candidate_posts"]:
                            if fallback_result.get("provider") == "duckduckgo":
                                duckduckgo_used_keywords.append(keyword)
                            else:
                                bing_used_keywords.append(keyword)
                    except Exception as e:
                        errors.append({"service": "web_search_fallback", "keyword": keyword, "city": job_city, "message": str(e)})

                if not posts:
                    try:
                        browser_fallback = await extract_browser_duckduckgo_posts(page, keyword, limit, job_city, job_district, only_today, max_age_minutes)
                        posts = browser_fallback["posts"]
                        browser_duckduckgo_candidate_posts += browser_fallback["candidate_posts"]
                        skipped_no_location_title += browser_fallback["skipped_no_location_title"]
                        skipped_not_today += browser_fallback["skipped_not_today"]
                        errors.extend(browser_fallback.get("errors") or [])
                        if browser_fallback["candidate_posts"]:
                            browser_duckduckgo_used_keywords.append(keyword)
                    except Exception as e:
                        errors.append({"service": "browser_duckduckgo_fallback", "keyword": keyword, "city": job_city, "message": str(e)})

                for post in posts:
                    checked_posts += 1
                    try:
                        result = await build_incident_from_post(post, keyword, store=store, city=job_city, district=job_district)
                        if result["analysis"]["confidence"] < 0.5:
                            continue
                        analyses.append(result["analysis"])
                        incidents.append(result["report"])
                    except Exception as e:
                        errors.append({"service": "supabase", "message": str(e), "source_url": post.get("source_url") or ""})
        await context.close()
        await browser.close()

    status = "success" if not errors else "partial_success"
    return safe_response(
        status,
        {
            "incidents": incidents,
            "analyses": analyses,
            "keywords": executed_keywords,
            "city": city,
            "district": district,
            "scan_scope": scan_scope,
            "batch_index": batch_index,
            "batch_total": batch_total,
            "scan_jobs": [
                {
                    "city": job.get("city"),
                    "district": job.get("district"),
                    "keyword_count": len(job.get("keywords") or []),
                }
                for job in jobs
            ],
            "only_today": only_today,
            "max_age_minutes": max_age_minutes,
            "today": taipei_now().date().isoformat(),
            "auth_state_loaded": bool(auth_state_path()),
            "cleanup": cleanup_result.get("data") if cleanup_result else {},
            "candidate_posts": candidate_posts,
            "skipped_no_location_title": skipped_no_location_title,
            "skipped_not_today": skipped_not_today,
            "bing_candidate_posts": bing_candidate_posts,
            "bing_used_keywords": bing_used_keywords,
            "duckduckgo_candidate_posts": duckduckgo_candidate_posts,
            "duckduckgo_used_keywords": duckduckgo_used_keywords,
            "browser_duckduckgo_candidate_posts": browser_duckduckgo_candidate_posts,
            "browser_duckduckgo_used_keywords": browser_duckduckgo_used_keywords,
            "skipped_non_post_url": skipped_non_post_url,
            "checked_posts": checked_posts,
            "created_or_updated": len(incidents),
        },
        "public browser incident scan completed",
        "threads_public_browser",
        errors,
    )


async def main():
    result = await scan_public_threads_browser()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") in {"success", "partial_success", "not_configured"} else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
