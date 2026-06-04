from typing import Any, Dict, List, Optional

import httpx

from config import (
    THREADS_ACCESS_TOKEN,
    THREADS_API_BASE_URL,
    THREADS_KEYWORD_SEARCH_PATH,
    THREADS_PROVIDER,
    THREADS_SEARCH_QUERY_PARAM,
    THREADS_SEARCH_URL,
)


def threads_is_configured() -> bool:
    return THREADS_PROVIDER == "official" and bool(THREADS_ACCESS_TOKEN)


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


async def search_threads_keyword(keyword: str, limit: int = 5) -> Dict[str, Any]:
    if not threads_is_configured():
        return {
            "status": "not_configured",
            "data": [],
            "message": "THREADS_ACCESS_TOKEN is missing or THREADS_PROVIDER is not official",
            "source": "threads",
            "errors": [],
        }

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
    if not threads_is_configured():
        return {
            "status": "not_configured",
            "data": [],
            "message": "THREADS_ACCESS_TOKEN is missing or THREADS_PROVIDER is not official",
            "source": "threads",
            "errors": [],
        }
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
