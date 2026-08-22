"""SQLite-backed local cache for historical klines.

Historical bars are immutable — once fetched they never change — so they are
persisted locally (backend/data/cache/klines.db) and reused across sessions,
chart history paging and backtests.

Coverage model: a request for `limit` bars ending at `end_time` is served fully
from cache only when the needed window is *contiguously* covered (adjacent to
end_time on top, constant interval spacing inside). Otherwise the missing part
is fetched from Binance in pages of 1000 (concurrently), persisted, and the
merged window is re-read from cache.

Note: rows are cached per (symbol, interval, ts). Official futures and the
spot mirror return near-identical OHLCV; a conflict is settled by last write.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import pandas as pd

from services import binance

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "cache" / "klines.db"

PAGE = 1000  # Binance max bars per request
FETCH_CONCURRENCY = 4

STEP_MS = {
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
    "1w": 604_800_000,
}

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS klines (
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                ts INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                taker_buy REAL,
                PRIMARY KEY (symbol, interval, ts)
            ) WITHOUT ROWID"""
        )
        conn.commit()
        _conn = conn
    return _conn


def _persist(symbol: str, interval: str, rows: list[tuple]) -> None:
    if not rows:
        return
    with _lock:
        _db().executemany(
            "INSERT OR REPLACE INTO klines "
            "(symbol, interval, ts, open, high, low, close, volume, taker_buy) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(symbol, interval, *r) for r in rows],
        )
        _db().commit()


def _read_ts_desc(symbol: str, interval: str, end_time: int, limit: int) -> list[int]:
    with _lock:
        cur = _db().execute(
            "SELECT ts FROM klines WHERE symbol=? AND interval=? AND ts<=? "
            "ORDER BY ts DESC LIMIT ?",
            (symbol, interval, end_time, limit),
        )
        return [int(r[0]) for r in cur.fetchall()]


def _read_rows(symbol: str, interval: str, end_time: int, limit: int) -> list[tuple]:
    """Rows (ts,o,h,l,c,v,tb) ascending, up to limit bars with ts <= end_time."""
    with _lock:
        cur = _db().execute(
            "SELECT ts, open, high, low, close, volume, taker_buy FROM klines "
            "WHERE symbol=? AND interval=? AND ts<=? ORDER BY ts DESC LIMIT ?",
            (symbol, interval, end_time, limit),
        )
        desc = cur.fetchall()
    desc.reverse()
    return [tuple(r) for r in desc]


def _contiguous_top_count(ts_desc: list[int], end_time: int, step: int) -> int:
    """How many bars from the top of ts_desc form a run adjacent to end_time."""
    if not ts_desc:
        return 0
    # newest cached bar must sit within one step below end_time
    if ts_desc[0] <= end_time - step:
        return 0
    count = 1
    for i in range(1, len(ts_desc)):
        if ts_desc[i - 1] - ts_desc[i] != step:
            break
        count += 1
    return count


def _raw_to_rows(raw: list) -> list[tuple]:
    rows: list[tuple] = []
    for k in raw:
        tb = float(k[9]) if len(k) > 9 and k[9] not in (None, "") else None
        rows.append((int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), tb))
    return rows


async def _fetch_pages(symbol: str, interval: str, need: int, fetch_end: int, step: int) -> None:
    """Fetch `need` bars ending at fetch_end in concurrent pages and persist them."""
    pages = -(-need // PAGE)  # ceil
    end_times = [fetch_end - i * PAGE * step for i in range(pages)]
    for i in range(0, len(end_times), FETCH_CONCURRENCY):
        batch = end_times[i : i + FETCH_CONCURRENCY]

        async def one(et: int) -> list[tuple]:
            raw = await binance.get_klines(symbol, interval, PAGE, end_time=et)
            return _raw_to_rows(raw)

        results = await asyncio.gather(*[one(et) for et in batch], return_exceptions=True)
        all_rows: list[tuple] = []
        for r in results:
            if isinstance(r, BaseException):
                continue  # partial page failure: keep what we have, retry next call
            all_rows.extend(r)
        _persist(symbol, interval, all_rows)


async def get_klines(
    symbol: str,
    interval: str,
    limit: int,
    end_time: int | None = None,
) -> list[tuple]:
    """Return up to `limit` rows (ts,o,h,l,c,v,tb) ascending with ts <= end_time.

    end_time=None means the newest bars: the top page is always fetched live
    (it keeps changing) and seeded into the cache; older bars come from cache.
    """
    symbol = symbol.upper()
    step = STEP_MS.get(interval)
    if step is None:
        raise ValueError(f"unsupported interval: {interval}")

    if end_time is None:
        live_raw = await binance.get_klines(symbol, interval, min(limit, PAGE))
        if not live_raw:
            return []
        live = _raw_to_rows(live_raw)
        _persist(symbol, interval, live)
        if limit <= PAGE:
            return live
        older = await _get_history(symbol, interval, limit - len(live), live[0][0] - 1, step)
        return older + live

    return await _get_history(symbol, interval, limit, end_time, step)


async def _get_history(symbol: str, interval: str, limit: int, end_time: int, step: int) -> list[tuple]:
    ts_desc = _read_ts_desc(symbol, interval, end_time, limit)
    covered = _contiguous_top_count(ts_desc, end_time, step)
    if covered >= limit:
        return _read_rows(symbol, interval, end_time, limit)

    # fetch the uncovered remainder (or the whole window if nothing covered)
    if covered > 0:
        fetch_end = ts_desc[covered - 1] - 1
    else:
        fetch_end = end_time
    await _fetch_pages(symbol, interval, limit - covered, fetch_end, step)
    return _read_rows(symbol, interval, end_time, limit)


def rows_to_df(rows: list[tuple]) -> pd.DataFrame:
    """Convert cached rows into the analysis-friendly DataFrame."""
    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume", "takerBuy"])
    df = pd.DataFrame(
        rows, columns=["time", "open", "high", "low", "close", "volume", "takerBuy"]
    ).sort_values("time")
    return df.reset_index(drop=True)
