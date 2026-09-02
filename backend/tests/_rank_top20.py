"""第五十二轮预备：成交量与市值前20盘点（币安 24h 额 + CoinGecko 市值）。

输出：
- 币安 USDT 交易对 24h quoteVolume 排名（官方合约→现货镜像优先级链，运行时探测）；
- CoinGecko 市值前 40（经 httpx，网络不可达时如实失败，改用人工提供的市值榜单）。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/_rank_top20.py
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx

from services import binance

STABLE_BASES = {
    "USDC", "TUSD", "FDUSD", "USDP", "DAI", "AEUR", "EURI", "EUR", "XUSD",
    "USD1", "PAXG", "WBTC", "WBETH", "USDE", "USDS", "SUSD", "PYUSD", "BUSD",
}


def is_stable(base: str) -> bool:
    if base in STABLE_BASES:
        return True
    if base.endswith(("UP", "DOWN", "BULL", "BEAR")):
        return True
    if base[-2:] in ("1L", "2L", "3L", "1S", "2S", "3S"):
        return True
    return False


async def binance_vol_top(n=60):
    tickers = await binance.get_ticker24h()
    rows = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or len(sym) <= 4:
            continue
        base = sym[:-4]
        if is_stable(base):
            continue
        try:
            vol = float(t.get("quoteVolume") or 0)
            last = float(t.get("lastPrice") or 0)
        except (TypeError, ValueError):
            continue
        if vol <= 0 or last <= 0:
            continue
        rows.append((sym, vol, last))
    rows.sort(key=lambda x: -x[1])
    return rows[:n]


async def coingecko_mcap(n=40):
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {"vs_currency": "usd", "order": "market_cap_desc", "per_page": n, "page": 1}
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
        return r.json()


async def main():
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"== 币安 24h 成交额排名（优先级链运行时探测）{ts} ==")
    try:
        rows = await binance_vol_top()
        for i, (sym, vol, last) in enumerate(rows, 1):
            print(f"{i:>3} {sym:<16} vol={vol/1e6:>10.1f}M last={last}")
        with open(os.path.join(os.path.dirname(__file__), "_top20_vol.json"), "w") as f:
            json.dump([{"symbol": s, "quoteVolume": v} for s, v, _ in rows], f, indent=1)
    except Exception as exc:
        print(f"binance ticker failed: {exc}")

    print(f"\n== CoinGecko 市值前 {40}（直连，不可达则如实失败）==")
    try:
        cg = await coingecko_mcap()
        for i, c in enumerate(cg, 1):
            print(f"{i:>3} {c['symbol'].upper():<8} mcap=${c['market_cap']/1e9:>9.2f}B vol24h=${(c.get('total_volume') or 0)/1e6:>9.1f}M")
        with open(os.path.join(os.path.dirname(__file__), "_top40_mcap.json"), "w") as f:
            json.dump(cg, f, indent=1)
    except Exception as exc:
        print(f"coingecko failed: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
