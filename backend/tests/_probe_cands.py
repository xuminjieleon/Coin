"""第五十二轮：前20候选可交易性与历史深度探测（成交额榜 ∪ 市值榜，未入列表者）。

榜单口径（2026-09-02 07:43 UTC 实测）：
- 成交量前20（币安 USDT 合约链 24h quoteVolume，剔稳定/黄金/杠杆）：BTC ETH SOL XRP ZEC
  UNI BNB ENA DOGE TRX NEAR SUI HEMI ARB LINK SNDKB FIL TRUMP AAVE ACE
- 市值前20（CMC r.jina.ai 代理抓取，剔稳定币）：BTC ETH BNB XRP SOL TRX HYPE ZEC DOGE XMR
  LEO LINK ADA XLM BCH CC UNI LTC GRAM AVAX（22/24/27 位含入并集口径）
- 已在推送列表（9）：BTC ETH SOL BNB XRP ZEC DOGE SUI LTC → 剔除
- 并集候选 20 个 + PYTH（上会话遗留候选，对照）

探测：fapi 合约 exchangeInfo + 现货镜像 exchangeInfo 交叉；上市时间=各周期首根K线；
深度=NOW_MS 钉窗下各周期可得根数（kline_cache 已缓存部分直读）。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/_probe_cands.py
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx

import profit_sweep2 as ps
from services import binance as bn

NOW_MS = 1788332400000  # 2026-09-02 07:00 UTC（与 backtest_newcands 钉窗一致）
STEP_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}
VOL20 = ["UNI", "ENA", "TRX", "NEAR", "HEMI", "ARB", "LINK", "SNDKB", "FIL", "TRUMP",
         "AAVE", "ACE"]
MCAP20 = ["TRX", "HYPE", "LINK", "ADA", "XLM", "BCH", "CC", "UNI", "GRAM", "AVAX",
          "XMR", "LEO"]
CANDS = sorted(set(VOL20) | set(MCAP20)) + ["PYTH"]
SPOT = "https://data-api.binance.vision"


def days(n_ms):
    return n_ms / 86400000


def fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%y-%m-%d")


async def probe_one(sym, spot_info, fapi_info, c):
    out = {"sym": sym, "perp": None, "spot": None, "first": {}, "bars": {}}
    usdt = f"{sym}USDT"
    fapi_sym = next((s for s in fapi_info if s.get("symbol") == usdt
                     and s.get("status") == "TRADING"), None)
    spot_sym = next((s for s in spot_info if s["symbol"] == usdt
                     and s["status"] == "TRADING"), None)
    out["perp"] = bool(fapi_sym)
    out["spot"] = bool(spot_sym) or usdt in (spot_info if isinstance(spot_info, set) else set())
    if not (fapi_sym or spot_sym):
        return out
    for itv in ("1h", "4h", "1d", "1w"):
        try:
            r = await c.get(f"{SPOT}/api/v3/klines" if not fapi_sym else "PLACEHOLDER",
                            params=None) if False else None
        except Exception:
            pass
        # 用 kline_cache 读（已缓存则零网络）；无缓存时直接拉首根+根数估计
        rows = ps.kline_cache._read_rows(usdt, itv, NOW_MS, 10 ** 9)
        if rows:
            out["bars"][itv] = len(rows)
            out["first"][itv] = int(rows[0][0])
        else:
            try:
                async with httpx.AsyncClient(timeout=20.0) as c2:
                    first = await c2.get(f"{SPOT}/api/v3/klines",
                                         params={"symbol": usdt, "interval": itv, "startTime": 0, "limit": 1})
                if first.status_code != 200 or not first.json():
                    out["bars"][itv] = 0
                    continue
                t0 = int(first.json()[0][0])
                out["first"][itv] = t0
                out["bars"][itv] = min(int((NOW_MS - t0) / STEP_MS[itv]) + 1, 10 ** 9)
            except Exception as exc:
                out["bars"][itv] = -1
    return out


async def main():
    async with httpx.AsyncClient(timeout=25.0) as c:
        r = await c.get(f"{SPOT}/api/v3/exchangeInfo")
        r.raise_for_status()
        spot_syms = [s for s in r.json()["symbols"] if s["status"] == "TRADING"]
        spot_set = {s["symbol"] for s in spot_syms}
    try:
        fapi_info = await bn.get_exchange_info()
        fapi_syms = fapi_info.get("symbols", [])
    except Exception as exc:
        print(f"[fapi unavailable] {exc}")
        fapi_syms = []

    print(f"{'sym':<10}{'perp':>6}{'spot':>6}{'1h根':>8}{'4h根':>8}{'1d根':>8}{'1w根':>8}  上市(1h首根)")
    for sym in CANDS:
        usdt = f"{sym}USDT"
        perp = any(s.get("symbol") == usdt and s.get("status") == "TRADING" for s in fapi_syms)
        spot = usdt in spot_set
        row = {"perp": perp, "spot": spot}
        first_1h = None
        for itv in ("1h", "4h", "1d", "1w"):
            rows = ps.kline_cache._read_rows(usdt, itv, NOW_MS, 10 ** 9)
            if rows:
                row[itv] = len(rows)
                if itv == "1h":
                    first_1h = int(rows[0][0])
            else:
                try:
                    async with httpx.AsyncClient(timeout=20.0) as c2:
                        fr = await c2.get(f"{SPOT}/api/v3/klines",
                                          params={"symbol": usdt, "interval": itv, "startTime": 0, "limit": 1})
                    if fr.status_code != 200 or not fr.json():
                        row[itv] = 0
                    else:
                        t0 = int(fr.json()[0][0])
                        row[itv] = int((NOW_MS - t0) / STEP_MS[itv]) + 1
                        if itv == "1h":
                            first_1h = t0
                except Exception:
                    row[itv] = -1
        print(f"{sym:<10}{('Y' if perp else '-'):>6}{('Y' if spot else '-'):>6}"
              f"{row.get('1h', 0):>8}{row.get('4h', 0):>8}{row.get('1d', 0):>8}{row.get('1w', 0):>8}"
              f"  {fmt(first_1h) if first_1h else '-'} (~{days(NOW_MS - first_1h):.1f}d)" if first_1h else "  -")


if __name__ == "__main__":
    asyncio.run(main())
