import os
from typing import Any, Dict

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()


def env_str(name: str, default: str = "") -> str:
    return os.getenv(name) or default


def env_int(name: str, default: int) -> int:
    try:
        return int(env_str(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(env_str(name, str(default)))
    except ValueError:
        return default

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
VISION_DAILY_LIMIT = env_int("VISION_DAILY_LIMIT", 10)
EVENT_ALERT_LEAD_MINUTES = env_int("EVENT_ALERT_LEAD_MINUTES", 180)
CHAT_GEMINI_MODE = env_str("CHAT_GEMINI_MODE", "fallback_only").lower()


class MissingSupabaseClient:
    def table(self, *_args, **_kwargs):
        raise RuntimeError("SUPABASE_URL or SUPABASE_KEY is missing")


supabase: Client | MissingSupabaseClient
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
else:
    supabase = MissingSupabaseClient()

CRON_STATUS: Dict[str, Any] = {
    "last_started_at": None,
    "last_finished_at": None,
    "last_status": "never_run",
    "last_message": "",
    "last_refreshed_count": 0,
    "last_error_count": 0,
}

