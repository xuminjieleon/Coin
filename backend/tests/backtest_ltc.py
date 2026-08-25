"""LTCUSDT backtest with the CURRENT production strategy (2026-08-25).

User request: apply the current (round-11b frozen, see AGENTS.md §7) strategy
to LTC and report. LTC never informed any tuning decision (the round-11b OOS
expansion was cancelled before LTC data arrived - DEVLOG), so this is pure
out-of-sample for the production config.

Faithful to production / round-11 harness conventions:
  - Decision engine exactly as calibrated: per-interval window (1h/4h 500
    bars, 1d 300, 1w 170), MTF from FULLY CLOSED higher-TF bars only
    (1h -> 4h+1d, 4h -> 1d), decision at bar close, orders live from the
    next bar. Funding/OI components are NOT simulated (the round-11/12
    baseline口径 - factor rounds rejected them).
  - Production PLAN_GEOMETRY / PLAN_THRESHOLD per interval (round-13
    five-year recalibration, 2026-08-25, see decision.py docstring):
      1h  (0.5, 2.0, 0.15, 0.5, 96, trail None)  fill 24  th 25 (native)
      4h  (0.75, 1.0, 0.75, None, 48, trail 0.35) fill 18  th 10 (loose)
      1d  (1.0, 1.2, 0.50, None, 12, trail 0.35) fill 9   th 10 (loose)
      1w  (0.75, 1.5, 0.50, None, 24, trail 0.75) fill 8   th 10 (loose)
  - Capacity-constrained serial execution (one position at a time; unfilled
    orders replaced by newer signals). Same-bar conservative order:
    stop > target > trigger; trail ratchet from prior bars only.
  - Fees/slippage NOT modeled (maker entry ~0.02%): all EV is gross.

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/backtest_ltc.py
       [--refresh] [--tf 1h,4h,1d,1w]
"""
import argparse
import asyncio
import os
import pickle
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import profit_sweep2 as ps
from profit2_cap import sim_outcome_full
from profit2_r5 import with_loose_plans

SYMBOL = "LTCUSDT"
H4_MS = 14_400_000

# tf -> window bars, warmup, min_bars, spacing, fill_bars, fwd_room,
#        production geometry (depth, stop, be_frac, tgt, texit, trail),
#        fill multiplier, plan threshold, MTF (interval, span_ms) list
CONF = {
    "1h": dict(window=17520, warmup=500, min_bars=300, spacing=4, fill_bars=24,
               fwd_room=130, geo=(0.5, 2.0, 0.15, 0.5, 96, None), fill_mult=1.0, th=25,
               mtf=(("4h", H4_MS), ("1d", ps.D1_MS))),
    "4h": dict(window=6570, warmup=500, min_bars=300, spacing=4, fill_bars=12,
               fwd_room=80, geo=(0.75, 1.0, 0.75, None, 48, 0.35), fill_mult=1.5, th=10,
               mtf=(("1d", ps.D1_MS),)),
    "1d": dict(window=1460, warmup=300, min_bars=220, spacing=2, fill_bars=6,
               fwd_room=110, geo=(1.0, 1.2, 0.50, None, 12, 0.35), fill_mult=1.5, th=10,
               mtf=()),
    "1w": dict(window=520, warmup=170, min_bars=120, spacing=1, fill_bars=4,
               fwd_room=32, geo=(0.75, 1.5, 0.50, None, 24, 0.75), fill_mult=2.0, th=10,
               mtf=()),
}
TFS = ("1h", "4h", "1d", "1w")


def fetch_dfs(tf: str) -> dict | None:
    need = {tf} | {itv for itv, _ in CONF[tf]["mtf"]}
    dfs: dict = {}
    for itv in sorted(need):
        rows = None
        for attempt in range(4):
            try:
                rows = asyncio.run(ps.kline_cache.get_klines(SYMBOL, itv, CONF[itv]["window"]))
                break
            except Exception as exc:
                wait = 20 * (attempt + 1)
                print(f"[warn] {SYMBOL} {itv} fetch failed ({exc}); retry in {wait}s", flush=True)
                time.sleep(wait)
        if rows is None:
            print(f"[skip] {tf}: {itv} data unavailable")
            return None
        dfs[itv] = ps.kline_cache.rows_to_df(rows)
        print(f"[data] {SYMBOL} {itv}: {len(dfs[itv])} bars")
    return dfs


def compute_records(tf: str, dfs: dict) -> list[dict]:
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
        rec["symbol"] = SYMBOL
        for h in (6, 24, 48):
            rec[f"ret_{h}"] = float(closes[i + h] / closes[i] - 1.0) if i + h < n else float("nan")
        records.append(rec)
        cnt += 1
        if cnt % 300 == 0:
            print(f"[calc] {tf}: {cnt} ({time.time()-t0:.0f}s)", flush=True)
    records.sort(key=lambda r: r["time"])
    return records


def load_records(tf: str, dfs: dict, refresh: bool) -> list[dict]:
    cache_file = os.path.join(ps.CACHE_DIR, f"_ltc_cache_{tf}.pkl")
    key = {"ver": 1, "tf": tf, "symbol": SYMBOL, "src": ps.source_hash()}
    if not refresh and os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                entry = pickle.load(f)
            if entry.get("key") == key:
                print(f"[cache] {tf}: {len(entry['records'])} records")
                return entry["records"]
        except Exception:
            pass
    records = compute_records(tf, dfs)
    tmp = cache_file + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump({"key": key, "records": records}, f)
    os.replace(tmp, cache_file)
    print(f"[cache] {tf}: saved {len(records)} records")
    return records


def capacity_run(records, geo, df, tidx, fill_bars):
    """Serial one-position execution (unfilled orders replaced by newer signals)."""
    depth, stopw, be_frac, tgt, texit, trail = geo
    trades: list[tuple[int, float]] = []
    n_orders = 0
    busy = -1
    for r in records:
        if r.get("plan") is None:
            continue
        i = tidx.get(r["time"])
        if i is None:
            continue
        if i <= busy:
            continue
        built = ps.build_plan(r, depth, stopw)
        if built is None:
            continue
        direction, entry, stop = built
        n_orders += 1
        out = sim_outcome_full(df, i, direction, entry, stop, be_frac, tgt, texit, fill_bars, trail)
        if out is None:
            continue
        rr, fill, exit_bar = out
        busy = exit_bar
        trades.append((r["time"], rr))
    return n_orders, trades


def trade_stats(trades):
    if not trades:
        return {"filled": 0, "winrate": float("nan"), "nonloss": float("nan"),
                "ev": float("nan"), "totalR": 0.0, "maxdd": float("nan"), "pf": float("nan")}
    arr = np.array([r for _, r in trades], dtype=float)
    cum = np.cumsum(arr)
    peak = np.maximum.accumulate(np.concatenate(([0.0], cum)))[1:]
    maxdd = float(np.max(peak - cum))
    wins = arr[arr > 1e-9].sum()
    losses = -arr[arr < -1e-9].sum()
    return {"filled": len(arr), "winrate": float(np.mean(arr > 1e-9)),
            "nonloss": float(np.mean(arr >= -1e-9)), "ev": float(arr.mean()),
            "totalR": float(arr.sum()), "maxdd": maxdd,
            "pf": float(wins / losses) if losses > 1e-9 else float("inf")}


def dir_stats(records, horizon):
    dirs, rets = [], []
    for r in records:
        s = r["score"]
        if abs(s) < 15:
            continue
        ret = r.get(f"ret_{horizon}")
        if ret is None or np.isnan(ret) or ret == 0:
            continue
        dirs.append(1 if s > 0 else -1)
        rets.append(ret)
    if not dirs:
        return 0, float("nan")
    return len(dirs), float(np.mean(np.sign(dirs) == np.sign(rets)))


def year_of(ms: int) -> int:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year


def run_tf(tf: str, refresh: bool):
    cfg = CONF[tf]
    depth, stopw, be_frac, tgt, texit, trail = cfg["geo"]
    fill_bars = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
    dfs = fetch_dfs(tf)
    if dfs is None:
        return None
    df = dfs[tf]
    records = load_records(tf, dfs, refresh)
    recs = records if cfg["th"] == 25 else with_loose_plans(records, cfg["th"])
    n_native = sum(1 for r in records if r.get("plan"))
    n_plans = sum(1 for r in recs if r.get("plan"))
    tidx = {int(t): i for i, t in enumerate(df["time"].to_numpy())}

    n_orders, trades = capacity_run(recs, cfg["geo"], df, tidx, fill_bars)
    st = trade_stats(trades)

    print(f"\n{'='*72}\n===== {tf}  LTC 生产策略回测 =====\n{'='*72}")
    print(f"几何: depth={depth} stop={stopw} be={be_frac} tgt={tgt} texit={texit} "
          f"trail={trail} fill={fill_bars} th={cfg['th']}")
    print(f"数据: {len(df)} 根 ({ps.fmt_ts(int(df['time'].iloc[0]))} .. "
          f"{ps.fmt_ts(int(df['time'].iloc[-1]))})")
    print(f"决策点 {len(records)}；计划 {n_plans}（原生 th=25/CVD 共振 {n_native}，"
          f"{'阈值放宽至 10' if cfg['th'] != 25 else '保守阈值'}）")
    if not trades:
        print("  无成交")
        return {"tf": tf, **st, "orders": n_orders, "decisions": len(records)}
    dd = f"{st['maxdd']:.1f}"
    pf = f"{st['pf']:.2f}" if st["pf"] != float("inf") else "inf"
    print(f"容量约束串行: 挂单={n_orders} 成交={st['filled']} "
          f"填充率={st['filled']/max(n_orders,1)*100:.0f}%")
    print(f"胜率={st['winrate']*100:.1f}% 非亏损={st['nonloss']*100:.1f}% "
          f"EV={st['ev']:+.3f}R 总利润={st['totalR']:+.1f}R 最大回撤={dd}R PF={pf}")

    by_year = defaultdict(list)
    for t, r in trades:
        by_year[year_of(t)].append(r)
    parts = []
    for y in sorted(by_year):
        arr = np.array(by_year[y])
        parts.append(f"{y}: n={len(arr)} sum={arr.sum():+.1f}R")
    print("分年: " + "  ".join(parts))

    acc = []
    for h in (6, 24, 48):
        n, hit = dir_stats(records, h)
        acc.append(f"{h}根: n={n} {hit*100 if n else 0:.1f}%")
    print(f"评分方向准确率（|score|>=15）: " + "  ".join(acc))
    return {"tf": tf, **st, "orders": n_orders, "decisions": len(records)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--tf", default=None)
    args = ap.parse_args()
    tfs = args.tf.split(",") if args.tf else list(TFS)
    results = []
    for tf in tfs:
        res = run_tf(tf, args.refresh)
        if res:
            results.append(res)

    print(f"\n{'='*72}\n===== 汇总（LTC，生产策略，未计手续费/滑点） =====\n{'='*72}")
    print(f"{'周期':<4} {'成交':>5} {'胜率':>7} {'非亏损':>8} {'EV':>8} {'总利润':>9} {'回撤':>7} {'PF':>6}")
    for r in results:
        if not r["filled"]:
            print(f"{r['tf']:<4} 无成交")
            continue
        pf = f"{r['pf']:.2f}" if r["pf"] != float("inf") else "inf"
        print(f"{r['tf']:<4} {r['filled']:>5} {r['winrate']*100:>6.1f}% {r['nonloss']*100:>7.1f}% "
              f"{r['ev']:>+7.3f}R {r['totalR']:>+8.1f}R {r['maxdd']:>6.1f}R {pf:>6}")


if __name__ == "__main__":
    main()
