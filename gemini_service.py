import base64
import json
import os
from typing import Any, Dict, List

import httpx

from config import GEMINI_API_KEY, supabase
from utils import stable_hash, taipei_now


def _today_start_iso() -> str:
    return f"{taipei_now().date().isoformat()}T00:00:00+08:00"


def _count_rows(table: str, select_expr: str, created_column: str = "created_at", start_iso: str = "") -> Dict[str, Any]:
    try:
        query = supabase.table(table).select(select_expr, count="exact")
        if start_iso:
            query = query.gte(created_column, start_iso)
        res = query.execute()
        return {"count": res.count if res.count is not None else len(res.data or []), "rows": res.data or []}
    except Exception as e:
        return {"count": 0, "rows": [], "error": str(e)}


def summarize_ai_usage() -> Dict[str, Any]:
    today_start = _today_start_iso()
    cache_today = _count_rows("ai_suggestion_cache", "*", "created_at", today_start)
    cache_total = _count_rows("ai_suggestion_cache", "*")
    vision_today = _count_rows("emergency_kit_scans", "id,created_at", "created_at", today_start)

    errors: List[Dict[str, str]] = []
    for table_name, result in [
        ("ai_suggestion_cache_today", cache_today),
        ("ai_suggestion_cache_total", cache_total),
        ("emergency_kit_scans_today", vision_today),
    ]:
        if result.get("error"):
            errors.append({"table": table_name, "message": result["error"]})

    data = {
        "date": taipei_now().date().isoformat(),
        "gemini_text_cache_created_today": cache_today["count"],
        "gemini_text_cache_entries_total": cache_total["count"],
        "vision_scans_today": vision_today["count"],
        "estimated_text_calls_saved_by_cache": max(cache_total["count"] - cache_today["count"], 0),
        "notes": [
            "gemini_text_cache_created_today 代表今天新增的 Gemini 結構化建議快取。",
            "目前未逐筆記錄 cache hit 次數，因此 saved_by_cache 為保守估算。",
        ],
    }
    return {
        "status": "success" if not errors else "partial_success",
        "data": data,
        "message": "AI usage summary loaded",
        "source": "supabase",
        "errors": errors,
    }

async def call_gemini_raw(prompt: str):
    """非同步呼叫 Gemini AI，避免拖垮主執行緒"""
    if not GEMINI_API_KEY:
        return ""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {'Content-Type': 'application/json'}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.8}
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload, timeout=30.0)
            response.raise_for_status()
            res_json = response.json()
            if 'candidates' in res_json and len(res_json['candidates']) > 0:
                return res_json['candidates'][0]['content']['parts'][0]['text'].strip()
            return f"[AI 罷工原因]: {json.dumps(res_json, ensure_ascii=False)}"
    except Exception as e:
        return f"[連線錯誤]: {str(e)}"


def parse_json_object(text: str) -> Dict[str, Any]:
    if not text:
        return {}
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.replace("json\n", "", 1).replace("JSON\n", "", 1)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end >= start:
        cleaned = cleaned[start:end + 1]
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


async def call_gemini_json(prompt: str, fallback: Dict[str, Any]) -> Dict[str, Any]:
    text = await call_gemini_raw(prompt)
    parsed = parse_json_object(text)
    return parsed or fallback


async def call_gemini_json_cached(prompt: str, fallback: Dict[str, Any], prompt_type: str, cache_subject: str, context: Dict[str, Any]) -> Dict[str, Any]:
    cache_key = stable_hash({
        "prompt_type": prompt_type,
        "cache_subject": cache_subject,
        "context_hash": stable_hash(context),
    })

    try:
        cached = supabase.table("ai_suggestion_cache").select("response").eq("cache_key", cache_key).limit(1).execute()
        if cached.data:
            response = cached.data[0].get("response") or {}
            if isinstance(response, dict):
                response["cache_hit"] = True
                return response
    except Exception:
        pass

    response = await call_gemini_json(prompt, fallback)
    response["cache_hit"] = False
    if response == {**fallback, "cache_hit": False}:
        return response
    try:
        supabase.table("ai_suggestion_cache").upsert({
            "cache_key": cache_key,
            "prompt_type": prompt_type,
            "subject": cache_subject,
            "context_hash": stable_hash(context),
            "response": response,
            "created_at": taipei_now().isoformat(),
        }, on_conflict="cache_key").execute()
    except Exception:
        pass
    return response


async def call_gemini_vision(image_bytes: bytes, mime_type: str, prompt: str) -> Dict[str, Any]:
    if not GEMINI_API_KEY:
        return {}

    model = os.getenv("GEMINI_VISION_MODEL", "gemini-1.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": base64.b64encode(image_bytes).decode("ascii"),
                        }
                    },
                ]
            }
        ],
        "generationConfig": {"temperature": 0.2},
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=45.0)
            response.raise_for_status()
            res_json = response.json()
            text = res_json.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            return parse_json_object(text)
    except Exception:
        return {}


