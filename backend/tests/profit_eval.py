"""Supplementary evaluation: specific geometries on 1d blind folds (B/C)
and per-symbol, plus 4h winner sanity check. Reuses profit_sweep cache.

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/profit_eval.py
"""
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio

import profit_sweep as ps

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_profit_cache.pkl")

CANDIDATES = [
    ("scale05_2.0_tgt3_te96", (1.0, 2.0, "scale05", 3.0, 96), "all"),
    ("scale05_2.0_tgt3_te24", (1.0, 2.0, "scale05", 3.0, 24), "all"),
    ("scale05_2.0_tgt1.5_te96", (1.0, 2.0, "scale05", 1.5, 96), "all"),
    ("be05_2.0_tgt3_te96", (1.0, 2.0, "be05", 3.0, 96), "all"),
    ("baseline(scale01)", ps.BASELINE, "all"),
]


def main():
    with open(CACHE, "rb") as f:
        cache = pickle.load(f)

    dfs: dict = {}
    for sym in ps.SYMBOLS:
        dfs[sym] = {}
        for itv in ("4h", "1d"):
            rows = asyncio.run(ps.kline_cache.get_klines(sym, itv, ps.TWO_YEARS[itv]))
            dfs[sym][itv] = ps.kline_cache.rows_to_df(rows)
    tidx: dict = {}
    for sym in ps.SYMBOLS:
        tidx[sym] = {}
        for k in dfs[sym]:
            tidx[sym][k] = {int(t): i for i, t in enumerate(dfs[sym][k]["time"].to_numpy())}

    for itv in ("1d", "4h"):
        records = cache[itv]["records"]
        a = int(len(records) * 0.4)
        b = int(len(records) * 0.7)
        FA, FB, FC = records[:a], records[a:b], records[b:]
        print(f"\n===== {itv} 补充评估（B/C 盲测段 + 分币种 B+C）=====")
        for name, geo, gate in CANDIDATES:
            rb = ps.evaluate(FB, geo, gate, dfs, tidx, itv)
            rc = ps.evaluate(FC, geo, gate, dfs, tidx, itv)
            print(f"  {name}: B[{ps.fmt(rb)}] C[{ps.fmt(rc)}]")
            if itv == "1d":
                for sym in ps.SYMBOLS:
                    r = ps.evaluate([r for r in FB + FC if r["symbol"] == sym], geo, gate, dfs, tidx, itv)
                    print(f"    {sym} B+C: {ps.fmt(r)}")


if __name__ == "__main__":
    main()
