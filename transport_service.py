from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from config import TDX_CLIENT_ID, TDX_CLIENT_SECRET
from utils import safe_int

def determine_transport_type(url: Optional[str]) -> Optional[str]:
    """根據網址判斷是台鐵(tra)還是高鐵(thsrc)"""
    if not url: return None
    url_lower = url.lower()
    if "railway.gov.tw" in url_lower or "tra" in url_lower: return "tra"
    elif "thsrc.com.tw" in url_lower: return "thsrc"
    return None


def build_transport_links(origin: str = "", destination: str = "", transport_type: Optional[str] = None) -> List[Dict[str, str]]:
    origin_q = quote(origin or "")
    destination_q = quote(destination or "")
    links = []
    if transport_type in [None, "", "thsrc"]:
        links.append({"transport_type": "thsrc", "title": "高鐵訂票", "url": "https://www.thsrc.com.tw/"})
    if transport_type in [None, "", "tra"]:
        links.append({"transport_type": "tra", "title": "台鐵訂票", "url": "https://www.railway.gov.tw/"})
    links.append({
        "transport_type": "maps",
        "title": "Google Maps 路線",
        "url": f"https://www.google.com/maps/dir/?api=1&origin={origin_q}&destination={destination_q}&travelmode=transit",
    })
    return links


def build_traffic_risk(weather_snapshot: Dict[str, Any], transport_type: Optional[str] = None) -> Dict[str, Any]:
    risk_tags = weather_snapshot.get("risk_tags") or []
    weather = weather_snapshot.get("weather") or weather_snapshot.get("current") or {}
    warnings = []
    level = weather_snapshot.get("risk_level") or "low"

    if "heavy_rain" in risk_tags:
        warnings.append("大雨可能造成道路積水、班次延誤或步行轉乘不便。")
    if "strong_wind" in risk_tags:
        warnings.append("強風可能影響高架路段、機車與戶外候車安全。")
    if safe_int(weather.get("pop")) >= 40:
        warnings.append("有降雨機率，建議預留交通緩衝時間。")
    if transport_type == "tra":
        warnings.append("台鐵受天候與路線狀況影響時，請出發前確認即時營運公告。")
    if transport_type == "thsrc":
        warnings.append("高鐵通常較穩定，但仍建議確認班次與接駁交通。")

    return {
        "level": level,
        "warnings": warnings or ["目前未偵測到明顯交通天氣風險。"],
        "tdx_status": "not_configured",
        "tdx_message": "目前未串接 TDX；先回傳高鐵/台鐵官方訂票與 Google Maps 備援連結。",
    }


async def fetch_tdx_token() -> Optional[str]:
    if not TDX_CLIENT_ID or not TDX_CLIENT_SECRET:
        return None
    token_url = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": TDX_CLIENT_ID,
        "client_secret": TDX_CLIENT_SECRET,
    }
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(token_url, data=data, timeout=15.0)
            res.raise_for_status()
            return res.json().get("access_token")
    except Exception:
        return None


async def build_tdx_status(transport_type: Optional[str]) -> Dict[str, Any]:
    if not TDX_CLIENT_ID or not TDX_CLIENT_SECRET:
        return {
            "tdx_status": "not_configured",
            "tdx_message": "TDX_CLIENT_ID 或 TDX_CLIENT_SECRET 未設定，使用官方訂票連結 fallback。",
        }
    token = await fetch_tdx_token()
    if not token:
        return {
            "tdx_status": "error",
            "tdx_message": "TDX token 取得失敗，使用官方訂票連結 fallback。",
        }
    return {
        "tdx_status": "success",
        "tdx_message": f"TDX token 可用；{transport_type or 'transit'} 進階營運資料可在下一階段接入指定 endpoint。",
    }


async def build_traffic_risk_async(weather_snapshot: Dict[str, Any], transport_type: Optional[str] = None) -> Dict[str, Any]:
    risk = build_traffic_risk(weather_snapshot, transport_type)
    risk.update(await build_tdx_status(transport_type))
    return risk


