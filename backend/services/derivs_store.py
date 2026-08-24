"""SQLite-backed persistence for derivatives history (OI / funding / LSR /
liquidation USD sizes).

Multi-source by priority (adapts to the network this runs in):
  1. Gate.io contract_stats  — OI / funding / LSR / liquidation USD sizes,
     daily x1000 + hourly x720 backfill per symbol, refreshed when stale.
  2. Binance futures public data (openInterestHist / fundingRate /
     globalLongShortAccountRatio / takerlongshortRatio) — no liquidation
     sizes, but fills percentile context when Gate.io is unreachable.

Rows merge per-column via UPSERT so events at the same timestamp from
different sources complement instead of overwriting each other. Live
snapshots from every /api/derivatives fetch also accumulate over time.

Provides percentile context (funding / OI / LSR vs the stored window) which
is what institutional desks look at ("funding at the 95th percentile of the
last year") instead of raw values.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path

from services import binance, gateio

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "cache" / "derivs.db"

REFRESH_AFTER_S = 6 * 3600  # backfill again if newest 1d row older than 6h
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
# in-flight backfill guard per symbol
_backfilling: set[str] = set()


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS gateio_stats (
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                ts INTEGER NOT NULL,
                oi INTEGER,
                oi_usd REAL,
                funding_rate REAL,
                lsr_account REAL,
                lsr_taker REAL,
                top_lsr REAL,
                long_liq_usd REAL,
                short_liq_usd REAL,
                mark_price REAL,
                PRIMARY KEY (symbol, interval, ts)
            ) WITHOUT ROWID"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS snapshots (
                ts INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                source TEXT,
                oi REAL,
                oi_usd REAL,
                funding REAL,
                lsr REAL,
                taker REAL,
                PRIMARY KEY (ts, symbol)
            ) WITHOUT ROWID"""
        )
        conn.commit()
        _conn = conn
    return _conn


_COLS = ("oi", "oi_usd", "funding_rate", "lsr_account", "lsr_taker", "top_lsr",
         "long_liq_usd", "short_liq_usd", "mark_price")


def _upsert_stats(symbol: str, interval: str, rows: list[tuple]) -> None:
    """Merge rows per-column: a NULL never overwrites an existing value, so
    events from different sources at the same timestamp complement."""
    if not rows:
        return
    updates = ", ".join(f"{c}=COALESCE(excluded.{c}, {c})" for c in _COLS)
    with _lock:
        _db().executemany(
            "INSERT INTO gateio_stats (symbol, interval, ts, " + ", ".join(_COLS) + ") "
            "VALUES (?,?,?," + ",".join("?" * len(_COLS)) + ") "
            f"ON CONFLICT(symbol, interval, ts) DO UPDATE SET {updates}",
            [(symbol, interval, *r) for r in rows],
        )
        _db().commit()


def _stats_to_rows(stats: list) -> list[tuple]:
    rows = []
    for s in stats:
        def _n(key):
            v = s.get(key)
            try:
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None
        rows.append((
            int(s["time"]),
            _n("open_interest"),
            _n("open_interest_usd"),
            _n("last_funding_rate"),
            _n("lsr_account"),
            _n("lsr_taker"),
            _n("top_lsr_size"),
            _n("long_liq_usd"),
            _n("short_liq_usd"),
            _n("mark_price"),
        ))
    return rows


def _newest_ts(symbol: str, interval: str) -> int:
    with _lock:
        cur = _db().execute(
            "SELECT MAX(ts) FROM gateio_stats WHERE symbol=? AND interval=?",
            (symbol, interval),
        )
        row = cur.fetchone()
    return int(row[0]) if row and row[0] else 0


def _read_stats(symbol: str, interval: str, limit: int) -> list[dict]:
    with _lock:
        cur = _db().execute(
            "SELECT ts, oi, oi_usd, funding_rate, lsr_account, lsr_taker, top_lsr, "
            "long_liq_usd, short_liq_usd, mark_price FROM gateio_stats "
            "WHERE symbol=? AND interval=? ORDER BY ts DESC LIMIT ?",
            (symbol, interval, limit),
        )
        desc = cur.fetchall()
    desc.reverse()
    keys = ["time", "oi", "oiUsd", "funding", "lsr", "taker", "topLsr",
            "longLiqUsd", "shortLiqUsd", "mark"]
    return [dict(zip(keys, r)) for r in desc]


async def _backfill_gateio(symbol: str) -> None:
    daily = await gateio.contract_stats(symbol, "1d", 1000)
    hourly = await gateio.contract_stats(symbol, "1h", 720)
    _upsert_stats(symbol, "1d", _stats_to_rows(daily))
    _upsert_stats(symbol, "1h", _stats_to_rows(hourly))


async def _backfill_binance(symbol: str) -> None:
    """Fallback history from Binance public futures data (no liquidation
    sizes). Funding events are 8h-spaced; they land on their own timestamps
    and merge per-column with daily OI/LSR rows. Binance timestamps are
    milliseconds — normalised to seconds to match Gate.io rows."""
    hist = await binance.get_open_interest_hist(symbol, "1d", 500)
    _upsert_stats(symbol, "1d", [
        (int(h["timestamp"]) // 1000, float(h["sumOpenInterest"]),
         float(h["sumOpenInterestValue"]), None, None, None, None, None, None, None)
        for h in hist
    ])
    fhist = await binance.get_funding_rate_hist(symbol, 1000)
    _upsert_stats(symbol, "1d", [
        (int(f["fundingTime"]) // 1000, None, None, float(f["fundingRate"]),
         None, None, None, None, None, None)
        for f in fhist
    ])
    lsr = await binance.get_long_short_ratio(symbol, "1d", 500)
    _upsert_stats(symbol, "1d", [
        (int(r["timestamp"]) // 1000, None, None, None, float(r["longShortRatio"]),
         None, None, None, None, None)
        for r in lsr
    ])
    taker = await binance.get_taker_ratio(symbol, "1d", 500)
    _upsert_stats(symbol, "1d", [
        (int(r["timestamp"]) // 1000, None, None, None, None, float(r["buySellRatio"]),
         None, None, None, None)
        for r in taker
    ])


async def ensure_backfill(symbol: str) -> str | None:
    """Backfill stats history if stale. Priority: Gate.io contract_stats,
    then Binance futures public data. Returns the source used, or None.

    Concurrent callers for the same symbol wait for the in-flight backfill
    instead of reading partial data."""
    symbol = symbol.upper()
    deadline = time.monotonic() + 40.0
    while symbol in _backfilling:
        if time.monotonic() > deadline:
            return None
        await asyncio.sleep(0.4)
    newest = _newest_ts(symbol, "1d")
    if newest and time.time() - newest / 1000.0 < REFRESH_AFTER_S:
        return "fresh"
    _backfilling.add(symbol)
    used: str | None = None
    try:
        try:
            await _backfill_gateio(symbol)
            used = "gateio"
        except Exception:
            try:
                await _backfill_binance(symbol)
                used = "binance"
            except Exception:
                used = None
        return used
    finally:
        _backfilling.discard(symbol)


def record_snapshot(symbol: str, source: str | None, result: dict) -> None:
    """Append the current /api/derivatives response to the snapshots table."""
    def _v(k):
        v = result.get(k)
        return float(v) if isinstance(v, (int, float)) else None
    with _lock:
        _db().execute(
            "INSERT OR REPLACE INTO snapshots (ts, symbol, source, oi, oi_usd, funding, lsr, taker) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (int(time.time() * 1000), symbol, source, _v("openInterest"),
             _v("openInterestValue"), _v("fundingRate"),
             _v("longShortRatio"), _v("takerBuySellRatio")),
        )
        _db().commit()


def _percentile(values: list[float], x: float) -> float | None:
    if not values:
        return None
    below = sum(1 for v in values if v <= x)
    return round(below / len(values) * 100.0, 1)


def history_stats(symbol: str) -> dict | None:
    """Percentile context from the stored daily window (up to ~1000 days)."""
    daily = _read_stats(symbol.upper(), "1d", 4000)
    if len(daily) < 30:
        return None
    cur = daily[-1]
    # row count is not day count when 8h funding events are mixed in
    span_days = max(1, (daily[-1]["time"] - daily[0]["time"]) // 86400)
    funding_all = [d["funding"] for d in daily if d["funding"] is not None]
    oi_all = [d["oiUsd"] for d in daily if d["oiUsd"] is not None]
    lsr_all = [d["lsr"] for d in daily if d["lsr"] is not None]
    out = {
        "days": span_days,
        "fundingPctl": _percentile(funding_all, cur["funding"]) if cur["funding"] is not None else None,
        "oiUsdPctl": _percentile(oi_all, cur["oiUsd"]) if cur["oiUsd"] is not None else None,
        "lsrPctl": _percentile(lsr_all, cur["lsr"]) if cur["lsr"] is not None else None,
    }
    # 1y liquidation context: total daily liq USD distribution
    liq_days = [d for d in daily if (d["longLiqUsd"] or 0) + (d["shortLiqUsd"] or 0) > 0]
    if liq_days:
        # dedupe per calendar day (hourly-refreshed rows share the same date)
        per_day: dict[str, float] = {}
        for d in liq_days:
            key = str(d["time"] // 86400)
            per_day[key] = max(per_day.get(key, 0.0),
                               (d["longLiqUsd"] or 0) + (d["shortLiqUsd"] or 0))
        totals = sorted(per_day.values())
        if totals:
            out["liqDayPctlBase"] = {
                "median": totals[len(totals) // 2],
                "p90": totals[int(len(totals) * 0.9)] if len(totals) >= 10 else None,
                "max": totals[-1],
                "days": len(totals),
            }
    return out


def hourly_series(symbol: str, limit: int = 720) -> list[dict]:
    return _read_stats(symbol.upper(), "1h", limit)
