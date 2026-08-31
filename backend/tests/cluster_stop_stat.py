"""凌晨下砸集群止损频率统计（2026-08-31，第四十四轮）

用户问题（08-31 凌晨 BTC/BNB/DOGE 三张 4h 多单被同一波下砸齐止损后）：
这种"多币同天全止损"在策略历史上出现频率如何？是否属正常事件？

口径：
- 生产 4h 计划完整重放（R13 几何、th10、fill 12×1.5、容量约束串行、
  保守同根顺序）——与 fee_compare/pool_stop_stat 同一 harness；
- 5 币池（BTC/ETH/BNB/SOL/DOGE）× 5 年 4h，毛口径未计费；
- 按离场 bar 的北京时间日聚合（用户视角的"同一天"）；DOGE 记录无本地
  缓存时重算并落盘 _5y_cache_DOGEUSDT_4h.pkl；
- "全止损"= 单笔 rr ≤ -0.99（同根保守顺序下恒为 -1.0R）。
- 本轮纯统计，零生产改动。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/cluster_stop_stat.py
"""
import asyncio
import os
import pickle
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

for s in (sys.stdout, sys.stderr):
    if s and hasattr(s, "reconfigure"):
        s.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import profit_sweep2 as ps
from backtest_5y import CONF5, W5, sim_outcome_fast
from backtest_ltc import trade_stats
from fee_compare import load_cached_records
from profit2_r5 import with_loose_plans

TF = "4h"
SYMS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT"]
TRIO = {"BTCUSDT", "BNBUSDT", "DOGEUSDT"}
CST_OFF = 8 * 3600 * 1000


def bj_day(ms):
    return int((ms + CST_OFF) // 86_400_000)


def bj_date(day):
    t = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=day * 86_400_000 - CST_OFF)
    return t.strftime("%Y-%m-%d")


def sim_trades(sym, recs, cfg, df):
    depth, stopw, be_frac, tgt, texit, trail = tuple(cfg["geo"])
    fill_bars = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    times = df["time"].to_numpy()
    n = len(df)
    tidx = {int(t): i for i, t in enumerate(times)}
    trades = []
    busy = -1
    for r in recs:
        if r.get("plan") is None:
            continue
        i = tidx.get(r["time"])
        if i is None or i <= busy:
            continue
        built = ps.build_plan(r, depth, stopw)
        if built is None:
            continue
        direction, entry, stop = built
        out = sim_outcome_fast(highs, lows, closes, n, i, direction, entry, stop,
                               be_frac, tgt, texit, fill_bars, trail)
        if out is None:
            continue
        rr, fill, exit_bar = out
        busy = exit_bar
        trades.append({
            "sym": sym, "dir": direction, "rr": float(rr),
            "entry": float(entry), "risk": float(abs(entry - stop)),
            "fill": int(times[fill]), "exit": int(times[exit_bar]),
        })
    return trades


async def fetch_all():
    dfs = {}
    for sym in SYMS:
        d = {}
        for itv in ("4h", "1d"):
            rows = await ps.kline_cache.get_klines(sym, itv, W5[itv])
            d[itv] = ps.kline_cache.rows_to_df(rows)
            print(f"[data] {sym} {itv}: {len(d[itv])} bars", flush=True)
        dfs[sym] = d
    return dfs


async def main():
    dfs = await fetch_all()
    cfg = CONF5[TF]
    all_trades = []
    print(f"\ngeo={tuple(cfg['geo'])} th={cfg['th']} fill={cfg['fill_bars']}x{cfg['fill_mult']}")
    for sym in SYMS:
        records = load_cached_records(sym, TF, dfs[sym])
        recs = records if cfg["th"] == 25 else with_loose_plans(records, cfg["th"])
        trs = sim_trades(sym, recs, cfg, dfs[sym][TF])
        all_trades.extend(trs)
        rr = np.array([t["rr"] for t in trs])
        full = rr <= -0.99
        streak = mx = 0
        for x in rr:
            streak = streak + 1 if x <= -0.99 else 0
            mx = max(mx, streak)
        st = trade_stats([(t["exit"], t["rr"]) for t in trs])
        print(f"{sym:<9} n={len(trs):>4} EV={rr.mean():+.3f}R 非亏损={st['nonloss']*100:.1f}% "
              f"全止损占比={full.mean()*100:.1f}% 总R={rr.sum():+.1f} 最长连续全止损={mx}")

    all_trades.sort(key=lambda t: t["exit"])
    print(f"\n成交/离场窗口: {bj_date(bj_day(all_trades[0]['exit']))} .. "
          f"{bj_date(bj_day(all_trades[-1]['exit']))}（北京时间）")

    day_rr = defaultdict(float)
    day_stops = defaultdict(set)
    for t in all_trades:
        d = bj_day(t["exit"])
        day_rr[d] += t["rr"]
        if t["rr"] <= -0.99:
            day_stops[d].add(t["sym"])

    days = sorted(day_rr)
    vals = np.array([day_rr[d] for d in days])
    print(f"\n有离场的天数={len(days)}；组合日R分布: 均值={vals.mean():+.3f} 中位={np.median(vals):+.3f} "
          f"最差={vals.min():+.2f} 最好={vals.max():+.2f}")
    for th in (-2.0, -3.0, -4.0):
        cnt = int((vals <= th).sum())
        print(f"  组合日R ≤ {th:+.0f}R 的天数: {cnt}（≈每年 {cnt/5:.1f} 次）")

    print(f"\n-- 同天 ≥3 币全止损（5 币池） --")
    cluster = [d for d in days if len(day_stops[d]) >= 3]
    print(f"共 {len(cluster)} 天（≈每年 {len(cluster)/5:.1f} 次）")
    fwd = []
    for d in cluster:
        nxt = [t["rr"] for t in all_trades if d < bj_day(t["exit"]) <= d + 30]
        fwd.append(sum(nxt))
    fwd = np.array(fwd) if fwd else np.array([0.0])
    print(f"事后 30 天组合R: 中位={np.median(fwd):+.1f} 均值={fwd.mean():+.1f} "
          f"最差={fwd.min():+.1f} 为正比例={100*(fwd>0).mean():.0f}%")
    worst = sorted(cluster, key=lambda d: day_rr[d])[:12]
    for d in worst:
        syms = ",".join(sorted(day_stops[d]))
        print(f"  {bj_date(d)} 日R={day_rr[d]:+.2f} 全止损币: {syms}")

    print(f"\n-- 用户三币组合 BTC+BNB+DOGE 同天全止损 --")
    trio_cluster = [d for d in days if len(day_stops[d] & TRIO) >= 3]
    print(f"共 {len(trio_cluster)} 天（≈每年 {len(trio_cluster)/5:.1f} 次）")
    for d in trio_cluster:
        day_trio = [t for t in all_trades if bj_day(t["exit"]) == d and t["sym"] in TRIO]
        det = "  ".join(f"{t['sym'][:4]}{t['dir'][:1]}{t['rr']:+.2f}" for t in day_trio)
        print(f"  {bj_date(d)} {det}")

    print(f"\n-- 组合最差 12 个交易日（按离场日，全部交易） --")
    for d in sorted(days, key=lambda x: day_rr[x])[:12]:
        syms = ",".join(sorted(day_stops[d])) or "-"
        print(f"  {bj_date(d)} 日R={day_rr[d]:+.2f} 全止损币: {syms}")

    # also for the exact user portfolio day after this dump, sanity print nothing (out of cache range)


if __name__ == "__main__":
    asyncio.run(main())