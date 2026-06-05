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
THREADS_PROVIDER = env_str("THREADS_PROVIDER", "official")
THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN")
THREADS_API_BASE_URL = env_str("THREADS_API_BASE_URL", "https://graph.threads.net/v1.0").rstrip("/")
THREADS_SEARCH_URL = env_str("THREADS_SEARCH_URL", "")
THREADS_KEYWORD_SEARCH_PATH = env_str("THREADS_KEYWORD_SEARCH_PATH", "keyword_search").strip("/")
THREADS_SEARCH_QUERY_PARAM = env_str("THREADS_SEARCH_QUERY_PARAM", "q")
THREADS_SCAN_KEYWORDS = env_str(
    "THREADS_SCAN_KEYWORDS",
    "火災,車禍,濃煙,爆炸,道路封閉,淹水,積水,塞車,警車,消防車,救護車,停電,瓦斯外洩",
)
THREADS_SCAN_MAX_KEYWORDS = env_int("THREADS_SCAN_MAX_KEYWORDS", 8)
THREADS_SCAN_POST_LIMIT = env_int("THREADS_SCAN_POST_LIMIT", 5)
THREADS_SCAN_REPLY_LIMIT = env_int("THREADS_SCAN_REPLY_LIMIT", 20)
INCIDENT_NOTIFY_CONFIDENCE = env_float("INCIDENT_NOTIFY_CONFIDENCE", 0.75)


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

