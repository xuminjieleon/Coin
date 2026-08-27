"""Macro linkage: BTC vs macro assets (Nasdaq/DXY/gold/VIX/10Y/MSTR/COIN).

Data source: Yahoo Finance chart API (probed reachable 2026-08-24 but
rate-limited — requires a browser User-Agent, request spacing and retry;
query1/query2 host rotation). Daily bars are cached forever in SQLite
(immutable history); only the stale tail is re-fetched.

Engine: daily-return Pearson correlations (30/60/90d) and 60d beta of BTC
vs each series, aligned by UTC date against local kline_cache BTC 1d bars.
2026-08-27: this network now gets 403 from both query hosts (blocked, not
rate-limited) — stale series fast-fail instead of retry-storming and keep
serving the last cached day. When a Windows system proxy is configured
(VPN), the proxied route is tried after the direct one and typically
un-blocks Yahoo; a hard block on every route arms a 15-min fast-fail window.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pandas as pd

from services import kline_cache, sysproxy

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "cache" / "macro.db"

# key -> {name, symbols(fallbacks)}
SERIES = {
    "ndx": {"name": "纳指100", "symbols": ["^NDX", "NQ=F"]},
    "dxy": {"name": "美元指数", "symbols": ["DX-Y.NYB"]},
    "gold": {"name": "黄金", "symbols": ["GC=F"]},
    "vix": {"name": "VIX 恐慌指数", "symbols": ["^VIX"]},
    "tnx": {"name": "美债10Y收益率", "symbols": ["^TNX"]},
    "mstr": {"name": "Strategy(MSTR)", "symbols": ["MSTR"]},
    "coin": {"name": "Coinbase(COIN)", "symbols": ["COIN"]},
}

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
       "Accept": "application/json,text/plain,*/*"}
_MIN_SPACING = 1.6  # seconds between yahoo requests


class _YahooBlocked(Exception):
    """403 from Yahoo: the network/exit IP is blocked — retrying is futile."""
_response_cache: dict | None = None
_response_cache_at = 0.0
_response_ttl = 1800.0

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

# yahoo rate limiting: global async lock + min spacing
_http_lock = asyncio.Lock()
_last_request = 0.0


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS macro_series (
                key TEXT NOT NULL,
                date TEXT NOT NULL,
                close REAL NOT NULL,
                PRIMARY KEY (key, date)
            ) WITHOUT ROWID"""
        )
        conn.commit()
        _conn = conn
    return _conn


def _read_series(key: str) -> pd.DataFrame:
    with _lock:
        cur = _db().execute(
            "SELECT date, close FROM macro_series WHERE key=? ORDER BY date", (key,))
        rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(columns=["date", "close"])
    return pd.DataFrame(rows, columns=["date", "close"])


def _upsert(key: str, points: list[tuple[str, float]]) -> None:
    if not points:
        return
    with _lock:
        _db().executemany(
            "INSERT OR REPLACE INTO macro_series (key, date, close) VALUES (?,?,?)",
            [(key, d, c) for d, c in points],
        )
        _db().commit()


# when every route (direct + proxied) is hard-blocked (403), skip network
# attempts for this window so the remaining series keys fail fast
_BLOCKED_WINDOW = 900.0
_blocked_until = 0.0


async def _yahoo_chart(symbol: str, range_: str = "1y") -> list[tuple[str, float]]:
    """Daily bars [(date, close)...]; raises on final failure.

    Route plan: direct hosts first, then the same hosts through the Windows
    system proxy when one is configured (VPN — this network's direct route
    has been 403-blocked since 2026-08-27 but works via proxy). A 403 on the
    last available route (or direct-403 + unusable proxy) raises
    _YahooBlocked and arms the _BLOCKED_WINDOW fast-fail for other keys.
    """
    global _last_request, _blocked_until
    if time.monotonic() < _blocked_until:
        raise _YahooBlocked("yahoo recently blocked on all routes")
    hosts = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
    proxy = sysproxy.proxy_url()
    routes: list[tuple[str, str | None]] = [(h, None) for h in hosts]
    if proxy:
        routes += [(h, "proxy") for h in hosts]
    direct_403 = False

    def _blocked(reason: str) -> _YahooBlocked:
        global _blocked_until
        _blocked_until = time.monotonic() + _BLOCKED_WINDOW
        return _YahooBlocked(reason)

    last_exc: Exception | None = None
    for attempt in range(2 if proxy else 3):
        for host, via in routes:
            url = (f"https://{host}/v8/finance/chart/{symbol}"
                   f"?interval=1d&range={range_}")
            async with _http_lock:
                wait = _MIN_SPACING - (time.monotonic() - _last_request)
                if wait > 0:
                    await asyncio.sleep(wait)
                _last_request = time.monotonic()
                resp = None
                try:
                    if via is None:
                        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0)) as client:
                            resp = await client.get(url, headers=_UA)
                    else:
                        resp = await sysproxy.request(url, headers=_UA, timeout=12.0)
                except sysproxy.ProxyUnavailable as exc:
                    if direct_403:
                        # direct hard-blocked AND proxy unusable: all dead
                        raise _blocked(f"yahoo direct 403, proxy unusable ({exc})")
                    last_exc = exc
                    continue
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    continue
                if resp.status_code == 403:
                    if via is None:
                        direct_403 = True
                        if not proxy:
                            raise _blocked("yahoo 403")
                        continue  # proxied routes may still work
                    if (host, via) == routes[-1]:
                        raise _blocked("yahoo 403 (all routes)")
                    continue
                if resp.status_code == 429:
                    last_exc = RuntimeError("yahoo 429")
                    continue
                try:
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    continue
            result = (data.get("chart") or {}).get("result") or []
            if not result:
                last_exc = RuntimeError(f"empty chart for {symbol}")
                continue
            ts = result[0].get("timestamp") or []
            quote = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
            closes = quote.get("close") or []
            out = []
            for t, c in zip(ts, closes):
                if c is None:
                    continue
                d = datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%Y-%m-%d")
                out.append((d, float(c)))
            if out:
                return out
            last_exc = RuntimeError(f"no closes for {symbol}")
        await asyncio.sleep(2.0 * (attempt + 1))
    raise last_exc or RuntimeError("yahoo failed")


async def _ensure_series(key: str) -> pd.DataFrame:
    """Disk-cached daily series; refresh the tail if stale (>2 days old)."""
    df = _read_series(key)
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    stale_after = None
    if not df.empty:
        stale = df["date"].iloc[-1] < _shift_day(today, -2)
        if not stale:
            return df
    cfg = SERIES[key]
    last_err = None
    for sym in cfg["symbols"]:
        try:
            points = await _yahoo_chart(sym)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
        _upsert(key, points)
        return _read_series(key)
    if last_err is not None and df.empty:
        # degrade: no data for this series
        return df
    return df


def _shift_day(iso: str, days: int) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (d + timedelta(days=days)).strftime("%Y-%m-%d")


def _corr(btc: pd.Series, other: pd.Series, window: int) -> float | None:
    if len(btc) < window or len(other) < window:
        return None
    b = btc.iloc[-window:].reset_index(drop=True)
    o = other.iloc[-window:].reset_index(drop=True)
    if b.std() == 0 or o.std() == 0:
        return None
    return round(float(b.corr(o)), 2)


def _beta(btc: pd.Series, other: pd.Series, window: int = 60) -> float | None:
    if len(btc) < window or len(other) < window:
        return None
    b = btc.iloc[-window:].reset_index(drop=True)
    o = other.iloc[-window:].reset_index(drop=True)
    var = float(o.var())
    if var == 0:
        return None
    return round(float(b.cov(o) / var), 2)


def _chg(df: pd.DataFrame, days: int) -> float | None:
    if len(df) < days + 1:
        days = len(df) - 1
    if days < 1 or df["close"].iloc[-days - 1] == 0:
        return None
    return (df["close"].iloc[-1] - df["close"].iloc[-days - 1]) / df["close"].iloc[-days - 1] * 100.0


async def macro_snapshot() -> dict:
    global _response_cache, _response_cache_at
    if _response_cache is not None and time.time() - _response_cache_at < _response_ttl:
        return _response_cache

    # BTC daily returns from the local kline cache
    try:
        rows = await kline_cache.get_klines("BTCUSDT", "1d", 400)
        btc_df = kline_cache.rows_to_df(rows)
        btc_df["date"] = pd.to_datetime(btc_df["time"], unit="ms", utc=True).dt.strftime("%Y-%m-%d")
        btc_daily = btc_df.set_index("date")["close"]
    except Exception:
        btc_daily = pd.Series(dtype=float)

    btc_ret = btc_daily.pct_change().dropna() if not btc_daily.empty else btc_daily

    out_series = []
    correlations = []
    for key, cfg in SERIES.items():
        df = await _ensure_series(key)
        if df.empty:
            out_series.append({"key": key, "name": cfg["name"], "last": None,
                               "chg1d": None, "chg7d": None, "chg30d": None, "spark": []})
            correlations.append({"key": key, "name": cfg["name"], "corr30": None,
                                 "corr60": None, "corr90": None, "beta60": None})
            continue
        s = df.set_index("date")["close"]
        row = {
            "key": key, "name": cfg["name"],
            "last": round(float(s.iloc[-1]), 4),
            "chg1d": _chg(df, 1), "chg7d": _chg(df, 7), "chg30d": _chg(df, 30),
            "spark": [round(float(v), 4) for v in s.iloc[-45:]],
        }
        out_series.append(row)
        if btc_ret.empty:
            correlations.append({"key": key, "name": cfg["name"], "corr30": None,
                                 "corr60": None, "corr90": None, "beta60": None})
            continue
        ret = s.pct_change().dropna()
        joined = pd.DataFrame({"btc": btc_ret, "s": ret}).dropna()
        if len(joined) < 15:
            corr = {"key": key, "name": cfg["name"], "corr30": None,
                    "corr60": None, "corr90": None, "beta60": None}
        else:
            corr = {
                "key": key, "name": cfg["name"],
                "corr30": _corr(joined["btc"], joined["s"], 30),
                "corr60": _corr(joined["btc"], joined["s"], 60),
                "corr90": _corr(joined["btc"], joined["s"], 90),
                "beta60": _beta(joined["btc"], joined["s"], 60),
            }
        correlations.append(corr)

    _response_cache = {
        "series": out_series,
        "correlations": correlations,
        "btc": {"last": round(float(btc_daily.iloc[-1]), 2)} if not btc_daily.empty else None,
        "updatedAt": int(time.time() * 1000),
        "source": "Yahoo Finance（日线，本地缓存）",
        "note": "相关性为 BTC 与各资产日收益率的 Pearson 相关（30/60/90 日窗口）；"
                "beta 为 60 日 BTC 对该资产的回归系数。宏观资产与加密市场共享流动性，"
                "高相关时段加密独立行情概率下降。",
    }
    _response_cache_at = time.time()
    return _response_cache
