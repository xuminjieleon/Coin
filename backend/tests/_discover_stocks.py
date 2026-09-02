"""链上美股代币发现（第五十二轮预备）——枚举币安现货镜像+合约的股票类代币。

用户：币安上现在有链上美股，先摸清有哪些、何时上市、流动性如何。
运行时探测为准（AGENTS §6）：现货镜像 data-api.binance.vision 直连 + 合约 fapi 经服务链。
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx

import services.binance as bn

SPOT = "https://data-api.binance.vision"
STOCKS = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "TSLA", "NFLX",
    "AVGO", "AMD", "INTC", "MU", "QCOM", "ORCL", "CRM", "PLTR", "SNOW",
    "COIN", "HOOD", "MSTR", "CRCL", "CLSK", "MARA", "RIOT", "CORZ", "IREN",
    "WULF", "CIFR", "BTCS", "GLXY", "BKKT", "SPY", "QQQ", "DIA", "IWM",
    "GLD", "TLT", "EEM", "SOXX", "NDX", "SBET", "GME", "LCID", "RIVN",
]
CRYPTO_FALLBACK = ["TRXUSDT", "LINKUSDT", "ADAUSDT"]


def fmt_ts(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%y-%m-%d")


async def main():
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(f"{SPOT}/api/v3/exchangeInfo")
        r.raise_for_status()
        spot = r.json()
        spot_syms = [s for s in spot["symbols"]
                    if s["status"] == "TRADING" and s["quoteAsset"] in ("USDT", "USDC")]

        def match(where):
            out = []
            for s in where:
                base = s["baseAsset"].upper()
                for name in STOCKS:
                    if name in base:
                        out.append((s["symbol"], base, s["quoteAsset"], name))
                        break
            return out

        spot_m = match(spot_syms)
        print(f"== 现货镜像（data-api.binance.vision）匹配 {len(spot_m)} 个 ==")
        for sym, base, quote, hit in spot_m:
            print(f"  {sym:<16} base={base:<10} quote={quote} hit={hit}")
        print(f"  （现货 USDT/USDC 交易对总数 {len(spot_syms)}）")

        # fapi 合约
        try:
            fapi = await bn.get_exchange_info()
            fapi_syms = [s for s in fapi.get("symbols", [])
                         if s.get("status") in ("TRADING",) and s.get("quoteAsset") in ("USDT", "USDC")]
            fapi_m = match(fapi_syms)
            print(f"\n== 合约 fapi 匹配 {len(fapi_m)} 个 ==")
            for sym, base, quote, hit in fapi_m:
                print(f"  {sym:<16} base={base:<10} quote={quote} hit={hit}")
        except Exception as exc:
            print(f"\n== 合约 fapi 不可达：{exc} ==")

        # 逐个匹配标的：上市时间 + 24h 成交额 + 1h/4h/1d 根数估计
        targets = []
        seen = set()
        for sym, base, quote, hit in spot_m:
            if sym not in seen:
                seen.add(sym)
                targets.append((sym, "spot"))
        try:
            for sym, base, quote, hit in fapi_m:
                if sym not in seen:
                    seen.add(sym)
                    targets.append((sym, "perp"))
        except NameError:
            pass
        if not targets:
            print("\n无匹配标的")
            return

        print(f"\n== 逐标的：上市时间 / span / 24h quoteVolume / 各周期已有根数 ==")
        for sym, kind in targets:
            try:
                q = sym
                async with httpx.AsyncClient(timeout=20.0) as c2:
                    tk = await c2.get(f"{SPOT}/api/v3/ticker/24hr", params={"symbol": q})
                    vol = float(tk.json().get("quoteVolume", 0) or 0) if tk.status_code == 200 else 0.0
                    first = await c2.get(f"{SPOT}/api/v3/klines",
                                         params={"symbol": q, "interval": "1h", "startTime": 0, "limit": 1})
                    last = await c2.get(f"{SPOT}/api/v3/klines",
                                        params={"symbol": q, "interval": "1h", "limit": 1})
                if first.status_code != 200 or not first.json():
                    print(f"  {sym:<16} [{kind}] klines 不可得 HTTP {first.status_code}")
                    continue
                t0 = int(first.json()[0][0])
                t1 = int(last.json()[0][0])
                days = (t1 - t0) / 86400000
                print(f"  {sym:<16} [{kind}] 上市 {fmt_ts(t0)} .. 最新 {fmt_ts(t1)} "
                      f"span={days:.0f}天  24h额={vol/1e6:.1f}M quote")
            except Exception as exc:
                print(f"  {sym:<16} [{kind}] 探测失败：{exc}")

        print("\n== 币圈候选（对照）当前 24h 额 ==")
        for sym in CRYPTO_FALLBACK:
            try:
                async with httpx.AsyncClient(timeout=20.0) as c3:
                    tk = await c3.get(f"{SPOT}/api/v3/ticker/24hr", params={"symbol": sym})
                    vol = float(tk.json().get("quoteVolume", 0) or 0) if tk.status_code == 200 else 0.0
                print(f"  {sym:<16} 24h额={vol/1e6:.1f}M")
            except Exception as exc:
                print(f"  {sym:<16} 探测失败：{exc}")


asyncio.run(main())