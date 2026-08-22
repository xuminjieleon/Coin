"""R3 targeted tests: tighter trail/stop for 4h/1d, fill-window sensitivity,
and final validation of candidates. Selection on A+B only; blind reported once.

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/profit2_r3.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import profit_sweep2 as ps

CAND_4H = [
    ("r2-winner", (0.75, 1.5, 0.5, None, 48, 0.75)),
    ("trail05", (0.75, 1.5, 0.5, None, 48, 0.5)),
    ("stop12", (0.75, 1.2, 0.5, None, 48, 0.75)),
    ("stop12-trail05", (0.75, 1.2, 0.5, None, 48, 0.5)),
    ("trail035", (0.75, 1.5, 0.5, None, 48, 0.35)),
]
CAND_1D = [
    ("r2-winner", (0.75, 1.5, 0.5, 5.0, 24, 0.75)),
    ("tgt-none", (0.75, 1.5, 0.5, None, 24, 0.75)),
    ("trail05", (0.75, 1.5, 0.5, None, 24, 0.5)),
    ("stop12", (0.75, 1.2, 0.5, None, 24, 0.75)),
]


def run_tf(tf, cands, fill_mults=(1.0, 1.5)):
    dfs = ps.load_dfs(tf, mtf1w=False)
    tidx = ps.make_tidx(dfs, tf)
    records = ps.load_records(tf, dfs, mtf1w=False, refresh=False)
    FA, FB, FC = ps.folds(records)
    print(f"\n{'='*70}\n===== {tf} R3：A+B 选择 =====\n{'='*70}")
    for name, geo in cands:
        res = ps.evaluate(FA + FB, geo, "base", dfs, tidx, tf)
        print(f"  {name:<14} {ps.geo_str(geo)}: {ps.fmt(res)}")
    # fill-window sensitivity on first candidate
    print(f"-- 成交窗口敏感性（A+B，r2-winner 几何）--")
    base = cands[0][1]
    for fm in fill_mults:
        res = ps.evaluate(FA + FB, base, "base", dfs, tidx, tf, fm)
        print(f"  fill×{fm}: {ps.fmt(res)}")
    return dfs, tidx, records


def blind_report(tf, dfs, tidx, records, geo, fill_mult, tag):
    FA, FB, FC = ps.folds(records)
    print(f"\n----- {tf} [{tag}] 盲测验证 {ps.geo_str(geo)} fill×{fill_mult} -----")
    for name, fold in (("A", FA), ("B", FB), ("C", FC)):
        res = ps.evaluate(fold, geo, "base", dfs, tidx, tf, fill_mult)
        print(f"  fold {name}: {ps.fmt(res)}")
    rbc = ps.evaluate(FB + FC, geo, "base", dfs, tidx, tf, fill_mult)
    print(f"  blind B+C: {ps.fmt(rbc)}")
    print("  分币种（全时段）：")
    for sym in ps.SYMBOLS:
        sub = [r for r in records if r["symbol"] == sym]
        res = ps.evaluate(sub, geo, "base", dfs, tidx, tf, fill_mult)
        print(f"    {sym}: {ps.fmt(res)}")
    print("  抽稀（C 段）：")
    for thin in (1, 2, 4):
        res = ps.evaluate(FC[::thin], geo, "base", dfs, tidx, tf, fill_mult)
        print(f"    1/{thin}: {ps.fmt(res)}")


def main():
    # --- selection phase (A+B only) ---
    dfs4, tidx4, rec4 = run_tf("4h", CAND_4H)
    dfs1, tidx1, rec1 = run_tf("1d", CAND_1D)

    # 1w: new-family fill x2 (geometry inherited, fill window chosen on A+B earlier)
    dfs_w = ps.load_dfs("1w", mtf1w=False)
    tidx_w = ps.make_tidx(dfs_w, "1w")
    rec_w = ps.load_records("1w", dfs_w, mtf1w=False, refresh=False)

    # --- blind validation: report ALL candidates once (selection uses A+B above) ---
    print("\n" + "=" * 70)
    print("===== 盲测验证（选择只依据上面的 A+B 段）=====")
    print("=" * 70)
    for name, geo in CAND_4H:
        blind_report("4h", dfs4, tidx4, rec4, geo, 1.0, f"4h-{name}")
    for name, geo in CAND_1D:
        blind_report("1d", dfs1, tidx1, rec1, geo, 1.0, f"1d-{name}")
    blind_report("1w", dfs_w, tidx_w, rec_w, (0.75, 1.5, 0.5, None, 24, 0.75), 2.0, "1w-family-fillx2-te24")


if __name__ == "__main__":
    main()
