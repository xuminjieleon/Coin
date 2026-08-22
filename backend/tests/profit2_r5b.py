"""R5b: extend the threshold sweep to {25, 20, 15, 10}; selection on A+B,
blind validation once for the A+B winner. 15 is the natural boundary (bias
definition); below that the direction sign is noise.

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/profit2_r5b.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import profit_sweep2 as ps
from profit2_r5 import with_loose_plans

GEOS = {
    "4h": ((0.75, 1.2, 0.5, None, 48, 0.5), 1.5),
    "1d": ((0.75, 1.5, 0.5, None, 24, 0.5), 1.5),
    "1w": ((0.75, 1.5, 0.5, None, 24, 0.75), 2.0),
}
THS = (25, 20, 15, 10)


def main():
    for tf, (geo, fill) in GEOS.items():
        dfs = ps.load_dfs(tf, mtf1w=False)
        tidx = ps.make_tidx(dfs, tf)
        records = ps.load_records(tf, dfs, mtf1w=False, refresh=False)
        FA, FB, FC = ps.folds(records)
        print(f"\n===== {tf} R5b 阈值扫描 {ps.geo_str(geo)} fill×{fill}（A+B 选择）=====")
        best_th, best_r = 25, None
        for th in THS:
            recs = FA + FB if th == 25 else with_loose_plans(FA + FB, th)
            res = ps.evaluate(recs, geo, "base", dfs, tidx, tf, fill)
            ok = ps.passes(res, ps.MIN_FILLED[tf] // 2)
            print(f"  th={th}: {'OK ' if ok else '   '}{ps.fmt(res)}")
            if ok and (best_r is None or res["totalR"] > best_r["totalR"]):
                best_th, best_r = th, res
        print(f"  [A+B 选定] th={best_th}")
        # blind for the winner (single shot per tf)
        blind = FB + FC if best_th == 25 else with_loose_plans(FB + FC, best_th)
        rb = ps.evaluate(blind, geo, "base", dfs, tidx, tf, fill)
        print(f"  blind B+C th={best_th}: {ps.fmt(rb)}")
        for sym in ps.SYMBOLS:
            sub = [r for r in records if r["symbol"] == sym]
            if best_th != 25:
                sub = with_loose_plans(sub, best_th)
            rr = ps.evaluate(sub, geo, "base", dfs, tidx, tf, fill)
            print(f"    {sym}: {ps.fmt(rr)}")
        # thinning on C for the winner
        for thin in (1, 2, 4):
            sub = FC[::thin]
            if best_th != 25:
                sub = with_loose_plans(sub, best_th)
            rr = ps.evaluate(sub, geo, "base", dfs, tidx, tf, fill)
            print(f"    抽稀 1/{thin}: {ps.fmt(rr)}")


if __name__ == "__main__":
    main()
