"""Trade journal: SQLite CRUD + deterministic plan replay for adherence.

The replay answers "what would the calibrated plan have done with this
trade": starting from the bar at open time it walks candles (from the local
kline cache) applying the frozen geometry (stop -> BE ladder at +beR half
scale-out -> runner trail/target -> time exit), with conservative intrabar
ordering (stop checked before triggers). The comparison of planned vs
actual exit is the adherence metric — the execution edge validated in the
round-9/11 backtests only materializes when trades are managed per plan.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from services import kline_cache
from services.analysis.decision import PLAN_GEOMETRY, PLAN_DEFAULT_INTERVAL

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "journal.db"
STEP_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

_COL_LIST = ["id", "created_at", "symbol", "interval", "direction", "entry", "stop",
             "qty", "leverage", "opened_at", "status", "closed_at", "exit_price",
             "exit_reason", "r_multiple", "plan_json", "plan_exit_json", "adherence", "notes"]
_COLS = ", ".join(_COL_LIST)


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry REAL NOT NULL,
                stop REAL,
                qty REAL,
                leverage REAL,
                opened_at INTEGER,
                status TEXT NOT NULL DEFAULT 'open',
                closed_at INTEGER,
                exit_price REAL,
                exit_reason TEXT,
                r_multiple REAL,
                plan_json TEXT,
                plan_exit_json TEXT,
                adherence TEXT,
                notes TEXT
            )"""
        )
        conn.commit()
        _conn = conn
    return _conn


def _row_to_dict(row) -> dict:
    d = dict(zip(_COL_LIST, row))
    d["plan"] = json.loads(d.pop("plan_json") or "null")
    d["planExit"] = json.loads(d.pop("plan_exit_json") or "null")
    return d


def create_trade(data: dict) -> dict:
    now = int(time.time() * 1000)
    with _lock:
        cur = _db().execute(
            "INSERT INTO trades (created_at, symbol, interval, direction, entry, stop, qty, "
            "leverage, opened_at, status, plan_json, notes) VALUES (?,?,?,?,?,?,?,?,?,'open',?,?)",
            (now, data["symbol"], data["interval"], data["direction"], data["entry"],
             data.get("stop"), data.get("qty"), data.get("leverage"), data.get("openedAt"),
             json.dumps(data.get("plan")) if data.get("plan") else None, data.get("notes")),
        )
        _db().commit()
        tid = cur.lastrowid
    return get_trade(int(tid))


def get_trade(tid: int) -> dict | None:
    with _lock:
        cur = _db().execute(f"SELECT {_COLS} FROM trades WHERE id=?", (tid,))
        row = cur.fetchone()
    return _row_to_dict(row) if row else None


def list_trades(status: str | None = None, symbol: str | None = None, limit: int = 100) -> list[dict]:
    q = f"SELECT {_COLS} FROM trades"
    conds, params = [], []
    if status:
        conds.append("status=?")
        params.append(status)
    if symbol:
        conds.append("symbol=?")
        params.append(symbol.upper())
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _lock:
        cur = _db().execute(q, params)
        rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def delete_trade(tid: int) -> bool:
    with _lock:
        cur = _db().execute("DELETE FROM trades WHERE id=?", (tid,))
        _db().commit()
    return cur.rowcount > 0


def _default_plan(interval: str, entry: float, stop: float | None) -> dict:
    depth, stopw, be_frac, tgt_r, texit, trail, fill_bars = PLAN_GEOMETRY.get(
        interval, PLAN_GEOMETRY[PLAN_DEFAULT_INTERVAL])
    return {"stop": stop, "beR": be_frac, "targetR": tgt_r, "trailR": trail,
            "texitBars": texit, "fillBars": fill_bars}


async def replay_plan(trade: dict, until_ts: int | None = None) -> dict:
    """Simulate plan management from open time; return the planned outcome.

    Conservative intrabar ordering: stop first, then BE trigger / trail
    update / target. Actual entry is assumed filled (journal records a real
    position)."""
    interval = trade["interval"]
    step = STEP_MS.get(interval, 3_600_000)
    long = trade["direction"] == "long"
    entry = float(trade["entry"])
    opened = int(trade.get("opened_at") or trade.get("created_at"))
    end_ts = int(until_ts or trade.get("closed_at") or time.time() * 1000)

    plan = dict(trade.get("plan") or {})
    defaults = _default_plan(interval, entry, trade.get("stop"))
    for k, v in defaults.items():
        plan.setdefault(k, v)
    # prefer the user's actual stop as the initial plan stop
    if trade.get("stop"):
        plan["stop"] = float(trade["stop"])
    stop = float(plan["stop"] or entry - (0.02 * entry))
    if stop == entry:  # degenerate
        stop = entry * (0.98 if long else 1.02)
    risk = abs(entry - stop)
    if risk <= 0:
        return {"r": None, "reason": "invalid", "exitPrice": None, "barsHeld": None}

    be_frac = float(plan.get("beR") or 0.5)
    tgt_r = plan.get("targetR")
    trail = plan.get("trailR")
    texit = int(plan.get("texitBars") or 48)

    bars_needed = min(500, max(5, (end_ts - opened) // step + 4))
    try:
        rows = await kline_cache.get_klines(trade["symbol"], interval, bars_needed)
    except Exception:
        rows = []
    bars = [r for r in rows if r[0] >= opened - step]
    bars = [b for b in bars if b[0] <= end_ts + step]

    be_trigger = entry + be_frac * risk if long else entry - be_frac * risk
    target = (entry + float(tgt_r) * risk if long else entry - float(tgt_r) * risk) if tgt_r else None

    stop_cur = stop
    be_done = False
    mfe = entry
    exit_price = None
    reason = None
    bars_held = 0

    for i, b in enumerate(bars):
        bars_held = i + 1
        _, o, h, low, c, v, tb = b
        # 1) stop (conservative: first)
        if (long and low <= stop_cur) or ((not long) and h >= stop_cur):
            exit_price, reason = stop_cur, ("be_stop" if be_done else "stop")
            break
        # 2) BE trigger
        if not be_done and ((long and h >= be_trigger) or ((not long) and low <= be_trigger)):
            be_done = True
            stop_cur = entry
        # 3) trail update from MFE (after BE; runner management)
        if be_done and trail is not None:
            mfe = max(mfe, h) if long else min(mfe, low)
            mfe_r = (mfe - entry) / risk if long else (entry - mfe) / risk
            trail_stop_r = mfe_r - float(trail)
            if trail_stop_r > 0:
                new_stop = entry + trail_stop_r * risk if long else entry - trail_stop_r * risk
                if (long and new_stop > stop_cur) or ((not long) and new_stop < stop_cur):
                    stop_cur = new_stop
        # 4) fixed target (1h family)
        if target is not None and ((long and h >= target) or ((not long) and low <= target)):
            exit_price, reason = target, "target"
            break
        # 5) time exit
        if bars_held >= texit:
            exit_price, reason = float(c), "time"
            break
        exit_price = float(c)  # running mark if loop ends without event

    if exit_price is None:
        exit_price = entry
        reason = "unfilled"
    elif reason is None:
        # bars exhausted before any management event fired
        reason = "open" if trade.get("status") == "open" else "censored_at_close"

    # total R with scale-out convention (half at be_frac R, half at exit)
    runner_r = ((exit_price - entry) / risk) if long else ((entry - exit_price) / risk)
    if be_done:
        total_r = 0.5 * be_frac + 0.5 * runner_r
        # BE leg: half position exits flat at entry after stop moved (0R) —
        # convention: first half exits at +be_frac R (scale-out at trigger)
    else:
        total_r = runner_r

    return {
        "r": round(total_r, 3),
        "reason": reason,
        "exitPrice": round(float(exit_price), 8),
        "barsHeld": bars_held,
        "beDone": be_done,
        "geometry": {"stopAtr": None, "beR": be_frac, "targetR": tgt_r,
                     "trailR": trail, "texitBars": texit},
    }


async def close_trade(tid: int, exit_price: float, reason: str, closed_at: int | None,
                      notes: str | None) -> dict | None:
    trade = get_trade(tid)
    if trade is None or trade["status"] != "open":
        return None
    long = trade["direction"] == "long"
    entry = float(trade["entry"])
    stop = float(trade["stop"] or entry * (0.98 if long else 1.02))
    risk = abs(entry - stop)
    actual_r = ((exit_price - entry) / risk) if long else ((entry - exit_price) / risk)

    plan_exit = await replay_plan(trade, until_ts=closed_at)
    # adherence: actual exit close to planned exit (within 0.5R) or the same
    # terminal reason
    adherence = "n/a"
    if plan_exit.get("r") is not None:
        same_reason = {"stop": {"stop", "be_stop"}, "target": {"target"},
                       "time": {"time"}, "trail": {"be_stop"}}.get(reason)
        if (abs(actual_r - plan_exit["r"]) <= 0.5) or (same_reason and plan_exit["reason"] in same_reason):
            adherence = "followed"
        else:
            adherence = "deviated"

    now = int(closed_at or time.time() * 1000)
    with _lock:
        _db().execute(
            "UPDATE trades SET status='closed', closed_at=?, exit_price=?, exit_reason=?, "
            "r_multiple=?, plan_exit_json=?, adherence=?, notes=COALESCE(?, notes) WHERE id=?",
            (now, exit_price, reason, round(actual_r, 3), json.dumps(plan_exit),
             adherence, notes, tid),
        )
        _db().commit()
    return get_trade(tid)


def stats() -> dict:
    with _lock:
        cur = _db().execute(
            "SELECT symbol, interval, direction, r_multiple, adherence, "
            "(opened_at - created_at) FROM trades WHERE status='closed'")
        rows = cur.fetchall()
    closed = len(rows)
    if not closed:
        return {"closed": 0}
    rs = [r[3] for r in rows if r[3] is not None]
    wins = sum(1 for r in rs if r > 0)
    non_loss = sum(1 for r in rs if r >= 0)
    followed = sum(1 for r in rows if r[4] == "followed")
    by_symbol: dict[str, list] = {}
    by_interval: dict[str, list] = {}
    for r in rows:
        by_symbol.setdefault(r[0], []).append(r[3] or 0.0)
        by_interval.setdefault(r[1], []).append(r[3] or 0.0)
    return {
        "closed": closed,
        "wins": wins,
        "winRate": round(wins / closed * 100, 1) if closed else None,
        "nonLossRate": round(non_loss / closed * 100, 1) if closed else None,
        "sumR": round(sum(rs), 2) if rs else None,
        "avgR": round(sum(rs) / len(rs), 3) if rs else None,
        "adherenceRate": round(followed / closed * 100, 1) if closed else None,
        "bySymbol": {k: round(sum(v), 2) for k, v in by_symbol.items()},
        "byInterval": {k: round(sum(v), 2) for k, v in by_interval.items()},
    }
