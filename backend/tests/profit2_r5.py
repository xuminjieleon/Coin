"""R5: plan-generation threshold test. Records carry plan direction only for
|score|>=25 or CVD confluence. Synthesizing plans for 20<=|score|<25 (direction
= sign of score) tests whether loosening the gate adds profit under the new
geometry. Selection on A+B; blind B+C + per-symbol for the winner.

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/profit2_r5.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import copy

import profit_sweep2 as ps

GEOS = {
    "4h": ((0.75, 1.2, 0.5, None, 48, 0.5), 1.5),
    "1d": ((0.75, 1.5, 0.5, None, 24, 0.5), 1.5),
    "1w": ((0.75, 1.5, 0.5, None, 24, 0.75), 2.0),
}


def with_loose_plans(records, th):
    """Extend records: plans for |score|>=th even without original plan."""
    out = []
    for r in records:
        r2 = dict(r)
        if r2.get("plan") is None and abs(r2["score"]) >= th:
            r2["plan"] = "long" if r2["score"] > 0 else "short"
            r2["_loose"] = True
        out.append(r2)
    return out


def main():
    for tf, (geo, fill) in GEOS.items():
        dfs = ps.load_dfs(tf, mtf1w=False)
        tidx = ps.make_tidx(dfs, tf)
        records = ps.load_records(tf, dfs, mtf1w=False, refresh=False)
        FA, FB, FC = ps.folds(records)
        print(f"\n===== {tf} R5 计划阈值 {ps.geo_str(geo)} fill×{fill} =====")
        for th in (25, 20):
            recs = records if th == 25 else with_loose_plans(records, th)
            ra = ps.evaluate(FA + FB, geo, "base", dfs, tidx, tf, fill)
            rt = ps.evaluate(FA + FB, geo, "base", dfs, tidx, tf, fill) if th == 25 else None
            # A+B with loose records
            faplus = with_loose_plans(FA + FB, th) if th != 25 else FA + FB
            ra2 = ps.evaluate(faplus, geo, "base", dfs, tidx, tf, fill)
            print(f"  th={th} A+B: {ps.fmt(ra2)}")
        # blind for both thresholds (report once each)
        for th in (25, 20):
            blind = with_loose_plans(FB + FC, th) if th != 25 else FB + FC
            rb = ps.evaluate(blind, geo, "base", dfs, tidx, tf, fill)
            print(f"  th={th} blind B+C: {ps.fmt(rb)}")
            for sym in ps.SYMBOLS:
                sub = [r for r in with_loose_plans(records, th) if r["symbol"] == sym] if th != 25 else \
                      [r for r in records if r["symbol"] == sym]
                rr = ps.evaluate(sub, geo, "base", dfs, tidx, tf, fill)
                print(f"    {sym}: {ps.fmt(rr)}")
        # EV of the marginal (loose-only) trades on A+B
        loose_only = [r for r in with_loose_plans(FA + FB, 20) if r.get("_loose")]
        rl = ps.evaluate(loose_only, geo, "base", dfs, tidx, tf, fill)
        print(f"  边际交易（20<=|score|<25，A+B）单独: {ps.fmt(rl)}")


if __name__ == "__main__":
    main()
