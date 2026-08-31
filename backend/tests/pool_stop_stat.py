"""止损贴池插针风险：描述性统计（2026-08-31，第四十四轮）

用户问题（08-31 凌晨 BTC/BNB/DOGE 三张 4h 多单被同一波下砸止损后）：
五年里"止损距未扫池 ≤0.5×ATR"的 4h 单子，整体 EV 是否系统性更差？
预登记判定规则（用户拍板）：显著更差 → 再预登记一轮决定是否加过滤；不显著 → 翻篇。

口径：
- 生产 4h 计划完整重放（R13 几何 0.75/1.0/0.75R/0.35R 跟踪、th10、fill 12×1.5、
  容量约束串行、保守同根顺序）——与 fee_compare/cluster_stop_stat 同一 harness；
- 插针标记 = 仓位建议同款规则的"信号时刻版"（无前视）：信号 bar 引擎流动性池
  （liquidityPools，按触碰数前 8）中风险侧最近的未扫池，与计划止损距离 ≤0.5×ATR14
  ——多头看卖方池（价下方）、空头看买方池（价上方），与 routers/position.py 一致；
  harness 口径与回测一致不含 prevDay 池（第三十轮审计：回测无 prevDay 组件）；
- 5 币池（BTC/ETH/BNB/SOL/DOGE）× 5 年 4h，毛口径未计费；
- 显著性：EV 差值 bootstrap 95% CI + 10k 置换检验，辅以分币/分年符号一致性。
- 本轮纯统计，零生产改动。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/pool_stop_stat.py
"""
import asyncio
import os
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
from fee_compare import load_cached_records
from profit2_r5 import with_loose_plans

TF = "4h"
SYMS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT"]
CST_OFF = 8 * 3600 * 1000
FLAG_ATR = 0.5
WARMUP = 500


def bj_year(ms):
    return (datetime(1970, 1, 1, tzinfo=timezone.utc)
            + timedelta(milliseconds=ms + CST_OFF)).year


def pools_flag(df, i, direction, stop, atr, price):
    """Mirror of the position-advise 插针 rule at signal bar i (no lookahead).
    Returns (dist_in_atr, nearest_pool_price) or (None, None) if no same-side pool."""
    win = df.iloc[max(0, i - WARMUP + 1): i + 1].reset_index(drop=True)
    full = ps.engine.full_analysis(win)
    pools = full["smc"]["liquidityPools"]
    if direction == "long":
        cand = [p for p in pools if p["type"] == "sell_side"
                and not p["swept"] and p["price"] < price]
        if not cand:
            return None, None
        nearest = max(cand, key=lambda p: p["price"])
    else:
        cand = [p for p in pools if p["type"] == "buy_side"
                and not p["swept"] and p["price"] > price]
        if not cand:
            return None, None
        nearest = min(cand, key=lambda p: p["price"])
    if not atr or atr <= 0:
        return None, None
    return abs(stop - nearest["price"]) / atr, nearest["price"]


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
        dist, pool = pools_flag(df, i, direction, float(stop), float(r["atr"]), float(r["price"]))
        trades.append({
            "sym": sym, "dir": direction, "rr": float(rr),
            "sig": int(r["time"]), "exit": int(times[exit_bar]),
            "dist": dist, "pool": pool,
        })
    return trades


def stats_line(name, trs):
    if not trs:
        return f"{name:<14} n=0"
    rr = np.array([t["rr"] for t in trs])
    return (f"{name:<14} n={len(trs):>4}  EV={rr.mean():+.3f}R  "
            f"非亏损={100 * np.mean(rr >= -1e-9):.1f}%  全止损={100 * np.mean(rr <= -0.99):.1f}%  "
            f"总R={rr.sum():+8.1f}  中位={np.median(rr):+.2f}")


async def fetch_all():
    dfs = {}
    for sym in SYMS:
        d = {}
        for itv in (TF, "1d"):
            rows = await ps.kline_cache.get_klines(sym, itv, W5[itv])
            d[itv] = ps.kline_cache.rows_to_df(rows)
        dfs[sym] = d
        print(f"[data] {sym}: 4h {len(d[TF])} bars / 1d {len(d['1d'])} bars", flush=True)
    return dfs


def live_vignette(dfs):
    """Flag status of the plan vintages live entering the 08-31 dump.

    Signal bars (UTC): BTC 08-30 12:00 & 16:00, BNB 08-30 12:00 (16:00 无计划),
    DOGE 08-30 16:00. Plan rebuilt from window (close-0.75*ATR / entry-1*ATR).
    Flag computed twice: harness口径 (no prevDay pools) and production advise
    口径 (with prevDay high/low from fully-closed daily bar rows[-2]).
    """
    geo_depth, geo_stop = 0.75, 1.0
    cases = [
        ("BTCUSDT", 12), ("BTCUSDT", 16),
        ("BNBUSDT", 12),
        ("DOGEUSDT", 16),
    ]
    print("\n[七] 本次事件三单判定（引擎口径双跑：回测口径 / 生产仓位建议口径含 prevDay 池）")
    for sym, hour in cases:
        df = dfs[sym][TF]
        t = int(datetime(2026, 8, 30, hour, 0, tzinfo=timezone.utc).timestamp() * 1000)
        i = int(np.where(df["time"].to_numpy() == t)[0][0])
        win = df.iloc[max(0, i - WARMUP + 1): i + 1].reset_index(drop=True)
        full = ps.engine.full_analysis(win)
        atr = next(v for v in reversed(full["indicators"]["atr14"]) if v is not None)
        close = float(win["close"].iloc[-1])
        entry = close - geo_depth * atr
        stop = entry - geo_stop * atr
        dist_np, pool_np = pools_flag(df, i, "long", stop, atr, close)

        d1 = dfs[sym]["1d"]
        drows = d1[d1["time"] <= t]
        prev = None
        if len(drows) >= 2:
            prev = {"high": float(drows["high"].iloc[-2]), "low": float(drows["low"].iloc[-2])}
        win_p = df.iloc[max(0, i - WARMUP + 1): i + 1].reset_index(drop=True)
        full_p = ps.engine.full_analysis(win_p, prev)
        pools_p = full_p["smc"]["liquidityPools"]
        cand = [p for p in pools_p if p["type"] == "sell_side"
                and not p["swept"] and p["price"] < close]
        dist_pd, pool_pd = None, None
        if cand:
            near = max(cand, key=lambda p: p["price"])
            dist_pd, pool_pd = abs(stop - near["price"]) / atr, near["price"]

        def tag(dist):
            if dist is None:
                return "无同侧池"
            return "★贴池" if dist <= FLAG_ATR else "不贴池"
        dn = f"{dist_np:.2f}×ATR" if dist_np is not None else "-"
        dp = f"{dist_pd:.2f}×ATR@{pool_pd:.6g}" if dist_pd is not None else "-"
        print(f"  {sym:<9} bar=UTC{hour}:00 entry={entry:.6g} stop={stop:.6g}  "
              f"回测口径 距最近未扫池={dn} → {tag(dist_np)} | 含prevDay {dp} → {tag(dist_pd)}")


async def main():
    dfs = await fetch_all()
    cfg = CONF5[TF]
    all_trades = []
    for sym in SYMS:
        records = load_cached_records(sym, TF, dfs[sym])
        recs = with_loose_plans(records, cfg["th"])
        trs = sim_trades(sym, recs, cfg, dfs[sym][TF])
        all_trades.extend(trs)
        fl = [t for t in trs if t["dist"] is not None and t["dist"] <= FLAG_ATR]
        nopool = [t for t in trs if t["dist"] is None]
        nl = sum(1 for t in fl if t["dir"] == "long")
        print(f"[sim] {sym}: {len(trs)} trades | 贴池 {len(fl)} ({100*len(fl)/max(1,len(trs)):.1f}%, "
              f"多{nl}/空{len(fl)-nl}) | 无同侧池 {len(nopool)}")

    all_trades.sort(key=lambda t: t["exit"])
    print(f"\n离场窗口: {ps.fmt_ts(all_trades[0]['exit'])} .. {ps.fmt_ts(all_trades[-1]['exit'])}")

    flagged = [t for t in all_trades if t["dist"] is not None and t["dist"] <= FLAG_ATR]
    unflagged = [t for t in all_trades if t["dist"] is not None and t["dist"] > FLAG_ATR]
    nopool = [t for t in all_trades if t["dist"] is None]

    print("\n[一] 池化对比（5 币 4h 5 年，毛口径）")
    print(stats_line("贴池 ≤0.5×ATR", flagged))
    print(stats_line("不贴池", unflagged))
    print(stats_line("无同侧池", nopool))
    print(stats_line("全部", all_trades))

    rf = np.array([t["rr"] for t in flagged])
    ru = np.array([t["rr"] for t in unflagged])
    diff = rf.mean() - ru.mean()
    rng = np.random.default_rng(7)
    boots = np.empty(10000)
    for b in range(10000):
        bf = rf[rng.integers(0, len(rf), len(rf))]
        bu = ru[rng.integers(0, len(ru), len(ru))]
        boots[b] = bf.mean() - bu.mean()
    lo, hi = np.percentile(boots, [2.5, 97.5])
    pooled_rr = np.concatenate([rf, ru])
    perms = np.empty(10000)
    for b in range(10000):
        rng.shuffle(pooled_rr)
        perms[b] = pooled_rr[:len(rf)].mean() - pooled_rr[len(rf):].mean()
    p = float(np.mean(np.abs(perms) >= abs(diff)))
    print(f"\n[二] EV 差（贴池−不贴池）= {diff:+.3f}R  bootstrap 95%CI [{lo:+.3f}, {hi:+.3f}]  "
          f"置换检验 p={p:.3f}")

    print("\n[三] 分币（EV 贴池 / 不贴池 / 差）")
    for sym in SYMS:
        f = [t["rr"] for t in flagged if t["sym"] == sym]
        u = [t["rr"] for t in unflagged if t["sym"] == sym]
        if f and u:
            print(f"  {sym:<9} 贴池 n={len(f):>3} EV={np.mean(f):+.3f}  "
                  f"不贴池 n={len(u):>3} EV={np.mean(u):+.3f}  差={np.mean(f)-np.mean(u):+.3f}")
        else:
            print(f"  {sym:<9} 贴池 n={len(f)} 不贴池 n={len(u)}（样本不足）")

    print("\n[四] 分年（离场北京时间年份；贴池 n/EV vs 不贴池 n/EV，差）")
    by_year = defaultdict(lambda: [[], []])
    for t in flagged:
        by_year[bj_year(t["exit"])][0].append(t["rr"])
    for t in unflagged:
        by_year[bj_year(t["exit"])][1].append(t["rr"])
    for y in sorted(by_year):
        f, u = by_year[y]
        if f and u:
            print(f"  {y}: 贴池 n={len(f):>3} EV={np.mean(f):+.3f}  不贴池 n={len(u):>3} "
                  f"EV={np.mean(u):+.3f}  差={np.mean(f)-np.mean(u):+.3f}")
        else:
            print(f"  {y}: 贴池 n={len(f)} 不贴池 n={len(u)}")

    print("\n[五] 标记的识别力")
    stops_all = [t for t in all_trades if t["rr"] <= -0.99]
    wins_all = [t for t in all_trades if t["rr"] > 0]
    def flrate(group):
        if not group:
            return float("nan")
        return 100 * sum(1 for t in group if t["dist"] is not None and t["dist"] <= FLAG_ATR) / len(group)
    print(f"  P(贴池|全止损)={flrate(stops_all):.1f}%   P(贴池|盈利单)={flrate(wins_all):.1f}%   "
          f"总体贴池率={flrate(all_trades):.1f}%")

    print("\n[六] 过滤账本（若剔除全部贴池单，池化权益曲线按离场时间重算）")
    def curve(trs):
        trs = sorted(trs, key=lambda t: t["exit"])
        r = np.array([t["rr"] for t in trs])
        cum = np.cumsum(r)
        peak = np.maximum.accumulate(np.concatenate(([0.0], cum)))[1:]
        return r.sum(), float(np.max(peak - cum))
    tot_all, dd_all = curve(all_trades)
    kept = [t for t in all_trades if not (t["dist"] is not None and t["dist"] <= FLAG_ATR)]
    tot_kept, dd_kept = curve(kept)
    print(f"  全部: {len(all_trades)} 笔 总R={tot_all:+.1f} DD={dd_all:.1f}")
    print(f"  剔除贴池单后: {len(kept)} 笔 总R={tot_kept:+.1f} DD={dd_kept:.1f}  "
          f"（Δ总R={tot_kept-tot_all:+.1f}，贴池单合计R={tot_all-tot_kept:+.1f}）")

    live_vignette(dfs)


if __name__ == "__main__":
    asyncio.run(main())