"""AUDIT ONLY (2026-08-28): machine re-verification of BACKTEST.md §4 numbers.

Read-only audit script (audit_ prefix per task convention; does NOT modify any
existing file). Re-evaluates on the cached 5y decision records:
  [T1] current CONF (round-13) geometry, full window, 4-symbol pooled  -> §4.3
  [T2] old round-11 geometry, full window, 4-symbol pooled             -> §4.1
  [T3] per-calendar-year totals (both geometries)                      -> §4.1 "逐年全正"
  [T7] pooled A40/B30/C30 fold split, fold-C blind new vs old          -> §4.2

Record caches are REQUIRED to hit (same key convention as
backtest_5y.load_records); a miss is reported as MISS and skipped (no
recompute, to stay within the runtime budget). 1w has no cache -> not audited.

Run: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/audit_baseline.py
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
from backtest_5y import SYMBOLS, W5, CONF5, capacity_run_fast
from backtest_ltc import trade_stats
from profit2_r5 import with_loose_plans

TFS = ("1h", "4h", "1d")
NEED_ITVS = ("1h", "4h", "1d")

# Round-11 production geometry (BACKTEST.md §1 / §4.1): (depth, stop, be, tgt, texit, trail)
OLD_GEO = {
    "1h": (0.75, 2.5, 0.1, 0.75, 96, None),
    "4h": (0.75, 1.2, 0.5, None, 48, 0.5),
    "1d": (0.75, 1.5, 0.5, None, 24, 0.5),
}


def load_records_strict(sym: str, tf: str):
    """backtest_5y.load_records key convention, but never recompute."""
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
        print(f"[cache-MISS] {sym} {tf}: key mismatch "
              f"(cached src={str(entry.get('key', {}).get('src'))[:40]}...)", flush=True)
        return None
    print(f"[cache-HIT] {sym} {tf}: {len(entry['records'])} records", flush=True)
    return entry["records"]


def fmt(st: dict) -> str:
    if not st or not st.get("filled"):
        return "n=0"
    pf = f"{st['pf']:.2f}" if st["pf"] != float("inf") else "inf"
    return (f"成交={st['filled']} 胜率={st['winrate']*100:.1f}% 非亏损={st['nonloss']*100:.1f}% "
            f"EV={st['ev']:+.3f}R 总={st['totalR']:+.1f}R DD={st['maxdd']:.1f}R PF={pf}")


def year_of(ms: int) -> int:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year


def eval_geo(loose, geo, arrs, tidx, fill_bars, symbols=SYMBOLS):
    """capacity_run_fast over the given record lists; returns (stats, trades, by_sym)."""
    all_trades: list = []
    by_sym: dict = {}
    for sym in symbols:
        if sym not in loose:
            continue
        _, tr = capacity_run_fast(loose[sym], geo, arrs[sym], tidx[sym], fill_bars)
        by_sym[sym] = tr
        all_trades.extend(tr)
    all_trades.sort(key=lambda x: x[0])
    return trade_stats(all_trades), all_trades, by_sym


def main():
    t0 = time.time()
    # ---- data (ONE asyncio.run total: binance shared client binds the first loop) ----
    dfs: dict = {sym: {} for sym in SYMBOLS}

    async def _fetch_all():
        for sym in SYMBOLS:
            for itv in NEED_ITVS:
                rows = await ps.kline_cache.get_klines(sym, itv, W5[itv])
                dfs[sym][itv] = ps.kline_cache.rows_to_df(rows)

    asyncio.run(_fetch_all())
    for sym in SYMBOLS:
        d = dfs[sym]
        print(f"[data] {sym}: " + "  ".join(
            f"{itv}={len(d[itv])}bars({ps.fmt_ts(int(d[itv]['time'].iloc[0]))}.."
            f"{ps.fmt_ts(int(d[itv]['time'].iloc[-1]))})" for itv in NEED_ITVS), flush=True)

    loose: dict = {tf: {} for tf in TFS}
    arrs: dict = {tf: {} for tf in TFS}
    tidx: dict = {tf: {} for tf in TFS}
    for sym in SYMBOLS:
        for tf in TFS:
            cfg = CONF5[tf]
            recs = load_records_strict(sym, tf)
            if recs is None:
                continue
            loose[tf][sym] = recs if cfg["th"] == 25 else with_loose_plans(recs, cfg["th"])
            df = dfs[sym][tf]
            arrs[tf][sym] = (df["high"].to_numpy(), df["low"].to_numpy(),
                             df["close"].to_numpy(), len(df))
            tidx[tf][sym] = {int(t): i for i, t in enumerate(df["time"].to_numpy())}

    # ================================================================ [T1]/[T2]
    for tf in TFS:
        cfg = CONF5[tf]
        fill_bars = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
        print(f"\n{'='*96}\n##### {tf} 全时段容量约束串行（fill={fill_bars} th={cfg['th']}）#####\n{'='*96}")
        for tag, geo in (("T1-NEW-R13", tuple(cfg["geo"])), ("T2-OLD-R11", OLD_GEO[tf])):
            st, trades, by_sym = eval_geo(loose[tf], geo, arrs[tf], tidx[tf], fill_bars)
            print(f"\n[{tag}] {tf} geo={geo}")
            for sym in SYMBOLS:
                if sym in by_sym:
                    print(f"  {sym:<9} {fmt(trade_stats(by_sym[sym]))}")
            print(f"  >> [{tag}] {tf} 四币合计: {fmt(st)}")
            # [T3] yearly totals (pooled) + per-symbol-per-year positivity
            by_year = defaultdict(float)
            neg_cells = []
            for t, r in trades:
                by_year[year_of(t)] += r
            parts = "  ".join(f"{y}:{v:+.1f}R" for y, v in sorted(by_year.items()))
            print(f"  >> [T3-{tag}] {tf} 分年合计: {parts}")
            for sym in SYMBOLS:
                if sym not in by_sym:
                    continue
                yy = defaultdict(float)
                for t, r in by_sym[sym]:
                    yy[year_of(t)] += r
                row = "  ".join(f"{y}:{v:+.1f}" for y, v in sorted(yy.items()))
                neg = [f"{y}:{v:+.1f}" for y, v in sorted(yy.items()) if v < 0]
                neg_cells.extend(f"{sym}/{n}" for n in neg)
                print(f"     [T3-{tag}] {tf} {sym}: {row}")
            print(f"  >> [T3-{tag}] {tf} 负值单元(币×年): {neg_cells if neg_cells else '无（逐年全正）'}")

    # ================================================================ [T7] fold C
    print(f"\n{'='*96}\n##### [T7] Phase2 口径：pooled A40/B30/C30，C 段盲测（新 vs 旧）#####\n{'='*96}")
    for tf in TFS:
        cfg = CONF5[tf]
        fill_bars = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
        pooled = sorted(((sym, r) for sym in SYMBOLS for r in loose[tf].get(sym, [])),
                        key=lambda x: x[1]["time"])
        a = int(len(pooled) * 0.4)
        b = int(len(pooled) * 0.7)
        FA, FB, FC = pooled[:a], pooled[a:b], pooled[b:]
        print(f"\n[T7] {tf}: 决策点 {len(pooled)}；A={len(FA)} B={len(FB)} C={len(FC)}"
              f"（A 止 {ps.fmt_ts(FA[-1][1]['time'])}，B 止 {ps.fmt_ts(FB[-1][1]['time'])}，"
              f"C 止 {ps.fmt_ts(FC[-1][1]['time'])}）")

        def fold_eval(fold, geo):
            by = defaultdict(dict)
            for sym, r in fold:
                by[sym].setdefault("recs", []).append(r)
            l2 = {sym: v["recs"] for sym, v in by.items()}
            return eval_geo(l2, geo, arrs[tf], tidx[tf], fill_bars)

        for tag, geo in (("NEW-R13", tuple(cfg["geo"])), ("OLD-R11", OLD_GEO[tf])):
            st, _, by_sym = fold_eval(FC, geo)
            worst = min((trade_stats(tr)["totalR"] for tr in by_sym.values()), default=float("nan"))
            print(f"  [T7-FC-{tag}] {tf}: {fmt(st)}  最差币={worst:+.1f}R")
            for sym in SYMBOLS:
                if sym in by_sym:
                    print(f"      {sym:<9} {fmt(trade_stats(by_sym[sym]))}")

    print(f"\n[audit_baseline] 总耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
