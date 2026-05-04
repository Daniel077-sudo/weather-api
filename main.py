import os
from dotenv import load_dotenv
import requests
from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
import uvicorn
import json
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from typing import Optional
import urllib3
import asyncio

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 載入金鑰 (堅持使用環境變數，保護安全)
load_dotenv()

app = FastAPI()

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
def call_gemini_raw(prompt: str):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {'Content-Type': 'application/json'}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.8}
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
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

def determine_transport_type(url: str) -> Optional[str]:
    """根據網址判斷是台鐵(tra)還是高鐵(thsrc)"""
    if not url: return None
    url_lower = url.lower()
    if "railway.gov.tw" in url_lower or "tra" in url_lower: return "tra"
    elif "thsrc.com.tw" in url_lower: return "thsrc"
    return None

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
            res = requests.get(weather_url, params=params, timeout=15, verify=False).json()
            dist_data = find_district(res, district)
            
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
        ai_suggestion = call_gemini_raw(final_prompt)

        try:
            db_data = {"user_input": f"[{city}{district}] {msg}", "ai_response": ai_suggestion}
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
        res = requests.get(url, params=params, timeout=20, verify=False).json()
        
        dist_data = find_district(res, district)
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
        # 如果單一縣市當機，獨立寫入 error 日誌，不影響其他縣市
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

async def _master_alert_and_log(delay_seconds: int):
    """📍 最終任務：所有縣市跑完後，抓取真實氣象署警報並寫入日誌"""
    await asyncio.sleep(delay_seconds)
    try:
        print("🚨 開始向氣象署請求真實警報資料...")
        
        # 1. 抓取真實氣象署特報 (W-C0033-002)
        alert_url = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/W-C0033-002"
        alert_params = {"Authorization": CWA_API_KEY, "format": "JSON"}
        alert_res = requests.get(alert_url, params=alert_params, timeout=20, verify=False).json()

        # 2. 解析警報資料 (防呆處理)
        records = alert_res.get("records", {})
        locations = records.get("location", [])

        active_alerts = []
        for loc in locations:
            loc_name = loc.get("locationName", "")
            hazard_conditions = loc.get("hazardConditions", {}).get("hazards", [])
            
            for hazard in hazard_conditions:
                # 取得警報現象 (例如: 大雨、強風) 與 程度 (例如: 特報)
                info = hazard.get("info", {})
                phenomena = info.get("phenomena", "未知警報")
                significance = info.get("significance", "特報")

                # 簡單判斷嚴重程度
                is_high_severity = any(keyword in phenomena or keyword in significance for keyword in ["大", "豪", "警報", "颱風"])
                
                # 組裝成翊翔資料庫要的格式
                active_alerts.append({
                    "title": f"{loc_name}{phenomena}{significance}",
                    "severity": "high" if is_high_severity else "medium",
                    "description": f"氣象署發布：{loc_name}目前有{phenomena}{significance}，請注意防範。",
                    # 寫入當下時間
                    "created_at": datetime.now(timezone(timedelta(hours=8))).isoformat()
                })

        # 3. 寫入資料庫 (weather_alerts)
        if active_alerts:
            # Supabase 支援一次 insert 多筆資料 (傳入 List)
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
        
        # 如果出錯，依然要寫入失敗日誌給 DB 負責人看
        supabase.table("sync_logs").insert({
            "task_name": "weather_update_all",
            "status": "error",
            "message": f"排程總結(含警報)執行失敗: {error_msg}"
        }).execute()

@app.post("/sync-all-taiwan")
async def sync_all_taiwan(background_tasks: BackgroundTasks):
    """鬧鐘排程專用：全台 22 縣市背景同步 (加入防封鎖延遲與日誌統整)"""
    delay = 0
    for city, district in REPRESENTATIVE_DISTRICTS.items():
        background_tasks.add_task(_delayed_sync, city, district, delay)
        delay += 1 
        
    # 💡 在最後一個縣市抓完之後 (delay + 2 秒)，執行「總結任務」
    background_tasks.add_task(_master_alert_and_log, delay + 2)
        
    return {
        "status": "processing", 
        "message": f"已啟動全台 {len(REPRESENTATIVE_DISTRICTS)} 縣市同步任務，將於 {delay} 秒內完成並記錄日誌。"
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
    url: Optional[str] = None
    description: Optional[str] = None

@app.post("/events")
async def create_event(event: EventCreate):
    try:
        transport_type = determine_transport_type(event.url)
        db_payload = event.model_dump()
        db_payload["transport_type"] = transport_type
        
        # 寫入 events 資料表 (請確保 Supabase 已有 transport_type 欄位)
        res = supabase.table("events").insert(db_payload).execute()
        if res.data:
            return {"status": "success", "data": res.data[0]}
        return {"status": "error", "message": "寫入失敗"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/events")
async def get_events():
    """前端讀取行程專用：包含天氣風險警告與 AI 備案"""
    try:
        # 1. 從 Supabase 撈取翊翔建好的 events 資料表
        # 假設資料表裡已經有 ai_suggestion, url, has_weather_risk 等欄位
        res = supabase.table("events").select("*").execute()
        
        events_data = res.data
        if not events_data:
            return {"status": "success", "data": [], "message": "目前無行程"}

        # 2. 轉換資料格式以符合瀚霆 (前端 SwiftUI) 的需求
        formatted_events = []
        for event in events_data:
            formatted_event = {
                "id": event.get("id"),
                "title": event.get("title", "未命名行程"),
                "start_time": event.get("start_time"),
                "end_time": event.get("end_time"),
                "transport_type": event.get("transport_type"),
                
                # 瀚霆要求把原本的 url 改名為 transport_ticket_link
                "transport_ticket_link": event.get("url"),
                
                # 氣象風險與 AI 備案 (直接讀取 DB 裡的值)
                "has_weather_risk": event.get("has_weather_risk", False),
                "ai_suggestion": event.get("ai_suggestion") # 這裡會直接是 JSON 物件或 None
            }
            formatted_events.append(formatted_event)

        return {
            "status": "success",
            "data": formatted_events
        }

    except Exception as e:
        print(f"❌ 讀取行程失敗: {e}")
        return {"status": "error", "message": f"伺服器錯誤: {str(e)}"}
    
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
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)