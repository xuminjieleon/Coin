"""Leave-one-symbol-out (LOSO) validation of the v2 plan-management frontier.

Why LOSO: the walk-forward blind folds (B/C) have already been inspected, so
re-selecting on the same time split would be contaminated. Cross-ASSET
validation asks a harder generalization question: does the management
geometry chosen on two symbols transfer to a third?

Pre-declared (fixed before looking at results):
  - Selection per fold: 96-cell grid (same as plan_sweep2), gate = all only
    (the ranging gate's tuning EV sat at the floor with n~160 -> small-sample
    trap per walk-forward blind results; dropped).
  - Guards on the training pair: filled >= 150, fill rate >= 40%, EV >= +0.05R.
  - Rule: max non-loss, tie -> higher EV. Blind result = the held-out symbol.
  - Also report a fixed candidate list (frontier cells + current production)
    on each held-out symbol for transparency.

Usage:
  PYTHONIOENCODING=utf-8 python tests/loso_validation.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest_decision as bt
import plan_sweep2 as ps2

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
TOTAL = 17000
POINTS = 1000

# fixed candidates for frontier transparency (from walk-forward top cells)
CANDIDATES = [
    ("current_production", (0.75, 1.5, 0.25, 0.75, False)),
    ("be05_plain", (0.75, 2.5, 0.05, 0.75, False)),
    ("be05_scale", (0.75, 2.5, 0.05, 0.75, True)),
    ("be10_scale", (0.75, 2.5, 0.1, 0.75, True)),
    ("be10_plain", (0.75, 2.5, 0.1, 0.75, False)),
    ("stop20_be10_scale", (0.75, 2.0, 0.1, 0.75, True)),
]


def select_geo(train_records):
    best = None
    for depth in ps2.DEPTHS:
        for stopw in ps2.STOPS:
            for be in ps2.BES:
                for tgt in ps2.TGTS:
                    for sc in ps2.SCALES:
                        geo = (depth, stopw, be, tgt, sc)
                        res = ps2.evaluate(train_records, geo, "all")
                        if (res["filled"] >= 150 and res["fillrate"] >= 0.40
                                and res["avgR"] >= 0.05):
                            key = (res["nonloss"], res["avgR"])
                            if best is None or key > best[0]:
                                best = (key, geo, res)
    return (best[1], best[2]) if best else (None, None)


def main():
    DFS = {}
    for sym in SYMBOLS:
        DFS[sym] = asyncio.run(bt.fetch_klines_1h(sym, TOTAL))
    ps2.DFS = DFS
    ps2.TIME_INDEX = {
        sym: {int(t): i for i, t in enumerate(DFS[sym]["time"].to_numpy())} for sym in DFS
    }
    records = bt.load_records(SYMBOLS, POINTS, TOTAL, False, DFS)
    records.sort(key=lambda r: r["time"])

    print("===== LOSO：留一币种交叉验证（门控固定为 all）=====")
    chosen_geos = []
    for held_out in SYMBOLS:
        train = [r for r in records if r["symbol"] != held_out]
        blind = [r for r in records if r["symbol"] == held_out]
        geo, tune_res = select_geo(train)
        chosen_geos.append(geo)
        blind_res = ps2.evaluate(blind, geo, "all") if geo else None
        print(f"\n-- 留出 {held_out}（训练 = 其余两币种 {len(train)} 点，盲测 = {held_out} {len(blind)} 点）--")
        print(f"   训练段选定: {ps2.geo_str(geo)}")
        print(f"   训练段: {ps2.fmt(tune_res)}")
        print(f"   盲测段: {ps2.fmt(blind_res)}")

    print("\n===== LOSO 选定几何一致性 =====")
    for g in set(chosen_geos):
        print(f"  {ps2.geo_str(g)}  被选 {chosen_geos.count(g)}/3 折")

    print("\n===== 固定候选在三个留出币种上的表现（非亏损率 / EV）=====")
    header = f"{'候选':<22}" + "".join(f"{s:>26}" for s in SYMBOLS)
    print(header)
    for name, geo in CANDIDATES:
        parts = []
        for held_out in SYMBOLS:
            blind = [r for r in records if r["symbol"] == held_out]
            res = ps2.evaluate(blind, geo, "all")
            if res["filled"]:
                parts.append(f"{res['nonloss']*100:.1f}%/{res['avgR']:+.3f}R(n={res['filled']})")
            else:
                parts.append("--")
        print(f"{name:<22}" + "".join(f"{p:>26}" for p in parts))

    # pooled blind for consensus candidate evaluation
    print("\n===== 合并三币种（全部 3000 点，含训练+盲测混合，仅参照）=====")
    for name, geo in CANDIDATES:
        res = ps2.evaluate(records, geo, "all")
        print(f"  {name:<22} {ps2.fmt(res)}")


if __name__ == "__main__":
    main()
