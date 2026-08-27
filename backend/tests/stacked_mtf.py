"""Stacked multi-timeframe backtest: run 1h AND 4h strategies concurrently,
compare against 1h-only (user question, 2026-08-27).

Question: does stacking 4h on top of 1h beat running 1h alone?

Method (all faithful to the round-13 production config, same harness as
backtest_5y / backtest_ltc):
  - Decision records recomputed fresh (the 5y caches went stale after the
    round-17 context.py extraction changed the source hash); cache files are
    refreshed in place with the same ver-1 key scheme backtest_5y.py uses.
  - Per symbol x TF: capacity-constrained serial execution (one position per
    symbol per TF — stacking allows BOTH a 1h and a 4h position on the same
    symbol simultaneously, which is exactly the question).
  - Stacked portfolio = the sum of both trade streams; each trade risks its
    own 1R (additive risk budgets). Reported alongside: overlap rate of same-
    symbol 1h/4h positions and direction agreement during overlap, plus the
    portfolio max concurrent positions (margin context for leverage).
  - Fees/slippage NOT modeled (maker ~0.02%).
  - Multiprocessing: one worker per symbol x TF (AGENTS.md §7.8; Windows
    spawn — entry point guarded).

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/stacked_mtf.py [--refresh]
"""
import argparse
import asyncio
import multiprocessing
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
from profit2_cap import sim_outcome_full
from profit2_r5 import with_loose_plans

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
TFS = ("1h", "4h")
W5 = {"1h": 43800, "4h": 10950, "1d": 1825}
H4_MS = 14_400_000

# round-13 production geometry (identical to backtest_ltc.CONF)
CONF = {
    "1h": dict(warmup=500, min_bars=300, spacing=4, fill_bars=24, fwd_room=130,
               geo=(0.5, 2.0, 0.15, 0.5, 96, None), th=25,
               mtf=(("4h", H4_MS), ("1d", ps.D1_MS))),
    "4h": dict(warmup=500, min_bars=300, spacing=4, fill_bars=18, fwd_room=80,
               geo=(0.75, 1.0, 0.75, None, 48, 0.35), th=10,
               mtf=(("1d", ps.D1_MS),)),
}


def fetch_dfs(sym: str, tf: str) -> dict:
    """ALL intervals fetched inside ONE asyncio.run — the shared httpx client
    in services/binance.py binds to the first event loop; a second
    asyncio.run in the same process would hit 'Event loop is closed'."""
    need = sorted({tf} | {itv for itv, _ in CONF[tf]["mtf"]})

    async def _all() -> dict:
        out = {}
        for itv in need:
            out[itv] = ps.kline_cache.rows_to_df(
                await ps.kline_cache.get_klines(sym, itv, W5[itv]))
        return out

    for attempt in range(4):
        try:
            return asyncio.run(_all())
        except Exception as exc:
            print(f"[warn] {sym} {need} fetch failed ({exc}); retry", flush=True)
            time.sleep(20 * (attempt + 1))
    raise RuntimeError(f"{sym} data unavailable: {need}")


def compute_records(sym: str, tf: str, dfs: dict) -> list[dict]:
    """Same as backtest_5y.compute_records (same key scheme, same fields)."""
    cfg = CONF[tf]
    df = dfs[tf]
    n = len(df)
    times = df["time"].to_numpy()
    closes = df["close"].to_numpy()
    records: list[dict] = []
    for i in range(cfg["warmup"], n - cfg["fwd_room"], cfg["spacing"]):
        t = int(times[i])
        htf = []
        for itv, span in cfg["mtf"]:
            m = ps.tf_summary_closed(dfs[itv], t, span)
            if m:
                htf.append(m)
        rec = ps.decide_at(df, htf, t, cfg["warmup"], cfg["min_bars"])
        if rec is None:
            continue
        rec["symbol"] = sym
        for h in (6, 24, 48):
            rec[f"ret_{h}"] = float(closes[i + h] / closes[i] - 1.0) if i + h < n else float("nan")
        records.append(rec)
    records.sort(key=lambda r: r["time"])
    return records


def load_records(sym: str, tf: str, dfs: dict, refresh: bool) -> list[dict]:
    cache_file = os.path.join(ps.CACHE_DIR, f"_5y_cache_{sym}_{tf}.pkl")
    key = {"ver": 1, "tf": tf, "symbol": sym, "src": ps.source_hash()}
    if not refresh and os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                entry = pickle.load(f)
            if entry.get("key") == key:
                print(f"[cache] {sym} {tf}: {len(entry['records'])} records", flush=True)
                return entry["records"]
        except Exception:
            pass
    t0 = time.time()
    records = compute_records(sym, tf, dfs)
    tmp = cache_file + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump({"key": key, "records": records}, f)
    os.replace(tmp, cache_file)
    print(f"[cache] {sym} {tf}: saved {len(records)} records ({time.time()-t0:.0f}s)", flush=True)
    return records


def worker(sym: str, tf: str, refresh: bool) -> dict:
    try:
        dfs = fetch_dfs(sym, tf)
        records = load_records(sym, tf, dfs, refresh)
    except Exception as exc:
        # never raise (incl. SystemExit) from a pool worker — hangs the pool
        print(f"[fail] {sym} {tf}: {exc}", flush=True)
        return {"symbol": sym, "tf": tf, "trades": [], "error": str(exc),
                "span": (0, 0)}
    cfg = CONF[tf]
    recs = records if cfg["th"] == 25 else with_loose_plans(records, cfg["th"])
    depth, stopw, be_frac, tgt, texit, trail = cfg["geo"]
    df = dfs[tf]
    times = df["time"].to_numpy()
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
        out = sim_outcome_full(df, i, direction, entry, stop, be_frac, tgt,
                               texit, cfg["fill_bars"], trail)
        if out is None:
            continue
        rr, fill, exit_bar = out
        busy = exit_bar
        trades.append({"entry": int(times[fill]), "exit": int(times[exit_bar]),
                       "dir": direction, "rr": float(rr)})
    print(f"[done] {sym} {tf}: {len(trades)} trades", flush=True)
    return {"symbol": sym, "tf": tf, "trades": trades,
            "span": (int(df["time"].iloc[0]), int(df["time"].iloc[-1]))}


# ---------------------------------------------------------------- aggregation

def stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "ev": float("nan"), "totalR": 0.0, "maxdd": float("nan"),
                "nonloss": float("nan")}
    arr = np.array([t["rr"] for t in trades], dtype=float)
    ordered = sorted(trades, key=lambda t: t["exit"])
    seq = np.array([t["rr"] for t in ordered], dtype=float)
    cum = np.cumsum(seq)
    peak = np.maximum.accumulate(np.concatenate(([0.0], cum)))[1:]
    maxdd = float(np.max(peak - cum))
    return {"n": len(arr), "ev": float(arr.mean()), "totalR": float(arr.sum()),
            "maxdd": maxdd, "nonloss": float(np.mean(arr >= -1e-9))}


def overlap_stats(t1: list[dict], t4: list[dict]) -> dict:
    """Same-symbol 1h/4h position overlap (entry<=other.exit and exit>=other.entry)."""
    n4 = len(t4)
    overlapped = 0
    agree = 0
    pairs = 0
    for b in t4:
        hit = [a for a in t1 if a["entry"] <= b["exit"] and a["exit"] >= b["entry"]]
        if hit:
            overlapped += 1
            for a in hit:
                pairs += 1
                if a["dir"] == b["dir"]:
                    agree += 1
    return {"n4": n4, "overlapped": overlapped,
            "overlapPct": overlapped / n4 * 100 if n4 else float("nan"),
            "pairs": pairs, "agreePct": agree / pairs * 100 if pairs else float("nan")}


def max_concurrent(trades: list[dict]) -> int:
    events = []
    for t in trades:
        events.append((t["entry"], 1))
        events.append((t["exit"], -1))
    events.sort(key=lambda e: (e[0], e[1]))
    cur = peak = 0
    for _, d in events:
        cur += d
        peak = max(peak, cur)
    return peak


def shared_budget_scaled(by: dict) -> list[dict]:
    """Same-symbol 1h+4h positions overlapping in time run at HALF size each
    (shared risk budget: combined risk during overlap = 1R, not 2R). Since
    trades of one TF never overlap each other (serial capacity), a trade's
    overlap measure = sum of pairwise intersections with the other TF."""
    out: list[dict] = []
    for s in SYMBOLS:
        merged = sorted(by[(s, "1h")] + by[(s, "4h")], key=lambda t: t["entry"])
        for i, t in enumerate(merged):
            span = t["exit"] - t["entry"]
            ov = 0
            for u in merged:
                if u is t:
                    continue
                lo = max(t["entry"], u["entry"])
                hi = min(t["exit"], u["exit"])
                if hi > lo:
                    ov += hi - lo
            frac = min(1.0, ov / span) if span > 0 else 0.0
            scaled = dict(t)
            scaled["rr"] = t["rr"] * (1.0 - 0.5 * frac)
            out.append(scaled)
    return out


def yearly(trades: list[dict]) -> dict:
    by = defaultdict(list)
    for t in trades:
        by[datetime.fromtimestamp(t["exit"] / 1000, tz=timezone.utc).year].append(t["rr"])
    return {y: float(sum(v)) for y, v in sorted(by.items())}


def fmt(st: dict) -> str:
    dd = f"{st['maxdd']:.1f}" if st["maxdd"] == st["maxdd"] else "-"
    ev = f"{st['ev']:+.3f}" if st["ev"] == st["ev"] else "-"
    nl = f"{st['nonloss']*100:.1f}%" if st["nonloss"] == st["nonloss"] else "-"
    return (f"n={st['n']:<6} 非亏损={nl:<7} EV={ev} 总={st['totalR']:+8.1f}R DD={dd}R")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    jobs = [(s, tf, args.refresh) for s in SYMBOLS for tf in TFS]
    with multiprocessing.Pool(processes=len(jobs)) as pool:
        results = pool.starmap(worker, jobs)

    failed = [f"{r['symbol']} {r['tf']}" for r in results if r.get("error")]
    if failed:
        raise SystemExit(f"workers failed: {failed}")
    by = {(r["symbol"], r["tf"]): r["trades"] for r in results}

    print(f"\n{'='*76}")
    print("===== 分周期（各币，1h 与 4h 各自独立跑，未计手续费/滑点） =====")
    print(f"{'='*76}")
    for tf in TFS:
        for s in SYMBOLS:
            print(f"{s:<9} {tf}: {fmt(stats(by[(s, tf)]))}")
    all1 = [t for s in SYMBOLS for t in by[(s, "1h")]]
    all4 = [t for s in SYMBOLS for t in by[(s, "4h")]]

    print(f"\n{'='*76}")
    print("===== 同币重叠（1h 与 4h 仓位时间区间相交） =====")
    print(f"{'='*76}")
    for s in SYMBOLS:
        ov = overlap_stats(by[(s, "1h")], by[(s, "4h")])
        print(f"{s:<9} 4h仓位数={ov['n4']:<5} 与1h重叠={ov['overlapped']:<5}"
              f"({ov['overlapPct']:.0f}%)  重叠对数={ov['pairs']:<6} 同向={ov['agreePct']:.0f}%")

    print(f"\n{'='*76}")
    print("===== 组合对比（四币合计，5 年） =====")
    print(f"{'='*76}")
    for name, trades in (("仅1h      ", all1), ("仅4h      ", all4), ("1h+4h 叠加", all1 + all4)):
        st = stats(trades)
        print(f"{name}: {fmt(st)}  并发峰值仓位数={max_concurrent(trades)}")
    shared = shared_budget_scaled(by)
    st = stats(shared)
    print(f"叠加·共享预算: {fmt(st)}  （同币重叠时段各半仓，组合风险≈仅1h口径）")

    y1, yc = yearly(all1), yearly(all1 + all4)
    ys = yearly(shared)
    print("\n分年（退出年份）：仅1h vs 叠加(全额) vs 叠加(共享预算)")
    for y in sorted(yc):
        print(f"  {y}: {y1.get(y, 0.0):+8.1f}R  vs  {yc[y]:+8.1f}R  vs  {ys[y]:+8.1f}R")

    print("\n注：全额叠加=两周期各自 1R 风险（同币可同时持 1h+4h 两仓，风险 x2）；")
    print("    共享预算=同币重叠时段各半仓（重叠时合计仍 1R），EV/笔不变、R 按重叠比例折减。")


if __name__ == "__main__":
    main()
