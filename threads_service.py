import html
import json
import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx

from config import (
    BING_SEARCH_BASE_URL,
    BING_THREADS_SITE,
    BING_SEARCH_MARKET,
    BING_SEARCH_TIMEOUT_SECONDS,
    DUCKDUCKGO_SEARCH_BASE_URL,
    THREADS_ACCESS_TOKEN,
    THREADS_API_BASE_URL,
    THREADS_KEYWORD_SEARCH_PATH,
    THREADS_PROVIDER,
    THREADS_SEARCH_QUERY_PARAM,
    THREADS_SEARCH_URL,
)


def threads_is_configured() -> bool:
    if THREADS_PROVIDER in {"bing", "duckduckgo"}:
        return True
    return THREADS_PROVIDER == "official" and bool(THREADS_ACCESS_TOKEN)


def _provider_not_configured() -> Dict[str, Any]:
    if THREADS_PROVIDER == "official":
        message = "THREADS_ACCESS_TOKEN is missing or THREADS_PROVIDER is not official"
    else:
        message = f"THREADS_PROVIDER={THREADS_PROVIDER} is not supported"
    return {
        "status": "not_configured",
        "data": [],
        "message": message,
        "source": "threads",
        "errors": [],
    }


def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {THREADS_ACCESS_TOKEN}"}


def _search_url() -> str:
    if THREADS_SEARCH_URL:
        return THREADS_SEARCH_URL
    return f"{THREADS_API_BASE_URL}/{THREADS_KEYWORD_SEARCH_PATH}"


def _text_from_post(post: Dict[str, Any]) -> str:
    return str(
        post.get("text")
        or post.get("caption")
        or post.get("message")
        or post.get("body")
        or ""
    )


def _url_from_post(post: Dict[str, Any]) -> str:
    return str(
        post.get("permalink")
        or post.get("permalink_url")
        or post.get("url")
        or post.get("thread_url")
        or post.get("source_url")
        or ""
    )


def _id_from_post(post: Dict[str, Any]) -> str:
    return str(post.get("id") or post.get("media_id") or post.get("pk") or post.get("code") or "")


def _normalize_post(post: Dict[str, Any], keyword: str) -> Optional[Dict[str, Any]]:
    post_id = _id_from_post(post)
    text = _text_from_post(post)
    source_url = _url_from_post(post)
    if not post_id and not source_url:
        return None
    return {
        "source": "threads",
        "source_id": post_id or source_url,
        "source_url": source_url,
        "keyword": keyword,
        "text": text,
        "author": post.get("username") or (post.get("user") or {}).get("username"),
        "created_at": post.get("timestamp") or post.get("created_at") or post.get("createdAt"),
        "raw": post,
    }


def _normalize_reply(reply: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source": "threads_reply",
        "source_id": _id_from_post(reply),
        "text": _text_from_post(reply),
        "author": reply.get("username") or (reply.get("user") or {}).get("username"),
        "created_at": reply.get("timestamp") or reply.get("created_at") or reply.get("createdAt"),
        "raw": reply,
    }


def _items_from_response(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = payload.get("data") or payload.get("items") or payload.get("results") or payload.get("posts") or []
    return items if isinstance(items, list) else []


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: List[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1

    def handle_endtag(self, tag: str):
        if tag in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data: str):
        if self.skip_depth:
            return
        cleaned = re.sub(r"\s+", " ", data or "").strip()
        if cleaned:
            self.parts.append(cleaned)


def _clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _is_threads_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return (
        host in {"threads.com", "www.threads.com", "threads.net", "www.threads.net"}
        or host.endswith(".threads.com")
        or host.endswith(".threads.net")
    )


def _normalize_bing_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    if "bing.com" in parsed.netloc and parsed.path.startswith("/ck/a"):
        target = parse_qs(parsed.query).get("u", [""])[0]
        if target:
            return unquote(target)
    return url


def _normalize_duckduckgo_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    if (not parsed.netloc or "duckduckgo.com" in parsed.netloc) and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return unquote(target)
    return url


def parse_bing_threads_urls(html_text: str, limit: int = 5) -> List[str]:
    candidates = re.findall(r'<a[^>]+href=["\']([^"\']+)["\']', html_text or "", flags=re.I)
    urls: List[str] = []
    seen = set()
    for candidate in candidates:
        url = _normalize_bing_url(html.unescape(candidate))
        if not _is_threads_url(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def parse_duckduckgo_threads_urls(html_text: str, limit: int = 5) -> List[str]:
    candidates = re.findall(r'<a[^>]+href=["\']([^"\']+)["\']', html_text or "", flags=re.I)
    urls: List[str] = []
    seen = set()
    for candidate in candidates:
        url = _normalize_duckduckgo_url(html.unescape(candidate))
        if not _is_threads_url(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def _extract_meta_content(html_text: str, names: List[str]) -> str:
    for name in names:
        pattern = (
            r'<meta[^>]+(?:name|property)=["\']'
            + re.escape(name)
            + r'["\'][^>]+content=["\']([^"\']+)["\']'
        )
        match = re.search(pattern, html_text or "", flags=re.I)
        if match:
            return _clean_text(match.group(1))
    return ""


def _extract_title(html_text: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html_text or "", flags=re.I | re.S)
    return _clean_text(match.group(1)) if match else ""


def _extract_json_texts(html_text: str, max_items: int = 30) -> List[str]:
    texts: List[str] = []
    seen = set()
    for match in re.finditer(r'"(?:text|caption|body|description)"\s*:\s*"((?:\\.|[^"\\])*)"', html_text or ""):
        raw = match.group(1)
        try:
            value = json.loads(f'"{raw}"')
        except Exception:
            value = raw
        cleaned = _clean_text(str(value))
        if len(cleaned) < 8 or cleaned in seen:
            continue
        seen.add(cleaned)
        texts.append(cleaned)
        if len(texts) >= max_items:
            break
    return texts


def parse_threads_public_page(html_text: str, source_url: str, keyword: str) -> Dict[str, Any]:
    meta_text = _extract_meta_content(html_text, ["og:description", "description", "twitter:description"])
    title = _extract_title(html_text)
    json_texts = _extract_json_texts(html_text)

    parser = TextExtractor()
    try:
        parser.feed(html_text or "")
    except Exception:
        pass
    visible_texts = [
        text for text in parser.parts
        if len(text) >= 8 and not text.lower().startswith(("threads", "instagram", "meta"))
    ]

    post_text = meta_text or (json_texts[0] if json_texts else title)
    reply_candidates = json_texts[1:] + visible_texts
    replies = []
    seen = {post_text}
    for text in reply_candidates:
        cleaned = _clean_text(text)
        if not cleaned or cleaned in seen or cleaned == title:
            continue
        seen.add(cleaned)
        replies.append({
            "source": "threads_reply",
            "source_id": "",
            "text": cleaned,
            "author": None,
            "created_at": None,
            "raw": {"source": "public_html"},
        })
        if len(replies) >= 20:
            break

    return {
        "source": "threads",
        "source_id": source_url,
        "source_url": source_url,
        "keyword": keyword,
        "text": post_text,
        "author": None,
        "created_at": None,
        "raw": {"title": title, "meta_description": meta_text, "provider": "bing"},
        "_replies": replies,
    }


async def _fetch_html(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    }
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(url, headers=headers, timeout=BING_SEARCH_TIMEOUT_SECONDS)
        if response.status_code == 202:
            raise RuntimeError("search provider returned 202; request was accepted but no result page was served")
        response.raise_for_status()
        return response.text


async def _search_bing_keyword(keyword: str, limit: int) -> Dict[str, Any]:
    query = f"site:{BING_THREADS_SITE} {keyword} 台灣"
    url = f"{BING_SEARCH_BASE_URL}?q={quote_plus(query)}&mkt={quote_plus(BING_SEARCH_MARKET)}"
    try:
        html_text = await _fetch_html(url)
        threads_urls = parse_bing_threads_urls(html_text, limit)
    except Exception as e:
        return {
            "status": "error",
            "data": [],
            "message": str(e),
            "source": "bing",
            "errors": [{"service": "bing", "message": str(e), "keyword": keyword}],
        }

    posts = []
    errors = []
    for source_url in threads_urls:
        try:
            page_html = await _fetch_html(source_url)
            posts.append(parse_threads_public_page(page_html, source_url, keyword))
        except Exception as e:
            errors.append({"service": "threads_public_page", "message": str(e), "source_url": source_url})

    status = "success" if not errors else "partial_success"
    return {
        "status": status,
        "data": posts,
        "message": "bing threads search completed",
        "source": "bing",
        "errors": errors,
    }


async def _search_duckduckgo_keyword(keyword: str, limit: int) -> Dict[str, Any]:
    query = f"site:{BING_THREADS_SITE} {keyword}"
    url = f"{DUCKDUCKGO_SEARCH_BASE_URL}?q={quote_plus(query)}"
    try:
        html_text = await _fetch_html(url)
        threads_urls = parse_duckduckgo_threads_urls(html_text, limit)
    except Exception as e:
        return {
            "status": "error",
            "data": [],
            "message": str(e),
            "source": "duckduckgo",
            "errors": [{"service": "duckduckgo", "message": str(e), "keyword": keyword}],
        }

    posts = []
    errors = []
    for source_url in threads_urls:
        try:
            page_html = await _fetch_html(source_url)
            posts.append(parse_threads_public_page(page_html, source_url, keyword))
        except Exception as e:
            errors.append({"service": "threads_public_page", "message": str(e), "source_url": source_url})

    status = "success" if not errors else "partial_success"
    return {
        "status": status,
        "data": posts,
        "message": "duckduckgo threads search completed",
        "source": "duckduckgo",
        "errors": errors,
    }


async def search_threads_keyword(keyword: str, limit: int = 5) -> Dict[str, Any]:
    if THREADS_PROVIDER == "duckduckgo":
        return await _search_duckduckgo_keyword(keyword, limit)
    if THREADS_PROVIDER == "bing":
        return await _search_bing_keyword(keyword, limit)

    if not threads_is_configured():
        return _provider_not_configured()

    params = {
        THREADS_SEARCH_QUERY_PARAM: keyword,
        "limit": limit,
        "fields": "id,text,permalink,timestamp,username",
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(_search_url(), headers=_headers(), params=params, timeout=30.0)
            response.raise_for_status()
            payload = response.json()
    except Exception as e:
        return {
            "status": "error",
            "data": [],
            "message": str(e),
            "source": "threads",
            "errors": [{"service": "threads", "message": str(e)}],
        }

    posts = []
    for item in _items_from_response(payload):
        if isinstance(item, dict):
            normalized = _normalize_post(item, keyword)
            if normalized:
                posts.append(normalized)
        if len(posts) >= limit:
            break

    return {"status": "success", "data": posts, "message": "threads keyword search completed", "source": "threads", "errors": []}


async def fetch_threads_replies(source_id: str, limit: int = 20) -> Dict[str, Any]:
    if THREADS_PROVIDER in {"bing", "duckduckgo"}:
        return {"status": "success", "data": [], "message": "replies are included from public page when visible", "source": "bing", "errors": []}

    if not threads_is_configured():
        return _provider_not_configured()
    if not source_id:
        return {"status": "success", "data": [], "message": "source_id missing", "source": "threads", "errors": []}

    url = f"{THREADS_API_BASE_URL}/{source_id}/replies"
    params = {"limit": limit, "fields": "id,text,timestamp,username"}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=_headers(), params=params, timeout=30.0)
            response.raise_for_status()
            payload = response.json()
    except Exception as e:
        return {
            "status": "error",
            "data": [],
            "message": str(e),
            "source": "threads",
            "errors": [{"service": "threads_replies", "message": str(e)}],
        }

    replies = [_normalize_reply(item) for item in _items_from_response(payload) if isinstance(item, dict)]
    return {"status": "success", "data": replies[:limit], "message": "threads replies loaded", "source": "threads", "errors": []}
