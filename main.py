import os
from dotenv import load_dotenv
import requests
from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
import uvicorn
import json
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
import asyncio
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
# 載入金鑰
load_dotenv()

app = FastAPI()

# ==========================================
# 🔑 金鑰與連線 (已改為環境變數)
# ==========================================
CWA_API_KEY = os.getenv("CWA_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ==========================================
# 🗺️ 全台 22 縣市代表性地區清單 (進階版核心)
# ==========================================
# 這裡定義了每個縣市我們要優先快取的「門面」地區
REPRESENTATIVE_DISTRICTS = {
    "臺北市": "信義區", "新北市": "板橋區", "桃園市": "桃園區", "臺中市": "西屯區",
    "臺南市": "東區", "高雄市": "左營區", "基隆市": "仁愛區", "新竹市": "東區",
    "嘉義市": "西區", "新竹縣": "竹北市", "苗栗縣": "苗栗市", "彰化縣": "彰化市",
    "南投縣": "南投市", "雲林縣": "斗六市", "嘉義縣": "太保市", "屏東縣": "屏東市",
    "宜蘭縣": "宜蘭市", "花蓮縣": "花蓮市", "臺東縣": "臺東市", "澎湖縣": "馬公市",
    "金門縣": "金城鎮", "連江縣": "南竿鄉"
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
# ⚙️ 核心邏輯：單一地區同步函式 (內部使用)
# ==========================================
async def _internal_sync(city: str, district: str):
    try:
        dataset_id = CITY_7DAY_MAP.get(city)
        if not dataset_id: return
        
        url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/{dataset_id}"
        params = {"Authorization": CWA_API_KEY, "format": "JSON", "locationName": district}
        # 這裡保留了我們剛剛加上的 verify=False
        res_obj = requests.get(url, params=params, timeout=20, verify=False)
        res = res_obj.json()
        
       # 【終極解析雷達】
        records = res.get("records", {})
        if not records:
            print(f"⚠️ 氣象署回傳異常: {res}")
            return
            
        # 印出 records 裡面到底有哪些第一層的 key
        if city == "臺北市" and district == "信義區":
            print(f"🔍 臺北市 records 的 Keys: {list(records.keys())}")
            # 假設裡面有 'locations'，我們印出它第一個元素的 keys
            if "locations" in records and len(records["locations"]) > 0:
                 print(f"🔍 臺北市 locations[0] 的 Keys: {list(records['locations'][0].keys())}")
            # 假設裡面只有 'location'，我們印出它第一個元素的 keys
            elif "location" in records and len(records["location"]) > 0:
                 print(f"🔍 臺北市 location[0] 的 Keys: {list(records['location'][0].keys())}")
                 
        # 注意這裡的 Locations 和 Location 都要大寫！
        locations_list = records.get("Locations", [{}])[0].get("Location", [])
        if not locations_list:
            print(f"⚠️ 找不到 {city}{district} 的資料！氣象署回傳: {str(res)[:200]}")
            return

        location_data = locations_list[0]
        
        # 兼容大小寫：取得天氣元素
        elements = location_data.get("WeatherElement") or location_data.get("weatherElement", [])
        
        time_map = {}
        for el in elements:
            en = el.get("ElementName") or el.get("elementName")
            times = el.get("Time") or el.get("time", [])
            for t in times:
                # 兼容各種時間命名
                dt = t.get("DataTime") or t.get("dataTime") or t.get("StartTime") or t.get("startTime")
                if not dt: continue
                if dt not in time_map: time_map[dt] = {"time": dt, "temp":0, "pop":0, "description":"未知"}
                
                # 兼容 Value 大小寫
                val_list = t.get("ElementValue") or t.get("elementValue", [{}])
                if val_list:
                    val = val_list[0].get("Value") or val_list[0].get("value")
                    if en in ["T", "MaxT"] and val is not None: 
                        try: time_map[dt]["temp"] = int(val) 
                        except: pass
                    elif "PoP" in en and val is not None: 
                        try: time_map[dt]["pop"] = int(val) 
                        except: pass
                    elif en == "Wx" and val is not None: 
                        time_map[dt]["description"] = val

        sorted_data = sorted(time_map.values(), key=lambda x: x["time"])
        
        # 終極防呆：如果資料還是空的，印出來讓我們抓蟲
        if not sorted_data:
            print(f"⚠️ {city} 解析後沒有資料，請檢查結構: {str(elements)[:300]}")
            return
            
        now = datetime.now(timezone(timedelta(hours=8)))
        
        db_payload = {
            "city_name": f"{city}{district}",
            "weather_data": {"current": sorted_data[0], "forecast": sorted_data},
            "updated_at": now.isoformat(),
            "valid_until": (now + timedelta(hours=3)).isoformat()
        }
        supabase.table("weather_cache").upsert(db_payload, on_conflict="city_name").execute()
        print(f"✅ 已同步: {city}{district}")
    except Exception as e:
        print(f"❌ 同步 {city} 失敗: {e}")

# ==========================================
# 🚀 進階 API 1：全台大同步 (鬧鐘改敲這裡！)
# ==========================================
@app.post("/sync-all-taiwan")
async def sync_all_taiwan(background_tasks: BackgroundTasks):
    """
    鬧鐘敲這個網址，我們會立刻回傳 200 OK，
    然後在背景慢慢把 22 縣市抓完，不怕 Render Timeout！
    """
    for city, district in REPRESENTATIVE_DISTRICTS.items():
        # 把任務丟進背景排隊，每抓一個縣市休息 1 秒，避免被氣象署封鎖
        background_tasks.add_task(_internal_sync, city, district)
        
    return {
        "status": "processing",
        "message": f"已啟動全台 {len(REPRESENTATIVE_DISTRICTS)} 縣市背景同步任務，請稍後查看資料庫。"
    }

# 保留原本的單一查詢介面給前端
@app.get("/weather")
async def get_weather(city: str = "臺南市", district: str = "東區"):
    # 先看資料庫有沒有，沒有再報錯 (這就是快取的力量)
    res = supabase.table("weather_cache").select("*").eq("city_name", f"{city}{district}").execute()
    if res.data:
        return res.data[0]
    return {"error": "資料庫尚無此地區快取，請先觸發同步。"}
# ==========================================
# 🚄 行程 (Events) API 模組 
# ==========================================
from typing import Optional

# 定義接收前端資料的格式
class EventCreate(BaseModel):
    title: str
    start_time: str
    end_time: str
    url: Optional[str] = None
    # 如果你們前端還有傳其他欄位（例如 description, location 等），請在這裡補上

# 判斷交通工具的邏輯
def determine_transport_type(url: str) -> Optional[str]:
    """根據網址判斷是台鐵(tra)還是高鐵(thsrc)"""
    if not url:
        return None
        
    url_lower = url.lower()
    if "railway.gov.tw" in url_lower or "tra" in url_lower:
        return "tra"
    elif "thsrc.com.tw" in url_lower:
        return "thsrc"
        
    return None

# 新增行程的 API
@app.post("/events")
async def create_event(event: EventCreate):
    try:
        # 1. 自動判斷並產生 transport_type
        transport_type = determine_transport_type(event.url)
        
        # 2. 準備要存進 Supabase 的資料
        db_payload = event.model_dump() # 如果 Pydantic 是 v2 版本用 model_dump()，v1 版用 dict()
        db_payload["transport_type"] = transport_type
        
        # 3. 寫入 Supabase (假設你的資料表叫做 events)
        res = supabase.table("events").insert(db_payload).execute()
        
        if res.data:
            return {"status": "success", "data": res.data[0]}
        else:
            return {"status": "error", "message": "寫入失敗"}
            
    except Exception as e:
        return {"status": "error", "message": str(e)}

# 取得行程的 API (讓前端去讀取)
@app.get("/events")
async def get_events():
    try:
        # 把行程從 Supabase 抓出來，前端就能直接拿到 transport_type 了
        res = supabase.table("events").select("*").execute()
        return {"status": "success", "data": res.data}
    except Exception as e:
        return {"status": "error", "message": str(e)}
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)