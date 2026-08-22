"""Sensitivity check around the R2 winners (plateau vs knife-edge) and 1w
fill-window mechanics test. Reports tune-fold (A+B) values for neighbor cells.

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/profit2_sens.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import profit_sweep2 as ps


def load_all():
    dfs: dict = {}
    tidx: dict = {}
    recs: dict = {}
    for tf in ("4h", "1d", "1w"):
        dfs[tf] = ps.load_dfs(tf, mtf1w=False)
        t = {}
        for sym in ps.SYMBOLS:
            t[sym] = {k: {int(x): i for i, x in enumerate(dfs[tf][sym][k]["time"].to_numpy())}
                      for k in dfs[tf][sym]}
        tidx[tf] = t
        recs[tf] = ps.load_records(tf, dfs[tf], mtf1w=False, refresh=False)
    return dfs, tidx, recs


def sens_table(tf, records, dfs, tidx, base_geo, gate, axes):
    FA, FB, FC = ps.folds(records)
    tune = FA + FB
    print(f"\n===== {tf} 敏感性（gate={gate}, base={ps.geo_str(base_geo)}，调参段 A+B）=====")
    for name, idx, values in axes:
        print(f"-- axis {name} --")
        for v in values:
            geo = list(base_geo)
            geo[idx] = v
            res = ps.evaluate(tune, tuple(geo), gate, dfs, tidx, tf)
            mark = " <-base" if v == base_geo[idx] else ""
            print(f"  {name}={v}: {ps.fmt(res)}{mark}")


def main():
    dfs, tidx, recs = load_all()

    # 4h winner sensitivity: trail and stop axes
    g4 = (0.75, 1.5, 0.5, None, 48, 0.75)
    sens_table("4h", recs["4h"], dfs["4h"], tidx["4h"], g4, "base", [
        ("trail", 5, (0.5, 0.75, 1.0, 1.5)),
        ("stop", 1, (1.2, 1.5, 1.8, 2.0)),
        ("depth", 0, (0.5, 0.75, 0.9, 1.0)),
        ("be", 2, (0.25, 0.5, 0.75)),
        ("texit", 4, (24, 48, 96)),
    ])

    # 1d winner sensitivity
    g1d = (0.75, 1.5, 0.5, 5.0, 24, 0.75)
    sens_table("1d", recs["1d"], dfs["1d"], tidx["1d"], g1d, "base", [
        ("trail", 5, (0.5, 0.75, 1.0, 1.5)),
        ("stop", 1, (1.2, 1.5, 1.8, 2.0)),
        ("depth", 0, (0.5, 0.75, 0.9, 1.0)),
        ("tgt", 3, (2.0, 3.0, 5.0, None)),
        ("texit", 4, (12, 24, 48)),
    ])

    # 1w: fill window x2 mechanics + trend gate on incumbent AND new-family geometry
    print("\n===== 1w 成交窗口与几何假设（调参段 A+B / 盲测段 B+C）=====")
    FA, FB, FC = ps.folds(recs["1w"])
    for label, geo in (
        ("incumbent", (1.0, 2.0, 0.5, 3.0, 12, None)),
        ("new-family", (0.75, 1.5, 0.5, None, 48, 0.75)),
        ("new-family-te24", (0.75, 1.5, 0.5, None, 24, 0.75)),
    ):
        for fill_mult, gname in ((1.0, "base"), (2.0, "base"), (2.0, "trend")):
            rt = ps.evaluate(FA + FB, geo, gname, dfs["1w"], tidx["1w"], "1w", fill_mult)
            rb = ps.evaluate(FB + FC, geo, gname, dfs["1w"], tidx["1w"], "1w", fill_mult)
            print(f"  {label:<15} fill×{fill_mult:.0f} {gname:<6} A+B[{ps.fmt(rt)}]")
            print(f"  {'':<15}                blind  [{ps.fmt(rb)}]")
        # per-symbol for the fillx2 base
        print(f"  -- {label} fill×2 base 分币种（全时段）--")
        for sym in ps.SYMBOLS:
            sub = [r for r in recs["1w"] if r["symbol"] == sym]
            r = ps.evaluate(sub, geo, "base", dfs["1w"], tidx["1w"], "1w", 2.0)
            print(f"     {sym}: {ps.fmt(r)}")


if __name__ == "__main__":
    main()
