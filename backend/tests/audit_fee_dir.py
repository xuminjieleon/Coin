"""AUDIT ONLY (2026-08-28): machine re-verification of AGENTS.md round-23
(fee sensitivity) and round-25 (long/short split) numbers.

Read-only audit; replicates tests/fee_compare.py and tests/direction_split.py
methodology on the cached 5y decision records (strict cache hit required):
  feeR = fee_rt x entry / risk  (双边名义额 x 费率, entry 近似出场价)
  [T4] median stop distance, feeR per trade at 双边0.10%, scenario table
       (gross / 双边0.05 / 0.06 / 0.07 / 0.10 / 0.12%), 1h vs 4h totals,
       breakeven & crossover rates, 1h nonloss gross->net.
  [T5] long/short EV & short share for 1h/4h/1d (gross).

Run: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/audit_fee_dir.py
"""
import asyncio
import os
import pickle
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import profit_sweep2 as ps
from backtest_5y import SYMBOLS, W5, CONF5, sim_outcome_fast
from backtest_ltc import trade_stats
from profit2_r5 import with_loose_plans

TFS = ("1h", "4h", "1d")
NEED_ITVS = ("1h", "4h", "1d")
SCENARIOS = [
    ("gross(无费用)", 0.0),
    ("双边0.05%", 0.0005),
    ("双边0.06%", 0.0006),
    ("双边0.07%(maker入+taker出)", 0.0007),
    ("双边0.10%(单边0.05%)", 0.0010),
    ("双边0.12%(单边0.06%)", 0.0012),
]


def load_records_strict(sym: str, tf: str):
    cache_file = os.path.join(ps.CACHE_DIR, f"_5y_cache_{sym}_{tf}.pkl")
    key = {"ver": 1, "tf": tf, "symbol": sym, "window": W5[tf], "src": ps.source_hash()}
    if not os.path.exists(cache_file):
        print(f"[cache-MISS] {sym} {tf}: file absent", flush=True)
        return None
    try:
        with open(cache_file, "rb") as f:
            entry = pickle.load(f)
    except Exception as exc:
        print(f"[cache-MISS] {sym} {tf}: unreadable ({exc})", flush=True)
        return None
    if entry.get("key") != key:
        print(f"[cache-MISS] {sym} {tf}: key mismatch", flush=True)
        return None
    print(f"[cache-HIT] {sym} {tf}: {len(entry['records'])} records", flush=True)
    return entry["records"]


def capacity_trades(recs, cfg, df):
    """fee_compare.capacity_trades 同口径, additionally keeps direction."""
    depth, stopw, be_frac, tgt, texit, trail = tuple(cfg["geo"])
    fill_bars = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    n = len(df)
    tidx = {int(t): i for i, t in enumerate(df["time"].to_numpy())}
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
        trades.append((r["time"], float(rr), direction, float(entry), float(abs(entry - stop))))
    return trades


def net_trades(trades, fee_rt):
    return [(t, rr - fee_rt * entry / risk) for t, rr, _, entry, risk in trades]


def fmt(st):
    if not st or not st.get("filled"):
        return "n=0"
    pf = f"{st['pf']:.2f}" if st["pf"] == st["pf"] and st["pf"] != float("inf") else "inf"
    return (f"成交={st['filled']} 胜率={st['winrate']*100:.1f}% 非亏损={st['nonloss']*100:.1f}% "
            f"EV={st['ev']:+.3f}R 总={st['totalR']:+.1f}R DD={st['maxdd']:.1f}R PF={pf}")


def main():
    t0 = time.time()
    dfs: dict = {sym: {} for sym in SYMBOLS}

    async def _fetch_all():
        for sym in SYMBOLS:
            for itv in NEED_ITVS:
                rows = await ps.kline_cache.get_klines(sym, itv, W5[itv])
                dfs[sym][itv] = ps.kline_cache.rows_to_df(rows)

    asyncio.run(_fetch_all())   # ONE loop total: binance shared client binds first loop
    for sym in SYMBOLS:
        print(f"[data] {sym}: " + "  ".join(f"{itv}={len(dfs[sym][itv])}bars"
                                             for itv in NEED_ITVS), flush=True)

    data: dict = {tf: {} for tf in TFS}   # tf -> sym -> trades
    for sym in SYMBOLS:
        for tf in TFS:
            cfg = CONF5[tf]
            recs = load_records_strict(sym, tf)
            if recs is None:
                continue
            recs2 = recs if cfg["th"] == 25 else with_loose_plans(recs, cfg["th"])
            data[tf][sym] = capacity_trades(recs2, cfg, dfs[sym][tf])
            print(f"[sim] {sym} {tf}: {len(data[tf][sym])} trades", flush=True)

    by_tf = {tf: sorted((tr for sym in SYMBOLS for tr in data[tf].get(sym, [])),
                        key=lambda x: x[0]) for tf in TFS}

    # ================================================================ [T4] fees
    print(f"\n{'='*100}\n##### [T4] 费用敏感性（四币合计 5 年，feeR=双边费率×entry/risk）#####\n{'='*100}")
    for tf in ("1h", "4h"):
        trades = by_tf[tf]
        stops_pct = np.array([risk / entry * 100 for _, _, _, entry, risk in trades])
        med = float(np.median(stops_pct))
        print(f"\n[T4] {tf}: {len(trades)} 笔；止损距离中位={med:.2f}% (均值={stops_pct.mean():.2f}%)；"
              f"双边0.10%≈{0.0010/(med/100):.3f}R/笔；双边0.07%≈{0.0007/(med/100):.3f}R/笔")
        for name, fee_rt in SCENARIOS:
            st = trade_stats(net_trades(trades, fee_rt))
            print(f"  [T4-{tf}] {name:<24}: {fmt(st)}")
    print("\n[T4] 1h vs 4h 总利润对比（四币合计）:")
    for name, fee_rt in SCENARIOS:
        s1 = trade_stats(net_trades(by_tf["1h"], fee_rt))
        s4 = trade_stats(net_trades(by_tf["4h"], fee_rt))
        print(f"  [T4-cmp] {name:<24}: 1h {s1['totalR']:+.1f}R (EV {s1['ev']:+.3f}) vs "
              f"4h {s4['totalR']:+.1f}R (EV {s4['ev']:+.3f})  差={s1['totalR']-s4['totalR']:+.1f}R")
    sums = {}
    for tf in ("1h", "4h"):
        tr = by_tf[tf]
        sums[tf] = (sum(rr for _, rr, _, _, _ in tr), sum(entry / risk for _, _, _, entry, risk in tr))
    for tf in ("1h", "4h"):
        g, s = sums[tf]
        print(f"  [T4-be] {tf}: 盈亏平衡费率=双边 {g/s*100:.3f}%（单边 {g/s*50:.3f}%）；"
              f"笔均费用敏感度 双边0.10%→{0.0010*s/len(by_tf[tf]):.3f}R/笔")
    g1, s1 = sums["1h"]
    g4, s4 = sums["4h"]
    f_x = (g1 - g4) / (s1 - s4)
    print(f"  [T4-cross] 1h/4h 总利润交叉点=双边 {f_x*100:.3f}%（单边 {f_x*50:.3f}%）")

    # ================================================================ [T5] direction
    print(f"\n{'='*100}\n##### [T5] 做多/做空拆分（四币 5 年，毛收益）#####\n{'='*100}")
    for tf in TFS:
        trades = by_tf[tf]
        for d, label in (("long", "做多"), ("short", "做空")):
            sub = [(t, rr) for (t, rr, dirn, _, _) in trades if dirn == d]
            st = trade_stats(sub)
            share = len(sub) / len(trades) * 100 if trades else 0
            print(f"  [T5-{tf}-{d}] {label}: 笔数={st['filled']} 占比={share:.1f}% "
                  f"胜率={st['winrate']*100:.1f}% 非亏损={st['nonloss']*100:.1f}% "
                  f"EV={st['ev']:+.3f}R 总={st['totalR']:+.1f}R")

    print(f"\n[audit_fee_dir] 总耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
