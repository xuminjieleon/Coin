"""US equity data layer: Yahoo (direct->VPN sys-proxy dual route) daily + 60m->4h RTH aggregate.

2026-08-29, round 32 (US backtest feasibility probe). Data sources probed live:
  - eastmoney US secids: RemoteProtocolError (burst-ban window active) — not used
  - tencent usQQQ daily: reachable (fallback, not wired)
  - Yahoo direct: 403 (consistent with macro); via VPN system proxy: 200 all
  - Yahoo 1d range=max: DOWNSAMPLED to monthly (~330 bars) — MUST use period1=0
    full-depth (~6911 bars for QQQ since 1999)
  - Yahoo 60m range=730d: RTH-only 7 bars/day (09:30..15:30 ET opens), 722 days
    -> the ONLY 4h raw material; hard window limit declared honestly

Adjustment: Yahoo quote OHLC is split-adjusted; adjclose is split+dividend
adjusted. All OHLC scaled by ratio=adjclose/close per day (fully adjusted
series — otherwise ETF dividend jumps would fake price gaps across decades).
60m bars have no adjclose -> scaled by the same-day 1d ratio.

4h aggregation (RTH two-segment): per trading day, first 4 RTH 60m bars
(09:30-13:30 ET, 4h) -> bar1; remaining bars (13:30-16:00, 2.5h) -> bar2.
No pre/post-market data exists in the source. Bar timestamp = segment open.

Rate limits: serialized, >=1.6s spacing, query1/query2 host rotation,
3 attempts with backoff. No takerBuy -> volume/2 placeholder (CVD neutral,
same honest degradation as the A-share layer).

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/us_data.py [--refresh]
"""
import argparse
import asyncio
import os
import pickle
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from math import isfinite

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx

# symbol -> descriptive name
POOL = {
    "QQQ": "纳指100ETF",
    "SPY": "标普500ETF",
    "DIA": "道指ETF",
    "IWM": "罗素2000ETF",
    "NVDA": "英伟达",
    "AAPL": "苹果",
    "MSFT": "微软",
    "AMZN": "亚马逊",
    "GOOGL": "谷歌",
    "META": "Meta",
    "TSLA": "特斯拉",
    "SOXX": "半导体ETF",
    "GLD": "黄金ETF",
    "TLT": "20年国债ETF",
    "EEM": "新兴市场ETF",
}

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "us")
UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")}
HOSTS = ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")
_host_i = 0
_last_req = 0.0


def _proxy() -> str | None:
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        enable, _ = winreg.QueryValueEx(k, "ProxyEnable")
        server, _ = winreg.QueryValueEx(k, "ProxyServer")
        if enable and server:
            s = server.split(";")[0]
            return s if "://" in s else f"http://{s}"
    except Exception:
        pass
    return None


async def _fetch(path: str) -> dict:
    """Rate-limited dual-route GET (direct first, then VPN sys-proxy)."""
    global _host_i, _last_req
    px = _proxy()
    last_err = None
    for attempt in range(3):
        for use_proxy in (False, True):
            if use_proxy and not px:
                continue
            host = HOSTS[_host_i % 2]
            _host_i += 1
            gap = 1.6 - (time.monotonic() - _last_req)
            if gap > 0:
                await asyncio.sleep(gap)
            try:
                if use_proxy:
                    async with httpx.AsyncClient(timeout=60, proxy=px) as c:
                        r = await c.get(f"{host}{path}", headers=UA)
                else:
                    async with httpx.AsyncClient(timeout=25) as c:
                        r = await c.get(f"{host}{path}", headers=UA)
                _last_req = time.monotonic()
                if r.status_code == 403:
                    last_err = f"403 on {'proxy' if use_proxy else 'direct'}"
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(3 * (attempt + 1))
    raise RuntimeError(last_err or "unreachable")


def _clean_daily(j: dict) -> list[tuple]:
    """Fully-adjusted 1d rows (ts, open, high, low, close, volume)."""
    res = j["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    adj = res["indicators"]["adjclose"][0]["adjclose"]
    rows = []
    for k in range(len(ts)):
        o, h, l, c, v = (q["open"][k], q["high"][k], q["low"][k],
                         q["close"][k], q["volume"][k])
        a = adj[k]
        if any(x is None for x in (o, h, l, c, v, a)):
            continue
        if not all(isfinite(float(x)) for x in (o, h, l, c, v, a)):
            continue
        ratio = float(a) / float(c)
        rows.append((int(ts[k]) * 1000, float(o) * ratio, float(h) * ratio,
                     float(l) * ratio, float(a), float(v)))
    rows.sort(key=lambda x: x[0])
    return rows


def _clean_60m(j: dict) -> list[tuple]:
    """Raw 60m rows (ts, open, high, low, close, volume), null bars dropped."""
    res = j["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    rows = []
    for k in range(len(ts)):
        o, h, l, c, v = (q["open"][k], q["high"][k], q["low"][k],
                         q["close"][k], q["volume"][k])
        if any(x is None for x in (o, h, l, c, v)):
            continue
        if not all(isfinite(float(x)) for x in (o, h, l, c, v)):
            continue
        rows.append((int(ts[k]) * 1000, float(o), float(h), float(l), float(c), float(v)))
    rows.sort(key=lambda x: x[0])
    return rows


def _utc_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def aggregate_4h(rows60: list[tuple], ratio_by_day: dict[str, float]) -> list[tuple]:
    """RTH two-segment: first 4 bars of each UTC day -> segment A (4h), rest -> B.
    All prices scaled by same-day 1d adjustment ratio. Volume summed."""
    by_day: dict[str, list[tuple]] = defaultdict(list)
    for r in rows60:
        by_day[_utc_date(r[0])].append(r)
    out = []
    for day in sorted(by_day):
        bars = by_day[day]
        ratio = ratio_by_day.get(day, 1.0)
        for seg_i, seg in enumerate((bars[:4], bars[4:])):
            if not seg:
                continue
            ts = seg[0][0]
            o = seg[0][1]
            c = seg[-1][4]
            h = max(b[2] for b in seg)
            l = min(b[3] for b in seg)
            v = sum(b[5] for b in seg)
            out.append((ts, o * ratio, h * ratio, l * ratio, c * ratio, v))
    out.sort(key=lambda x: x[0])
    return out


async def fetch_symbol(sym: str) -> dict:
    """Returns {'1d': rows, '4h': rows} fully-adjusted."""
    j1 = await _fetch(f"/v8/finance/chart/{sym}?interval=1d&period1=0&period2=9999999999")
    rows_1d = _clean_daily(j1)
    if not rows_1d:
        raise RuntimeError("empty 1d")
    # ratio needs the RAW (unadjusted) close of the day -> from raw json
    res = j1["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    adj = res["indicators"]["adjclose"][0]["adjclose"]
    ratio_by_day = {}
    for k in range(len(ts)):
        if q["close"][k] is None or adj[k] is None:
            continue
        ratio_by_day[_utc_date(int(ts[k]) * 1000)] = float(adj[k]) / float(q["close"][k])
    j2 = await _fetch(f"/v8/finance/chart/{sym}?interval=60m&range=730d")
    rows_60m = _clean_60m(j2)
    rows_4h = aggregate_4h(rows_60m, ratio_by_day)
    return {"1d": rows_1d, "4h": rows_4h}


def load_df(sym: str, tf: str):
    """Analysis-ready DataFrame: time,open,high,low,close,volume,takerBuy(=v/2)."""
    import pandas as pd
    with open(os.path.join(DATA_DIR, f"{sym}_{tf}.pkl"), "rb") as f:
        entry = pickle.load(f)
    df = pd.DataFrame(entry["rows"], columns=["time", "open", "high", "low",
                                              "close", "volume"]).sort_values("time")
    df = df.reset_index(drop=True)
    df["takerBuy"] = df["volume"] / 2.0
    return df


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    os.makedirs(DATA_DIR, exist_ok=True)
    px = _proxy()
    print(f"[sysproxy] {px or 'OFF — Yahoo likely 403 on direct route'}")
    for sym in POOL:
        f1 = os.path.join(DATA_DIR, f"{sym}_1d.pkl")
        f4 = os.path.join(DATA_DIR, f"{sym}_4h.pkl")
        if not args.refresh and os.path.exists(f1) and os.path.exists(f4):
            print(f"[skip] {sym} cached")
            continue
        try:
            data = await fetch_symbol(sym)
        except Exception as exc:
            print(f"[fail] {sym}: {exc}")
            continue
        for tf in ("1d", "4h"):
            tmp = os.path.join(DATA_DIR, f"{sym}_{tf}.pkl.tmp")
            with open(tmp, "wb") as fh:
                pickle.dump({"name": POOL[sym], "source": "yahoo",
                             "fetched": datetime.now(timezone.utc).isoformat(),
                             "rows": data[tf]}, fh)
            os.replace(tmp, os.path.join(DATA_DIR, f"{sym}_{tf}.pkl"))
        r1, r4 = data["1d"], data["4h"]
        d0 = datetime.fromtimestamp(r1[0][0] / 1000, tz=timezone.utc).date()
        d1 = datetime.fromtimestamp(r1[-1][0] / 1000, tz=timezone.utc).date()
        h0 = datetime.fromtimestamp(r4[0][0] / 1000, tz=timezone.utc).date()
        h1 = datetime.fromtimestamp(r4[-1][0] / 1000, tz=timezone.utc).date()
        seg2 = sum(1 for i in range(1, len(r4)) if (r4[i][0] - r4[i - 1][0]) < 3 * 3600 * 1000)
        print(f"[ok] {sym} {POOL[sym]:<10} 1d={len(r1)} ({d0}..{d1})  "
              f"4h={len(r4)} ({h0}..{h1}) intra-day-B={seg2}")


if __name__ == "__main__":
    asyncio.run(main())