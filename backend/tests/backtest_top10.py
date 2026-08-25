"""Backtest the CURRENT production strategy on the top-10 volume coins (2026-08-25).

User request: backtest the top 10 coins by 24h quote volume (production
scanner filter) and rank them by the strategy's return. Runs the exact
production config via CONF in backtest_ltc.py (single source of truth):
per-interval PLAN_GEOMETRY / PLAN_THRESHOLD, decision windows, closed-bar
MTF, capacity-constrained serial execution, conservative same-bar order.
BTC/ETH/SOL are the tuning symbols (geometry calibrated on them); the rest
are pure out-of-sample. Fees/slippage NOT modeled (maker ~0.02%).

Ranking: primary = total R summed across intervals (strategy profit on the
available history); pooled per-trade EV (sumR / filled) shown alongside as
the per-trade rate. Coins with shorter listing history have fewer
opportunities - data spans are printed.

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/backtest_top10.py
       [--refresh] [--symbols A,B,C] [--tf 1h,4h,1d,1w]
"""
import argparse
import asyncio
import os
import pickle
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import profit_sweep2 as ps
from profit2_cap import sim_outcome_full  # noqa: F401  (re-exported for clarity)
from profit2_r5 import with_loose_plans
from backtest_ltc import CONF, TFS, capacity_run, trade_stats

TUNED = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}


def fetch_dfs(sym: str, tf: str) -> dict | None:
    need = {tf} | {itv for itv, _ in CONF[tf]["mtf"]}
    dfs: dict = {}
    for itv in sorted(need):
        rows = None
        for attempt in range(3):
            try:
                rows = asyncio.run(ps.kline_cache.get_klines(sym, itv, CONF[itv]["window"]))
                break
            except Exception as exc:
                wait = 20 * (attempt + 1)
                print(f"[warn] {sym} {itv} fetch failed ({exc}); retry in {wait}s", flush=True)
                time.sleep(wait)
        if rows is None:
            print(f"[skip] {sym} {tf}: {itv} data unavailable", flush=True)
            return None
        dfs[itv] = ps.kline_cache.rows_to_df(rows)
    return dfs


def compute_records(sym: str, tf: str, dfs: dict) -> list[dict]:
    cfg = CONF[tf]
    df = dfs[tf]
    n = len(df)
    times = df["time"].to_numpy()
    closes = df["close"].to_numpy()
    records: list[dict] = []
    t0 = time.time()
    cnt = 0
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
        cnt += 1
        if cnt % 500 == 0:
            print(f"[calc] {sym} {tf}: {cnt} ({time.time()-t0:.0f}s)", flush=True)
    records.sort(key=lambda r: r["time"])
    return records


def load_records(sym: str, tf: str, dfs: dict, refresh: bool) -> list[dict]:
    cache_file = os.path.join(ps.CACHE_DIR, f"_t10_cache_{sym}_{tf}.pkl")
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
    records = compute_records(sym, tf, dfs)
    tmp = cache_file + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump({"key": key, "records": records}, f)
    os.replace(tmp, cache_file)
    print(f"[cache] {sym} {tf}: saved {len(records)} records", flush=True)
    return records


def run_symbol(sym: str, tfs: list[str], refresh: bool) -> dict:
    result = {"symbol": sym, "tfs": {}, "trades": []}
    for tf in tfs:
        cfg = CONF[tf]
        fill_bars = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
        dfs = fetch_dfs(sym, tf)
        if dfs is None:
            continue
        df = dfs[tf]
        if len(df) <= cfg["warmup"] + cfg["fwd_room"] + cfg["spacing"]:
            print(f"[skip] {sym} {tf}: too few bars ({len(df)})", flush=True)
            result["tfs"][tf] = {"bars": len(df), "filled": 0}
            continue
        records = load_records(sym, tf, dfs, refresh)
        recs = records if cfg["th"] == 25 else with_loose_plans(records, cfg["th"])
        tidx = {int(t): i for i, t in enumerate(df["time"].to_numpy())}
        n_orders, trades = capacity_run(recs, cfg["geo"], df, tidx, fill_bars)
        st = trade_stats(trades)
        st["orders"] = n_orders
        st["decisions"] = len(records)
        st["bars"] = len(df)
        st["span"] = (ps.fmt_ts(int(df["time"].iloc[0])), ps.fmt_ts(int(df["time"].iloc[-1])))
        result["tfs"][tf] = st
        result["trades"].extend((t, r, tf) for t, r in trades)
        if st["filled"]:
            pf = f"{st['pf']:.2f}" if st["pf"] != float("inf") else "inf"
            print(f"[done] {sym} {tf}: 成交={st['filled']} 胜率={st['winrate']*100:.1f}% "
                  f"EV={st['ev']:+.3f}R 总={st['totalR']:+.1f}R DD={st['maxdd']:.1f}R PF={pf}",
                  flush=True)
        else:
            print(f"[done] {sym} {tf}: 无成交", flush=True)
    return result


def summarize(result: dict) -> dict:
    trades = sorted(result["trades"], key=lambda x: x[0])
    arr = np.array([r for _, r, _ in trades], dtype=float)
    out = {"symbol": result["symbol"], "filled": 0}
    if len(arr):
        cum = np.cumsum(arr)
        peak = np.maximum.accumulate(np.concatenate(([0.0], cum)))[1:]
        wins = arr[arr > 1e-9].sum()
        losses = -arr[arr < -1e-9].sum()
        out.update({
            "filled": len(arr),
            "winrate": float(np.mean(arr > 1e-9)),
            "ev": float(arr.mean()),
            "totalR": float(arr.sum()),
            "maxdd": float(np.max(peak - cum)),
            "pf": float(wins / losses) if losses > 1e-9 else float("inf"),
        })
    else:
        out.update({"winrate": float("nan"), "ev": float("nan"), "totalR": 0.0,
                    "maxdd": float("nan"), "pf": float("nan")})
    return out


async def top_volume(n: int) -> list[dict]:
    from services.scanner import _top_usdt_pairs
    return await _top_usdt_pairs(n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--symbols", default=None, help="comma list; default = top10 by 24h volume")
    ap.add_argument("--tf", default=None)
    args = ap.parse_args()
    tfs = args.tf.split(",") if args.tf else list(TFS)

    if args.symbols:
        symbols = args.symbols.split(",")
        vols = {}
    else:
        rows = asyncio.run(top_volume(10))
        symbols = [r["symbol"] for r in rows]
        vols = {r["symbol"]: r["quoteVolume"] for r in rows}
        print("成交量前10（24h 成交额，扫描器同款过滤）：")
        for r in rows:
            print(f"  {r['symbol']:<10} {r['quoteVolume']/1e9:.2f}B")

    results = []
    for sym in symbols:
        tag = "（调参标的）" if sym in TUNED else "（纯样本外）"
        print(f"\n===== {sym} {tag} =====", flush=True)
        results.append(run_symbol(sym, tfs, args.refresh))

    print(f"\n{'='*76}\n===== 排名（当前生产策略，容量约束串行，未计手续费/滑点）=====\n{'='*76}")
    sums = [(summarize(r), r) for r in results]
    sums.sort(key=lambda x: -x[0]["totalR"])
    print(f"{'币种':<10} {'总利润':>9} {'笔数':>5} {'每笔EV':>8} {'胜率':>7} {'回撤':>6} {'PF':>6}  备注")
    for s, r in sums:
        sym = s["symbol"]
        vol = f" 24h额{vols[sym]/1e9:.1f}B" if sym in vols else ""
        note = ("调参标的" if sym in TUNED else "样本外") + vol
        if not s["filled"]:
            print(f"{sym:<10} 无成交 {note}")
            continue
        pf = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
        print(f"{sym:<10} {s['totalR']:>+8.1f}R {s['filled']:>5} {s['ev']:>+7.3f}R "
              f"{s['winrate']*100:>6.1f}% {s['maxdd']:>5.1f}R {pf:>6}  {note}")

    print("\n分周期明细（EV/总利润）：")
    for s, r in sums:
        parts = []
        for tf in tfs:
            st = r["tfs"].get(tf)
            if not st or not st.get("filled"):
                parts.append(f"{tf}: 无")
                continue
            parts.append(f"{tf}: EV{st['ev']:+.3f} 总{st['totalR']:+.1f}R(n={st['filled']})")
        print(f"  {s['symbol']:<10} " + "  ".join(parts))


if __name__ == "__main__":
    main()
