"""R4 final round: new geometry x fill-window x gate interaction (A+B selection),
blind validation of the chosen production candidates. Last use of the blind folds.

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/profit2_r4.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import profit_sweep2 as ps

GEO_4H = (0.75, 1.2, 0.5, None, 48, 0.5)
GEO_1D = (0.75, 1.5, 0.5, None, 24, 0.5)
GEO_1W = (0.75, 1.5, 0.5, None, 24, 0.75)


def main():
    for tf, geo, fills, gates in (
        ("4h", GEO_4H, (1.0, 1.5, 2.0), ("base", "trend", "noexp")),
        ("1d", GEO_1D, (1.0, 1.5, 2.0), ("base", "trend", "noexp")),
    ):
        dfs = ps.load_dfs(tf, mtf1w=False)
        tidx = ps.make_tidx(dfs, tf)
        records = ps.load_records(tf, dfs, mtf1w=False, refresh=False)
        FA, FB, FC = ps.folds(records)
        print(f"\n===== {tf} R4：{ps.geo_str(geo)} × fill × gate（A+B 选择）=====")
        for fm in fills:
            for g in gates:
                res = ps.evaluate(FA + FB, geo, g, dfs, tidx, tf, fm)
                print(f"  fill×{fm} {g:<6}: {ps.fmt(res)}")
        # blind validation: A+B winners (printed after selection above only)
        print(f"-- {tf} 盲测（A+B 最优组合 + 参照）--")
        for fm, g in ((1.0, "base"), (1.5, "base")):
            rbc = ps.evaluate(FB + FC, geo, g, dfs, tidx, tf, fm)
            print(f"  blind fill×{fm} {g}: {ps.fmt(rbc)}")
            for sym in ps.SYMBOLS:
                sub = [r for r in records if r["symbol"] == sym]
                r = ps.evaluate(sub, geo, g, dfs, tidx, tf, fm)
                print(f"    {sym}: {ps.fmt(r)}")

    # 1w final confirmation with full fold breakdown (already selected)
    dfs = ps.load_dfs("1w", mtf1w=False)
    tidx = ps.make_tidx(dfs, "1w")
    records = ps.load_records("1w", dfs, mtf1w=False, refresh=False)
    FA, FB, FC = ps.folds(records)
    print(f"\n===== 1w 最终确认 {ps.geo_str(GEO_1W)} fill×2 =====")
    for name, fold in (("A", FA), ("B", FB), ("C", FC)):
        res = ps.evaluate(fold, GEO_1W, "base", dfs, tidx, "1w", 2.0)
        print(f"  fold {name}: {ps.fmt(res)}")
    rbc = ps.evaluate(FB + FC, GEO_1W, "base", dfs, tidx, "1w", 2.0)
    print(f"  blind B+C: {ps.fmt(rbc)}")


if __name__ == "__main__":
    main()
