"""Event calendar: locally curated macro/unlock events.

The corporate network blocks calendar APIs (Coinglass/Coingecko), so events
live in backend/data/events.json and are manually curated. Update that file
to add FOMC/CPI/unlock dates.
"""
import json
import os
from datetime import date

from fastapi import APIRouter

router = APIRouter(prefix="/api")

_DATA_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "events.json")

_NOTE = (
    "事件清单为本地维护（当前网络无法访问宏观日历 API），可在 backend/data/events.json 中更新。"
    "FOMC 日期为美联储公布的 2026 年议息日程；CPI 每月中旬发布，具体日期以美国劳工统计局为准。"
)


def _load_events() -> list[dict]:
    try:
        with open(_DATA_FILE, encoding="utf-8") as f:
            events = json.load(f)
        if isinstance(events, list):
            return [e for e in events if isinstance(e, dict) and e.get("date")]
    except (OSError, json.JSONDecodeError):
        pass
    return []


@router.get("/calendar")
async def get_calendar():
    events = _load_events()
    today = date.today().isoformat()
    upcoming = [e for e in events if e["date"] >= today]
    upcoming.sort(key=lambda e: (e["date"], e.get("time") or ""))
    return {"events": upcoming, "note": _NOTE}
