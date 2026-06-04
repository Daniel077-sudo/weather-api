import os
from typing import Any, Dict

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

CWA_API_KEY = os.getenv("CWA_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
CRON_SECRET = os.getenv("CRON_SECRET")
TIMETREE_ACCESS_TOKEN = os.getenv("TIMETREE_ACCESS_TOKEN")
TIMETREE_CALENDAR_ID = os.getenv("TIMETREE_CALENDAR_ID")
TIMETREE_EVENTS_URL = os.getenv("TIMETREE_EVENTS_URL")
TDX_CLIENT_ID = os.getenv("TDX_CLIENT_ID")
TDX_CLIENT_SECRET = os.getenv("TDX_CLIENT_SECRET")
VISION_DAILY_LIMIT = int(os.getenv("VISION_DAILY_LIMIT", "10"))
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

CRON_STATUS: Dict[str, Any] = {
    "last_started_at": None,
    "last_finished_at": None,
    "last_status": "never_run",
    "last_message": "",
    "last_refreshed_count": 0,
    "last_error_count": 0,
}

