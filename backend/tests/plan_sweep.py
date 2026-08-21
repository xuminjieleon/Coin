"""Walk-forward sweep of trade-plan management geometry (anti-overfit protocol).

Goal: raise the BE-managed plan non-loss rate toward 90% WITHOUT gaming the
metric (EV must stay >= +0.05R per filled trade).

Pre-declared protocol (fixed before looking at results):
  - Folds by time: A = first 40% (tune), B = next 30% (validate), C = last 30%.
  - Phase 1: tune on A, validate once on B.
  - Phase 2: re-tune on A+B, validate once on C. Production ships phase-2 config.
  - Coarse grid (36 cells): pullback depth {0.5, 0.75} ATR x stop width {1.0, 1.5}
    ATR x BE trigger {0.25, 0.35, 0.5} R x target {0.75, 1.0, 1.5} R,
    time exit fixed at 96 bars (marked-to-market at close, stricter than
    the old accounting which dropped timeouts).
  - Gates evaluated only for the top-3 geometries: all / aligned25 / ranging /
    not_expanded (12 cells per phase).
  - Selection rule: max non-loss rate subject to filled >= 120 (grid) / >= 100
    (gate stage) and avgR >= +0.05; ties broken by higher avgR.
  - Timeout exits are marked to market (outcome can be win/scratch/loss).

Usage:
  PYTHONIOENCODING=utf-8 python tests/plan_sweep.py
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

# pre-declared coarse grid
DEPTHS = (0.5, 0.75)
STOPS = (1.0, 1.5)
BES = (0.25, 0.35, 0.5)
TARGETS = (0.75, 1.0, 1.5)
TEXIT = 96

BASELINE = {"depth": 0.5, "stopw": 1.0, "be": 0.5, "tgt": 1.0, "texit": 168}


def build_plan(rec: dict, depth: float, stopw: float) -> tuple[str, float, float] | None:
    """Reconstruct (direction, entry, stop) with the given geometry."""
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
                be_frac: float, tgt_r: float, texit: int):
    """Return outcome in R multiples (win=+tgt, scratch=0, loss=-1, timeout=MTM)."""
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
    for j in range(fill, min(fill + texit, n)):
        stop_lvl = entry if be else stop
        hit_stop = lows[j] <= stop_lvl if long else highs[j] >= stop_lvl
        hit_tg = highs[j] >= target if long else lows[j] <= target
        if hit_stop:
            return 0.0 if be else -1.0
        if hit_tg:
            return tgt_r
        if not be and ((long and highs[j] >= be_trig) or ((not long) and lows[j] <= be_trig)):
            be = True
    j_end = min(fill + texit, n) - 1
    if j_end < fill:
        j_end = fill
    r = (closes[j_end] - entry) / risk if long else (entry - closes[j_end]) / risk
    return float(r)


GATES = {
    "all": lambda r: True,
    "aligned25": lambda r: abs(r["score"]) >= 25 and r["alignment"] == "aligned",
    "ranging": lambda r: r["regime"] == "ranging",
    "not_expanded": lambda r: r.get("vol_state") != "expanded",
}


def evaluate(records, dfs, time_index, geo, gate_name):
    depth, stopw, be, tgt, texit = geo
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
        df = dfs.get(r["symbol"])
        i = time_index.get(r["symbol"], {}).get(r["time"])
        if df is None or i is None:
            continue
        out = sim_outcome(df, i, direction, entry, stop, be, tgt, texit)
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


def tune(records, tag):
    """Fixed selection rule on the tuning fold. Returns chosen (geo, gate)."""
    print(f"\n----- 调参段 [{tag}]：几何网格（gate=all）-----")
    grid_results = []
    for depth in DEPTHS:
        for stopw in STOPS:
            for be in BES:
                for tgt in TARGETS:
                    geo = (depth, stopw, be, tgt, TEXIT)
                    res = evaluate(records, DFS, TIME_INDEX, geo, "all")
                    ok = res["filled"] >= 120 and res["avgR"] >= 0.05
                    grid_results.append((res["nonloss"] if ok else -1, res["avgR"] if ok else -9,
                                         geo, res, ok))
                    if ok:
                        print(f"  depth={depth} stop={stopw} be={be} tgt={tgt}: {fmt(res)}")
    grid_results.sort(key=lambda x: (-x[0], -x[1]))
    top3 = [g for g in grid_results[:3] if g[4]]
    if not top3:
        raise SystemExit("no grid cell satisfied constraints on tuning fold")
    print(f"----- 调参段 [{tag}]：门控阶段（top3 几何 x 4 门控）-----")
    cand = []
    for _, _, geo, _, _ in top3:
        for gname in GATES:
            res = evaluate(records, DFS, TIME_INDEX, geo, gname)
            ok = res["filled"] >= 100 and res["avgR"] >= 0.05
            if ok:
                print(f"  {geo} x {gname}: {fmt(res)}")
                cand.append((res["nonloss"], res["avgR"], geo, gname))
    cand.sort(key=lambda x: (-x[0], -x[1]))
    if not cand:
        _, _, geo, _ = top3[0]
        return geo, "all"
    return cand[0][2], cand[0][3]


def main():
    global DFS, TIME_INDEX
    DFS = {}
    for sym in SYMBOLS:
        DFS[sym] = asyncio.run(bt.fetch_klines_1h(sym, TOTAL))
    TIME_INDEX = {sym: {int(t): i for i, t in enumerate(DFS[sym]["time"].to_numpy())} for sym in DFS}
    records = bt.load_records(SYMBOLS, POINTS, TOTAL, False, DFS)
    records.sort(key=lambda r: r["time"])
    a = int(len(records) * 0.4)
    b = int(len(records) * 0.7)
    FA, FB, FC = records[:a], records[a:b], records[b:]
    print(f"folds: A={len(FA)} B={len(FB)} C={len(FC)} (time-ordered)")

    # baseline under stricter MTM accounting
    print("\n===== 基线（当前生产几何，严格市价结算超时单）=====")
    for name, fold in (("A", FA), ("B", FB), ("C", FC)):
        geo = (BASELINE["depth"], BASELINE["stopw"], BASELINE["be"], BASELINE["tgt"], BASELINE["texit"])
        res = evaluate(fold, DFS, TIME_INDEX, geo, "all")
        print(f"  fold {name}: {fmt(res)}")

    # phase 1: tune A -> validate B
    geo1, gate1 = tune(FA, "A")
    print(f"\n[Phase1 选定] geo={geo1} gate={gate1}")
    res_b = evaluate(FB, DFS, TIME_INDEX, geo1, gate1)
    print(f"[Phase1 盲测 B] {fmt(res_b)}")

    # phase 2: tune A+B -> validate C
    geo2, gate2 = tune(FA + FB, "A+B")
    print(f"\n[Phase2 选定] geo={geo2} gate={gate2}")
    res_c = evaluate(FC, DFS, TIME_INDEX, geo2, gate2)
    print(f"[Phase2 盲测 C] {fmt(res_c)}")

    # thinning check on C with phase-2 config
    print("\n===== 抽稀独立性校验（C 段，Phase2 配置）=====")
    for thin in (1, 4, 8):
        sub = FC[::thin]
        res = evaluate(sub, DFS, TIME_INDEX, geo2, gate2)
        print(f"  1/{thin}: {fmt(res)}")

    # per-symbol on C
    print("===== 分币种（C 段，Phase2 配置）=====")
    for sym in SYMBOLS:
        sub = [r for r in FC if r["symbol"] == sym]
        res = evaluate(sub, DFS, TIME_INDEX, geo2, gate2)
        print(f"  {sym}: {fmt(res)}")


if __name__ == "__main__":
    main()
