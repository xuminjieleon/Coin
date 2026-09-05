"""Automated execution of CoinLens trade plans on Binance USDT-M futures.

Decision source is IDENTICAL to the push/API path: closed-bar replay analysis
(services.analysis.context.run_analysis, as_of = newest closed bar) — the
executor acts on the same 【新】/【改单】/【转向】/【消失】state machine the
notifier pushes, and manages fills with the SAME frozen geometry the journal
replay and the 5-year backtests use (stop -> +beR half out + stop to entry ->
trail runner -> time exit; conservative intrabar order: stop before triggers).
Plan semantics freeze on placement: once an entry fills, that position is
managed by its frozen plan only — new plans for the same symbol|interval are
ignored until it closes (backtest口径, DEVLOG 第十七轮).

Safety invariants (the design goal — local process death ≠ naked position):
- The protective STOP_MARKET (reduce-only) rests on the EXCHANGE together
  with the entry limit. reduce-only means it can never open/increase a
  position; triggered while flat it simply expires.
- The management layer (be / trail / time exit) only ever moves protection
  CLOSER to price (stop to entry, trail ratchet); if the local process dies
  the worst case is an under-optimized exit, never an unprotected one.
- Every management tick verifies each open position still HAS a resting
  stop; a missing stop (manual cancel etc.) is immediately re-placed.
- Entries are post-only (GTX) limit orders — the maker discipline the fee
  rounds showed 1h depends on. A post-only rejection parks the plan in a
  retry state; the entry is re-attempted while the plan is still valid.
- One executor instance per account: client order ids carry a per-machine
  instance tag so a second machine never adopts/cancels the first one's
  orders. Never run executors on both machines simultaneously anyway.

Honest gaps vs the backtest (documented in AGENTS §5, do not paper over):
- fills are real, not assumed: a fast move through the entry before the
  order rests is missed (backtest fills on touch); slippage on stop-market
  exits; funding carry not modeled; fees real.
- be-fill -> stop-to-entry takes effect on the next management tick (≤30s);
  within that window the OLD stop level still protects (worse for us, never
  better — the honest direction).

Modes: dryRun (paper broker, simulated fills from real 1m klines, no keys),
testnet (testnet.binancefuture.com), live (fapi.binance.com — requires
confirmLive=true + keys + enabled). Config: backend/data/executor.json
(API secret — gitignored); state: backend/data/executor.db.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import socket
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from services import binance, binance_trade, journal_store, kline_cache, notifier
from services.analysis.context import STEP_MS, closed_bar_analysis, last_closed_open
from services.notifier import drift_material

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CFG_PATH = DATA_DIR / "executor.json"
DB_PATH = DATA_DIR / "executor.db"

EXEC_INTERVALS = {"1h", "4h"}  # 2026-09-02 用户拍板：自动化 1h+4h（与推送一致）

DEFAULT_CFG = {
    "enabled": False,        # master switch; False = no new placements
    "testnet": True,         # 测试网先行（拍板）
    "dryRun": True,          # paper broker by default — no keys needed
    "apiKey": "",
    "apiSecret": "",
    "confirmLive": False,    # must be explicitly armed for mainnet
    "instance": "",          # per-machine coid tag (auto from hostname)
    "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
                "ZECUSDT", "DOGEUSDT", "SUIUSDT", "LTCUSDT", "LINKUSDT",
                "ADAUSDT"],
    "intervals": ["1h", "4h"],
    # 单笔风险 % 权益：2026-09-02 用户两次确认要 15%（f 阶梯审计：1h+4h RUIN@17.5%、
    # 15% 时 DD ~94%）——paper/testnet 沙盒按 15 跑给用户看实际回撤；
    # 实盘硬闸 ≤3 在路由层强制（突破须显式再拍板）
    "riskPct": 15.0,
    "leverage": 5,           # isolated margin leverage (risk is stop-based)
    "maxConcurrent": 8,      # stacked-backtest peak concurrency
    # Notional caps are MARGIN backstops only — risk sizing is stop-based and
    # tight crypto stops legitimately need 0.5~1.3x equity notional at f=1.5%
    # (the 5y backtests had no notional cap; median stop distance ~1.8%).
    "maxNotionalPctPer": 300.0,   # % of equity per position notional cap
    "maxGrossNotionalPct": 800.0,  # % of equity total open notional cap
    "dailyLossLimitR": 6.0,  # realized-R stop for new entries (UTC day)
    "equityUsd": 10000.0,    # paper sizing base
    "postOnlyEntry": True,   # GTX maker entry (1h fee economics)
    "pushEvents": True,      # push fills/closes through the notify channel
}

REASON_LABEL = {
    "stop": "止损", "be_stop": "保本/跟踪止损", "target": "目标止盈",
    "time": "时间退出", "panic": "紧急全撤", "plan_gone": "计划消失撤单",
    "flip": "计划转向撤单", "amend": "改单撤换", "expired": "挂单到期撤单",
    "disabled": "关闭自动交易撤单", "manual_extern": "场外手动平仓",
    "post_only": "未成交撤单", "place_failed": "下单失败", "mode_switch": "模式切换关闭",
}

STATE_LABEL = {"pending": "挂单中", "open": "持仓中", "closed": "已平仓"}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _fmt_num(x: float) -> str:
    s = f"{x:.8f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _floor_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    return math.floor(qty / step + 1e-12) * step


def _round_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    return round(price / tick) * tick


def _fmt_p(p: float) -> str:
    if p >= 1000:
        return f"{p:,.0f}"
    if p >= 1:
        return f"{p:,.2f}"
    if p == 0:
        return "0"
    exp = math.floor(math.log10(abs(p)))
    return f"{p:.{max(0, 4 - exp)}f}"


# ===================================================================== DB

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

_POS_COLS = ("id, key, symbol, interval, direction, plan_json, entry, stop, qty, "
             "filled, be_qty, avg_price, state, be_done, mfe, seq, awaiting_entry, "
             "entry_coid, stop_coid, be_coid, tgt_coid, created_at, opened_at, "
             "be_fill_at, closed_at, exit_reason, exit_price, r_multiple, notes")


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")  # fsync per commit stalls the loop
        conn.execute(
            """CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL,
                symbol TEXT NOT NULL, interval TEXT NOT NULL, direction TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                entry REAL NOT NULL, stop REAL NOT NULL,
                qty REAL NOT NULL, filled REAL DEFAULT 0, be_qty REAL DEFAULT 0,
                avg_price REAL, state TEXT NOT NULL DEFAULT 'pending',
                be_done INTEGER DEFAULT 0, mfe REAL, seq INTEGER DEFAULT 0,
                awaiting_entry INTEGER DEFAULT 0,
                entry_coid TEXT, stop_coid TEXT, be_coid TEXT, tgt_coid TEXT,
                created_at INTEGER NOT NULL, opened_at INTEGER, be_fill_at INTEGER,
                closed_at INTEGER, exit_reason TEXT, exit_price REAL,
                r_multiple REAL, notes TEXT)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS sim_orders (
                coid TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
                otype TEXT NOT NULL, price REAL, stop_price REAL, qty REAL NOT NULL,
                reduce_only INTEGER DEFAULT 0, post_only INTEGER DEFAULT 0,
                pos_id INTEGER,
                status TEXT NOT NULL, filled REAL DEFAULT 0, avg_price REAL,
                created_at INTEGER, updated_at INTEGER)"""
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "at INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_positions_key ON positions(key)")
        conn.commit()
        _conn = conn
    return _conn


def _row_dict(row) -> dict:
    d = dict(zip(_POS_COLS.split(", "), row))
    d["plan"] = json.loads(d.pop("plan_json") or "{}")
    d["be_done"] = bool(d["be_done"])
    d["awaiting_entry"] = bool(d["awaiting_entry"])
    return d


def _get_pos(key: str) -> dict | None:
    with _lock:
        cur = _db().execute(
            f"SELECT {_POS_COLS} FROM positions WHERE key=? AND state!='closed'", (key,)
        )
        row = cur.fetchone()
    return _row_dict(row) if row else None


def _get_pos_by_id(pid: int) -> dict | None:
    with _lock:
        cur = _db().execute(f"SELECT {_POS_COLS} FROM positions WHERE id=?", (pid,))
        row = cur.fetchone()
    return _row_dict(row) if row else None


def _active_positions() -> list[dict]:
    with _lock:
        cur = _db().execute(
            f"SELECT {_POS_COLS} FROM positions WHERE state!='closed' ORDER BY id"
        )
        rows = cur.fetchall()
    return [_row_dict(r) for r in rows]


def _place_failed_this_bar(sym: str, itv: str, direction: str,
                           entry: float, stop: float, tick: float) -> bool:
    """Circuit breaker: an identical placement (same key+direction+tick-rounded
    entry/stop) already failed inside the CURRENT plan bar's window → do not
    re-attempt until the next bar. Root cause of the 2026-09-03 incident: a
    -4120 (STOP_MARKET not on this endpoint) rejection row is state='closed',
    so _active_positions never sees it and _plan_tick re-created the same
    pending row every hour — 21 identical failures for BCH in one day.

    Window anchor = last_closed_open(now, itv): the plan being placed was
    computed at that as-of bar and stays identical until the bar closes, so
    any failure stamped >= that anchor belongs to the same placement."""
    anchor = last_closed_open(_now_ms(), itv)
    tol = max(tick, 1e-12) / 2.0
    with _lock:
        cur = _db().execute(
            "SELECT entry, stop FROM positions WHERE key=? AND state='closed' "
            "AND exit_reason='place_failed' AND direction=? AND created_at>=? "
            "ORDER BY id DESC LIMIT 1",
            (f"{sym}|{itv}", direction, anchor),
        )
        row = cur.fetchone()
    if row is None:
        return False
    return abs(row[0] - entry) <= tol and abs(row[1] - stop) <= tol


def _upd(pid: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        _db().execute(f"UPDATE positions SET {cols} WHERE id=?", (*fields.values(), pid))
        _db().commit()


def _meta_get(k: str, default: str = "") -> str:
    with _lock:
        cur = _db().execute("SELECT v FROM meta WHERE k=?", (k,))
        row = cur.fetchone()
    return row[0] if row else default


def _meta_set(k: str, v: str) -> None:
    with _lock:
        _db().execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, v))
        _db().commit()


def _log_event(kind: str, text: str) -> None:
    with _lock:
        _db().execute(
            "INSERT INTO events (at, kind, text) VALUES (?,?,?)",
            (_now_ms(), kind, text),
        )
        _db().commit()


def _recent_events(limit: int = 30) -> list[dict]:
    with _lock:
        cur = _db().execute(
            "SELECT at, kind, text FROM events ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = cur.fetchall()
    return [{"at": r[0], "kind": r[1], "text": r[2]} for r in rows]


# ============================================================== brokers

class PaperBroker:
    """Simulated exchange: orders in SQLite, fills from real 1m klines.

    Touch semantics per processed bar, priority = entry -> stop -> be/tgt
    (stop before profit triggers = the conservative replay ordering; the
    entry bar itself is managed, matching sim/journal replay). Post-only
    entries are rejected when they would cross the last close. Stop amends
    take effect on the next engine sync (≤30s) — deliberately the SAME
    latency a live broker has, so paper results don't flatter the live run.
    """

    def __init__(self):
        self._xinfo_cache: tuple[float, dict] = (0.0, {})

    async def sim_tick(self, symbols: list[str]) -> None:
        for sym in symbols:
            try:
                bars = await binance.get_klines(sym, "1m", 3, cache_ttl=20)
            except Exception:  # noqa: BLE001
                continue
            if not bars:
                continue
            last_closed = (_now_ms() // 60_000 - 1) * 60_000
            prev = int(_meta_get(f"simbar|{sym}", "0"))
            fresh = [b for b in bars if prev < b[0] <= last_closed]
            for bar in fresh:
                self._fill_bar(sym, int(bar[0]), float(bar[1]), float(bar[2]),
                               float(bar[3]), float(bar[4]))
            if fresh:
                _meta_set(f"simbar|{sym}", str(int(fresh[-1][0])))

    def _orders(self, symbol: str) -> list[dict]:
        with _lock:
            cur = _db().execute(
                "SELECT coid, side, otype, price, stop_price, qty, pos_id, status "
                "FROM sim_orders WHERE symbol=? AND status='NEW'", (symbol,)
            )
            return [dict(zip(("coid", "side", "otype", "price", "stop_price",
                              "qty", "pos_id", "status"), r)) for r in cur.fetchall()]

    def _fill_bar(self, sym, ts, o, h, low, c) -> None:
        def prio(od: dict) -> int:
            # entry limits first (opens), then stops (protection), then tps
            if od["otype"] == "LIMIT" and od["pos_id"]:
                row = _get_pos_by_id(od["pos_id"])
                if row and row["state"] == "pending":
                    return 0
            if od["otype"] == "STOP_MARKET":
                return 1
            return 2

        now = _now_ms()
        updates: list[tuple] = []
        for od in sorted(self._orders(sym), key=prio):
            px = od["price"] if od["otype"] == "LIMIT" else od["stop_price"]
            touched = False
            if od["side"] == "BUY":
                touched = low <= px if od["otype"] == "LIMIT" else h >= px
            else:
                touched = h >= px if od["otype"] == "LIMIT" else low <= px
            if touched:
                updates.append((px, now, od["coid"]))
        if not updates:
            return
        with _lock:
            # one commit per bar (W9): per-order commits fsynced the loop
            _db().executemany(
                "UPDATE sim_orders SET status='FILLED', filled=qty, "
                "avg_price=?, updated_at=? WHERE coid=?", updates)
            _db().commit()

    async def place(self, spec: dict) -> dict:
        if spec.get("post_only"):
            px = await self._last_price(spec["symbol"])
            if px is not None:
                if (spec["side"] == "BUY" and spec["price"] >= px) or \
                   (spec["side"] == "SELL" and spec["price"] <= px):
                    raise binance_trade.TradeError(
                        -5022, "Post Only order would be rejected (crossing)")
        with _lock:
            _db().execute(
                "INSERT OR REPLACE INTO sim_orders (coid, symbol, side, otype, price, "
                "stop_price, qty, reduce_only, post_only, pos_id, status, filled, "
                "avg_price, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?, 'NEW', 0, NULL, ?, ?)",
                (spec["coid"], spec["symbol"], spec["side"], spec["otype"],
                 spec.get("price"), spec.get("stop_price"), spec["qty"],
                 1 if spec.get("reduce_only") else 0, 1 if spec.get("post_only") else 0,
                 spec.get("pos_id"), _now_ms(), _now_ms()),
            )
            _db().commit()
        return {"clientOrderId": spec["coid"], "status": "NEW"}

    async def cancel(self, symbol: str, coid: str) -> None:
        with _lock:
            _db().execute(
                "UPDATE sim_orders SET status='CANCELED', updated_at=? "
                "WHERE coid=? AND status='NEW'", (_now_ms(), coid)
            )
            _db().commit()

    async def get(self, symbol: str, coid: str) -> dict | None:
        with _lock:
            cur = _db().execute(
                "SELECT status, filled, avg_price FROM sim_orders WHERE coid=?", (coid,)
            )
            row = cur.fetchone()
        if not row:
            return None
        return {"status": row[0], "executedQty": float(row[1] or 0),
                "avgPrice": float(row[2]) if row[2] is not None else None}

    async def market(self, symbol: str, side: str, qty: float, coid: str,
                     pos_id: int | None = None) -> float:
        px = await self._last_price(symbol)
        if px is None:
            raise binance_trade.TradeError(-1000, "no price for paper market order")
        with _lock:
            _db().execute(
                "INSERT OR REPLACE INTO sim_orders (coid, symbol, side, otype, price, "
                "qty, reduce_only, pos_id, status, filled, avg_price, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,1,?,'FILLED',?,?,?,?)",
                (coid, symbol, side, "MARKET", px, qty, pos_id, qty, px, _now_ms(), _now_ms()),
            )
            _db().commit()
        return px

    async def _last_price(self, symbol: str) -> float | None:
        try:
            bars = await binance.get_klines(symbol, "1m", 1, cache_ttl=10)
            return float(bars[-1][4]) if bars else None
        except Exception:  # noqa: BLE001
            return None

    async def equity(self) -> float | None:
        return None  # engine falls back to cfg equityUsd

    async def position_qty(self, symbol: str) -> float:
        return 0.0  # sizing uses local rows in paper mode

    async def filters(self, symbol: str) -> dict | None:
        return await _shared_filters(symbol, self)

    async def xinfo(self) -> dict:
        if time.time() - self._xinfo_cache[0] > 3600:
            info = await binance.get_exchange_info()
            self._xinfo_cache = (time.time(), info)
        return self._xinfo_cache[1]

    async def ensure_setup(self, symbol: str) -> None:
        pass

    async def open_all(self) -> list[dict]:
        with _lock:
            cur = _db().execute(
                "SELECT coid, symbol, side, otype, price, stop_price, qty, pos_id, "
                "status FROM sim_orders WHERE status='NEW'")
            rows = cur.fetchall()
        keys = ("clientOrderId", "symbol", "side", "type", "price", "stopPrice",
                "origQty", "pos_id", "status")
        return [dict(zip(keys, r)) for r in rows]

    async def available(self) -> float | None:
        return None  # sim locks no margin — sizing must not clamp in paper

    async def positions_all(self) -> list[dict]:
        return []


_ALGO_ROLES = {"S"}  # coid roles routed to the Algo Order API (protective stops)


def _is_algo_coid(coid: str | None) -> bool:
    """coid scheme cl<inst><pid><ROLE><seq> — protective stops ("S") live on the
    Algo Order API since Binance migrated conditional orders off
    POST /fapi/v1/order (testnet rejects them with -4120, 实测 2026-09-03;
    mainnet algo endpoints live the same day)."""
    m = re.search(r"\d([A-Z])\d+$", coid or "")
    return bool(m and m.group(1) in _ALGO_ROLES)


class LiveBroker:
    """Binance USDT-M futures via services/binance_trade.py (signed)."""

    def __init__(self, testnet: bool):
        binance_trade.configure(
            binance_trade.testnet_base() if testnet else binance_trade.mainnet_base(),
            _cfg.get("apiKey", ""), _cfg.get("apiSecret", ""))
        self._xinfo_cache: tuple[float, dict] = (0.0, {})

    async def place(self, spec: dict) -> dict:
        if spec["otype"] == "STOP_MARKET":
            return await binance_trade.place_algo_order({
                "algoType": "CONDITIONAL",
                "symbol": spec["symbol"], "side": spec["side"],
                "type": "STOP_MARKET", "quantity": _fmt_num(spec["qty"]),
                "triggerPrice": _fmt_num(spec["stop_price"]),
                "reduceOnly": "true",
                "workingType": "CONTRACT_PRICE",  # last-price touch = backtest口径
                "clientAlgoId": spec["coid"],
            })
        params = {
            "symbol": spec["symbol"], "side": spec["side"],
            "type": spec["otype"], "quantity": _fmt_num(spec["qty"]),
            "newClientOrderId": spec["coid"],
        }
        if spec.get("reduce_only"):
            params["reduceOnly"] = "true"
        if spec["otype"] == "LIMIT":
            params["price"] = _fmt_num(spec["price"])
            params["timeInForce"] = "GTX" if spec.get("post_only") else "GTC"
        return await binance_trade.place_order(params)

    async def cancel(self, symbol: str, coid: str) -> None:
        if _is_algo_coid(coid):
            await binance_trade.cancel_algo_order(coid)
        else:
            await binance_trade.cancel_order(symbol, coid)

    async def get(self, symbol: str, coid: str) -> dict | None:
        if _is_algo_coid(coid):
            return await self._get_algo(symbol, coid)
        o = await binance_trade.get_order(symbol, coid)
        if o is None:
            return None
        return {"status": o.get("status"),
                "executedQty": float(o.get("executedQty") or 0),
                "avgPrice": float(o["avgPrice"]) if o.get("avgPrice") not in (None, "", "0") else None}

    async def _get_algo(self, symbol: str, coid: str) -> dict | None:
        """Algo stop state mapped onto the order contract. TRIGGERED/FINISHED =
        the stop fired and its spawned MARKET order filled; CANCELED/EXPIRED/
        REJECTED are returned as None (= gone) so the management loop heals a
        dead protective stop the same way it heals a vanished one."""
        o = await binance_trade.get_algo_order(coid)
        if o is None:
            return None
        st = str(o.get("algoStatus") or "")
        if st == "NEW":
            return {"status": "NEW", "executedQty": 0.0, "avgPrice": None}
        if st in ("TRIGGERED", "FINISHED"):
            qty = float(o.get("actualQty") or 0)
            avg = None
            aid = o.get("actualOrderId")
            if aid not in (None, "", "0"):
                try:
                    od = await binance_trade.get_order_by_id(symbol, aid)
                    if od:
                        qty = float(od.get("executedQty") or 0) or qty
                        if od.get("avgPrice") not in (None, "", "0"):
                            avg = float(od["avgPrice"])
                except Exception:  # noqa: BLE001
                    pass
            return {"status": "FILLED", "executedQty": qty, "avgPrice": avg}
        return None  # CANCELED / EXPIRED / REJECTED / unknown → treat as gone

    async def market(self, symbol: str, side: str, qty: float, coid: str,
                     pos_id: int | None = None) -> float:
        await binance_trade.place_order({
            "symbol": symbol, "side": side, "type": "MARKET",
            "quantity": _fmt_num(qty), "newClientOrderId": coid, "reduceOnly": "true",
        })
        o = None
        for _ in range(3):
            await asyncio.sleep(0.4)
            o = await binance_trade.get_order(symbol, coid)
            if o and o.get("status") == "FILLED":
                break
        return float(o.get("avgPrice") or 0) if o else 0.0

    async def equity(self) -> float | None:
        return await binance_trade.usdt_equity()

    async def position_qty(self, symbol: str) -> float:
        try:
            rows = await binance_trade.position_risk(symbol)
            return sum(float(r.get("positionAmt") or 0) for r in rows or [])
        except Exception:  # noqa: BLE001
            return 0.0

    async def filters(self, symbol: str) -> dict | None:
        if time.time() - self._xinfo_cache[0] > 3600:
            info = await binance_trade.exchange_info()
            self._xinfo_cache = (time.time(), info)
        return _filters_from_info(self._xinfo_cache[1], symbol)

    async def ensure_setup(self, symbol: str) -> None:
        lev = int(_cfg.get("leverage") or 5)
        try:
            await binance_trade.set_leverage(symbol, lev)
        except binance_trade.TradeError as exc:
            _warn(f"{symbol} set_leverage({lev}) failed: {exc}")
        await binance_trade.set_margin_type(symbol, isolated=True)

    async def open_all(self) -> list[dict]:
        orders = list(await binance_trade.open_orders(None) or [])
        try:
            for o in await binance_trade.open_algo_orders() or []:
                orders.append({"clientOrderId": o.get("clientAlgoId"),
                               "symbol": o.get("symbol"),
                               "type": o.get("orderType"),
                               "status": o.get("algoStatus")})
        except Exception:  # noqa: BLE001
            pass  # reconcile coverage is best-effort; stops still work
        return orders

    async def available(self) -> float | None:
        """Free USDT margin (wallet minus open-order/position initial margin).
        -2019 storm root cause (2026-09-04): sizing never looked at this."""
        try:
            return await binance_trade.usdt_available()
        except Exception:  # noqa: BLE001
            return None

    async def positions_all(self) -> list[dict]:
        try:
            rows = await binance_trade.position_risk(None)
        except Exception:  # noqa: BLE001
            return []
        return [r for r in rows or [] if abs(float(r.get("positionAmt") or 0)) > 1e-9]


async def _shared_filters(symbol: str, broker: PaperBroker) -> dict | None:
    return _filters_from_info(await broker.xinfo(), symbol)


def _filters_from_info(info: dict, symbol: str) -> dict | None:
    for s in (info or {}).get("symbols", []):
        if s.get("symbol") == symbol:
            fl = {f["filterType"]: f for f in s.get("filters", [])}
            min_notional = 5.0
            mn = fl.get("MIN_NOTIONAL") or fl.get("MIN_NOTIONAL_5")
            if mn and "notional" in mn:
                min_notional = float(mn["notional"])
            return {"step": float(fl["LOT_SIZE"]["stepSize"]),
                    "tick": float(fl["PRICE_FILTER"]["tickSize"]),
                    "minNotional": min_notional}
    return None


# ================================================================ engine

_cfg: dict = dict(DEFAULT_CFG)
_broker: PaperBroker | LiveBroker | None = None
_mode: str | None = None
_task_hourly: asyncio.Task | None = None
_task_mgmt: asyncio.Task | None = None
_equity_cache: tuple[float, float] = (0.0, 0.0)  # (expires, value)
_reconciled: bool = False
_one_way_ok: bool = False
_last_error: str | None = None
_last_plan_run: int | None = None
_warned: set[str] = set()


def _warn(text: str) -> None:
    if text not in _warned:
        _warned.add(text)
        _log_event("warn", text)


def _set_error(msg: str) -> None:
    global _last_error
    _last_error = f"{msg} (@ {datetime.now().strftime('%m-%d %H:%M:%S')})"


def _load_cfg() -> None:
    global _cfg
    _cfg = dict(DEFAULT_CFG)
    try:
        raw = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    if isinstance(raw, dict):
        _cfg.update({k: raw[k] for k in DEFAULT_CFG if k in raw})
    if not _cfg.get("instance"):
        host = re.sub(r"[^a-z0-9]", "", (socket.gethostname() or "local").lower())[:4]
        _cfg["instance"] = host or "locl"
        _save_cfg()
    _cfg["intervals"] = [i for i in _cfg.get("intervals", []) if i in EXEC_INTERVALS] or ["1h"]


def _save_cfg() -> None:
    """Atomic write (temp + os.replace): a taskkill/power cut mid-write must
    never truncate the config — panic's enabled=false especially."""
    try:
        CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CFG_PATH.with_name(CFG_PATH.name + ".tmp")
        tmp.write_text(json.dumps(_cfg, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, CFG_PATH)
    except Exception:  # noqa: BLE001
        pass


def _coid(pid: int, role: str, seq: int) -> str:
    # e.g. "clDESK12S0" — machine-tagged so a second machine never touches us
    return f"cl{_cfg['instance']}{pid}{role}{seq}"


async def _push(title_tag: str, content: str) -> None:
    if not _cfg.get("pushEvents", True):
        return
    try:
        if not notifier.current_token():
            return
        await notifier.send_now(
            f"CoinLens 自动交易 {title_tag} {datetime.now().strftime('%H:%M')}",
            content)
    except Exception:  # noqa: BLE001
        pass


async def _equity() -> float | None:
    """Sizing base. Paper: configured equityUsd. Live/testnet: exchange wallet
    — and when it can't be fetched, None (W2 fix): real orders are NEVER
    sized off a guessed equity; the placement is simply skipped this round."""
    global _equity_cache
    if _mode == "paper":
        return float(_cfg.get("equityUsd") or 10000.0)
    if time.time() < _equity_cache[0]:
        return _equity_cache[1]
    val = None
    if _broker is not None:
        try:
            val = await _broker.equity()
        except Exception:  # noqa: BLE001
            val = None
    if val is None:
        return None
    _equity_cache = (time.time() + 300.0, val)
    return val


def _mode_of_cfg() -> str:
    if _cfg.get("dryRun", True):
        return "paper"
    return "testnet" if _cfg.get("testnet", True) else "live"


def _ensure_broker() -> None:
    global _broker, _mode, _reconciled, _one_way_ok, _equity_cache
    mode = _mode_of_cfg()
    if mode == "live" and not (_cfg.get("confirmLive") and _cfg.get("apiKey")):
        _set_error("实盘模式未武装：需要 confirmLive=true 且已配置 API key")
        _broker = None  # hard stop — never silently fall back to paper
        return
    if _broker is None or mode != _mode:
        _broker = PaperBroker() if mode == "paper" else LiveBroker(mode == "testnet")
        _mode = mode
        _reconciled = False
        _one_way_ok = False
        _equity_cache = (0.0, 0.0)


# ------------------------------------------------------- plan cycle

async def _plan_tick() -> None:
    """Hourly closed-bar plan cycle: place / amend / cancel pending entries.
    Filled positions are NOT touched here (plan freezes on fill)."""
    global _last_plan_run
    _last_plan_run = _now_ms()
    _ensure_broker()
    if _broker is None or not _cfg.get("enabled"):
        return
    if _mode != "paper" and not _reconciled:
        return  # never place before a successful exchange reconciliation

    symbols = [s for s in _cfg.get("symbols", []) if s]
    intervals = [i for i in _cfg.get("intervals", []) if i in EXEC_INTERVALS]
    jobs = [(s, i) for i in intervals for s in symbols]
    if not jobs:
        return
    # shared closed-bar semantics (round 55): same implementation as the
    # notifier push cycle — patching context.run_analysis stubs both
    analyses = await asyncio.gather(*[closed_bar_analysis(s, i) for s, i in jobs])
    plans: dict[str, dict | None] = {}
    failed: set[str] = set()
    for (sym, itv), a in zip(jobs, analyses):
        key = f"{sym}|{itv}"
        if a is None:
            failed.add(key)
            plans[key] = None
        else:
            plans[key] = (a.get("summary") or {}).get("tradePlan")

    active = {p["key"]: p for p in _active_positions()}
    step_ms = {i: STEP_MS[i] for i in intervals}
    now = _now_ms()
    to_place: list[tuple[str, str, dict]] = []

    # --- manage existing pending entries ---
    for key, pos in list(active.items()):
        if pos["state"] != "pending" or key not in plans:
            continue
        if key in failed:
            continue  # never cancel on stale data (notifier invariant)
        sym, itv = key.split("|", 1)
        plan = plans[key]
        if plan is None:
            await _cancel_pending(pos, "plan_gone")
            await _push("撤单", f"【撤单】{sym} {itv} 计划消失，已撤销未成交挂单\n"
                        f"入场 {_fmt_p(pos['entry'])} / 止损 {_fmt_p(pos['stop'])}")
        elif plan["direction"] != pos["direction"]:
            await _cancel_pending(pos, "flip")
            to_place.append((sym, itv, plan, "转向"))
        elif drift_material(pos["plan"], plan):
            await _cancel_pending(pos, "amend")
            to_place.append((sym, itv, plan, "改单"))
        else:
            held_bars = (now - pos["created_at"]) // step_ms[itv]
            if held_bars >= plan.get("fillBars", 24):
                await _cancel_pending(pos, "expired")
                await _push("撤单", f"【到期】{sym} {itv} 挂单 "
                            f"{plan.get('fillBars', 24)} 根未成交，已撤")

    # --- new placements ---
    if _daily_paused():
        return
    active = {p["key"]: p for p in _active_positions()}  # refresh after cancels
    for sym, itv, plan, tag in to_place:
        if f"{sym}|{itv}" in active:
            continue
        placed = await _try_place(sym, itv, plan, active)
        if placed and tag == "转向":
            await _push("转向", f"【转向】{sym} {itv} 已按新方向重新挂单（原挂单已撤）")
        elif placed and tag == "改单":
            await _push("改单", f"【改单】{sym} {itv} 挂单价格漂移，已自动改单")
    for key, plan in plans.items():
        if plan is None or key in failed or key in active:
            continue
        sym, itv = key.split("|", 1)
        if await _try_place(sym, itv, plan, active):
            active[key] = True


def _daily_paused() -> bool:
    day = datetime.utcnow().strftime("%Y-%m-%d")
    val = float(_meta_get(f"dayR|{day}", "0") or 0)
    limit = float(_cfg.get("dailyLossLimitR") or 0)
    if limit > 0 and val <= -limit:
        _warn(f"当日已亏 {val:.1f}R ≤ -{limit:g}R，暂停新开仓（持仓仍正常管理）")
        return True
    return False


async def _place_probed(spec: dict) -> dict:
    """Place an order; on ANY placement error probe the exchange by coid
    before treating it as failed — a delivered-but-unanswered order (e.g.
    read timeout after Binance accepted it) must never be mistaken for
    never-placed, or we'd cancel the protective stop and strand a live
    entry. This is the never-naked invariant for ambiguous deliveries."""
    try:
        return await _broker.place(spec)
    except Exception:
        info = None
        try:
            info = await _broker.get(spec["symbol"], spec["coid"])
        except Exception:  # noqa: BLE001
            info = None
        if info is not None and info.get("status") in ("NEW", "PARTIALLY_FILLED", "FILLED"):
            out = dict(info)
            out["clientOrderId"] = spec["coid"]
            out["recovered"] = True
            return out
        raise


async def _market_probed(symbol: str, side: str, qty: float, coid: str,
                         pos_id: int | None = None) -> float:
    """Market order with the same probe-by-coid recovery on ambiguity."""
    try:
        return await _broker.market(symbol, side, qty, coid, pos_id=pos_id)
    except Exception:
        try:
            info = await _broker.get(symbol, coid)
            if info is not None and info.get("status") == "FILLED":
                return float(info.get("avgPrice") or 0)
        except Exception:  # noqa: BLE001
            pass
        raise


async def _probe(symbol: str, coid: str) -> tuple[dict | None, bool]:
    """Order/algo state probe with a hard distinction (2026-09-05 BTC/XRP
    incident): ok=False means the PROBE ITSELF failed — no conclusion may be
    drawn about the order's existence. A flaky probe once cleared a resting
    entry coid, the plan cycle then cancelled only the stop, and the orphan
    entry later filled as an unmanaged naked position. ok=True + info=None
    is the only path that confirms the order is really gone (-2013)."""
    try:
        return await _broker.get(symbol, coid), True
    except Exception as exc:  # noqa: BLE001
        _warn(f"{symbol} 挂单探针失败（本轮不据此改动）: {exc}")
        return None, False


async def _try_place(sym: str, itv: str, plan: dict, active: dict) -> bool:
    """Guard chain + protective stop first + post-only entry.
    Returns True when a pending row was created."""
    if _broker is None:
        return False
    if len(active) >= int(_cfg.get("maxConcurrent") or 8):
        return False  # concurrency cap (pending+open reserve a slot)
    equity = await _equity()
    if equity is None or equity <= 0:
        _set_error("实网余额获取失败，本轮跳过新开仓（不按猜测的权益定仓）")
        return False  # W2: never size real orders off a guessed equity
    risk_usd = equity * float(_cfg.get("riskPct") or 0) / 100.0
    # shared budget: same symbol already active in the other interval -> half
    other = [k for k in active
             if k != f"{sym}|{itv}" and k.startswith(f"{sym}|")]
    if other:
        risk_usd /= 2.0
    filters = None
    try:
        filters = await _broker.filters(sym)
    except Exception as exc:  # noqa: BLE001
        _warn(f"{sym} filters unavailable: {exc}")
        return False
    if filters is None:
        _warn(f"{sym} 不在交易所合约列表（测试网可能未上线），跳过")
        return False
    risk = abs(plan["entry"] - plan["stop"])
    if risk <= 0:
        return False
    qty = _floor_step(risk_usd / risk, filters["step"])
    if qty <= 0:
        _warn(f"{sym} 风险预算不足以开 1 step（risk=${risk_usd:.0f}）")
        return False
    entry = _round_tick(plan["entry"], filters["tick"])
    stop = _round_tick(plan["stop"], filters["tick"])
    if _place_failed_this_bar(sym, itv, plan["direction"], entry, stop,
                              filters["tick"]):
        _warn(f"{sym} {itv} 相同挂单本 bar 已失败过，熔断至下一 bar")
        return False
    qty = min(qty, _floor_step(
        equity * float(_cfg.get("maxNotionalPctPer") or 30) / 100.0 / entry,
        filters["step"]))
    notional = qty * entry
    if notional < filters["minNotional"]:
        _warn(f"{sym} 名义额 {notional:.0f} 低于最小 {filters['minNotional']:.0f}，跳过")
        return False
    gross = 0.0
    for p in active.values():
        if isinstance(p, dict):
            q = p["filled"] or p["qty"]
            gross += q * p["entry"]
    if (gross + notional) > equity * float(_cfg.get("maxGrossNotionalPct") or 250) / 100.0:
        _warn("总名义额超上限，跳过新开仓")
        return False
    if _mode != "paper":
        # -2019 guard (2026-09-05): clamp the order to what the free margin
        # can actually fund — 15% risk × ~2% stops demands notional far above
        # wallet×leverage, and Binance rejects every entry with "Margin is
        # insufficient" once earlier orders/positions lock the wallet.
        avail = None
        try:
            avail = await _broker.available()
        except Exception:  # noqa: BLE001
            avail = None
        if avail is not None:
            lev = max(int(_cfg.get("leverage") or 5), 1)
            cap_notional = avail * lev * 0.95
            if notional > cap_notional:
                q2 = _floor_step(cap_notional / entry, filters["step"])
                if q2 * entry < filters["minNotional"]:
                    _warn(f"{sym} 可用保证金 {_fmt_num(avail)} 撑不起最小名义额 "
                          f"{filters['minNotional']:.0f}，跳过（下轮重试）")
                    return False
                _warn(f"{sym} 保证金钳制 qty {_fmt_num(qty)} → {_fmt_num(q2)}"
                      f"（可用 {_fmt_num(avail)} × {lev}x 杠杆）")
                qty = q2
                notional = qty * entry

    if _mode != "paper":
        try:
            await _broker.ensure_setup(sym)
        except Exception as exc:  # noqa: BLE001
            _warn(f"{sym} 杠杆/保证金模式设置失败: {exc}")
            return False

    long = plan["direction"] == "long"
    seq = 0
    with _lock:
        cur = _db().execute(
            "INSERT INTO positions (key, symbol, interval, direction, plan_json, entry, "
            "stop, qty, state, created_at) VALUES (?,?,?,?,?,?,?,?, 'pending', ?)",
            (f"{sym}|{itv}", sym, itv, plan["direction"],
             json.dumps({k: plan.get(k) for k in
                         ("direction", "entry", "stop", "target1", "beTrigger", "beR",
                          "targetR", "trailR", "texitBars", "fillBars")}),
             entry, stop, qty, _now_ms()),
        )
        _db().commit()
        pid = int(cur.lastrowid)
    e_coid = _coid(pid, "E", seq)
    s_coid = _coid(pid, "S", seq)
    stop_spec = {"symbol": sym, "side": "SELL" if long else "BUY",
                 "otype": "STOP_MARKET", "qty": qty, "stop_price": stop,
                 "reduce_only": True, "coid": s_coid, "pos_id": pid}
    entry_spec = {"symbol": sym, "side": "BUY" if long else "SELL",
                  "otype": "LIMIT", "qty": qty, "price": entry,
                  "post_only": bool(_cfg.get("postOnlyEntry", True)),
                  "coid": e_coid, "pos_id": pid}
    try:
        await _place_probed(stop_spec)  # protection first
    except Exception as exc:  # noqa: BLE001
        _upd(pid, state="closed", exit_reason="place_failed", notes=str(exc))
        _set_error(f"{sym} 止损单失败: {exc}")
        return False
    # W4 fix: persist the stop coid the moment the stop rests — a crash
    # between here and the entry placement must not strand an unrecorded
    # protective order + a stuck pending row
    _upd(pid, seq=seq, stop_coid=s_coid)
    awaiting = 0
    try:
        await _place_probed(entry_spec)
    except binance_trade.TradeError as exc:
        if exc.code in (-5022, -2010, -2011):  # post-only would cross
            awaiting = 1  # keep stop; retry entry while plan valid
        else:
            # entry rejected (-2019 margin etc.) — the just-rested stop MUST
            # be verified gone: a silently failed cancel left an orphan FIL
            # stop (2026-09-05) that then blocked margin-mode setup (-4067)
            await _cancel_verified(sym, s_coid, e_coid)
            _upd(pid, state="closed", exit_reason="place_failed", notes=str(exc))
            _set_error(f"{sym} 入场单失败: {exc}")
            return False
    except Exception as exc:  # noqa: BLE001
        # _place_probed already verified the order does NOT rest — cancel
        # the stop (and the entry defensively) and close the row
        await _cancel_verified(sym, s_coid, e_coid)
        _upd(pid, state="closed", exit_reason="place_failed", notes=str(exc))
        _set_error(f"{sym} 入场单失败: {exc}")
        return False
    _upd(pid, awaiting_entry=awaiting,
         entry_coid=(None if awaiting else e_coid))
    active[f"{sym}|{itv}"] = _get_pos_by_id(pid)
    _log_event("place", f"挂单 {sym} {itv} {'做多' if long else '做空'} "
               f"入场 {_fmt_p(entry)} 止损 {_fmt_p(stop)} qty {_fmt_num(qty)}"
               + ("（post-only 拒绝，待回踩上方重挂）" if awaiting else ""))
    await _push("挂单", f"【挂单】{sym} {itv} {'做多' if long else '做空'}\n"
                f"入场 {_fmt_p(entry)}（限价回踩）｜止损 {_fmt_p(stop)}")
    return True


async def _cancel_quietly(symbol: str, *coids: str) -> None:
    """Best-effort cancels; -2011 (already gone) and network errors pass."""
    if _broker is None:
        return
    for coid in coids:
        if not coid:
            continue
        try:
            await _broker.cancel(symbol, coid)
        except Exception:  # noqa: BLE001
            pass


async def _cancel_verified(symbol: str, *coids: str) -> None:
    """Cancel + CONFIRM gone, twice. Protective-order cancels must not fail
    silently (FIL 09-05: an orphaned stop then blocked -4067 setup checks).
    A coid that still rests after two tries is logged loudly — the 30s
    orphan sweep will retry it every round until clean."""
    if _broker is None:
        return
    for coid in coids:
        if not coid:
            continue
        for _ in range(2):
            try:
                await _broker.cancel(symbol, coid)
            except Exception:  # noqa: BLE001
                pass
            info, ok = await _probe(symbol, coid)
            if not ok or info is None:
                break  # gone (or unverifiable this round — sweep will retry)
        else:
            _log_event("warn", f"{symbol} 撤单后 {coid} 仍存在，孤儿清扫将持续处理")


async def _retry_entry(pos: dict) -> None:
    """Pending with no resting entry (post-only rejected earlier, or a row
    stranded before its entry was placed): re-attempt the GTX placement
    whenever it can rest as maker again."""
    if _broker is None or pos["state"] != "pending" or pos.get("entry_coid"):
        return  # an entry already rests — nothing to re-place
    # Race fix (round 60): the caller's snapshot may be stale — the plan tick
    # can close this row between the fetch and now (e.g. stop placement failed
    # with -4120 on 2026-09-03 and an entry was re-placed onto a closed row,
    # leaving a NAKED filled position on the exchange). Re-read under lock and
    # never place an entry for a row whose protective stop never rested.
    fresh = _get_pos_by_id(pos["id"])
    if fresh is None or fresh["state"] != "pending" or fresh.get("entry_coid") \
            or not fresh.get("stop_coid"):
        return
    pos = fresh
    spec = {"symbol": pos["symbol"],
            "side": "BUY" if pos["direction"] == "long" else "SELL",
            "otype": "LIMIT", "qty": pos["qty"], "price": pos["entry"],
            "post_only": bool(_cfg.get("postOnlyEntry", True)),
            "coid": _coid(pos["id"], "E", pos["seq"] + 1), "pos_id": pos["id"]}
    try:
        await _broker.place(spec)
    except Exception:  # noqa: BLE001
        return
    # The row may have closed while the placement was in flight — never leave
    # the entry resting without its row (and its stop) alive.
    fresh2 = _get_pos_by_id(pos["id"])
    if fresh2 is None or fresh2["state"] != "pending":
        try:
            await _broker.cancel(spec["symbol"], spec["coid"])
        except Exception:  # noqa: BLE001
            pass
        return
    _upd(pos["id"], seq=pos["seq"] + 1, entry_coid=spec["coid"], awaiting_entry=0)
    _log_event("place", f"重挂入场 {pos['symbol']} {pos['interval']} @ {_fmt_p(pos['entry'])}")


async def _cancel_pending(pos: dict, reason: str, adopt_fill: bool = True) -> None:
    """Cancel a pending entry; adopt the filled part as an open position."""
    if _broker is None:
        return
    filled = 0.0
    avg = None
    if pos.get("entry_coid"):
        info, ok = await _probe(pos["symbol"], pos["entry_coid"])
        if not ok:
            # probe failed while the entry may have FILLED — cancelling the
            # stop now would strand a naked position. Keep the row as is and
            # let the next tick retry with fresh probe data.
            _set_error(f"{pos['symbol']} 撤单前入场探针失败，本轮保留不动")
            return
        if info:
            filled = float(info.get("executedQty") or 0)
            avg = info.get("avgPrice")
        try:
            await _broker.cancel(pos["symbol"], pos["entry_coid"])
        except Exception:  # noqa: BLE001
            pass
    if adopt_fill and filled > 0:
        # W3 fix: ADOPT FIRST — the partial position keeps the OLD stop until
        # the correctly-sized replacement rests (place-then-cancel inside
        # _replace_stop); only rows that truly never filled get their stop
        # canceled outright
        _log_event("fill", f"{pos['symbol']} 部分成交 {filled}，转为持仓按原计划管理")
        await _on_entry_filled(pos, filled, avg or pos["entry"], _now_ms())
        await _replace_stop(pos, filled)
        return
    await _cancel_quietly(pos["symbol"], pos.get("stop_coid"))
    _upd(pos["id"], state="closed", exit_reason=reason, r_multiple=0.0,
         exit_price=None)
    _log_event("cancel", f"撤单 {pos['symbol']} {pos['interval']} "
               f"({REASON_LABEL.get(reason, reason)})")


# ------------------------------------------------------- fills / manage

def _runner_qty(pos: dict) -> float:
    return float(pos["filled"] or 0) - float(pos["be_qty"] or 0)


async def _on_entry_filled(pos: dict, fill_qty: float, avg_price: float,
                           fill_at: int) -> None:
    plan = pos["plan"]
    filters = await _safe_filters(pos["symbol"])
    step = filters["step"] if filters else 0.0
    be_qty = _floor_step(fill_qty / 2.0, step) if step > 0 else fill_qty / 2.0
    if be_qty <= 0:
        be_qty = 0.0  # too small to scale out: runner rides the stop alone
    seq = pos["seq"]
    be_coid = tgt_coid = None
    long = pos["direction"] == "long"
    exit_side = "SELL" if long else "BUY"
    if _broker is not None:
        if be_qty > 0 and plan.get("beTrigger") is not None:
            be_coid = _coid(pos["id"], "B", seq)
            try:
                await _place_probed({"symbol": pos["symbol"], "side": exit_side,
                                     "otype": "LIMIT", "qty": be_qty,
                                     "price": _round_tick(plan["beTrigger"],
                                                          filters["tick"] if filters else 0),
                                     "reduce_only": True, "coid": be_coid,
                                     "pos_id": pos["id"]})
            except Exception as exc:  # noqa: BLE001
                be_coid = None
                _set_error(f"{pos['symbol']} 保本止盈单失败: {exc}")
        if be_qty > 0 and plan.get("target1") is not None:
            tgt_qty = _floor_step(fill_qty - be_qty, step) if step > 0 else fill_qty - be_qty
            if tgt_qty > 0:
                tgt_coid = _coid(pos["id"], "T", seq)
                try:
                    await _place_probed({"symbol": pos["symbol"], "side": exit_side,
                                         "otype": "LIMIT", "qty": tgt_qty,
                                         "price": _round_tick(plan["target1"],
                                                              filters["tick"] if filters else 0),
                                         "reduce_only": True, "coid": tgt_coid,
                                         "pos_id": pos["id"]})
                except Exception as exc:  # noqa: BLE001
                    tgt_coid = None
                    _set_error(f"{pos['symbol']} 目标止盈单失败: {exc}")
    _upd(pos["id"], state="open", filled=fill_qty, avg_price=avg_price,
         opened_at=fill_at, be_coid=be_coid, tgt_coid=tgt_coid, notes=None
         if be_qty > 0 else "仓位过小未分批，止损单管理全部数量")
    _log_event("fill", f"成交 {pos['symbol']} {pos['interval']} "
               f"{'做多' if long else '做空'} @ {_fmt_p(avg_price)} x {fill_qty}")
    await _push("成交", f"【成交】{pos['symbol']} {pos['interval']} "
                f"{'做多' if long else '做空'} @ {_fmt_p(avg_price)}\n"
                f"止损 {_fmt_p(pos['stop'])}｜"
                + (f"{_fmt_p(plan['beTrigger'])} 减半保本" if be_qty > 0 else "止损单保护全仓"))


async def _safe_filters(symbol: str) -> dict | None:
    try:
        return await _broker.filters(symbol) if _broker else None
    except Exception:  # noqa: BLE001
        return None


async def _replace_stop(pos: dict, qty: float, price: float | None = None) -> None:
    """Place-then-cancel stop amendment: the position is never unprotected."""
    if _broker is None or qty <= 0:
        return
    filters = await _safe_filters(pos["symbol"])
    level = _round_tick(price if price is not None else pos["stop"],
                        filters["tick"] if filters else 0)
    new_coid = _coid(pos["id"], "S", pos["seq"] + 1)
    long = pos["direction"] == "long"
    await _place_probed({"symbol": pos["symbol"], "side": "SELL" if long else "BUY",
                         "otype": "STOP_MARKET", "qty": qty, "stop_price": level,
                         "reduce_only": True, "coid": new_coid, "pos_id": pos["id"]})
    if pos.get("stop_coid"):
        try:
            await _broker.cancel(pos["symbol"], pos["stop_coid"])
        except Exception:  # noqa: BLE001
            pass
    _upd(pos["id"], seq=pos["seq"] + 1, stop_coid=new_coid, stop=level)


async def _on_be_filled(pos: dict, fill_price: float) -> None:
    """Half out at +beR: stop to entry for the runner (frozen-plan semantics)."""
    be_qty = float(pos["filled"] or 0) / 2.0
    if pos.get("be_coid") and _broker is not None:
        try:
            info = await _broker.get(pos["symbol"], pos["be_coid"])
        except Exception:  # noqa: BLE001
            info = None
        if info:
            be_qty = float(info.get("executedQty") or 0) or be_qty
    runner = float(pos["filled"] or 0) - be_qty
    _upd(pos["id"], be_done=1, be_qty=be_qty, be_fill_at=_now_ms(),
         mfe=pos["entry"])
    if runner > 0:
        await _replace_stop(pos, runner, price=pos["entry"])
    _log_event("be", f"保本 {pos['symbol']} {pos['interval']} 半仓@{_fmt_p(fill_price)}，"
               f"止损移至入场 {_fmt_p(pos['entry'])}，剩余 {runner}")
    await _push("保本", f"【保本】{pos['symbol']} {pos['interval']} 半仓止盈 @ {_fmt_p(fill_price)}\n"
                f"止损已移至入场价 {_fmt_p(pos['entry'])}（剩余 {_fmt_num(runner)}）")


async def _close_position(pos: dict, exit_price: float | None, reason: str,
                          runner_exit: float | None = None) -> None:
    """Close bookkeeping + cancel leftovers + R accounting (scale-out convention)."""
    if _broker is not None:
        for role in ("entry_coid", "stop_coid", "be_coid", "tgt_coid"):
            coid = pos.get(role)
            if coid:
                try:
                    await _broker.cancel(pos["symbol"], coid)
                except Exception:  # noqa: BLE001
                    pass
    plan = pos["plan"]
    risk = abs(float(plan.get("entry") or pos["entry"]) - float(plan.get("stop") or pos["stop"]))
    entry_ref = float(pos["avg_price"] or pos["entry"])
    long = pos["direction"] == "long"
    if exit_price is not None and exit_price <= 0:
        exit_price = None  # unknown market-close price: close without R
    total_r = None
    if risk > 0 and exit_price is not None:
        def _r(p: float) -> float:
            return (p - entry_ref) / risk if long else (entry_ref - p) / risk
        filled = float(pos["filled"] or 0)
        be_qty = float(pos["be_qty"] or 0)
        if be_qty > 0 and filled > be_qty:
            be_r = _r(float(plan["beTrigger"]))
            runner_r = _r(runner_exit if runner_exit is not None else exit_price)
            total_r = (be_qty / filled) * be_r + ((filled - be_qty) / filled) * runner_r
        else:
            total_r = _r(exit_price)
    _upd(pos["id"], state="closed", closed_at=_now_ms(), exit_reason=reason,
         exit_price=runner_exit if runner_exit is not None else exit_price,
         r_multiple=round(total_r, 3) if total_r is not None else None)
    day = datetime.utcnow().strftime("%Y-%m-%d")
    if total_r is not None:
        _meta_set(f"dayR|{day}", str(float(_meta_get(f"dayR|{day}", "0") or 0) + total_r))
    label = REASON_LABEL.get(reason, reason)
    r_txt = f"{total_r:+.2f}R" if total_r is not None else "—"
    _log_event("close", f"平仓 {pos['symbol']} {pos['interval']} {label} "
               f"@ {_fmt_p(exit_price) if exit_price is not None else '—'} → {r_txt}")
    await _push("平仓", f"【平仓】{pos['symbol']} {pos['interval']} {label}\n"
                f"离场 {_fmt_p(exit_price) if exit_price is not None else '—'}｜结果 {r_txt}")


async def _update_trail(pos: dict) -> None:
    """Trail ratchet from CLOSED bars only, via journal_store.trail_stop_level
    (round 55: ONE trail math shared with replay_plan — live exits stay
    identical to journal/backtest levels by construction)."""
    plan = pos["plan"]
    if not pos["be_done"] or plan.get("trailR") is None:
        return
    risk = abs(float(plan.get("entry")) - float(plan.get("stop")))
    if risk <= 0:
        return
    itv = pos["interval"]
    step = STEP_MS[itv]
    last_closed = last_closed_open(_now_ms(), itv)
    be_bar = (int(pos["be_fill_at"] or pos["opened_at"] or _now_ms()) // step) * step
    need = (last_closed - be_bar) // step + 2
    if need <= 0:
        return
    try:
        rows = await kline_cache.get_klines(pos["symbol"], itv, min(need, 100),
                                            end_time=last_closed)
    except Exception:  # noqa: BLE001
        return
    long = pos["direction"] == "long"
    mfe = float(plan["entry"])
    for r in rows:
        if int(r[0]) < be_bar:
            continue
        mfe = max(mfe, float(r[2])) if long else min(mfe, float(r[3]))
    new_level = journal_store.trail_stop_level(float(plan["entry"]), mfe, risk,
                                              float(plan["trailR"]), long)
    if new_level is None:
        return  # trail not armed yet (MFE < entry + trailR x risk)
    filters = await _safe_filters(pos["symbol"])
    tick = filters["tick"] if filters else 0.0
    new_level = _round_tick(new_level, tick)
    improves = (new_level > pos["stop"] + tick * 0.5) if long else (new_level < pos["stop"] - tick * 0.5)
    if improves:
        runner = _runner_qty(pos)
        if runner > 0:
            await _replace_stop(pos, runner, price=new_level)
            _log_event("trail", f"跟踪 {pos['symbol']} {itv} 止损 "
                       f"{_fmt_p(pos['stop'])} → {_fmt_p(new_level)}")


async def _check_time_exit(pos: dict) -> None:
    plan = pos["plan"]
    texit = int(plan.get("texitBars") or 0)
    if texit <= 0 or not pos.get("opened_at"):
        return
    held = journal_store.bars_held_since(int(pos["opened_at"]), pos["interval"], _now_ms())
    if held < texit:
        return
    runner = _runner_qty(pos)
    if runner <= 0:
        return
    long = pos["direction"] == "long"
    coid = _coid(pos["id"], "X", pos["seq"] + 1)
    px = await _market_probed(pos["symbol"], "SELL" if long else "BUY",
                              runner, coid, pos_id=pos["id"])
    _upd(pos["id"], seq=pos["seq"] + 1)
    if not px or px <= 0:
        _set_error(f"{pos['symbol']} 时间退出市价单未确认成交价，下一轮重试")
        return
    await _close_position(pos, px, "time")


async def _sync_one(pos: dict) -> None:
    """One position against the exchange (or the sim): fill transitions,
    self-healing protective orders, trail, time exit."""
    if _broker is None:
        return
    if pos["state"] == "pending":
        if pos["awaiting_entry"] or not pos.get("entry_coid"):
            await _retry_entry(pos)
            pos = _get_pos_by_id(pos["id"]) or pos
        if pos.get("entry_coid"):
            info, ok = await _probe(pos["symbol"], pos["entry_coid"])
            if not ok:
                return  # probe failure ≠ order gone — keep tracking (BTC lesson)
            if info is None:
                # exchange CONFIRMED the order vanished without our cancel
                _warn(f"{pos['symbol']} 入场单查询不到，转为待重挂")
                _upd(pos["id"], awaiting_entry=1, entry_coid=None)
                return
            status = info.get("status")
            if status == "FILLED":
                await _on_entry_filled(pos, float(info["executedQty"]),
                                       info.get("avgPrice") or pos["entry"], _now_ms())
            elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                _upd(pos["id"], awaiting_entry=1, entry_coid=None)
        return

    # --- open positions: the never-naked invariants ---
    runner = _runner_qty(pos)
    stop_info = None
    if pos.get("stop_coid"):
        stop_info, s_ok = await _probe(pos["symbol"], pos["stop_coid"])
        if not s_ok:
            return  # probe failure ≠ stop missing — a false "missing" would
            # double-place stops or even close a healthy position as
            # manual_extern (position probe failing in the same network blip)
    if stop_info is not None and stop_info.get("status") == "FILLED":
        exit_px = stop_info.get("avgPrice") or pos["stop"]
        reason = "be_stop" if pos["be_done"] else "stop"
        await _close_position(pos, exit_px, reason)
        return
    if stop_info is None:
        if runner <= 0:
            await _close_position(pos, None, "manual_extern")
            return
        if _mode != "paper":
            # missing stop AND no net exchange position = closed elsewhere;
            # with a net position (e.g. stacked 1h+4h offsets) keep protecting
            ex_qty = await _broker.position_qty(pos["symbol"])
            if abs(ex_qty) < 1e-9:
                px = None
                try:
                    bars = await binance.get_klines(pos["symbol"], "1m", 1, cache_ttl=10)
                    px = float(bars[-1][4]) if bars else None
                except Exception:  # noqa: BLE001
                    pass
                await _close_position(pos, px, "manual_extern")
                return
        # EMERGENCY: re-place the protective stop at the stored level
        _warn(f"{pos['symbol']} 止损单缺失，立即补挂 @ {_fmt_p(pos['stop'])}")
        await _replace_stop(pos, runner)
        pos = _get_pos_by_id(pos["id"]) or pos

    if not pos["be_done"] and pos.get("be_coid"):
        be_info, b_ok = await _probe(pos["symbol"], pos["be_coid"])
        if b_ok:
            if be_info is not None and be_info.get("status") == "FILLED":
                await _on_be_filled(pos, be_info.get("avgPrice") or pos["plan"]["beTrigger"])
                pos = _get_pos_by_id(pos["id"]) or pos
            elif be_info is None:
                filters = await _safe_filters(pos["symbol"])
                be_qty = _floor_step(pos["filled"] / 2, filters["step"] if filters else 0)
                if be_qty > 0:
                    _warn(f"{pos['symbol']} 保本单缺失，补挂")
                    await _broker.place({"symbol": pos["symbol"],
                                         "side": "SELL" if pos["direction"] == "long" else "BUY",
                                         "otype": "LIMIT", "qty": be_qty,
                                         "price": _round_tick(pos["plan"]["beTrigger"],
                                                              filters["tick"] if filters else 0),
                                         "reduce_only": True,
                                         "coid": _coid(pos["id"], "B", pos["seq"] + 1),
                                         "pos_id": pos["id"]})
                    _upd(pos["id"], seq=pos["seq"] + 1,
                         be_coid=_coid(pos["id"], "B", pos["seq"] + 1))

    if pos.get("tgt_coid"):
        tgt_info, t_ok = await _probe(pos["symbol"], pos["tgt_coid"])
        if t_ok and tgt_info is not None and tgt_info.get("status") == "FILLED":
            await _close_position(pos, tgt_info.get("avgPrice") or pos["plan"]["target1"],
                                  "target")
            return

    await _update_trail(pos)
    await _check_time_exit(_get_pos_by_id(pos["id"]) or pos)


async def _reconcile() -> bool:
    """Startup/one-off exchange sync: verify every order state once, flag
    unknown executor-prefixed orders (other machine warning). Must be
    reachable with an EMPTY positions table too (C2 fix, round 55): a fresh
    testnet/live enable can never be blocked behind a reconcile that only
    runs when positions already exist."""
    global _reconciled, _one_way_ok
    if _broker is None:
        return False
    if _mode != "paper" and not _one_way_ok:
        try:
            dual = await binance_trade.position_mode()
        except Exception as exc:  # noqa: BLE001
            _set_error(f"持仓模式检查失败: {exc}")
            return False
        if dual:
            _set_error("账户为双向持仓(hedge)模式，执行器只支持单向模式，请在币安切换后重试")
            return False
        _one_way_ok = True
    try:
        rows = _active_positions()
        for pos in rows:
            await _sync_one(pos)
        ours = []
        try:
            ours = await _broker.open_all()
        except Exception:  # noqa: BLE001
            pass
        known = set()
        for pos in _active_positions():
            for role in ("entry_coid", "stop_coid", "be_coid", "tgt_coid"):
                if pos.get(role):
                    known.add(pos[role])
        prefix = f"cl{_cfg['instance']}"
        for o in ours or []:
            cid = o.get("clientOrderId") or ""
            if cid.startswith("cl") and not cid.startswith(prefix):
                _warn(f"发现他机执行器挂单 {cid}（双机同跑禁止！本机不管理该单）")
            elif cid.startswith(prefix) and cid not in known:
                _warn(f"发现未登记的本机挂单 {cid}（DB 丢失？），请在交易所核对")
        _reconciled = True
        return True
    except Exception as exc:  # noqa: BLE001
        _set_error(f"对账失败: {exc}")
        return False


async def _sync_one_guarded(pos: dict) -> None:
    try:
        await _sync_one(pos)
    except Exception as exc:  # noqa: BLE001
        _set_error(f"管理 {pos['key']} 出错: {exc}")


async def _sweep_orphans() -> None:
    """30s defense-in-depth (2026-09-05 incident): every cl{instance}* order
    on the exchange must map to a live pending/open row; orders whose pid row
    is closed (or missing) are leftovers of interrupted flows — cancel them
    loudly. In-flight placements always commit their row BEFORE the order
    rests, so a live row's pid is skipped regardless of seq races. Unmanaged
    net positions (no active row on that symbol) are WARNED about, never
    auto-closed — they may be manual trades."""
    if _broker is None or _mode == "paper":
        return
    try:
        ours = await _broker.open_all()
    except Exception:  # noqa: BLE001
        return
    prefix = f"cl{_cfg['instance']}"
    for o in ours or []:
        cid = str(o.get("clientOrderId") or "")
        if not cid.startswith(prefix):
            continue  # other machines / manual orders: never touched
        m = re.match(r"^(\d+)", cid[len(prefix):])
        if not m:
            continue
        row = _get_pos_by_id(int(m.group(1)))
        if row is not None and row["state"] in ("pending", "open"):
            continue
        try:
            await _broker.cancel(o.get("symbol"), cid)
            _log_event("warn", f"清理孤儿挂单 {o.get('symbol')} {cid}（无活跃行）")
        except Exception as exc:  # noqa: BLE001
            _log_event("warn", f"孤儿挂单 {cid} 撤单失败: {exc}")
    try:
        live = await _broker.positions_all()
    except Exception:  # noqa: BLE001
        live = []
    if live:
        active_syms = {p["symbol"] for p in _active_positions()}
        for p in live:
            sym = p.get("symbol")
            if sym not in active_syms:
                _warn(f"存在非执行器管理的仓位 {sym} "
                      f"qty={float(p.get('positionAmt') or 0):g}（请人工核对，不自动处理）")


async def _mgmt_tick() -> None:
    """~30s: reconciliation, orphan sweep, fills, self-healing stops, trail
    ratchet, time exits. Positions sync CONCURRENTLY (W8 fix): a slow
    exchange round-trip for one position must not delay the missing-stop
    self-heal of the next."""
    _ensure_broker()
    if _broker is None:
        return
    # C2 fix: reconcile BEFORE the empty-table early return — otherwise a
    # fresh testnet/live enable with zero rows can never place its first order
    if _mode != "paper" and not _reconciled:
        if not await _reconcile():
            return  # hold off until exchange state is verified once
    await _sweep_orphans()
    rows = _active_positions()
    if not rows:
        return
    if _mode == "paper":
        await _broker.sim_tick(sorted({p["symbol"] for p in rows}))
    await asyncio.gather(*[_sync_one_guarded(p) for p in rows])


async def _cancel_all_pendings(reason: str) -> None:
    for pos in _active_positions():
        if pos["state"] == "pending":
            await _cancel_pending(pos, reason)


# ---------------------------------------------------------------- loops

def _next_slot_ts(now_ts: float) -> float:
    dt = datetime.fromtimestamp(now_ts)
    slot = dt.replace(minute=5, second=25, microsecond=0)
    if slot <= dt:
        slot += timedelta(hours=1)
    return slot.timestamp()


async def _hourly_loop() -> None:
    await asyncio.sleep(15)
    while True:
        try:
            target = _next_slot_ts(time.time())
            await asyncio.sleep(max(1.0, target - time.time()))
            await _plan_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _set_error(f"计划轮 {type(exc).__name__}: {exc}")
            await asyncio.sleep(60)


async def _mgmt_loop() -> None:
    await asyncio.sleep(25)
    while True:
        try:
            await _mgmt_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _set_error(f"管理轮 {type(exc).__name__}: {exc}")
        await asyncio.sleep(30)


# ---------------------------------------------------------- public API

def start() -> None:
    """Load config + spawn loops (FastAPI startup; strong-ref anti-GC)."""
    global _task_hourly, _task_mgmt
    _load_cfg()
    _db()
    if _task_hourly is None or _task_hourly.done():
        _task_hourly = asyncio.create_task(_hourly_loop())
    if _task_mgmt is None or _task_mgmt.done():
        _task_mgmt = asyncio.create_task(_mgmt_loop())


async def update_config(patch: dict) -> None:
    """Apply a validated patch; mode changes fence stale state (C3 fix):
    pending rows from the OLD environment are never allowed to materialize
    as real orders on the NEW one — the documented paper→testnet→live
    rollout must not wake up hours-old frozen plans as live placements."""
    global _reconciled, _broker
    _load_cfg()
    prev_enabled = _cfg.get("enabled")
    mode_before = _mode_of_cfg()

    # preview the post-patch mode BEFORE mutating anything: switching away
    # from testnet/live with real open positions is refused outright
    dry_after = patch.get("dryRun", _cfg.get("dryRun", True))
    tn_after = patch.get("testnet", _cfg.get("testnet", True))
    mode_after = "paper" if dry_after else ("testnet" if tn_after else "live")
    if mode_before != mode_after and mode_before in ("testnet", "live"):
        opens = [p for p in _active_positions() if p["state"] == "open"]
        if opens:
            raise RuntimeError(
                f"存在 {len(opens)} 个真实持仓（{mode_before}），禁止直接切换模式："
                f"先在交易面板平仓或紧急全撤")

    creds_changed = any(patch.get(k) for k in ("apiKey", "apiSecret", "testnet", "dryRun"))
    for k in ("enabled", "testnet", "dryRun", "confirmLive", "apiKey",
              "apiSecret", "postOnlyEntry", "pushEvents"):
        if k in patch and patch[k] is not None:
            _cfg[k] = patch[k]
    for k in ("riskPct", "leverage", "maxConcurrent", "maxNotionalPctPer",
              "maxGrossNotionalPct", "dailyLossLimitR", "equityUsd"):
        if patch.get(k) is not None:
            _cfg[k] = float(patch[k])
    if patch.get("symbols") is not None:
        _cfg["symbols"] = patch["symbols"]
    if patch.get("intervals") is not None:
        _cfg["intervals"] = patch["intervals"]
    if mode_before == "live" and mode_after != "live":
        _cfg["confirmLive"] = False  # leaving live disarms (W1 fix)
    _save_cfg()

    if mode_before != mode_after:
        # close every stale row and cancel its orders through the OLD broker
        actives = _active_positions()
        for p in actives:
            await _cancel_quietly(p["symbol"], p.get("entry_coid"), p.get("stop_coid"))
            _upd(p["id"], state="closed", exit_reason="mode_switch",
                 r_multiple=0.0 if p["state"] == "pending" else None,
                 notes=None if p["state"] == "pending" else "模式切换关闭（旧环境仓位记录）")
        if actives:
            _log_event("warn", f"模式切换 {mode_before}→{mode_after}："
                       f"已清理 {len(actives)} 个旧环境仓位记录")
        _broker = None  # rebuild with fresh credentials on the next tick
        _reconciled = False
    else:
        new_scope = {f"{s}|{i}" for s in _cfg["symbols"] for i in _cfg["intervals"]}
        for pos in _active_positions():
            if pos["state"] == "pending" and pos["key"] not in new_scope:
                await _cancel_pending(pos, "disabled")
        if prev_enabled and not _cfg.get("enabled"):
            await _cancel_all_pendings("disabled")
        if creds_changed and mode_after != "paper":
            _broker = None  # rebuild with fresh credentials on the next tick
            _reconciled = False


async def test_connection() -> dict:
    _ensure_broker()
    if _mode == "paper":
        info = await binance.get_exchange_info()
        n = len(info.get("symbols", [])) if isinstance(info, dict) else 0
        return {"ok": True, "mode": "paper",
                "note": f"模拟模式运行中（行情过滤 {n} 个合约正常）"}
    try:
        await binance_trade.sync_time()
        balance = await binance_trade.ping_signed()
        usdt = None
        for item in balance or []:
            if item.get("asset") == "USDT":
                usdt = float(item.get("balance") or 0)
                break
        dual = await binance_trade.position_mode()
        return {"ok": True, "mode": _mode, "usdtWallet": usdt,
                "dualSidePosition": dual,
                "note": ("账户为双向持仓模式，需在币安切为单向" if dual
                         else "密钥/路由/账户模式全部正常")}
    except binance_trade.TradeError as exc:
        return {"ok": False, "mode": _mode, "error": f"币安返回 {exc.code}: {exc.msg}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "mode": _mode, "error": f"{type(exc).__name__}: {exc}"}


async def panic() -> dict:
    """Emergency: disable FIRST (atomically persisted), then cancel every
    executor order and market-close every position — each position isolated
    in try/except so one network error can never abort the rest of the kill
    (C4 fix). A position that fails is reported loudly for manual handling."""
    _ensure_broker()
    _cfg["enabled"] = False
    _save_cfg()
    persisted_off = False
    try:
        persisted_off = not json.loads(
            CFG_PATH.read_text(encoding="utf-8")).get("enabled")
    except Exception:  # noqa: BLE001
        persisted_off = False
        _save_cfg()  # one retry; still verified below
        try:
            persisted_off = not json.loads(
                CFG_PATH.read_text(encoding="utf-8")).get("enabled")
        except Exception:  # noqa: BLE001
            persisted_off = False

    closed = 0
    errors: list[str] = []
    if _broker is not None:
        for pos in _active_positions():
            try:
                if pos["state"] == "pending":
                    await _cancel_pending(pos, "panic", adopt_fill=True)
                else:
                    runner = _runner_qty(pos)
                    if runner > 0:
                        long = pos["direction"] == "long"
                        coid = _coid(pos["id"], "P", pos["seq"] + 1)
                        px = await _market_probed(pos["symbol"],
                                                  "SELL" if long else "BUY",
                                                  runner, coid, pos_id=pos["id"])
                        _upd(pos["id"], seq=pos["seq"] + 1)
                        await _close_position(pos, px, "panic")
                    else:
                        await _close_position(pos, None, "panic")
                closed += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{pos['key']}: {exc}")
                _log_event("panic", f"紧急全撤失败 {pos['key']}: {exc}"
                           f"（请立即到交易所手动撤单/平仓！）")
    if not persisted_off:
        errors.append("停用状态落盘未确认——进程重启后自动交易可能恢复开启，请立即手动检查")
    _log_event("panic", f"紧急全撤完成：处理 {closed} 个仓位"
               + (f"，失败 {len(errors)} 项" if errors else ""))
    await _push("紧急全撤", f"【紧急全撤】已撤销全部挂单并市价平仓（处理 {closed} 个仓位），"
                f"自动交易已关闭" + ("；有失败项请去交易所手动核对！" if errors else ""))
    return {"ok": not errors, "closed": closed, "errors": errors}


async def stop() -> None:
    """Cancel both engine loops and await their teardown (called from FastAPI
    shutdown BEFORE the HTTP clients close — an in-flight tick must never
    complete real order placements after shutdown began; the trade client
    also refuses to rebuild after close)."""
    global _task_hourly, _task_mgmt
    for t in (_task_hourly, _task_mgmt):
        if t is not None and not t.done():
            t.cancel()
    for t in (_task_hourly, _task_mgmt):
        if t is not None:
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
    _task_hourly = _task_mgmt = None


def _pos_public(p: dict) -> dict:
    plan = p["plan"]
    held = None
    if p.get("opened_at"):
        held = journal_store.bars_held_since(int(p["opened_at"]), p["interval"], _now_ms())
    return {
        "id": p["id"], "key": p["key"], "symbol": p["symbol"],
        "interval": p["interval"], "direction": p["direction"],
        "state": p["state"], "stateLabel": STATE_LABEL.get(p["state"], p["state"]),
        "entry": p["entry"], "stop": p["stop"], "filled": p["filled"],
        "beQty": p["be_qty"], "avgPrice": p["avg_price"],
        "beDone": p["be_done"], "awaitingEntry": p["awaiting_entry"],
        "beTrigger": plan.get("beTrigger"), "target1": plan.get("target1"),
        "trailR": plan.get("trailR"), "texitBars": plan.get("texitBars"),
        "fillBars": plan.get("fillBars"), "barsHeld": held,
        "qty": p["qty"], "mfe": p["mfe"],
        "createdAt": p["created_at"], "openedAt": p["opened_at"],
        "closedAt": p["closed_at"], "exitReason": p["exit_reason"],
        "exitPrice": p["exit_price"], "rMultiple": p["r_multiple"],
    }


def status() -> dict:
    _load_cfg()
    active = _active_positions()
    day = datetime.utcnow().strftime("%Y-%m-%d")
    today_r = float(_meta_get(f"dayR|{day}", "0") or 0)
    with _lock:
        cur = _db().execute(
            "SELECT COUNT(*), COALESCE(SUM(r_multiple), 0) FROM positions "
            "WHERE state='closed' AND r_multiple IS NOT NULL")
        closed_n, closed_r = cur.fetchone()
    masked_key = ""
    k = _cfg.get("apiKey") or ""
    if k:
        masked_key = k[:4] + "***" + k[-4:] if len(k) > 8 else "***"
    next_run = None
    if _task_hourly is not None and not _task_hourly.done() and _cfg.get("enabled"):
        next_run = int(_next_slot_ts(time.time()) * 1000)
    return {
        "enabled": bool(_cfg.get("enabled")),
        "mode": _mode_of_cfg(),
        "testnet": bool(_cfg.get("testnet")),
        "dryRun": bool(_cfg.get("dryRun")),
        "confirmLive": bool(_cfg.get("confirmLive")),
        "keysSet": bool(_cfg.get("apiKey") and _cfg.get("apiSecret")),
        "apiKeyMasked": masked_key,
        "instance": _cfg.get("instance"),
        "symbols": _cfg["symbols"],
        "intervals": _cfg["intervals"],
        "riskPct": _cfg["riskPct"],
        "leverage": _cfg["leverage"],
        "maxConcurrent": _cfg["maxConcurrent"],
        "maxNotionalPctPer": _cfg["maxNotionalPctPer"],
        "maxGrossNotionalPct": _cfg["maxGrossNotionalPct"],
        "dailyLossLimitR": _cfg["dailyLossLimitR"],
        "equityUsd": _cfg["equityUsd"],
        "postOnlyEntry": bool(_cfg.get("postOnlyEntry", True)),
        "pushEvents": bool(_cfg.get("pushEvents", True)),
        "paused": _daily_paused(),
        "todayRealizedR": round(today_r, 2),
        "reconciled": _reconciled,
        "positions": [_pos_public(p) for p in active],
        "closedCount": int(closed_n or 0),
        "closedSumR": round(float(closed_r or 0), 2),
        "lastPlanRun": _last_plan_run,
        "nextRun": next_run,
        "lastError": _last_error,
        "events": _recent_events(30),
        "routes": binance_trade.route_status(),
        "note": "自动执行与推送/回测同一收盘口径；已成交仓位按冻结计划管理（止损→减半保本→跟踪→时间退出）",
    }
