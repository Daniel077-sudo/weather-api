import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright

from incident_service import (
    build_confidence,
    build_summary,
    classify_incident_type,
    extract_locations,
    infer_city_district,
    incident_keywords,
    _store_incident,
)
from config import INCIDENT_NOTIFY_CONFIDENCE, THREADS_SCAN_POST_LIMIT
from utils import safe_response, stable_hash, taipei_now


THREADS_SEARCH_URL = "https://www.threads.com/search?q={query}"


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


async def extract_post_detail(page, source_url: str) -> Dict[str, Any]:
    await page.goto(source_url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(6000)
    data = await page.evaluate(
        """
        () => ({
          title: document.title || '',
          og: document.querySelector('meta[property="og:description"]')?.content || '',
          desc: document.querySelector('meta[name="description"]')?.content || '',
          body: document.body?.innerText || ''
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
    }


async def extract_search_posts(page, keyword: str, limit: int) -> List[Dict[str, Any]]:
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
            if (seen.has(href)) continue;
            seen.add(href);
            let node = anchor;
            for (let i = 0; i < 6 && node && node.parentElement; i++) {
              if ((node.innerText || '').length > 40) break;
              node = node.parentElement;
            }
            const text = (node?.innerText || anchor.innerText || '').replace(/\\s+/g, ' ').trim();
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
    for item in items:
        source_url = item.get("source_url")
        if not source_url:
            continue
        try:
            detail = await extract_post_detail(page, source_url)
        except Exception:
            detail = {"text": clean_text(item.get("text") or ""), "title": "", "meta_description": "", "body_sample": ""}
        text = clean_text(detail.get("text") or item.get("text") or "")
        body_sample = clean_text(detail.get("body_sample") or "")
        combined_text = f"{text} {body_sample}"
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
            "created_at": None,
            "raw": {**(item.get("raw") or {}), **detail},
        })
    return normalized


async def scan_public_threads_browser() -> Dict[str, Any]:
    incidents = []
    errors = []
    checked_posts = 0
    keywords = incident_keywords()
    limit = max(1, THREADS_SCAN_POST_LIMIT)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="zh-TW",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
        )
        page = await context.new_page()
        for keyword in keywords:
            try:
                posts = await extract_search_posts(page, keyword, limit)
            except Exception as e:
                errors.append({"service": "threads_public_browser", "keyword": keyword, "message": str(e)})
                continue

            for post in posts:
                checked_posts += 1
                texts = [post.get("text") or ""]
                locations = extract_locations(texts)
                location_parts = infer_city_district(texts, locations)
                incident_type = classify_incident_type(" ".join(texts))
                confidence = build_confidence(post.get("text") or "", [], locations)
                if confidence < 0.5:
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
                    "city": location_parts["city"],
                    "district": location_parts["district"],
                    "location_text": locations[0] if locations else "",
                    "locations": locations,
                    "confidence": confidence,
                    "evidence_count": 1,
                    "status": "confirmed" if confidence >= INCIDENT_NOTIFY_CONFIDENCE else "candidate",
                    "raw": {"post": post, "keyword": keyword, "provider": "public_browser"},
                    "last_seen_at": taipei_now().isoformat(),
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
                try:
                    incidents.append(_store_incident(report, evidence))
                except Exception as e:
                    errors.append({"service": "supabase", "message": str(e), "source_url": source_url})
        await context.close()
        await browser.close()

    status = "success" if not errors else "partial_success"
    return safe_response(
        status,
        {
            "incidents": incidents,
            "keywords": keywords,
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
