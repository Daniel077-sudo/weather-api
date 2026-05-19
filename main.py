import os
import json
import asyncio
import math
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from dotenv import load_dotenv
import httpx
from fastapi import FastAPI, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn
from supabase import create_client, Client

# 載入金鑰 (堅持使用環境變數，保護安全)
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

def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def maps_url(lat: float, lng: float) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"

def normalize_shelter(shelter: Dict[str, Any], origin_lat: Optional[float] = None, origin_lng: Optional[float] = None) -> Dict[str, Any]:
    lat = float(shelter.get("lat") or shelter.get("latitude") or 0)
    lng = float(shelter.get("lng") or shelter.get("longitude") or 0)
    result = {
        "id": shelter.get("id"),
        "name": shelter.get("name") or "",
        "city": shelter.get("city") or "",
        "district": shelter.get("district") or "",
        "address": shelter.get("address") or "",
        "lat": lat,
        "lng": lng,
        "capacity": shelter.get("capacity"),
        "shelter_type": shelter.get("shelter_type") or "shelter",
        "maps_url": maps_url(lat, lng),
    }
    if origin_lat is not None and origin_lng is not None:
        result["distance_km"] = round(haversine_km(origin_lat, origin_lng, lat, lng), 2)
    return result

def geocode_fallback(query: str) -> Dict[str, Any]:
    if query in GEOCODE_FALLBACKS:
        return GEOCODE_FALLBACKS[query]

    for name, data in GEOCODE_FALLBACKS.items():
        if query in name or name in query:
            return data

    return {
        "name": query,
        "lat": 25.0478,
        "lng": 121.5170,
        "city": "台北市",
        "district": "中正區",
        "note": "fallback_default_location",
    }

# ==========================================
# 🔑 金鑰與連線區
# ==========================================
CWA_API_KEY = os.getenv("CWA_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ==========================================
# 🛠️ 共用工具函式區
# ==========================================
async def call_gemini_raw(prompt: str):
    """非同步呼叫 Gemini AI，避免拖垮主執行緒"""
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

def find_district(data, target):
    if isinstance(data, dict):
        if data.get("locationName") == target or data.get("LocationName") == target: return data
        for k, v in data.items():
            found = find_district(v, target)
            if found: return found
    elif isinstance(data, list):
        for item in data:
            found = find_district(item, target)
            if found: return found
    return None

def determine_transport_type(url: Optional[str]) -> Optional[str]:
    """根據網址判斷是台鐵(tra)還是高鐵(thsrc)"""
    if not url: return None
    url_lower = url.lower()
    if "railway.gov.tw" in url_lower or "tra" in url_lower: return "tra"
    elif "thsrc.com.tw" in url_lower: return "thsrc"
    return None

def normalize_event(event: dict) -> dict:
    """Return the exact JSON shape expected by the iOS frontend."""
    ai_text = event.get("ai_suggestion")
    if isinstance(ai_text, dict):
        reason = ai_text.get("reason") or ""
        alternative = ai_text.get("alternative_location") or ""
        ai_text = " ".join(part for part in [reason, alternative] if part)
    elif ai_text is None:
        ai_text = ""

    event_url = event.get("url") or event.get("transport_ticket_link") or ""
    transport_type = event.get("transport_type") or determine_transport_type(event_url)

    risk_tags = event.get("risk_tags") or []
    if isinstance(risk_tags, str):
        risk_tags = [tag.strip() for tag in risk_tags.split(",") if tag.strip()]

    return {
        "id": event.get("id"),
        "title": event.get("title") or "",
        "start_time": event.get("start_time"),
        "end_time": event.get("end_time"),
        "location": event.get("location") or event.get("location_name") or "",
        "url": event_url,
        "transport_type": transport_type or "",
        "has_weather_risk": bool(event.get("has_weather_risk", False)),
        "ai_suggestion": str(ai_text),
        "risk_level": event.get("risk_level") or ("medium" if event.get("has_weather_risk") else "low"),
        "risk_tags": risk_tags,
        "recommended_action": event.get("recommended_action") or str(ai_text),
    }

DISASTER_CODE_ALIASES = {
    "地震": "earthquake",
    "earthquake": "earthquake",
    "大雨": "heavy_rain",
    "豪雨": "heavy_rain",
    "heavy_rain": "heavy_rain",
    "淹水": "flood",
    "洪水": "flood",
    "flood": "flood",
    "颱風": "typhoon",
    "台風": "typhoon",
    "typhoon": "typhoon",
    "濃霧": "fog",
    "fog": "fog",
    "強風": "strong_wind",
    "strong_wind": "strong_wind",
    "火災": "fire",
    "fire": "fire",
}

RISK_KEYWORDS = {
    "heavy_rain": ["大雨", "豪雨", "降雨", "雷雨", "rain"],
    "flood": ["淹水", "積水", "低窪", "溪水", "flood"],
    "typhoon": ["颱風", "強颱", "typhoon"],
    "fog": ["濃霧", "低能見度", "fog"],
    "strong_wind": ["強風", "陣風", "wind"],
    "earthquake": ["地震", "earthquake"],
}

GAME_QUESTIONS = {
    "flood": [
        {
            "id": "flood-1",
            "question": "遇到淹水地下道時，最安全的做法是？",
            "choices": ["快速通過", "停下並改道", "跟著前車走"],
            "answer": 1,
            "explanation": "地下道積水深度很難判斷，車輛可能熄火或被困，應立即改道。",
        },
        {
            "id": "flood-2",
            "question": "暴雨時看到河水暴漲，應該怎麼做？",
            "choices": ["靠近拍照", "遠離河道與堤防", "站在橋下避雨"],
            "answer": 1,
            "explanation": "河水暴漲時應遠離河道、堤防與橋下，避免被急流或落石影響。",
        },
    ],
    "earthquake": [
        {
            "id": "earthquake-1",
            "question": "地震發生當下在室內，第一步應該做什麼？",
            "choices": ["衝去搭電梯", "趴下、掩護、穩住", "站在窗邊觀察"],
            "answer": 1,
            "explanation": "地震當下應先保護頭頸，採取趴下、掩護、穩住。",
        }
    ],
    "typhoon": [
        {
            "id": "typhoon-1",
            "question": "颱風來臨前，哪個準備最重要？",
            "choices": ["固定門窗並準備飲水與手電筒", "到海邊看浪", "把車停在地下低窪處"],
            "answer": 0,
            "explanation": "颱風前應固定門窗、準備飲水與照明，並避免海邊與低窪地區。",
        }
    ],
    "fire": [
        {
            "id": "fire-1",
            "question": "火災逃生時遇到濃煙，應該怎麼移動？",
            "choices": ["低姿勢沿牆移動", "站直快速奔跑", "搭電梯下樓"],
            "answer": 0,
            "explanation": "濃煙會往上竄，應低姿勢沿牆移動，並避免搭電梯。",
        }
    ],
}

GEOCODE_FALLBACKS = {
    "台北車站": {"name": "台北車站", "lat": 25.0478, "lng": 121.5170, "city": "台北市", "district": "中正區"},
    "台北市政府": {"name": "台北市政府", "lat": 25.0375, "lng": 121.5645, "city": "台北市", "district": "信義區"},
    "台中車站": {"name": "台中車站", "lat": 24.1368, "lng": 120.6850, "city": "台中市", "district": "中區"},
    "高雄左營": {"name": "高雄左營", "lat": 22.6880, "lng": 120.3090, "city": "高雄市", "district": "左營區"},
    "高雄車站": {"name": "高雄車站", "lat": 22.6395, "lng": 120.3020, "city": "高雄市", "district": "三民區"},
}

SHELTER_FALLBACKS = [
    {
        "id": "shelter-tpe-001",
        "name": "台北車站地下街臨時避難點",
        "city": "台北市",
        "district": "中正區",
        "address": "台北市中正區忠孝西路一段",
        "lat": 25.0478,
        "lng": 121.5170,
        "capacity": 300,
        "shelter_type": "temporary",
    },
    {
        "id": "shelter-tpe-002",
        "name": "信義國小活動中心",
        "city": "台北市",
        "district": "信義區",
        "address": "台北市信義區松勤街",
        "lat": 25.0330,
        "lng": 121.5660,
        "capacity": 500,
        "shelter_type": "school",
    },
    {
        "id": "shelter-txg-001",
        "name": "台中公園避難廣場",
        "city": "台中市",
        "district": "中區",
        "address": "台中市中區公園路",
        "lat": 24.1447,
        "lng": 120.6847,
        "capacity": 800,
        "shelter_type": "park",
    },
    {
        "id": "shelter-khh-001",
        "name": "左營高中活動中心",
        "city": "高雄市",
        "district": "左營區",
        "address": "高雄市左營區海功路",
        "lat": 22.6890,
        "lng": 120.2940,
        "capacity": 600,
        "shelter_type": "school",
    },
]

GAME_SCORE_MEMORY: List[Dict[str, Any]] = []

def normalize_disaster_code(disaster: Optional[str]) -> str:
    if not disaster:
        return ""
    return DISASTER_CODE_ALIASES.get(disaster, disaster)

def analyze_text_risk(text: str) -> Dict[str, Any]:
    lowered = text.lower()
    tags = []
    for tag, keywords in RISK_KEYWORDS.items():
        if any(keyword.lower() in lowered for keyword in keywords):
            tags.append(tag)

    if any(tag in tags for tag in ["flood", "typhoon"]):
        level = "high"
    elif tags:
        level = "medium"
    else:
        level = "low"

    return {
        "has_weather_risk": level != "low",
        "risk_level": level,
        "risk_tags": tags,
    }

def build_recommended_action(risk_level: str, risk_tags: List[str], location: str = "") -> str:
    if risk_level == "low":
        return "目前未偵測到明顯天氣風險，仍建議出門前確認最新預報。"
    if "flood" in risk_tags:
        return f"{location}可能有淹水或積水風險，請避開地下道、河堤與低窪路段。"
    if "heavy_rain" in risk_tags:
        return f"{location}可能有大雨風險，建議提早出門並攜帶雨具。"
    if "strong_wind" in risk_tags:
        return f"{location}可能有強風風險，請避開招牌、路樹與施工圍籬。"
    if "fog" in risk_tags:
        return f"{location}可能有濃霧或低能見度，交通移動請放慢速度。"
    if "typhoon" in risk_tags:
        return f"{location}可能受颱風影響，非必要請減少外出並確認交通異動。"
    return f"{location}有天氣風險，請保留彈性時間並注意官方警報。"

# ==========================================
# 🗺️ 字典與設定區
# ==========================================
TAIWAN_LOCATIONS = {
    "基隆市": ["仁愛區", "信義區", "中正區", "中山區", "安樂區", "暖暖區", "七堵區"],
    "臺北市": ["中正區", "大同區", "中山區", "松山區", "大安區", "萬華區", "信義區", "士林區", "北投區", "內湖區", "南港區", "文山區"],
    "新北市": ["板橋區", "新莊區", "中和區", "永和區", "土城區", "樹林區", "三峽區", "鶯歌區", "三重區", "蘆洲區", "五股區", "泰山區", "林口區", "八里區", "淡水區", "三芝區", "石門區", "金山區", "萬里區", "汐止區", "瑞芳區", "貢寮區", "平溪區", "雙溪區", "新店區", "深坑區", "石碇區", "坪林區", "烏來區"],
    "桃園市": ["桃園區", "中壢區", "平鎮區", "八德區", "楊梅區", "蘆竹區", "大溪區", "龍潭區", "龜山區", "大園區", "觀音區", "新屋區", "復興區"],
    "新竹市": ["東區", "北區", "香山區"],
    "新竹縣": ["竹北市", "竹東鎮", "新埔鎮", "關西鎮", "湖口鄉", "新豐鄉", "芎林鄉", "橫山鄉", "北埔鄉", "寶山鄉", "峨眉鄉", "尖石鄉", "五峰鄉"],
    "苗栗縣": ["苗栗市", "苑裡鎮", "通霄鎮", "竹南鎮", "頭份市", "後龍鎮", "卓蘭鎮", "大湖鄉", "公館鄉", "銅鑼鄉", "南庄鄉", "頭屋鄉", "三義鄉", "西湖鄉", "造橋鄉", "三灣鄉", "獅潭鄉", "泰安鄉"],
    "臺中市": ["中區", "東區", "南區", "西區", "北區", "北屯區", "西屯區", "南屯區", "太平區", "大里區", "霧峰區", "烏日區", "豐原區", "后里區", "石岡區", "東勢區", "和平區", "新社區", "潭子區", "大雅區", "神岡區", "大肚區", "沙鹿區", "龍井區", "梧棲區", "清水區", "大甲區", "外埔區", "大安區"],
    "彰化縣": ["彰化市", "鹿港鎮", "和美鎮", "線西鄉", "伸港鄉", "福興鄉", "秀水鄉", "花壇鄉", "芬園鄉", "員林市", "溪湖鎮", "田中鎮", "大村鄉", "埔鹽鄉", "埔心鄉", "永靖鄉", "社頭鄉", "二水鄉", "北斗鎮", "二林鎮", "田尾鄉", "埤頭鄉", "芳苑鄉", "大城鄉", "竹塘鄉", "溪州鄉"],
    "南投縣": ["南投市", "埔里鎮", "草屯鎮", "竹山鎮", "集集鎮", "名間鄉", "鹿谷鄉", "中寮鄉", "魚池鄉", "國姓鄉", "水里鄉", "信義鄉", "仁愛鄉"],
    "雲林縣": ["斗六市", "斗南鎮", "虎尾鎮", "西螺鎮", "土庫鎮", "北港鎮", "古坑鄉", "大埤鄉", "莿桐鄉", "林內鄉", "二崙鄉", "崙背鄉", "麥寮鄉", "東勢鄉", "褒忠鄉", "臺西鄉", "元長鄉", "四湖鄉", "口湖鄉", "水林鄉"],
    "嘉義市": ["東區", "西區"],
    "嘉義縣": ["太保市", "朴子市", "布袋鎮", "大林鎮", "民雄鄉", "溪口鄉", "新港鄉", "六腳鄉", "東石鄉", "義竹鄉", "鹿草鄉", "水上鄉", "中埔鄉", "竹崎鄉", "梅山鄉", "番路鄉", "大埔鄉", "阿里山鄉"],
    "臺南市": ["中西區", "東區", "南區", "北區", "安平區", "安南區", "永康區", "歸仁區", "新化區", "左鎮區", "玉井區", "楠西區", "南化區", "仁德區", "關廟區", "龍崎區", "官田區", "麻豆區", "佳里區", "西港區", "七股區", "將軍區", "學甲區", "北門區", "新營區", "後壁區", "白河區", "東山區", "六甲區", "下營區", "柳營區", "鹽水區", "善化區", "大內區", "山上區", "新市區", "安定區"],
    "高雄市": ["新興區", "前金區", "苓雅區", "鹽埕區", "鼓山區", "旗津區", "前鎮區", "三民區", "楠梓區", "小港區", "左營區", "仁武區", "大社區", "岡山區", "路竹區", "阿蓮區", "田寮區", "燕巢區", "橋頭區", "梓官區", "彌陀區", "永安區", "湖內區", "鳳山區", "大寮區", "林園區", "鳥松區", "大樹區", "旗山區", "美濃區", "六龜區", "內門區", "杉林區", "甲仙區", "桃源區", "那瑪夏區", "茂林區"],
    "屏東縣": ["屏東市", "潮州鎮", "東港鎮", "恆春鎮", "萬丹鄉", "長治鄉", "麟洛鄉", "九如鄉", "里港鄉", "鹽埔鄉", "高樹鄉", "萬巒鄉", "內埔鄉", "竹田鄉", "新埤鄉", "枋寮鄉", "新園鄉", "崁頂鄉", "林邊鄉", "南州鄉", "佳冬鄉", "琉球鄉", "車城鄉", "滿州鄉", "枋山鄉", "三地門鄉", "霧臺鄉", "瑪家鄉", "泰武鄉", "來義鄉", "春日鄉", "獅子鄉", "牡丹鄉"],
    "宜蘭縣": ["宜蘭市", "羅東鎮", "蘇澳鎮", "頭城鎮", "礁溪鄉", "壯圍鄉", "員山鄉", "冬山鄉", "五結鄉", "三星鄉", "大同鄉", "南澳鄉"],
    "花蓮縣": ["花蓮市", "鳳林鎮", "玉里鎮", "新城鄉", "吉安鄉", "壽豐鄉", "光復鄉", "豐濱鄉", "瑞穗鄉", "富里鄉", "秀林鄉", "萬榮鄉", "卓溪鄉"],
    "臺東縣": ["臺東市", "成功鎮", "關山鎮", "卑南鄉", "大武鄉", "太麻里鄉", "東河鄉", "長濱鄉", "鹿野鄉", "池上鄉", "綠島鄉", "延平鄉", "海端鄉", "達仁鄉", "金峰鄉", "蘭嶼鄉"],
    "澎湖縣": ["馬公市", "湖西鄉", "白沙鄉", "西嶼鄉", "望安鄉", "七美鄉"],
    "金門縣": ["金城鎮", "金湖鎮", "金沙鎮", "金寧鄉", "烈嶼鄉", "烏坵鄉"],
    "連江縣": ["南竿鄉", "北竿鄉", "莒光鄉", "東引鄉"]
}

REPRESENTATIVE_DISTRICTS = {
    "臺北市": "信義區", "新北市": "板橋區", "桃園市": "桃園區", "臺中市": "西屯區",
    "臺南市": "東區", "高雄市": "左營區", "基隆市": "仁愛區", "新竹市": "東區",
    "嘉義市": "西區", "新竹縣": "竹北市", "苗栗縣": "苗栗市", "彰化縣": "彰化市",
    "南投縣": "南投市", "雲林縣": "斗六市", "嘉義縣": "太保市", "屏東縣": "屏東市",
    "宜蘭縣": "宜蘭市", "花蓮縣": "花蓮市", "臺東縣": "臺東市", "澎湖縣": "馬公市",
    "金門縣": "金城鎮", "連江縣": "南竿鄉"
}

CITY_MAP = {
    "宜蘭縣": "F-D0047-001", "桃園市": "F-D0047-005", "新竹縣": "F-D0047-009", "苗栗縣": "F-D0047-013",
    "彰化縣": "F-D0047-017", "南投縣": "F-D0047-021", "雲林縣": "F-D0047-025", "嘉義縣": "F-D0047-029",
    "屏東縣": "F-D0047-033", "臺東縣": "F-D0047-037", "花蓮縣": "F-D0047-041", "澎湖縣": "F-D0047-045",
    "基隆市": "F-D0047-049", "新竹市": "F-D0047-053", "嘉義市": "F-D0047-057", "臺北市": "F-D0047-061",
    "高雄市": "F-D0047-065", "新北市": "F-D0047-069", "臺中市": "F-D0047-073", "臺南市": "F-D0047-077",
    "連江縣": "F-D0047-081", "金門縣": "F-D0047-085"
}

CITY_7DAY_MAP = {
    "宜蘭縣": "F-D0047-003", "桃園市": "F-D0047-007", "新竹縣": "F-D0047-011", "苗栗縣": "F-D0047-015",
    "彰化縣": "F-D0047-019", "南投縣": "F-D0047-023", "雲林縣": "F-D0047-027", "嘉義縣": "F-D0047-031",
    "屏東縣": "F-D0047-035", "臺東縣": "F-D0047-039", "花蓮縣": "F-D0047-043", "澎湖縣": "F-D0047-047",
    "基隆市": "F-D0047-051", "新竹市": "F-D0047-055", "嘉義市": "F-D0047-059", "臺北市": "F-D0047-063",
    "高雄市": "F-D0047-067", "新北市": "F-D0047-071", "臺中市": "F-D0047-075", "臺南市": "F-D0047-079",
    "連江縣": "F-D0047-083", "金門縣": "F-D0047-087"
}

# ==========================================
# 🚀 API 1：前端地區選單
# ==========================================
@app.get("/locations")
async def get_locations():
    return {"status": "success", "data": TAIWAN_LOCATIONS}

# ==========================================
# 🚀 API 2：AI 防災與生活助理
# ==========================================
class UserQuery(BaseModel):
    city: str
    district: str
    message: str

@app.post("/ask-assistant")
async def ask_assistant(query: UserQuery):
    try:
        city, district, msg = query.city, query.district, query.message
        dataset_id = CITY_MAP.get(city)
        if not dataset_id: return {"status": "error", "message": f"目前尚不支援 {city} 的天氣查詢"}

        weather_url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/{dataset_id}"
        params = {"Authorization": CWA_API_KEY, "format": "JSON"}
        
        wx, pop = "未知", "未知"
        try:
            # 替換為 httpx 非同步請求
            async with httpx.AsyncClient() as client:
                res = await client.get(weather_url, params=params, timeout=15.0)
                res.raise_for_status()
                dist_data = find_district(res.json(), district)
            
            if dist_data:
                elements = dist_data.get("weatherElement") or dist_data.get("WeatherElement") or []
                for el in elements:
                    en = el.get("elementName") or el.get("ElementName") or ""
                    try:
                        times = el.get("time") or el.get("Time") or []
                        if not times: continue
                        vals = times[0].get("elementValue") or times[0].get("ElementValue") or []
                        if not vals: continue
                        val = list(vals[0].values())[0]

                        if "天氣現象" in en: wx = val
                        elif "降雨機率" in en: pop = val
                    except: continue
        except Exception as e:
            print(f"氣象解析錯誤: {e}") 

        final_prompt = f"地點:{city}{district}，天氣:{wx}，降雨機率:{pop}%。行程:{msg}。請給40字內防災或生活建議。"
        # 等待非同步的 Gemini 回應
        ai_suggestion = await call_gemini_raw(final_prompt)

        try:
            db_data = {"user_input": f"[{city}{district}] {msg}", "ai_response": ai_suggestion}
            # Supabase Python SDK 目前仍為同步，但在快速寫入下可接受
            supabase.table("chat_logs").insert(db_data).execute()
        except Exception as db_e:
            print(f"備份對話紀錄失敗: {db_e}")

        return {
            "target_location": f"{city}{district}",
            "weather": {"wx": wx, "pop": f"{pop}%"},
            "ai_suggestion": ai_suggestion
        }
    except Exception as e:
        return {"status": "error", "message": f"解析失敗: {str(e)}"}

# ==========================================
# 🚀 API 3 & 4：天氣快取機制 (含新裝備 & 背景同步防封鎖 & 系統日誌)
# ==========================================
async def _internal_sync(city: str, district: str):
    """內部背景核心同步邏輯 (加上單一縣市的錯誤捕捉)"""
    try:
        dataset_id = CITY_7DAY_MAP.get(city)
        if not dataset_id: return
        
        url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/{dataset_id}"
        params = {"Authorization": CWA_API_KEY, "format": "JSON"}
        
        # 替換為 httpx 非同步請求
        async with httpx.AsyncClient() as client:
            res = await client.get(url, params=params, timeout=20.0)
            res.raise_for_status()
            res_json = res.json()
        
        dist_data = find_district(res_json, district)
        if not dist_data: return

        elements = dist_data.get("weatherElement") or dist_data.get("WeatherElement") or []
        time_map = {}

        for el in elements:
            en = el.get("elementName") or el.get("ElementName") or ""
            times = el.get("time") or el.get("Time") or []
            
            for t in times:
                dt = t.get("dataTime") or t.get("DataTime") or t.get("startTime") or t.get("StartTime")
                if not dt: continue
                
                if dt not in time_map:
                    time_map[dt] = {
                        "time": dt, "temp": 0, "pop": 0, "hum": 0, 
                        "description": "未知", "app_temp": 0, "uvi": 0, "wind_speed": "0"
                    }
                
                try:
                    vals = t.get("elementValue") or t.get("ElementValue") or []
                    if not vals: continue
                    val = list(vals[0].values())[0]

                    if "天氣現象" in en or en == "Wx": time_map[dt]["description"] = val
                    elif "降雨機率" in en or "PoP" in en: time_map[dt]["pop"] = int(val) if str(val).isdigit() else 0
                    elif "溫度" in en or en in ["T", "MaxT", "MinT"]: time_map[dt]["temp"] = int(val) if str(val).isdigit() else 0
                    elif "相對濕度" in en or en == "RH": time_map[dt]["hum"] = int(val) if str(val).isdigit() else 0
                    elif "體感溫度" in en or en == "AT": time_map[dt]["app_temp"] = int(val) if str(val).lstrip('-').isdigit() else 0 
                    elif "紫外線" in en or en == "UVI": time_map[dt]["uvi"] = int(val) if str(val).isdigit() else 0
                    elif "風速" in en or en == "WS": time_map[dt]["wind_speed"] = val 
                except: continue

        sorted_data = sorted(time_map.values(), key=lambda x: x["time"])
        if not sorted_data: return
            
        now = datetime.now(timezone(timedelta(hours=8)))
        db_payload = {
            "city_name": f"{city}{district}",
            "weather_data": {"current": sorted_data[0], "forecast": sorted_data},
            "updated_at": now.isoformat(),
            "valid_until": (now + timedelta(hours=3)).isoformat()
        }
        supabase.table("weather_cache").upsert(db_payload, on_conflict="city_name").execute()
        print(f"✅ 已同步: {city}{district} (含擴充裝備)")
        
    except Exception as e:
        error_msg = str(e)
        print(f"❌ 同步 {city} 失敗: {error_msg}")
        try:
            supabase.table("sync_logs").insert({
                "task_name": f"weather_sync_{city}",
                "status": "error",
                "message": f"{city} 同步失敗: {error_msg}"
            }).execute()
        except Exception:
            pass

async def _delayed_sync(city: str, district: str, delay_seconds: int):
    """延遲執行小幫手：保護 IP 不被氣象署封鎖"""
    await asyncio.sleep(delay_seconds)
    await _internal_sync(city, district)

async def _master_alert_and_log():
    """📍 最終任務：抓取真實氣象署警報並寫入日誌 (移除 delay_seconds，交由 orchestrator 控制)"""
    try:
        print("🚨 開始向氣象署請求真實警報資料...")
        
        # 1. 抓取真實氣象署特報 (W-C0033-002)
        alert_url = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/W-C0033-002"
        alert_params = {"Authorization": CWA_API_KEY, "format": "JSON"}
        
        # 替換為 httpx 非同步請求
        async with httpx.AsyncClient() as client:
            alert_res_http = await client.get(alert_url, params=alert_params, timeout=20.0)
            alert_res_http.raise_for_status()
            alert_res = alert_res_http.json()

        # 2. 解析警報資料 (防呆處理)
        records = alert_res.get("records", {})
        locations = records.get("location", [])

        active_alerts = []
        for loc in locations:
            loc_name = loc.get("locationName", "")
            hazard_conditions = loc.get("hazardConditions", {}).get("hazards", [])
            
            for hazard in hazard_conditions:
                info = hazard.get("info", {})
                phenomena = info.get("phenomena", "未知警報")
                significance = info.get("significance", "特報")

                is_high_severity = any(keyword in phenomena or keyword in significance for keyword in ["大", "豪", "警報", "颱風"])
                
                active_alerts.append({
                    "title": f"{loc_name}{phenomena}{significance}",
                    "severity": "high" if is_high_severity else "medium",
                    "description": f"氣象署發布：{loc_name}目前有{phenomena}{significance}，請注意防範。",
                    "created_at": datetime.now(timezone(timedelta(hours=8))).isoformat()
                })

        # 3. 寫入資料庫 (weather_alerts)
        if active_alerts:
            supabase.table("weather_alerts").insert(active_alerts).execute()
            print(f"🚨 成功寫入 {len(active_alerts)} 筆真實氣象警報！")
        else:
            print("🌤️ 目前全台天氣穩定，無特殊氣象警報。")

        # 4. 寫入排程總結日誌 (翊翔的需求)
        supabase.table("sync_logs").insert({
            "task_name": "weather_update_all",
            "status": "success",
            "message": "全台 22 縣市天氣與真實警報排程執行完畢"
        }).execute()
        print("✅ 系統排程總結已記錄至 sync_logs")

    except Exception as e:
        error_msg = str(e)
        print(f"❌ 警報抓取或排程總結日誌寫入失敗: {error_msg}")
        
        supabase.table("sync_logs").insert({
            "task_name": "weather_update_all",
            "status": "error",
            "message": f"排程總結(含警報)執行失敗: {error_msg}"
        }).execute()

async def master_sync_orchestrator():
    """👨‍✈️ 總指揮官任務：確保所有縣市都跑完，再執行總結"""
    tasks = []
    delay = 0
    for city, district in REPRESENTATIVE_DISTRICTS.items():
        # 將每個縣市的同步任務加入清單，並依序增加延遲防封鎖
        tasks.append(_delayed_sync(city, district, delay))
        delay += 1 
        
    # 等待這 22 個縣市的任務 "全部" 執行完畢 (解決定時炸彈與競態條件)
    await asyncio.gather(*tasks)
    
    # 全部完成後，才安全地執行最後的警報與日誌統整
    await _master_alert_and_log()

@app.post("/sync-all-taiwan")
async def sync_all_taiwan(background_tasks: BackgroundTasks):
    """鬧鐘排程專用：全台 22 縣市背景同步"""
    # 只要將總指揮官丟進背景執行即可
    background_tasks.add_task(master_sync_orchestrator)
        
    return {
        "status": "processing", 
        "message": f"已啟動全台 {len(REPRESENTATIVE_DISTRICTS)} 縣市同步任務，將依序完成並記錄日誌。"
    }

@app.get("/weather")
async def get_weather(city: str = "臺南市", district: str = "東區"):
    """前端讀取天氣專用 (直接從快取拿，速度最快)"""
    res = supabase.table("weather_cache").select("*").eq("city_name", f"{city}{district}").execute()
    if res.data:
        return res.data[0]
    return {"error": "資料庫尚無此地區快取，請先觸發同步。"}

# ==========================================
# 🚄 API 5：行程與交通工具判斷 (Events)
# ==========================================
class EventCreate(BaseModel):
    title: str
    start_time: str
    end_time: str
    location: Optional[str] = None
    url: Optional[str] = None
    description: Optional[str] = None
    transport_type: Optional[str] = None
    has_weather_risk: bool = False
    ai_suggestion: Optional[str] = None
    risk_level: Optional[str] = None
    risk_tags: List[str] = Field(default_factory=list)
    recommended_action: Optional[str] = None

class EventRiskCheckRequest(BaseModel):
    title: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    location: Optional[str] = None
    city: Optional[str] = None
    district: Optional[str] = None
    activity: Optional[str] = "commuting"
    transport_type: Optional[str] = None

class GameSubmitRequest(BaseModel):
    question_id: str
    selected_index: int
    game_type: Optional[str] = None

class GameScoreCreate(BaseModel):
    player_name: Optional[str] = "guest"
    game_type: str
    score: int
    total_questions: Optional[int] = None
    correct_count: Optional[int] = None

class GeocodeRequest(BaseModel):
    query: str

@app.post("/events")
async def create_event(event: EventCreate):
    try:
        db_payload = event.model_dump(exclude_none=True)
        db_payload["transport_type"] = event.transport_type or determine_transport_type(event.url)
        if event.risk_level or event.risk_tags:
            db_payload["has_weather_risk"] = event.has_weather_risk or event.risk_level in ["medium", "high"]
        
        # 寫入 events 資料表 (請確保 Supabase 已有 transport_type 欄位)
        try:
            res = supabase.table("events").insert(db_payload).execute()
        except Exception:
            legacy_keys = {"title", "start_time", "end_time", "url", "description", "transport_type", "has_weather_risk", "ai_suggestion"}
            legacy_payload = {key: value for key, value in db_payload.items() if key in legacy_keys}
            res = supabase.table("events").insert(legacy_payload).execute()
        if res.data:
            return {"status": "success", "data": normalize_event(res.data[0])}
        return {"status": "error", "message": "寫入失敗"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# 注意：路由從 /events 改成了 /api/events 配合前端
@app.post("/api/events")
async def create_api_event(event: EventCreate):
    return await create_event(event)

@app.get("/api/events")
async def get_events():
    """前端讀取行程專用：完全符合瀚霆的 SwiftUI 契約"""
    try:
        res = supabase.table("events").select("*").execute()
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
        print(f"❌ 讀取行程失敗: {e}")
        return {"status": "error", "message": str(e)}
    
async def build_event_risk(payload: EventRiskCheckRequest) -> Dict[str, Any]:
    location = payload.location or "".join(part for part in [payload.city, payload.district] if part) or "目的地"
    weather_text = ""
    alert_text = ""

    try:
        if payload.city and payload.district:
            cache = supabase.table("weather_cache").select("*").eq("city_name", f"{payload.city}{payload.district}").execute()
            if cache.data:
                weather_data = cache.data[0].get("weather_data") or {}
                weather_text = json.dumps(weather_data, ensure_ascii=False)
    except Exception as e:
        weather_text = f"weather_cache unavailable: {e}"

    try:
        alerts = supabase.table("weather_alerts").select("*").order("created_at", desc=True).limit(5).execute()
        if alerts.data:
            alert_text = json.dumps(alerts.data, ensure_ascii=False)
    except Exception as e:
        alert_text = f"weather_alerts unavailable: {e}"

    combined_text = " ".join([
        payload.title or "",
        location,
        payload.activity or "",
        payload.transport_type or "",
        weather_text,
        alert_text,
    ])
    risk = analyze_text_risk(combined_text)
    action = build_recommended_action(risk["risk_level"], risk["risk_tags"], location)

    return {
        "event": {
            "title": payload.title or "",
            "start_time": payload.start_time,
            "end_time": payload.end_time,
            "location": location,
            "activity": payload.activity,
            "transport_type": payload.transport_type,
        },
        **risk,
        "recommended_action": action,
        "ai_suggestion": action,
        "sources": {
            "weather_cache_used": bool(weather_text and "unavailable" not in weather_text),
            "weather_alerts_used": bool(alert_text and "unavailable" not in alert_text),
        },
    }

@app.post("/api/events/risk-check")
async def check_event_risk(payload: EventRiskCheckRequest):
    try:
        risk_result = await build_event_risk(payload)
        return {"status": "success", "data": risk_result}
    except Exception as e:
        return {"status": "error", "message": str(e)}

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
alter table public.events add column if not exists risk_level text default 'low';
alter table public.events add column if not exists risk_tags jsonb default '[]'::jsonb;
alter table public.events add column if not exists recommended_action text;

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
