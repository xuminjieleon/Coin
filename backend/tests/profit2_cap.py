"""Capacity-constrained simulation: ONE position at a time per symbol (working
orders may be replaced by newer signals; a filled position blocks new entries
until it exits). This measures the profit a single account can actually
realize - the unconstrained totals double-count overlapping positions.

Run per threshold {25,20,15,10} x {4h,1d,1w}; folds A/B/C; blind = B+C.

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/profit2_cap.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import profit_sweep2 as ps
from profit2_r5 import with_loose_plans

GEOS = {
    "4h": ((0.75, 1.2, 0.5, None, 48, 0.5), 1.5),
    "1d": ((0.75, 1.5, 0.5, None, 24, 0.5), 1.5),
    "1w": ((0.75, 1.5, 0.5, None, 24, 0.75), 2.0),
}
THS = (25, 20, 15, 10)


def sim_outcome_full(df, i, direction, entry, stop, be_frac, tgt_r, texit, fill_bars, trail):
    """Same logic as ps.sim_outcome but returns (R, fill_bar, exit_bar) or None."""
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    n = len(df)
    long = direction == "long"
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    target = None
    if tgt_r is not None:
        target = entry + tgt_r * risk if long else entry - tgt_r * risk
    be_trig = entry + be_frac * risk if long else entry - be_frac * risk
    fill = None
    for j in range(i + 1, min(i + 1 + fill_bars, n)):
        if (long and lows[j] <= entry) or ((not long) and highs[j] >= entry):
            fill = j
            break
    if fill is None:
        return None
    be = False
    locked = 0.0
    ratchet = 0.0
    for j in range(fill, min(fill + texit, n)):
        if be and trail is not None:
            stop_lvl = entry + ratchet * risk if long else entry - ratchet * risk
        else:
            stop_lvl = entry if be else stop
        hit_stop = lows[j] <= stop_lvl if long else highs[j] >= stop_lvl
        if hit_stop:
            if not be:
                return (-1.0, fill, j)
            runner_r = ratchet if trail is not None else 0.0
            return (locked + 0.5 * runner_r, fill, j)
        if target is not None and (highs[j] >= target if long else lows[j] <= target):
            frac = 0.5 if be else 1.0
            return (locked + frac * tgt_r, fill, j)
        if not be and ((long and highs[j] >= be_trig) or ((not long) and lows[j] <= be_trig)):
            be = True
            locked = 0.5 * be_frac
        if be and trail is not None:
            mfe = (highs[j] - entry) / risk if long else (entry - lows[j]) / risk
            ratchet = max(ratchet, mfe - trail)
    j_end = min(fill + texit, n) - 1
    if j_end < fill:
        j_end = fill
    r = (closes[j_end] - entry) / risk if long else (entry - closes[j_end]) / risk
    if be:
        return (locked + 0.5 * r, fill, j_end)
    return (float(r), fill, j_end)


def capacity_eval(records, geo, dfs, tidx, tf, fill_mult=1.0):
    """Serial one-position-per-symbol execution."""
    depth, stopw, be_frac, tgt, texit, trail = geo
    cfg = ps.CFG[tf]
    fill_bars = max(1, int(round(cfg["fill_bars"] * fill_mult)))
    trades: list[tuple] = []  # (time, R) for time-ordered equity
    n_orders = 0
    busy: dict[str, int] = {s: -1 for s in ps.SYMBOLS}
    for r in records:  # time-ordered
        if r.get("plan") is None:
            continue
        sym = r["symbol"]
        df = dfs[sym][tf]
        i = tidx[sym][tf].get(r["time"])
        if i is None:
            continue
        if i <= busy[sym]:
            continue  # position open (or order from a later signal replaced)
        built = ps.build_plan(r, depth, stopw)
        if built is None:
            continue
        direction, entry, stop = built
        n_orders += 1
        out = sim_outcome_full(df, i, direction, entry, stop, be_frac, tgt, texit, fill_bars, trail)
        if out is None:
            continue  # unfilled: no block, next signal replaces
        rr, fill, exit_bar = out
        busy[sym] = exit_bar
        trades.append((r["time"], rr))
    if not trades:
        return {"orders": n_orders, "filled": 0, "winrate": float("nan"), "ev": float("nan"),
                "totalR": 0.0, "maxdd": float("nan"), "pf": float("nan")}
    arr = np.array([t[1] for t in trades], dtype=float)
    cum = np.cumsum(arr)
    peak = np.maximum.accumulate(np.concatenate(([0.0], cum)))[1:]
    maxdd = float(np.max(peak - cum))
    wins = arr[arr > 1e-9].sum()
    losses = -arr[arr < -1e-9].sum()
    pf = float(wins / losses) if losses > 1e-9 else float("inf")
    return {"orders": n_orders, "filled": len(arr), "winrate": float(np.mean(arr > 1e-9)),
            "ev": float(arr.mean()), "totalR": float(arr.sum()), "maxdd": maxdd, "pf": pf}


def fmt(res):
    if not res["filled"]:
        return "n=0"
    dd = f"{res['maxdd']:.1f}"
    pf = f"{res['pf']:.2f}" if res["pf"] != float("inf") else "inf"
    return (f"单={res['filled']}/{res['orders']} 胜率={res['winrate']*100:.1f}% "
            f"EV={res['ev']:+.3f}R 总={res['totalR']:+.1f}R DD={dd}R PF={pf}")


def main():
    for tf, (geo, fill) in GEOS.items():
        dfs = ps.load_dfs(tf, mtf1w=False)
        tidx = ps.make_tidx(dfs, tf)
        records = ps.load_records(tf, dfs, mtf1w=False, refresh=False)
        FA, FB, FC = ps.folds(records)
        print(f"\n===== {tf} 容量约束（单仓位/币种 串行）{ps.geo_str(geo)} fill×{fill} =====")
        for th in THS:
            mk = (lambda t: (lambda recs: recs if t == 25 else with_loose_plans(recs, t)))(th)
            ra = capacity_eval(mk(FA + FB), geo, dfs, tidx, tf, fill)
            rb = capacity_eval(mk(FB + FC), geo, dfs, tidx, tf, fill)
            print(f"  th={th} A+B[{fmt(ra)}]")
            print(f"        blind[{fmt(rb)}]")
        # per-symbol blind for th=15 and th=10 (report once)
        for th in (15, 10):
            mk = (lambda t: (lambda recs: recs if t == 25 else with_loose_plans(recs, t)))(th)
            blind = mk(FB + FC)
            for sym in ps.SYMBOLS:
                sub = [r for r in blind if r["symbol"] == sym]
                rr = capacity_eval(sub, geo, dfs, tidx, tf, fill)
                print(f"  th={th} {sym}: {fmt(rr)}")


if __name__ == "__main__":
    main()
