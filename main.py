import os
from dotenv import load_dotenv
import requests
from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn
import json
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client

# 載入 .env 檔案中的環境變數
load_dotenv()

app = FastAPI()

# ==========================================
# 🔑 金鑰與資料庫連線區 (改用 os.getenv 讀取)
# ==========================================
CWA_API_KEY = os.getenv("CWA_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# 確保 URL 和 KEY 都有成功讀取，否則拋出錯誤提醒自己
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("資料庫連線金鑰遺失，請檢查環境變數設定！")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ... 下方的程式碼完全不用動 ...
# ==========================================
# 🛠️ 共用工具函式區
# ==========================================
def call_gemini_raw(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent?key={GEMINI_API_KEY}"
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

# ==========================================
# 🗺️ 台灣行政區劃資料 (給前端連動選單)
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

@app.get("/locations")
async def get_locations():
    return {"status": "success", "data": TAIWAN_LOCATIONS}

# ==========================================
# 🌐 全台 22 縣市 API 代碼字典
# ==========================================
CITY_MAP = {
    "宜蘭縣": "F-D0047-001", "桃園市": "F-D0047-005", "新竹縣": "F-D0047-009", "苗栗縣": "F-D0047-013",
    "彰化縣": "F-D0047-017", "南投縣": "F-D0047-021", "雲林縣": "F-D0047-025", "嘉義縣": "F-D0047-029",
    "屏東縣": "F-D0047-033", "臺東縣": "F-D0047-037", "花蓮縣": "F-D0047-041", "澎湖縣": "F-D0047-045",
    "基隆市": "F-D0047-049", "新竹市": "F-D0047-053", "嘉義市": "F-D0047-057", "臺北市": "F-D0047-061",
    "高雄市": "F-D0047-065", "新北市": "F-D0047-069", "臺中市": "F-D0047-073", "臺南市": "F-D0047-077",
    "連江縣": "F-D0047-081", "金門縣": "F-D0047-085",
    "台北市": "F-D0047-061", "台中市": "F-D0047-073", "台南市": "F-D0047-077", "台東縣": "F-D0047-037"
}

CITY_7DAY_MAP = {
    "宜蘭縣": "F-D0047-003", "桃園市": "F-D0047-007", "新竹縣": "F-D0047-011", "苗栗縣": "F-D0047-015",
    "彰化縣": "F-D0047-019", "南投縣": "F-D0047-023", "雲林縣": "F-D0047-027", "嘉義縣": "F-D0047-031",
    "屏東縣": "F-D0047-035", "臺東縣": "F-D0047-039", "花蓮縣": "F-D0047-043", "澎湖縣": "F-D0047-047",
    "基隆市": "F-D0047-051", "新竹市": "F-D0047-055", "嘉義市": "F-D0047-059", "臺北市": "F-D0047-063",
    "高雄市": "F-D0047-067", "新北市": "F-D0047-071", "臺中市": "F-D0047-075", "臺南市": "F-D0047-079",
    "連江縣": "F-D0047-083", "金門縣": "F-D0047-087",
    "台北市": "F-D0047-063", "台中市": "F-D0047-075", "台南市": "F-D0047-079", "台東縣": "F-D0047-039"
}

# ==========================================
# 🚀 核心功能 1：氣象與 AI 防災助理
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
            res = requests.get(weather_url, params=params, timeout=15).json()
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
            print(f"備份失敗: {db_e}")

        return {
            "target_location": f"{city}{district}",
            "weather": {"wx": wx, "pop": f"{pop}%"},
            "ai_suggestion": ai_suggestion
        }
    except Exception as e:
        return {"status": "error", "message": f"解析失敗: {str(e)}"}

# ==========================================
# 🌤️ 核心功能 2：純天氣預報 API (給前端首頁看板專用)
# ==========================================
@app.get("/weather")
async def get_weather(city: str = "臺南市", district: str = "東區"):
    try:
        dataset_id = CITY_MAP.get(city) 
        if not dataset_id: return {"error": "找不到該縣市代碼"}
        
        weather_url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/{dataset_id}"
        params = {"Authorization": CWA_API_KEY, "format": "JSON"}
        res = requests.get(weather_url, params=params, timeout=10).json()
        
        dist_data = find_district(res, district)
        wx, pop, temp = "未知", "未知", "未知"
        
        if dist_data:
            elements = dist_data.get("weatherElement") or dist_data.get("WeatherElement") or []
            for el in elements:
                en = el.get("elementName") or el.get("ElementName") or ""
                try:
                    t_list = el.get("time", el.get("Time", [{}]))
                    v_list = t_list[0].get("elementValue", t_list[0].get("ElementValue", [{}]))
                    val = list(v_list[0].values())[0]
                    if "天氣現象" in en or en == "Wx": wx = val
                    elif "降雨機率" in en or en in ["PoP6h", "PoP12h"]: pop = val
                    elif "溫度" in en or en == "T": temp = val
                except: continue

        return {
            "city": city, "district": district, "wx": wx, "pop": f"{pop}%", "temp": f"{temp}°C"
        }
    except Exception as e:
        return {"error": str(e)}

# ==========================================
# 💾 核心功能 3：存入資料庫 (新增 體感、紫外線、風速)
# ==========================================
@app.post("/sync-weather-cache")
async def sync_weather_cache(city: str = "臺南市", district: str = "東區"): 
    try:
        dataset_id = CITY_7DAY_MAP.get(city)
        if not dataset_id:
            return {"status": "error", "message": f"尚未設定 {city} 的 7 天預報代碼"}

        weather_url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/{dataset_id}"
        params = {"Authorization": CWA_API_KEY, "format": "JSON"}
        res = requests.get(weather_url, params=params, timeout=15).json()

        dist_data = find_district(res, district)
        if not dist_data:
            return {"status": "error", "message": f"氣象署未回傳 {city}{district} 的資料"}

        elements = dist_data.get("weatherElement") or dist_data.get("WeatherElement") or []
        time_map = {}

        for el in elements:
            en = el.get("elementName") or el.get("ElementName") or ""
            times = el.get("time") or el.get("Time") or []
            
            for t in times:
                dt = t.get("dataTime") or t.get("DataTime") or t.get("startTime") or t.get("StartTime")
                if not dt: continue
                
                # 🛠️ 在這裡加上新裝備！
                if dt not in time_map:
                    time_map[dt] = {
                        "time": dt, 
                        "temp": 0, 
                        "pop": 0, 
                        "hum": 0, 
                        "description": "未知",
                        "app_temp": 0,     # 體感溫度
                        "uvi": 0,          # 紫外線指數
                        "wind_speed": "0", # 風速 (保留字串格式因可能有 <= 符號)
                        "alert": "無"      # 天氣警特報預設值
                    }
                
                try:
                    vals = t.get("elementValue") or t.get("ElementValue") or []
                    if not vals: continue
                    val = list(vals[0].values())[0]

                    if "天氣現象" in en or en == "Wx": time_map[dt]["description"] = val
                    elif "降雨機率" in en or "PoP" in en: time_map[dt]["pop"] = int(val) if str(val).isdigit() else 0
                    elif "溫度" in en or en in ["T", "MaxT", "MinT"]: time_map[dt]["temp"] = int(val) if str(val).isdigit() else 0
                    elif "相對濕度" in en or en == "RH": time_map[dt]["hum"] = int(val) if str(val).isdigit() else 0
                    # 🛠️ 攔截氣象署的新裝備資料
                    elif "體感溫度" in en or en == "AT": time_map[dt]["app_temp"] = int(val) if str(val).lstrip('-').isdigit() else 0 
                    elif "紫外線" in en or en == "UVI": time_map[dt]["uvi"] = int(val) if str(val).isdigit() else 0
                    elif "風速" in en or en == "WS": time_map[dt]["wind_speed"] = val 
                except: continue

        sorted_forecast = sorted(time_map.values(), key=lambda x: x["time"])
        weather_data_json = {
            "current": sorted_forecast[0] if sorted_forecast else {},
            "forecast": sorted_forecast
        }

        target_location = f"{city}{district}"
        tz = timezone(timedelta(hours=8))
        now = datetime.now(tz)
        
        db_payload = {
            "city_name": target_location, 
            "weather_data": weather_data_json,
            "updated_at": now.isoformat(),
            "valid_until": (now + timedelta(hours=3)).isoformat()
        }

        supabase.table("weather_cache").upsert(db_payload, on_conflict="city_name").execute()

        return {
            "status": "success",
            "message": f"✅ {target_location} 完整預報同步成功！(含體感、紫外線、風速)",
            "inserted_data": db_payload
        }
    except Exception as e:
        return {"status": "error", "message": f"快取更新失敗: {str(e)}"}

# ==========================================
# 🛠️ 手動資料庫寫入測試區
# ==========================================
class ScheduleData(BaseModel):
    title: str
    start_time: str
    location_name: str
    description: str

@app.post("/test-write-schedule", status_code=201)
async def test_write_schedule(schedule: ScheduleData):
    try:
        data = {
            "title": schedule.title, "start_time": schedule.start_time,
            "location_name": schedule.location_name, "description": schedule.description
        }
        res = supabase.table("schedules").insert(data).execute()
        return {"status": "寫入成功", "data": res.data}
    except Exception as e: return {"status": "寫入失敗", "error": str(e)}

class ChatLogData(BaseModel):
    user_input: str 
    ai_response: str

@app.post("/test-write-chat", status_code=201)
async def test_write_chat(chat: ChatLogData):
    try:
        data = {"user_input": chat.user_input, "ai_response": chat.ai_response}
        res = supabase.table("chat_logs").insert(data).execute()
        return {"status": "寫入成功", "data": res.data}
    except Exception as e: return {"status": "寫入失敗", "error": str(e)}
    
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)