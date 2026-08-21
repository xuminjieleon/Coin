"""Walk-forward sweep v2 of trade-plan management (target: max non-loss rate,
anti-overfit, EV-constrained).

Mathematical prior (pre-declared): with plain limit fills, P(full loss) >=
f/(1+f) for BE trigger f (R units) under zero drift; 99% non-loss requires
f ~= 0.01 which collapses EV toward 0. The legitimate frontier-shifter is
SCALE-OUT: exit half the position at the BE trigger (+f R locked), move stop
to entry, run the remainder to the target.

Pre-declared protocol (fixed before looking at results):
  - Folds by time: A = 40% (tune), B = 30% (blind), C = 30% (blind).
  - Phase 1: tune A -> validate B once. Phase 2: re-tune A+B -> validate C once.
  - Coarse grid (96 cells): depth {0.75, 1.0} x stop {1.5, 2.0, 2.5} ATR x
    be {0.05, 0.1, 0.15, 0.25} R x tgt {0.75, 1.0} R x scaleout {off, on};
    time exit 96 bars, timeouts marked to market.
  - Guards (all stages): filled >= 120 (grid) / >= 100 (gate stage),
    fill rate >= 40%, avgR >= +0.05.
  - Selection: max non-loss rate, ties broken by higher avgR.
  - Gates: all / aligned25 / ranging / not_expanded (top-3 geometries only).
  - Report: top-10 frontier cells, monotonicity neighbors, thinning, per-symbol.

Usage:
  PYTHONIOENCODING=utf-8 python tests/plan_sweep2.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import backtest_decision as bt

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
TOTAL = 17000
POINTS = 1000
FILL_BARS = 24
TEXIT = 96

DEPTHS = (0.75, 1.0)
STOPS = (1.5, 2.0, 2.5)
BES = (0.05, 0.1, 0.15, 0.25)
TGTS = (0.75, 1.0)
SCALES = (False, True)

BASELINE = (0.75, 1.5, 0.25, 0.75, False)  # current production geometry

GATES = {
    "all": lambda r: True,
    "aligned25": lambda r: abs(r["score"]) >= 25 and r["alignment"] == "aligned",
    "ranging": lambda r: r["regime"] == "ranging",
    "not_expanded": lambda r: r.get("vol_state") != "expanded",
}

DFS = {}
TIME_INDEX = {}


def build_plan(rec: dict, depth: float, stopw: float):
    plan = rec.get("plan")
    if plan is None or not rec.get("atr"):
        return None
    long = plan["direction"] == "long"
    price, atr = rec["price"], rec["atr"]
    if long:
        zone_top = rec.get("zone_bull_top")
        if zone_top and price - zone_top <= depth * atr:
            entry = min(price, zone_top)
        else:
            entry = price - depth * atr
        stop = entry - stopw * atr
    else:
        zone_bottom = rec.get("zone_bear_bottom")
        if zone_bottom and zone_bottom - price <= depth * atr:
            entry = max(price, zone_bottom)
        else:
            entry = price + depth * atr
        stop = entry + stopw * atr
    return ("long" if long else "short"), float(entry), float(stop)


def sim_outcome(df, i: int, direction: str, entry: float, stop: float,
                be_frac: float, tgt_r: float, texit: int, scaleout: bool):
    """Outcome in R multiples. Scale-out: half exits at the BE trigger
    (locking +0.5*be_frac R), stop -> entry, remainder runs to tgt_r.
    Conservative same-bar ordering: stop > target > BE-trigger."""
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    n = len(df)
    long = direction == "long"
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    target = entry + tgt_r * risk if long else entry - tgt_r * risk
    be_trig = entry + be_frac * risk if long else entry - be_frac * risk
    fill = None
    for j in range(i + 1, min(i + 1 + FILL_BARS, n)):
        if (long and lows[j] <= entry) or ((not long) and highs[j] >= entry):
            fill = j
            break
    if fill is None:
        return "nofill"
    be = False
    locked = 0.0
    for j in range(fill, min(fill + texit, n)):
        stop_lvl = entry if be else stop
        hit_stop = lows[j] <= stop_lvl if long else highs[j] >= stop_lvl
        hit_tg = highs[j] >= target if long else lows[j] <= target
        if hit_stop:
            return -1.0 if not be else locked  # runner stopped at entry
        if hit_tg:
            # gap straight to target without BE set: count full position (rare, conservative)
            frac = 0.5 if (scaleout and be) else 1.0
            return locked + frac * tgt_r
        if not be and ((long and highs[j] >= be_trig) or ((not long) and lows[j] <= be_trig)):
            be = True
            if scaleout:
                locked = 0.5 * be_frac
    j_end = min(fill + texit, n) - 1
    if j_end < fill:
        j_end = fill
    r = (closes[j_end] - entry) / risk if long else (entry - closes[j_end]) / risk
    if scaleout and be:
        return locked + 0.5 * r
    return float(r)


def evaluate(records, geo, gate_name):
    depth, stopw, be, tgt, scaleout = geo
    gfn = GATES[gate_name]
    outcomes = []
    n_planned = 0
    for r in records:
        if r.get("plan") is None or not gfn(r):
            continue
        n_planned += 1
        built = build_plan(r, depth, stopw)
        if built is None:
            continue
        direction, entry, stop = built
        df = DFS.get(r["symbol"])
        i = TIME_INDEX.get(r["symbol"], {}).get(r["time"])
        if df is None or i is None:
            continue
        out = sim_outcome(df, i, direction, entry, stop, be, tgt, TEXIT, scaleout)
        if out != "nofill" and out is not None:
            outcomes.append(out)
    if not outcomes:
        return {"n": n_planned, "filled": 0, "nonloss": float("nan"), "avgR": float("nan"),
                "fillrate": 0.0}
    arr = np.array(outcomes, dtype=float)
    return {
        "n": n_planned,
        "filled": len(arr),
        "nonloss": float(np.mean(arr >= -1e-9)),
        "avgR": float(arr.mean()),
        "fillrate": len(arr) / max(n_planned, 1),
    }


def fmt(res):
    if not res["filled"]:
        return "n=0"
    return (f"n={res['filled']}/{res['n']} fill={res['fillrate']*100:.0f}% "
            f"非亏损={res['nonloss']*100:.1f}% EV={res['avgR']:+.3f}R")


def geo_str(geo):
    depth, stopw, be, tgt, scaleout = geo
    return (f"depth={depth} stop={stopw} be={be} tgt={tgt} "
            f"{'scaleout' if scaleout else 'plain'}")


def passes(res, min_filled):
    return (res["filled"] >= min_filled and res["fillrate"] >= 0.40
            and res["avgR"] >= 0.05)


def tune(records, tag):
    print(f"\n----- 调参段 [{tag}]：几何网格（gate=all，96 格）-----")
    cells = []
    for depth in DEPTHS:
        for stopw in STOPS:
            for be in BES:
                for tgt in TGTS:
                    for sc in SCALES:
                        geo = (depth, stopw, be, tgt, sc)
                        res = evaluate(records, geo, "all")
                        cells.append((geo, res, passes(res, 120)))
    ok = [(g, r) for g, r, p in cells if p]
    ok.sort(key=lambda x: (-x[1]["nonloss"], -x[1]["avgR"]))
    print(f"  满足约束的格子: {len(ok)}/96；Top-10（非亏损率/EV 边界）：")
    for g, r in ok[:10]:
        print(f"  {geo_str(g)}: {fmt(r)}")
    if not ok:
        raise SystemExit("no grid cell satisfied constraints on tuning fold")
    # frontier transparency: also show best-EV cells for comparison
    by_ev = sorted(ok, key=lambda x: -x[1]["avgR"])[:5]
    print("  按 EV 最高的 5 格（参照）：")
    for g, r in by_ev:
        print(f"  {geo_str(g)}: {fmt(r)}")

    top3 = ok[:3]
    print(f"----- 调参段 [{tag}]：门控阶段（top3 几何 x 4 门控）-----")
    cand = []
    for g, _ in top3:
        for gname in GATES:
            res = evaluate(records, g, gname)
            if passes(res, 100):
                print(f"  {geo_str(g)} x {gname}: {fmt(res)}")
                cand.append((res["nonloss"], res["avgR"], g, gname))
    cand.sort(key=lambda x: (-x[0], -x[1]))
    if not cand:
        return top3[0][0], "all"
    return cand[0][2], cand[0][3]


def monotonic_check(records, geo, gate="all"):
    """Evaluate axis-neighbors of the chosen geometry on the tuning fold."""
    depth, stopw, be, tgt, sc = geo
    neighbors = []
    for sw in STOPS:
        if sw != stopw:
            neighbors.append((depth, sw, be, tgt, sc))
    for bf in BES:
        if bf != be:
            neighbors.append((depth, stopw, bf, tgt, sc))
    print(f"  邻格单调性（选定格 {geo_str(geo)}）：")
    base = evaluate(records, geo, gate)
    print(f"    chosen: {fmt(base)}")
    for g in neighbors:
        r = evaluate(records, g, gate)
        print(f"    {geo_str(g)}: {fmt(r)}")


def main():
    global DFS, TIME_INDEX
    for sym in SYMBOLS:
        DFS[sym] = asyncio.run(bt.fetch_klines_1h(sym, TOTAL))
    TIME_INDEX = {sym: {int(t): i for i, t in enumerate(DFS[sym]["time"].to_numpy())} for sym in DFS}
    records = bt.load_records(SYMBOLS, POINTS, TOTAL, False, DFS)
    records.sort(key=lambda r: r["time"])
    a = int(len(records) * 0.4)
    b = int(len(records) * 0.7)
    FA, FB, FC = records[:a], records[a:b], records[b:]
    print(f"folds: A={len(FA)} B={len(FB)} C={len(FC)} (time-ordered)")

    print("\n===== 基线（当前生产几何 = v1 走样本配置）=====")
    for name, fold in (("A", FA), ("B", FB), ("C", FC)):
        res = evaluate(fold, BASELINE, "all")
        print(f"  fold {name}: {fmt(res)}")

    # phase 1
    geo1, gate1 = tune(FA, "A")
    print(f"\n[Phase1 选定] {geo_str(geo1)} x {gate1}")
    monotonic_check(FA, geo1, gate1)
    res_b = evaluate(FB, geo1, gate1)
    print(f"[Phase1 盲测 B] {fmt(res_b)}")

    # phase 2
    geo2, gate2 = tune(FA + FB, "A+B")
    print(f"\n[Phase2 选定] {geo_str(geo2)} x {gate2}")
    monotonic_check(FA + FB, geo2, gate2)
    res_c = evaluate(FC, geo2, gate2)
    print(f"[Phase2 盲测 C] {fmt(res_c)}")

    print("\n===== 抽稀独立性校验（C 段，Phase2 配置）=====")
    for thin in (1, 4, 8):
        sub = FC[::thin]
        res = evaluate(sub, geo2, gate2)
        print(f"  1/{thin}: {fmt(res)}")

    print("===== 分币种（C 段，Phase2 配置）=====")
    for sym in SYMBOLS:
        sub = [r for r in FC if r["symbol"] == sym]
        res = evaluate(sub, geo2, gate2)
        print(f"  {sym}: {fmt(res)}")

    # combined blind folds B+C for the final headline
    print("\n===== 合并盲测段 B+C（Phase2 配置，仅报告）=====")
    res_bc = evaluate(FB + FC, geo2, gate2)
    print(f"  B+C: {fmt(res_bc)}")


if __name__ == "__main__":
    main()
