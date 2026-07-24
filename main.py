import base64
import hashlib
import json
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks, Header, Query
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from calendar_service import fetch_timetree_events, sync_timetree_event_payloads
from chat_service import build_chat_command, get_chat_history, get_user_memory_response
from config import CRON_SECRET, CRON_STATUS, CWA_API_KEY, GEMINI_API_KEY, SUPABASE_KEY, SUPABASE_URL, TDX_CLIENT_ID, TDX_CLIENT_SECRET, TIMETREE_ACCESS_TOKEN, VISION_DAILY_LIMIT, supabase
from data import GAME_QUESTIONS, GAME_SCORE_MEMORY, REQUIRED_EMERGENCY_KIT_ITEMS, SHELTER_FALLBACKS, TAIWAN_LOCATIONS
from disaster_service import cleanup_expired_disaster_alerts, get_active_disaster_alerts, monitor_watch_areas, refresh_disaster_alerts, summarize_disaster_alert_risk
from event_service import build_event_risk, monitor_event_weather_window, normalize_event
from gemini_service import call_gemini_raw, call_gemini_vision, summarize_ai_usage
from local_ai_service import build_local_ai_suggestion, load_local_ai_rules
from schemas import ChatRequest, EmergencyKitVisionRequest, EventCreate, EventRiskCheckRequest, GameScoreCreate, GameSubmitRequest, GeocodeRequest, LocalAIRequest, QuizScoreSubmitRequest, UserQuery, WatchAreaCreate, WeatherSuggestionRequest
from transport_service import build_traffic_risk_async, build_transport_links, determine_transport_type
from utils import analyze_text_risk, build_recommended_action, geocode_fallback, maps_url, normalize_disaster_code, normalize_shelter, parse_datetime, require_cron_secret, safe_int, safe_response, taipei_now
from weather_service import analyze_weather_risk, build_weather_snapshot, build_weather_suggestion, fetch_cwa_forecast, master_sync_orchestrator, pick_current_weather, refresh_expired_weather_cache, refresh_weather_cache_city, resolve_event_location_parts, summarize_weather_cache

load_dotenv()

app = FastAPI(title="Disaster Helper Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {
        "status": "success",
        "message": "Disaster Helper Backend is running",
        "docs": "/docs",
        "health": "/health",
    }

@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "service": "disaster_helper_backend",
        "time": datetime.now(timezone(timedelta(hours=8))).isoformat(),
    }

@app.get("/api/debug/status")
async def debug_status():
    services = {
        "supabase": {
            "configured": bool(SUPABASE_URL and SUPABASE_KEY),
            "url_configured": bool(SUPABASE_URL),
            "key_configured": bool(SUPABASE_KEY),
        },
        "cwa": {"configured": bool(CWA_API_KEY)},
        "gemini": {"configured": bool(GEMINI_API_KEY)},
        "tdx": {"configured": bool(TDX_CLIENT_ID and TDX_CLIENT_SECRET)},
        "timetree": {"configured": bool(TIMETREE_ACCESS_TOKEN)},
        "cron": {"configured": bool(CRON_SECRET), "last_status": CRON_STATUS.get("last_status")},
    }
    return safe_response(
        "success",
        {
            "services": services,
            "weather_cache": summarize_weather_cache(limit=10).get("data", {}),
        },
        "debug status loaded",
        "debug",
    )

# 🚀 API 1：前端地區選單
# ==========================================
@app.get("/api/sync-logs")
async def get_sync_logs(
    source: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    task_name: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
):
    try:
        query = supabase.table("sync_logs").select("*").order("finished_at", desc=True).limit(limit)
        if source:
            query = query.eq("source", source)
        if status:
            query = query.eq("status", status)
        if task_name:
            query = query.eq("task_name", task_name)
        res = query.execute()
        return safe_response("success", res.data or [], "sync logs loaded", "sync_logs")
    except Exception as e:
        return safe_response("error", [], str(e), "sync_logs", [{"service": "supabase", "message": str(e)}])

@app.get("/locations")
async def get_locations():
    return {"status": "success", "data": TAIWAN_LOCATIONS}

# ==========================================
# 🚀 API 2：AI 防災與生活助理
# ==========================================

@app.post("/ask-assistant")
async def ask_assistant(query: UserQuery):
    try:
        city, district, msg = query.city, query.district, query.message
        weather_payload: Dict[str, Any] = {}
        weather_source = "cache"

        try:
            cache_res = supabase.table("weather_cache").select("*").eq("city_name", f"{city}{district}").execute()
            if cache_res.data:
                cached_weather = cache_res.data[0].get("weather_data") or {}
                forecast = cached_weather.get("forecast") or []
                current = cached_weather.get("current") or pick_current_weather(forecast)
                weather_payload = {
                    "current": current,
                    "forecast": forecast,
                    **analyze_weather_risk(current),
                }
        except Exception as cache_e:
            print(f"天氣快取讀取失敗: {cache_e}")

        if not weather_payload:
            weather_source = "cwa_live"
            try:
                weather_payload = await fetch_cwa_forecast(city, district, seven_day=True)
            except Exception as weather_e:
                print(f"氣象解析錯誤: {weather_e}")
                weather_source = "fallback"
                current = {
                    "description": "未知",
                    "pop": 0,
                    "temp": 0,
                    "hum": 0,
                    "app_temp": 0,
                    "uvi": 0,
                    "wind_speed": "0",
                }
                weather_payload = {
                    "current": current,
                    "forecast": [],
                    **analyze_weather_risk(current),
                }

        current_weather = weather_payload.get("current") or {}
        local_suggestion = build_weather_suggestion(city, district, msg, current_weather, weather_payload)
        final_prompt = (
            f"地點:{city}{district}，天氣:{current_weather.get('description', '未知')}，"
            f"降雨機率:{safe_int(current_weather.get('pop'))}%，"
            f"風險等級:{weather_payload.get('risk_level', 'low')}，"
            f"行程:{msg}。請給40字內防災或生活建議，語氣自然直接。"
        )
        ai_suggestion = await call_gemini_raw(final_prompt)
        if not ai_suggestion or ai_suggestion.startswith("["):
            ai_suggestion = local_suggestion

        try:
            db_data = {
                "user_input": f"[{city}{district}] {msg}",
                "ai_response": ai_suggestion,
            }
            # Supabase Python SDK 目前仍為同步，但在快速寫入下可接受
            supabase.table("chat_logs").insert(db_data).execute()
        except Exception as e:
            print(f"備份對話紀錄失敗: {e}")

        return {
            "status": "success",
            "target_location": f"{city}{district}",
            "weather": {
                "wx": current_weather.get("description", "未知"),
                "pop": f"{safe_int(current_weather.get('pop'))}%",
                "temp": current_weather.get("temp"),
                "hum": current_weather.get("hum"),
                "app_temp": current_weather.get("app_temp"),
                "uvi": current_weather.get("uvi"),
                "wind_speed": current_weather.get("wind_speed"),
            },
            "risk_level": weather_payload.get("risk_level", "low"),
            "risk_tags": weather_payload.get("risk_tags", []),
            "has_weather_risk": weather_payload.get("has_weather_risk", False),
            "ai_suggestion": ai_suggestion,
            "suggestion_source": "gemini" if ai_suggestion != local_suggestion else "local_fallback",
            "weather_source": weather_source,
        }
    except Exception as e:
        return {"status": "error", "message": f"解析失敗: {str(e)}"}


@app.post("/api/chat")
async def chat_command(payload: ChatRequest):
    return await build_chat_command(payload.user_id or "", payload.message)

@app.get("/api/chat/history")
async def get_chat_history_endpoint(
    user_id: str = Query(...),
    limit: int = Query(30, ge=1, le=100),
):
    return get_chat_history(user_id, limit)

@app.get("/api/chat/memory")
async def get_chat_memory_endpoint(user_id: str = Query(...)):
    return get_user_memory_response(user_id)


@app.post("/api/weather/suggestion")
async def weather_suggestion(payload: WeatherSuggestionRequest):
    if payload.weather_data:
        weather_payload = payload.weather_data
        current_weather = weather_payload.get("current") or weather_payload.get("weather") or weather_payload
        risk_payload = {
            **analyze_weather_risk(current_weather),
            **{
                key: weather_payload.get(key)
                for key in ["risk_level", "risk_tags", "has_weather_risk"]
                if weather_payload.get(key) is not None
            },
        }
        suggestion = build_weather_suggestion(
            payload.city,
            payload.district,
            payload.message or payload.activity or "",
            current_weather,
            risk_payload,
        )
        return {
            "status": "success",
            "data": {
                "user_id": payload.user_id,
                "city": payload.city,
                "district": payload.district,
                "message": payload.message,
                "weather": current_weather,
                "risk_level": risk_payload.get("risk_level"),
                "risk_tags": risk_payload.get("risk_tags"),
                "has_weather_risk": risk_payload.get("has_weather_risk"),
                "suggestion": suggestion,
                "suggestion_source": "local_fallback",
                "weather_source": "request_payload",
            },
        }

    query = UserQuery(city=payload.city, district=payload.district, message=payload.message or payload.activity or "行程")
    result = await ask_assistant(query)
    if result.get("status") != "success":
        return result
    return {
        "status": "success",
        "data": {
            "user_id": payload.user_id,
            "city": payload.city,
            "district": payload.district,
            "message": payload.message,
            "weather": result.get("weather"),
            "risk_level": result.get("risk_level"),
            "risk_tags": result.get("risk_tags"),
            "has_weather_risk": result.get("has_weather_risk"),
            "suggestion": result.get("ai_suggestion"),
            "suggestion_source": result.get("suggestion_source"),
            "weather_source": result.get("weather_source"),
        },
    }

# ==========================================
# 🚀 API 3 & 4：天氣快取機制 (含新裝備 & 背景同步防封鎖 & 系統日誌)
# ==========================================




@app.post("/sync-all-taiwan")
async def sync_all_taiwan(background_tasks: BackgroundTasks):
    """鬧鐘排程專用：全台 22 縣市背景同步"""
    # 只要將總指揮官丟進背景執行即可
    background_tasks.add_task(master_sync_orchestrator)
        
    return {
        "status": "processing", 
        "message": f"已啟動全台 {len(REPRESENTATIVE_DISTRICTS)} 縣市同步任務，將依序完成並記錄日誌。"
    }


@app.post("/api/cron/refresh-weather-cache")
async def cron_refresh_weather_cache(
    background_tasks: BackgroundTasks,
    force: bool = False,
    background: bool = True,
    x_cron_secret: Optional[str] = Header(None),
):
    auth_error = require_cron_secret(x_cron_secret)
    if auth_error:
        return auth_error
    if background:
        background_tasks.add_task(refresh_expired_weather_cache, force)
        return safe_response("processing", {"force": force}, "weather_cache refresh started in background", "cron")
    return await refresh_expired_weather_cache(force)

@app.get("/api/cron/status")
async def get_cron_status():
    return safe_response("success", CRON_STATUS, "cron status loaded", "cron")

@app.post("/api/cron/refresh-disaster-alerts")
async def cron_refresh_disaster_alerts(
    background_tasks: BackgroundTasks,
    background: bool = True,
    x_cron_secret: Optional[str] = Header(None),
):
    auth_error = require_cron_secret(x_cron_secret)
    if auth_error:
        return auth_error
    if background:
        background_tasks.add_task(refresh_disaster_alerts)
        return safe_response("processing", {}, "disaster alerts refresh started in background", "cron")
    return await refresh_disaster_alerts()

@app.post("/api/cron/cleanup-disaster-alerts")
async def cron_cleanup_disaster_alerts(x_cron_secret: Optional[str] = Header(None)):
    auth_error = require_cron_secret(x_cron_secret)
    if auth_error:
        return auth_error
    return cleanup_expired_disaster_alerts()

@app.post("/api/cron/monitor-watch-areas")
async def cron_monitor_watch_areas(
    limit: int = Query(500, ge=1, le=2000),
    x_cron_secret: Optional[str] = Header(None),
):
    auth_error = require_cron_secret(x_cron_secret)
    if auth_error:
        return auth_error
    return monitor_watch_areas(limit)

async def run_disaster_pipeline(
    hours_ahead: int = Query(36, ge=1, le=168),
    alert_lead_minutes: int = Query(180, ge=1, le=1440),
    watch_area_limit: int = Query(500, ge=1, le=2000),
):
    steps = []
    errors = []

    for name, runner in [
        ("refresh_disaster_alerts", refresh_disaster_alerts),
        ("cleanup_expired_disaster_alerts", cleanup_expired_disaster_alerts),
    ]:
        try:
            result = await runner() if name == "refresh_disaster_alerts" else runner()
            steps.append({"name": name, "result": result})
            errors.extend(result.get("errors") or [])
        except Exception as e:
            error = {"step": name, "message": str(e)}
            steps.append({"name": name, "result": safe_response("error", {}, str(e), name, [error])})
            errors.append(error)

    try:
        watch_result = monitor_watch_areas(watch_area_limit)
        steps.append({"name": "monitor_watch_areas", "result": watch_result})
        errors.extend(watch_result.get("errors") or [])
    except Exception as e:
        error = {"step": "monitor_watch_areas", "message": str(e)}
        steps.append({"name": "monitor_watch_areas", "result": safe_response("error", {}, str(e), "watch_areas", [error])})
        errors.append(error)

    try:
        event_result = await monitor_event_weather_window(hours_ahead, alert_lead_minutes)
        steps.append({"name": "monitor_event_weather_window", "result": event_result})
        errors.extend(event_result.get("errors") or [])
    except Exception as e:
        error = {"step": "monitor_event_weather_window", "message": str(e)}
        steps.append({"name": "monitor_event_weather_window", "result": {"status": "error", "errors": [error]}})
        errors.append(error)

    status = "success" if not errors else "partial_success"
    return safe_response(status, {"steps": steps, "error_count": len(errors)}, "disaster pipeline completed", "cron", errors)


@app.post("/api/cron/disaster-pipeline")
async def cron_disaster_pipeline(
    background_tasks: BackgroundTasks,
    hours_ahead: int = Query(36, ge=1, le=168),
    alert_lead_minutes: int = Query(180, ge=1, le=1440),
    watch_area_limit: int = Query(500, ge=1, le=2000),
    background: bool = Query(True),
    x_cron_secret: Optional[str] = Header(None),
):
    auth_error = require_cron_secret(x_cron_secret)
    if auth_error:
        return auth_error
    if background:
        background_tasks.add_task(run_disaster_pipeline, hours_ahead, alert_lead_minutes, watch_area_limit)
        return safe_response(
            "processing",
            {"hours_ahead": hours_ahead, "alert_lead_minutes": alert_lead_minutes, "watch_area_limit": watch_area_limit},
            "disaster pipeline started in background",
            "cron",
        )
    return await run_disaster_pipeline(hours_ahead, alert_lead_minutes, watch_area_limit)

@app.get("/weather")
async def get_weather(city: str = "臺南市", district: str = "東區"):
    """前端讀取天氣專用：快取優先，沒有快取時即時補抓。"""
    try:
        res = supabase.table("weather_cache").select("*").eq("city_name", f"{city}{district}").execute()
        if res.data:
            cached = res.data[0]
            valid_until = parse_datetime(cached.get("valid_until"))
            cached["status"] = "success"
            cached["source"] = "cache"
            cached["stale"] = bool(valid_until and valid_until < taipei_now())
            return cached
    except Exception as cache_e:
        print(f"讀取天氣快取失敗: {cache_e}")

    try:
        weather_payload = await fetch_cwa_forecast(city, district, seven_day=True)
        now = taipei_now()
        return {
            "status": "success",
            "source": "cwa_live",
            "stale": False,
            "city_name": f"{city}{district}",
            "weather_data": {
                "current": weather_payload["current"],
                "forecast": weather_payload["forecast"],
                "risk_level": weather_payload["risk_level"],
                "risk_tags": weather_payload["risk_tags"],
                "has_weather_risk": weather_payload["has_weather_risk"],
            },
            "updated_at": now.isoformat(),
            "valid_until": (now + timedelta(hours=3)).isoformat(),
        }
    except Exception as e:
        return {"status": "error", "message": f"無法取得 {city}{district} 天氣資料: {str(e)}"}


@app.get("/api/weather/live")
async def get_weather_live(
    background_tasks: BackgroundTasks,
    city: str = Query("臺南市"),
    district: str = Query("東區"),
    lat: Optional[float] = Query(None),
    lng: Optional[float] = Query(None),
):
    response = await get_weather(city, district)
    if isinstance(response, dict):
        response["requested_location"] = {
            "city": city,
            "district": district,
            "lat": lat,
            "lng": lng,
        }
        if response.get("source") == "cache" and response.get("stale"):
            response["refresh_status"] = "queued"
            background_tasks.add_task(refresh_weather_cache_city, city, district)
    return response

# ==========================================
# 🚄 API 5：行程與交通工具判斷 (Events)
# ==========================================








@app.get("/api/disaster-alerts")
async def list_disaster_alerts(
    city: Optional[str] = Query(None),
    district: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
):
    return get_active_disaster_alerts(city, district, limit)


@app.post("/api/watch-areas")
async def create_watch_area(payload: WatchAreaCreate):
    try:
        data = payload.model_dump(exclude_none=True)
        data["updated_at"] = taipei_now().isoformat()
        res = supabase.table("user_watch_areas").insert(data).execute()
        return safe_response("success", res.data[0] if res.data else data, "watch area created", "user_watch_areas")
    except Exception as e:
        return safe_response("error", {}, str(e), "user_watch_areas", [{"service": "supabase", "message": str(e)}])


@app.get("/api/watch-areas")
async def list_watch_areas(
    user_id: str = Query(...),
    active_only: bool = Query(True),
    limit: int = Query(20, ge=1, le=100),
):
    try:
        query = supabase.table("user_watch_areas").select("*").eq("user_id", user_id).order("updated_at", desc=True).limit(limit)
        if active_only:
            query = query.eq("is_active", True)
        res = query.execute()
        return safe_response("success", res.data or [], "watch areas loaded", "user_watch_areas")
    except Exception as e:
        return safe_response("error", [], str(e), "user_watch_areas", [{"service": "supabase", "message": str(e)}])


@app.delete("/api/watch-areas/{watch_area_id}")
async def delete_watch_area(watch_area_id: int, user_id: str = Query(...)):
    try:
        res = (
            supabase.table("user_watch_areas")
            .update({"is_active": False, "updated_at": taipei_now().isoformat()})
            .eq("id", watch_area_id)
            .eq("user_id", user_id)
            .execute()
        )
        return safe_response("success", res.data or {"id": watch_area_id, "is_active": False}, "watch area disabled", "user_watch_areas")
    except Exception as e:
        return safe_response("error", {"id": watch_area_id}, str(e), "user_watch_areas", [{"service": "supabase", "message": str(e)}])


@app.get("/api/watch-areas/status")
async def get_watch_area_statuses(
    background_tasks: BackgroundTasks,
    user_id: str = Query(...),
    limit: int = Query(20, ge=1, le=50),
):
    areas_response = await list_watch_areas(user_id=user_id, active_only=True, limit=limit)
    if areas_response.get("status") != "success":
        return areas_response
    statuses = []
    errors = []
    for area in areas_response.get("data") or []:
        try:
            status_response = await get_area_status(
                background_tasks,
                area.get("city") or "",
                area.get("district") or "",
                area.get("lat"),
                area.get("lng"),
            )
            statuses.append({"watch_area": area, "area_status": status_response.get("data", {})})
        except Exception as e:
            errors.append({"watch_area_id": area.get("id"), "message": str(e)})
    return safe_response(
        "success" if not errors else "partial_success",
        {"items": statuses, "count": len(statuses)},
        "watch area statuses loaded",
        "watch_areas",
        errors,
    )


@app.get("/api/area-alert-notifications")
async def get_area_alert_notifications(
    user_id: str = Query(...),
    status: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
):
    try:
        query = supabase.table("area_alert_notifications").select("*").eq("user_id", user_id).order("created_at", desc=True).limit(limit)
        if status:
            query = query.eq("status", status)
        res = query.execute()
        return safe_response("success", res.data or [], "area alert notifications loaded", "area_alert_notifications")
    except Exception as e:
        return safe_response("error", [], str(e), "area_alert_notifications", [{"service": "supabase", "message": str(e)}])


@app.patch("/api/area-alert-notifications/{notification_id}/read")
async def mark_area_alert_notification_read(notification_id: int, user_id: str = Query(...)):
    try:
        res = (
            supabase.table("area_alert_notifications")
            .update({"status": "read", "read_at": taipei_now().isoformat()})
            .eq("id", notification_id)
            .eq("user_id", user_id)
            .execute()
        )
        return safe_response("success", res.data or {"id": notification_id, "status": "read"}, "area alert notification marked as read", "area_alert_notifications")
    except Exception as e:
        return safe_response("error", {"id": notification_id}, str(e), "area_alert_notifications", [{"service": "supabase", "message": str(e)}])


@app.get("/api/notifications/summary")
async def get_notifications_summary(user_id: str = Query(...)):
    errors = []
    latest = {"area_alerts": [], "event_weather_alerts": []}

    def count_table(table_name: str) -> int:
        try:
            res = (
                supabase.table(table_name)
                .select("id", count="exact")
                .eq("user_id", user_id)
                .eq("status", "unread")
                .limit(1)
                .execute()
            )
            return res.count if res.count is not None else len(res.data or [])
        except Exception as e:
            errors.append({"service": table_name, "message": str(e)})
            return 0

    area_count = count_table("area_alert_notifications")
    event_count = count_table("event_weather_alerts")
    for table_name, key in [
        ("area_alert_notifications", "area_alerts"),
        ("event_weather_alerts", "event_weather_alerts"),
    ]:
        try:
            latest_res = (
                supabase.table(table_name)
                .select("*")
                .eq("user_id", user_id)
                .eq("status", "unread")
                .order("created_at", desc=True)
                .limit(3)
                .execute()
            )
            latest[key] = latest_res.data or []
        except Exception as e:
            errors.append({"service": table_name, "message": f"latest lookup failed: {e}"})
    data = {
        "user_id": user_id,
        "unread_total": area_count + event_count,
        "area_alert_unread": area_count,
        "event_weather_alert_unread": event_count,
        "latest": latest,
    }
    return safe_response("success" if not errors else "partial_success", data, "notification summary loaded", "notifications", errors)


@app.get("/api/area/status")
async def get_area_status(
    background_tasks: BackgroundTasks,
    city: str = Query("臺南市"),
    district: str = Query("東區"),
    lat: Optional[float] = Query(None),
    lng: Optional[float] = Query(None),
):
    weather = await get_weather_live(background_tasks, city, district, lat, lng)
    alerts_response = get_active_disaster_alerts(city, district, 20)
    alerts = alerts_response.get("data") if alerts_response.get("status") == "success" else []
    if not isinstance(alerts, list):
        alerts = []
    weather_data = (weather or {}).get("weather_data") or {}
    traffic_risk = await build_traffic_risk_async(weather_data or summarize_disaster_alert_risk(alerts), None)
    destination = f"{city}{district}"
    return safe_response(
        "success",
        {
            "location": {"city": city, "district": district, "lat": lat, "lng": lng},
            "weather": weather,
            "disaster_alerts": alerts,
            "traffic_risk": traffic_risk,
            "booking_links": build_transport_links("", destination, None),
            "risk_summary": {
                "weather_risk_level": weather_data.get("risk_level"),
                "weather_risk_tags": weather_data.get("risk_tags") or [],
                **summarize_disaster_alert_risk(alerts),
            },
            "updated_at": taipei_now().isoformat(),
        },
        "area status loaded",
        "area_status",
        alerts_response.get("errors", []),
    )


async def update_event_weather_snapshot(event_id: Any, event_payload: Dict[str, Any]):
    try:
        location_parts = resolve_event_location_parts(event_payload)
        city = event_payload.get("city") or location_parts["city"]
        district = event_payload.get("district") or location_parts["district"]
        event_time = parse_datetime(event_payload.get("start_time"))
        snapshot = await build_weather_snapshot(city, district, event_time)
        update_payload = {
            "city": city,
            "district": district,
            "weather_snapshot": snapshot,
            "weather_checked_at": snapshot["captured_at"],
            "risk_level": event_payload.get("risk_level") or snapshot["risk_level"],
            "risk_tags": event_payload.get("risk_tags") or snapshot["risk_tags"],
            "has_weather_risk": bool(event_payload.get("has_weather_risk") or snapshot["has_weather_risk"]),
            "weather_alert_status": "checked",
        }
        recommendation = build_weather_suggestion(
            city,
            district,
            event_payload.get("title") or "行程",
            snapshot["weather"],
            snapshot,
        )
        if not event_payload.get("recommended_action"):
            update_payload["recommended_action"] = recommendation
        if not event_payload.get("ai_suggestion"):
            update_payload["ai_suggestion"] = recommendation
        try:
            supabase.table("events").update(update_payload).eq("id", event_id).execute()
        except Exception:
            legacy_payload = {
                key: value
                for key, value in update_payload.items()
                if key
                in {
                    "weather_snapshot",
                    "weather_checked_at",
                    "risk_level",
                    "risk_tags",
                    "has_weather_risk",
                    "recommended_action",
                    "ai_suggestion",
                }
            }
            supabase.table("events").update(legacy_payload).eq("id", event_id).execute()
    except Exception as weather_e:
        print(f"背景更新行程天氣快照失敗: {weather_e}")
        try:
            supabase.table("events").update({
                "weather_alert_status": "weather_update_failed",
                "weather_checked_at": taipei_now().isoformat(),
            }).eq("id", event_id).execute()
        except Exception:
            pass


@app.post("/events")
async def create_event(event: EventCreate, background_tasks: BackgroundTasks):
    try:
        db_payload = event.model_dump(exclude_none=True)
        db_payload["transport_type"] = event.transport_type or determine_transport_type(event.url)

        location_parts = resolve_event_location_parts(db_payload)
        db_payload["city"] = db_payload.get("city") or location_parts["city"]
        db_payload["district"] = db_payload.get("district") or location_parts["district"]

        should_refresh_weather = not db_payload.get("weather_snapshot")
        if should_refresh_weather:
            db_payload["weather_alert_status"] = "pending"

        if False and not db_payload.get("weather_snapshot"):
            try:
                event_time = parse_datetime(event.start_time)
                snapshot = await build_weather_snapshot(db_payload["city"], db_payload["district"], event_time)
                db_payload["weather_snapshot"] = snapshot
                db_payload["weather_checked_at"] = snapshot["captured_at"]
                db_payload["risk_level"] = db_payload.get("risk_level") or snapshot["risk_level"]
                db_payload["risk_tags"] = db_payload.get("risk_tags") or snapshot["risk_tags"]
                db_payload["has_weather_risk"] = event.has_weather_risk or snapshot["has_weather_risk"]
                db_payload["recommended_action"] = db_payload.get("recommended_action") or build_weather_suggestion(
                    db_payload["city"],
                    db_payload["district"],
                    event.title,
                    snapshot["weather"],
                    snapshot,
                )
                db_payload["ai_suggestion"] = db_payload.get("ai_suggestion") or db_payload["recommended_action"]
            except Exception as weather_e:
                print(f"建立行程時取得天氣快照失敗: {weather_e}")

        if event.risk_level or event.risk_tags:
            db_payload["has_weather_risk"] = event.has_weather_risk or event.risk_level in ["medium", "high"]
        
        # 寫入 events 資料表 (請確保 Supabase 已有 transport_type 欄位)
        try:
            res = supabase.table("events").insert(db_payload).execute()
        except Exception:
            legacy_keys = {
                "title", "start_time", "end_time", "url", "description",
                "transport_type", "has_weather_risk", "ai_suggestion",
                "location", "risk_level", "risk_tags", "recommended_action",
                "external_source", "external_event_id", "last_synced_at",
            }
            legacy_payload = {key: value for key, value in db_payload.items() if key in legacy_keys}
            res = supabase.table("events").insert(legacy_payload).execute()
        if res.data:
            created_event = res.data[0]
            event_id = created_event.get("id")
            if should_refresh_weather and event_id and background_tasks:
                background_tasks.add_task(update_event_weather_snapshot, event_id, {**db_payload, **created_event})
            return {"status": "success", "data": normalize_event(created_event)}
        return {"status": "error", "message": "寫入失敗"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# 注意：路由從 /events 改成了 /api/events 配合前端
@app.post("/api/events")
async def create_api_event(event: EventCreate, background_tasks: BackgroundTasks):
    return await create_event(event, background_tasks)

@app.get("/api/events")
async def get_events(
    user_id: Optional[str] = Query(None),
    from_time: Optional[str] = Query(None, alias="from"),
    to_time: Optional[str] = Query(None, alias="to"),
    limit: int = Query(100, ge=1, le=500),
):
    """前端讀取行程專用：完全符合瀚霆的 SwiftUI 契約"""
    try:
        query = supabase.table("events").select("*").order("start_time", desc=False).limit(limit)
        if user_id:
            query = query.eq("user_id", user_id)
        if from_time:
            query = query.gte("start_time", from_time)
        if to_time:
            query = query.lte("start_time", to_time)
        res = query.execute()
        events_data = res.data
        if not events_data:
            return {"status": "success", "data": []}

        return {
            "status": "success",
            "data": [normalize_event(event) for event in events_data],
        }

        formatted_events = []
        for event in events_data:
            # 確保 ai_suggestion 是純文字
            ai_text = event.get("ai_suggestion")
            if isinstance(ai_text, dict):
                # 如果 DB 裡還是存 JSON，自動幫它轉成純文字組合
                ai_text = f"{ai_text.get('reason', '')} 建議備案：{ai_text.get('alternative_location', '')}"
            
            formatted_event = {
                "id": event.get("id"),
                "title": event.get("title", "未命名行程"),
                "start_time": event.get("start_time"),
                "end_time": event.get("end_time"),
                "url": event.get("url"), # 退回使用 url，配合前端合約
                "transport_type": event.get("transport_type"),
                "has_weather_risk": event.get("has_weather_risk", False),
                "ai_suggestion": ai_text # 這裡必須是純文字
            }
            formatted_events.append(formatted_event)

        return {
            "status": "success",
            "data": formatted_events
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}



@app.post("/api/integrations/timetree/sync")
async def sync_timetree_events():
    fetched = await fetch_timetree_events()
    if fetched["status"] != "success":
        return fetched

    created = []
    updated = []
    errors = []
    for event_payload in fetched["data"].get("events", []):
        try:
            existing = supabase.table("events").select("id").eq("external_source", "timetree").eq("external_event_id", event_payload["external_event_id"]).limit(1).execute()
            if existing.data:
                event_id = existing.data[0]["id"]
                supabase.table("events").update(event_payload).eq("id", event_id).execute()
                updated.append(event_payload["external_event_id"])
            else:
                supabase.table("events").insert(event_payload).execute()
                created.append(event_payload["external_event_id"])
        except Exception as e:
            errors.append({"external_event_id": event_payload.get("external_event_id"), "message": str(e)})

    status = "success" if not errors else "partial_success"
    payload = {"created": created, "updated": updated, "errors": errors}
    log_sync("timetree_sync", status, "TimeTree sync completed", "timetree", payload)
    return safe_response(status, payload, "TimeTree sync completed", "timetree", errors)
    

@app.post("/api/events/risk-check")
async def check_event_risk(payload: EventRiskCheckRequest):
    try:
        risk_result = await build_event_risk(payload)
        return {"status": "success", "data": risk_result}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/events/weather-monitor")
async def run_event_weather_monitor(
    background_tasks: BackgroundTasks,
    hours_ahead: int = Query(36, ge=1, le=168),
    alert_lead_minutes: int = Query(180, ge=1, le=1440),
    background: bool = False,
):
    if background:
        background_tasks.add_task(monitor_event_weather_window, hours_ahead, alert_lead_minutes)
        return {
            "status": "processing",
            "message": f"已開始背景檢查未來 {hours_ahead} 小時的行程；只會在行程前 {alert_lead_minutes} 分鐘內產生提醒。",
        }
    return await monitor_event_weather_window(hours_ahead, alert_lead_minutes)

@app.get("/api/events/weather-alerts")
async def get_event_weather_alerts(
    user_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
):
    try:
        query = supabase.table("event_weather_alerts").select("*").order("created_at", desc=True).limit(limit)
        if user_id:
            query = query.eq("user_id", user_id)
        if status:
            query = query.eq("status", status)
        res = query.execute()
        return safe_response("success", res.data or [], "weather alerts loaded", "event_weather_alerts")
    except Exception as e:
        return safe_response("error", [], str(e), "event_weather_alerts", [{"service": "supabase", "message": str(e)}])


@app.patch("/api/events/weather-alerts/{alert_id}/read")
async def mark_event_weather_alert_read(alert_id: int):
    try:
        updated = supabase.table("event_weather_alerts").update({
            "status": "read",
        }).eq("id", alert_id).execute()
        return safe_response("success", updated.data or {"id": alert_id, "status": "read"}, "weather alert marked as read", "event_weather_alerts")
    except Exception as e:
        return safe_response("error", {"id": alert_id}, str(e), "event_weather_alerts", [{"service": "supabase", "message": str(e)}])


@app.get("/api/ai/usage-summary")
async def get_ai_usage_summary():
    return summarize_ai_usage()


@app.get("/api/ai/local-rules")
async def get_local_ai_rules():
    return safe_response("success", load_local_ai_rules(), "local AI rules loaded", "local_rules")


@app.post("/api/ai/local-suggest")
async def local_ai_suggest(payload: LocalAIRequest):
    event = {
        "title": payload.title or "",
        "location": payload.location or "",
        "city": payload.city or "",
        "district": payload.district or "",
        "activity": payload.activity or "",
        "transport_type": payload.transport_type or "",
    }
    risk = {
        "risk_level": payload.risk_level or "low",
        "risk_tags": payload.risk_tags,
        "has_weather_risk": payload.risk_level not in [None, "low"] or bool(payload.risk_tags),
    }
    data = build_local_ai_suggestion(
        event,
        {"weather": payload.weather},
        risk,
    )
    return safe_response("success", data, "local AI suggestion generated", "local_rules")


@app.get("/api/weather/cache-status")
async def get_weather_cache_status(limit: int = Query(30, ge=1, le=100)):
    return summarize_weather_cache(limit)


@app.post("/api/weather/cache-refresh")
async def refresh_weather_cache_endpoint(
    city: str = Query(...),
    district: str = Query(...),
):
    return await refresh_weather_cache_city(city, district)

@app.get("/api/briefing/today")
async def get_today_briefing():
    try:
        today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        res = supabase.table("events").select("*").gte("start_time", f"{today}T00:00:00").lt("start_time", f"{today}T23:59:59").execute()
        events = res.data or []
        alerts = []

        for event in events:
            normalized = normalize_event(event)
            risk = analyze_text_risk(json.dumps(normalized, ensure_ascii=False))
            risk_level = normalized.get("risk_level") or risk["risk_level"]
            risk_tags = normalized.get("risk_tags") or risk["risk_tags"]
            has_risk = normalized.get("has_weather_risk") or risk_level != "low"
            if has_risk:
                alerts.append({
                    "event_id": normalized.get("id"),
                    "title": normalized.get("title"),
                    "start_time": normalized.get("start_time"),
                    "location": normalized.get("location"),
                    "risk_level": risk_level,
                    "risk_tags": risk_tags,
                    "message": normalized.get("ai_suggestion") or build_recommended_action(risk_level, risk_tags, normalized.get("location")),
                })

        summary = f"今天共有 {len(alerts)} 個行程需要注意天氣或災害風險。" if alerts else "今天行程目前沒有明顯天氣風險，仍建議出門前確認最新預報。"
        return {
            "status": "success",
            "date": today,
            "summary": summary,
            "alerts": alerts,
            "events": [normalize_event(event) for event in events],
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/alerts")
async def get_alerts():
    """前端讀取突發警報專用：觸發紅色警告圖卡與情境推播"""
    try:
        # 從 Supabase 的 weather_alerts 表撈取最新的警報
        # order("created_at", desc=True) 確保最新的警報排在最前面，limit(5) 只取最近 5 筆
        res = supabase.table("weather_alerts").select("*").order("created_at", desc=True).limit(5).execute()
        
        alerts_data = res.data
        if not alerts_data:
            return {"status": "success", "data": [], "message": "目前全台無特殊氣象警報"}

        return {
            "status": "success",
            "data": alerts_data
        }

    except Exception as e:
        print(f"❌ 讀取警報失敗: {e}")
        return {"status": "error", "message": f"伺服器錯誤: {str(e)}"}

@app.post("/api/emergency-kit/vision-check")
async def check_emergency_kit_image(payload: EmergencyKitVisionRequest):
    allowed_types = {"image/jpeg", "image/png", "image/webp"}
    if payload.mime_type not in allowed_types:
        return safe_response(
            "error",
            {"detected_items": [], "missing_items": REQUIRED_EMERGENCY_KIT_ITEMS},
            "只支援 jpeg/png/webp 圖片。",
            "validation",
        )

    image_base64 = payload.image_base64
    if "," in image_base64:
        image_base64 = image_base64.split(",", 1)[1]
    try:
        image_bytes = base64.b64decode(image_base64, validate=True)
    except Exception:
        return safe_response(
            "error",
            {"detected_items": [], "missing_items": REQUIRED_EMERGENCY_KIT_ITEMS},
            "image_base64 格式錯誤。",
            "validation",
        )

    if len(image_bytes) > 8 * 1024 * 1024:
        return safe_response(
            "error",
            {"detected_items": [], "missing_items": REQUIRED_EMERGENCY_KIT_ITEMS},
            "圖片超過 8MB，請壓縮後再上傳。",
            "validation",
        )

    image_hash = hashlib.sha256(image_bytes).hexdigest()
    errors = []
    try:
        cached_query = supabase.table("emergency_kit_scans").select("*").eq("image_hash", image_hash).order("created_at", desc=True).limit(1)
        if payload.user_id:
            cached_query = cached_query.eq("user_id", payload.user_id)
        cached_scan = cached_query.execute()
        if cached_scan.data:
            cached = cached_scan.data[0]
            cached_result = {
                "user_id": payload.user_id or cached.get("user_id"),
                "kit_id": payload.kit_id or cached.get("kit_id"),
                "detected_items": cached.get("detected_items") or [],
                "missing_items": cached.get("missing_items") or REQUIRED_EMERGENCY_KIT_ITEMS,
                "extra_items": cached.get("extra_items") or [],
                "confidence": cached.get("confidence") or 0,
                "supplement_suggestions": [f"請補齊 {item}" for item in (cached.get("missing_items") or REQUIRED_EMERGENCY_KIT_ITEMS)],
                "daily_limit": VISION_DAILY_LIMIT,
                "notes": cached.get("notes") or "",
                "checked_at": cached.get("created_at"),
                "image_hash": image_hash,
                "cache_hit": True,
            }
            return safe_response("success", cached_result, "emergency kit vision cache hit", "emergency_kit_scans")
    except Exception as e:
        print(f"Vision cache lookup skipped: {e}")

    if payload.user_id:
        try:
            today = taipei_now().date().isoformat()
            usage = (
                supabase.table("emergency_kit_scans")
                .select("id", count="exact")
                .eq("user_id", payload.user_id)
                .gte("created_at", f"{today}T00:00:00+08:00")
                .limit(1)
                .execute()
            )
            usage_count = usage.count if usage.count is not None else len(usage.data or [])
            if usage_count >= VISION_DAILY_LIMIT:
                return safe_response(
                    "error",
                    {"detected_items": [], "missing_items": REQUIRED_EMERGENCY_KIT_ITEMS, "daily_limit": VISION_DAILY_LIMIT},
                    "今日避難包辨識次數已達上限。",
                    "quota",
                )
        except Exception as e:
            errors.append({"service": "supabase", "message": f"Vision quota check failed: {e}"})

    prompt = (
        "你是台灣防災避難包檢查助手。請辨識圖片中出現的避難物資。"
        "只回傳 JSON，不要 markdown。"
        f"必要物資清單:{json.dumps(REQUIRED_EMERGENCY_KIT_ITEMS, ensure_ascii=False)}。"
        "JSON 欄位: detected_items(陣列), missing_items(陣列), extra_items(陣列), confidence(0到1), notes(字串)。"
    )
    vision_result = await call_gemini_vision(image_bytes, payload.mime_type, prompt)
    detected_items = vision_result.get("detected_items") or []
    if not isinstance(detected_items, list):
        detected_items = []
    detected_text = " ".join(str(item) for item in detected_items)
    missing_items = [
        item for item in REQUIRED_EMERGENCY_KIT_ITEMS
        if item not in detected_items and item not in detected_text
    ]
    if isinstance(vision_result.get("missing_items"), list) and vision_result.get("missing_items"):
        missing_items = sorted(set(missing_items + [str(item) for item in vision_result["missing_items"]]))

    result = {
        "user_id": payload.user_id,
        "kit_id": payload.kit_id,
        "detected_items": [str(item) for item in detected_items],
        "missing_items": missing_items,
        "extra_items": vision_result.get("extra_items") or [],
        "confidence": vision_result.get("confidence") or 0,
        "supplement_suggestions": [f"請補齊：{item}" for item in missing_items],
        "daily_limit": VISION_DAILY_LIMIT,
        "notes": vision_result.get("notes") or ("Gemini Vision 未回傳結果，請重新拍攝清楚的避難包照片。" if not vision_result else ""),
        "checked_at": taipei_now().isoformat(),
        "image_hash": image_hash,
        "cache_hit": False,
    }

    try:
        scan_payload = {
            "user_id": payload.user_id,
            "kit_id": payload.kit_id,
            "image_hash": image_hash,
            "detected_items": result["detected_items"],
            "missing_items": result["missing_items"],
            "extra_items": result["extra_items"],
            "confidence": result["confidence"],
            "notes": result["notes"],
            "created_at": result["checked_at"],
        }
        try:
            supabase.table("emergency_kit_scans").insert(scan_payload).execute()
        except Exception:
            scan_payload.pop("image_hash", None)
            supabase.table("emergency_kit_scans").insert(scan_payload).execute()
    except Exception as e:
        errors.append({"service": "supabase", "message": f"emergency_kit_scans 寫入失敗: {e}"})

    return safe_response("success" if not errors else "partial_success", result, "emergency kit vision check completed", "gemini_vision", errors)


@app.get("/api/emergency-kit/scans")
async def get_emergency_kit_scans(
    user_id: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
):
    try:
        query = supabase.table("emergency_kit_scans").select("*").order("created_at", desc=True).limit(limit)
        if user_id:
            query = query.eq("user_id", user_id)
        res = query.execute()
        return safe_response("success", res.data or [], "emergency kit scans loaded", "emergency_kit_scans")
    except Exception as e:
        return safe_response("error", [], str(e), "emergency_kit_scans", [{"service": "supabase", "message": str(e)}])
     
@app.get("/api/guidelines")
async def get_guidelines(
    activity: Optional[str] = Query(None),
    disaster: Optional[str] = Query(None),
    user_activity: Optional[str] = Query(None),
    disaster_type: Optional[str] = Query(None),
):
    """
    情境感知推播專用：前端傳入狀態與災害，後端回傳避難圖卡文字
    範例網址：/api/guidelines?activity=driving&disaster=大雨
    """
    try:
        resolved_activity = activity or user_activity
        resolved_disaster = normalize_disaster_code(disaster or disaster_type)
        if not resolved_activity or not resolved_disaster:
            return {
                "status": "error",
                "message": "Missing activity/disaster. Example: /api/guidelines?activity=driving&disaster=earthquake",
            }

        # 直接使用翊翔提供的 SQL 邏輯，轉成 Supabase 語法
        res = supabase.table("disaster_guidelines") \
            .select("instruction, priority") \
            .eq("user_activity", resolved_activity) \
            .eq("disaster_type", resolved_disaster) \
            .execute()
        
        if res.data:
            return {
                "status": "success",
                "data": res.data[0] # 回傳符合條件的第一筆指引
            }
        else:
            fallback_priority = "high" if resolved_disaster in ["flood", "typhoon"] else "medium"
            return {
                "status": "success",
                "data": {
                    "instruction": build_recommended_action(fallback_priority, [resolved_disaster], "目前位置"),
                    "priority": fallback_priority,
                    "disaster_type": resolved_disaster,
                },
            }
            return {
                "status": "success", 
                "data": {"instruction": "請注意安全，隨時留意氣象變化。", "priority": "low"}
            }
            
    except Exception as e:
        print(f"❌ 讀取避難指引失敗: {e}")
        return {"status": "error", "message": str(e)}
@app.get("/api/game/questions")
async def get_game_questions(type: str = Query("flood")):
    game_type = normalize_disaster_code(type)
    questions = GAME_QUESTIONS.get(game_type, [])
    return {
        "status": "success",
        "type": game_type,
        "data": [
            {
                "id": question["id"],
                "question": question["question"],
                "choices": question["choices"],
            }
            for question in questions
        ],
    }


@app.get("/api/quiz/generate")
async def generate_quiz(topic: str = Query("flood"), user_id: Optional[str] = Query(None)):
    quiz_type = normalize_disaster_code(topic)
    questions = GAME_QUESTIONS.get(quiz_type) or GAME_QUESTIONS.get("flood", [])
    return {
        "status": "success",
        "data": {
            "user_id": user_id,
            "topic": quiz_type,
            "questions": [
                {
                    "id": question["id"],
                    "question": question["question"],
                    "choices": question["choices"],
                }
                for question in questions
            ],
        },
    }


@app.post("/api/quiz/submit")
async def submit_quiz_score(payload: QuizScoreSubmitRequest):
    score_data = {
        "player_name": payload.user_id or "guest",
        "game_type": normalize_disaster_code(payload.topic or "quiz"),
        "score": payload.score,
        "total_questions": None,
        "correct_count": None,
        "created_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "is_verified": payload.is_verified,
    }
    db_payload = {key: value for key, value in score_data.items() if key != "is_verified"}
    try:
        res = supabase.table("game_scores").insert(db_payload).execute()
        if res.data:
            return {
                "status": "success",
                "data": {**res.data[0], "is_verified": payload.is_verified},
                "source": "supabase",
            }
    except Exception:
        GAME_SCORE_MEMORY.append(db_payload)

    return {
        "status": "success",
        "data": score_data,
        "source": "memory_fallback",
    }

@app.post("/api/game/submit")
async def submit_game_answer(payload: GameSubmitRequest):
    game_types = [normalize_disaster_code(payload.game_type)] if payload.game_type else list(GAME_QUESTIONS.keys())
    for game_type in game_types:
        for question in GAME_QUESTIONS.get(game_type, []):
            if question["id"] == payload.question_id:
                is_correct = payload.selected_index == question["answer"]
                return {
                    "status": "success",
                    "data": {
                        "question_id": payload.question_id,
                        "correct": is_correct,
                        "score": 10 if is_correct else 0,
                        "correct_index": question["answer"],
                        "explanation": question["explanation"],
                    },
                }

    return {"status": "error", "message": "Question not found"}

@app.post("/api/game/scores")
async def create_game_score(payload: GameScoreCreate):
    score_data = payload.model_dump()
    score_data["created_at"] = datetime.now(timezone(timedelta(hours=8))).isoformat()

    try:
        res = supabase.table("game_scores").insert(score_data).execute()
        if res.data:
            return {"status": "success", "data": res.data[0], "source": "supabase"}
    except Exception:
        GAME_SCORE_MEMORY.append(score_data)

    return {"status": "success", "data": score_data, "source": "memory_fallback"}

@app.get("/api/game/scores")
async def get_game_scores(game_type: Optional[str] = None, limit: int = 10):
    try:
        query = supabase.table("game_scores").select("*").order("score", desc=True).limit(limit)
        if game_type:
            query = query.eq("game_type", normalize_disaster_code(game_type))
        res = query.execute()
        return {"status": "success", "data": res.data or [], "source": "supabase"}
    except Exception:
        scores = GAME_SCORE_MEMORY
        if game_type:
            normalized_type = normalize_disaster_code(game_type)
            scores = [score for score in scores if score.get("game_type") == normalized_type]
        scores = sorted(scores, key=lambda item: item.get("score", 0), reverse=True)[:limit]
        return {"status": "success", "data": scores, "source": "memory_fallback"}

@app.post("/api/location/geocode")
async def post_geocode_location(payload: GeocodeRequest):
    data = geocode_fallback(payload.query)
    return {"status": "success", "data": data}

@app.get("/api/location/geocode")
async def get_geocode_location(query: str):
    data = geocode_fallback(query)
    return {"status": "success", "data": data}

@app.get("/api/shelters")
async def get_shelters(city: Optional[str] = None, district: Optional[str] = None):
    try:
        query = supabase.table("shelters").select("*")
        if city:
            query = query.eq("city", city)
        if district:
            query = query.eq("district", district)
        res = query.execute()
        shelters = res.data or []
        if shelters:
            return {"status": "success", "data": [normalize_shelter(item) for item in shelters], "source": "supabase"}
    except Exception:
        pass

    shelters = SHELTER_FALLBACKS
    if city:
        shelters = [item for item in shelters if item.get("city") == city]
    if district:
        shelters = [item for item in shelters if item.get("district") == district]
    return {"status": "success", "data": [normalize_shelter(item) for item in shelters], "source": "fallback"}

@app.get("/api/shelters/nearby")
async def get_nearby_shelters(lat: float, lng: float, limit: int = 5):
    try:
        res = supabase.table("shelters").select("*").execute()
        shelters = res.data or []
    except Exception:
        shelters = SHELTER_FALLBACKS

    if not shelters:
        shelters = SHELTER_FALLBACKS

    normalized = [normalize_shelter(item, lat, lng) for item in shelters]
    normalized.sort(key=lambda item: item.get("distance_km", 999999))
    return {"status": "success", "data": normalized[:limit]}

@app.get("/api/database/schema")
async def get_database_schema_sql():
    sql = """
alter table public.events add column if not exists location text;
alter table public.events add column if not exists city text;
alter table public.events add column if not exists district text;
alter table public.events add column if not exists risk_level text default 'low';
alter table public.events add column if not exists risk_tags jsonb default '[]'::jsonb;
alter table public.events add column if not exists recommended_action text;
alter table public.events add column if not exists weather_snapshot jsonb;
alter table public.events add column if not exists weather_checked_at timestamptz;
alter table public.events add column if not exists weather_alert_status text default 'none';
alter table public.events add column if not exists external_source text;
alter table public.events add column if not exists external_event_id text;
alter table public.events add column if not exists last_synced_at timestamptz;

alter table public.sync_logs add column if not exists source text default 'backend';
alter table public.sync_logs add column if not exists payload jsonb default '{}'::jsonb;
alter table public.sync_logs add column if not exists finished_at timestamptz;

create table if not exists public.ai_suggestion_cache (
  cache_key text primary key,
  prompt_type text,
  subject text,
  context_hash text,
  response jsonb default '{}'::jsonb,
  created_at timestamptz default now()
);

create table if not exists public.event_weather_alerts (
  id bigint generated by default as identity primary key,
  event_id text,
  title text,
  message text not null,
  severity text default 'medium',
  change_summary jsonb default '{}'::jsonb,
  suggested_location text,
  status text default 'unread',
  created_at timestamptz default now()
);

create table if not exists public.emergency_kit_scans (
  id bigint generated by default as identity primary key,
  user_id text,
  kit_id text,
  detected_items jsonb default '[]'::jsonb,
  missing_items jsonb default '[]'::jsonb,
  extra_items jsonb default '[]'::jsonb,
  confidence double precision default 0,
  notes text,
  created_at timestamptz default now()
);

create table if not exists public.shelters (
  id text primary key,
  name text not null,
  city text,
  district text,
  address text,
  lat double precision not null,
  lng double precision not null,
  capacity integer,
  shelter_type text default 'shelter',
  created_at timestamptz default now()
);

create table if not exists public.game_scores (
  id bigint generated by default as identity primary key,
  player_name text default 'guest',
  game_type text not null,
  score integer not null,
  total_questions integer,
  correct_count integer,
  created_at timestamptz default now()
);
"""
    return {"status": "success", "sql": sql.strip()}

@app.get("/api/transport/options")
async def get_transport_options(
    from_location: str = Query("", alias="from"),
    to: str = Query(""),
):
    origin = quote(from_location)
    destination = quote(to)
    maps_url = f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={destination}&travelmode=transit"
    return {
        "status": "success",
        "data": [
            {
                "transport_type": "thsrc",
                "title": "高鐵訂票",
                "url": "https://www.thsrc.com.tw/",
            },
            {
                "transport_type": "tra",
                "title": "台鐵訂票",
                "url": "https://www.railway.gov.tw/",
            },
            {
                "transport_type": "maps",
                "title": "Google Maps 路線",
                "url": maps_url,
            },
        ],
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
