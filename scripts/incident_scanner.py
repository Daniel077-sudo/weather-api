import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from incident_service import scan_threads_incidents


async def main():
    result = await scan_threads_incidents()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") in {"success", "partial_success", "not_configured"} else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
