"""Eastmoney daily-K fetch/cache for A-share ETFs (2026-08-27, round 18).

Purpose: A股 ETF 策略可行性验证的数据层。东财 push2his 日K（前复权），
pickle 缓存于 backend/data/ashare/。网络段 asyncio 并发（§7.8）；
CPU 段在 backtest_ashare.py 的多进程 worker 里。

Honest fidelity notes:
  - 东财不提供 takerBuy 字段 → 以 volume/2 填充：CVD/CVD 背离组件在 A 股
    数据上呈中性（决策评分中 CVD 共振贡献为零，如实降级，不伪造）
  - 前复权 fqt=1（红利ETF等分红标的价格已调整）
  - 日线时间戳取该日期 00:00 UTC（仅用于排序/窗口切分，绝对偏移无影响）
  - 东财日线含当日已收盘数据；成交量单位=手（×100=股），金额单位=元

Usage:
  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/ashare_data.py [--refresh] [--probe]
"""
import argparse
import asyncio
import os
import pickle
import sys
import time
from datetime import datetime, timezone

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pandas as pd

# code -> (secid, name, t0)   t0=True: 场内 T+0（跨境/黄金ETF），可当日止损
ETFS = {
    "510050": ("1.510050", "上证50ETF", False),
    "510300": ("1.510300", "沪深300ETF", False),
    "510500": ("1.510500", "中证500ETF", False),
    "512100": ("1.512100", "中证100/1000ETF(以API名为准)", False),
    "588000": ("1.588000", "科创50ETF", False),
    "159915": ("0.159915", "创业板ETF", False),
    "512880": ("1.512880", "券商ETF", False),
    "512480": ("1.512480", "半导体ETF", False),
    "510880": ("1.510880", "红利ETF", False),
    "513100": ("1.513100", "纳指ETF", True),
    "518880": ("1.518880", "黄金ETF", True),
    "513330": ("1.513330", "恒生科技ETF", True),
}

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ashare")
URL = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
       "?secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57"
       "&klt=101&fqt=1&end=20500101&lmt=8000")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _parse(code: str, data: dict) -> tuple[str, list[tuple]]:
    name = data.get("name") or ""
    rows: list[tuple] = []
    for line in data.get("klines") or []:
        p = line.split(",")
        # fields2: date, open, close, high, low, volume(手), amount(元)
        ts = int(datetime.strptime(p[0], "%Y-%m-%d")
                 .replace(tzinfo=timezone.utc).timestamp() * 1000)
        o, c, h, l = float(p[1]), float(p[2]), float(p[3]), float(p[4])
        v = float(p[5]) * 100.0  # 手 -> 股
        a = float(p[6])
        rows.append((ts, o, h, l, c, v, a))
    return name, rows


async def _fetch_one(client: httpx.AsyncClient, code: str, sem: asyncio.Semaphore) -> tuple:
    secid = ETFS[code][0]
    async with sem:
        for attempt in range(4):
            try:
                r = await client.get(URL.format(secid=secid), headers=HEADERS)
                r.raise_for_status()
                data = (r.json() or {}).get("data") or {}
                name, rows = _parse(code, data)
                if not rows:
                    raise RuntimeError(f"empty klines for {code}")
                return code, name, rows, None
            except Exception as exc:
                if attempt == 3:
                    return code, ETFS[code][1], [], str(exc)
                await asyncio.sleep(2 * (attempt + 1))
    return code, ETFS[code][1], [], "unreachable"


async def fetch_all(refresh: bool) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    sem = asyncio.Semaphore(3)
    async with httpx.AsyncClient(timeout=20) as client:
        tasks = []
        for code in ETFS:
            cache = os.path.join(CACHE_DIR, f"{code}.pkl")
            if not refresh and os.path.exists(cache):
                continue
            tasks.append(_fetch_one(client, code, sem))
            await asyncio.sleep(0.25)
        results = await asyncio.gather(*tasks)
    for code, name, rows, err in results:
        if err:
            print(f"[fail] {code} {ETFS[code][1]}: {err}")
            continue
        tmp = os.path.join(CACHE_DIR, f"{code}.pkl.tmp")
        with open(tmp, "wb") as f:
            pickle.dump({"name": name, "fetched": datetime.now(timezone.utc).isoformat(),
                         "rows": rows}, f)
        os.replace(tmp, os.path.join(CACHE_DIR, f"{code}.pkl"))
        print(f"[ok] {code} {name}: {len(rows)} bars")


def load_df(code: str) -> pd.DataFrame:
    """Analysis-ready DataFrame: time,open,high,low,close,volume,takerBuy(=v/2)."""
    with open(os.path.join(CACHE_DIR, f"{code}.pkl"), "rb") as f:
        entry = pickle.load(f)
    df = pd.DataFrame(entry["rows"], columns=["time", "open", "high", "low", "close",
                                              "volume", "amount"]).sort_values("time")
    df = df.reset_index(drop=True)
    df["takerBuy"] = df["volume"] / 2.0
    return df


def etf_name(code: str) -> str:
    with open(os.path.join(CACHE_DIR, f"{code}.pkl"), "rb") as f:
        return pickle.load(f)["name"]


def probe() -> None:
    print(f"{'代码':<7} {'名称':<14} {'上市':<11} {'bar数':>6} {'60日均额(亿)':>10} {'T+0':>4}")
    for code in ETFS:
        try:
            df = load_df(code)
            t0 = "T+0" if ETFS[code][2] else "T+1"
            first = datetime.fromtimestamp(df['time'].iloc[0] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
            avg_amt = df['amount'].tail(60).mean() / 1e8
            print(f"{code:<7} {etf_name(code):<14} {first:<11} {len(df):>6} {avg_amt:>10.2f} {t0:>4}")
        except FileNotFoundError:
            print(f"{code:<7} 无缓存（先运行 --refresh）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args()
    if args.probe:
        probe()
    else:
        asyncio.run(fetch_all(args.refresh))
        probe()
