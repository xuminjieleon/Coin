"""Independence check for the 1000-point backtest.

Adjacent decision points (~16h apart) share most of their analysis window and
forward window, so counts are inflated by autocorrelation. This script thins
the cached records (every k-th point) and recomputes the headline metrics:

  - BE-managed trade plan non-loss rate (win + scratch)
  - direction hit rate of the composite gate (mtf|th15 @1W)

Usage:
  PYTHONIOENCODING=utf-8 python tests/thin_analysis.py
"""
import asyncio
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import backtest_decision as bt

CACHE_FILE = bt.CACHE_FILE
TOTAL = 17000
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def be_sim(records, dfs, time_index, mult=1.0):
    filled = win = scratch = loss = 0
    for r in records:
        if r["plan"] is None:
            continue
        df = dfs.get(r["symbol"])
        i = time_index.get(r["symbol"], {}).get(r["time"])
        if df is None or i is None:
            continue
        out = bt.plan_sim_be(df, i, r["plan"], mult)
        if out == "win":
            filled += 1
            win += 1
        elif out == "scratch":
            filled += 1
            scratch += 1
        elif out == "loss":
            filled += 1
            loss += 1
    return filled, win, scratch, loss


def dir_hit(records, horizon="1W"):
    dirs, rets = [], []
    for r in records:
        vals = [r["score"]] + [r["mtf_scores"].get(k, 0.0) for k in ("4h", "1d")]
        s = sum(vals) / len(vals)
        if abs(s) < 15:
            continue
        ret = r.get(f"ret_{horizon}")
        if ret is None or np.isnan(ret) or ret == 0:
            continue
        dirs.append(1 if s > 0 else -1)
        rets.append(ret)
    if not dirs:
        return 0, float("nan")
    dirs = np.array(dirs)
    rets = np.array(rets)
    return len(dirs), float(np.mean(np.sign(dirs) == np.sign(rets)))


def main():
    with open(CACHE_FILE, "rb") as f:
        cached = pickle.load(f)
    records = cached["records"]
    records.sort(key=lambda r: r["time"])
    k = int(len(records) * 0.6)
    IS, OOS = records[:k], records[k:]
    print(f"records={len(records)}  IS={len(IS)}  OOS={len(OOS)}")

    dfs = {}
    for sym in SYMBOLS:
        dfs[sym] = asyncio.run(bt.fetch_klines_1h(sym, TOTAL))
    time_index = {sym: {int(t): i for i, t in enumerate(dfs[sym]["time"].to_numpy())} for sym in dfs}

    print("\n===== 抽稀独立性校验（保本管理计划 T=1.0x，OOS）=====")
    print(f"{'抽稀':<8}{'点距':>10}{'成交':>8}{'盈利':>8}{'保本':>8}{'全损':>8}{'非亏损率':>10}")
    for thin, note in ((1, "~16h（全部）"), (4, "~2.6天"), (8, "~5.3天"), (16, "~10.5天")):
        sub = OOS[::thin]
        filled, win, scratch, loss = be_sim(sub, dfs, time_index)
        nl = (win + scratch) / filled if filled else float("nan")
        print(f"1/{thin:<7}{note:>10}{filled:>8}{win:>8}{scratch:>8}{loss:>8}{nl*100:>9.1f}%")

    print("\n===== 方向门控（mtf|th15 @1W）抽稀 =====")
    for thin in (1, 4, 8, 16):
        sub = OOS[::thin]
        n, hit = dir_hit(sub)
        print(f"  1/{thin:<4} n={n:<5} 胜率={hit*100 if n else 0:.1f}%")

    print("\n===== 分币种（OOS 全部点）=====")
    for sym in SYMBOLS:
        sub = [r for r in OOS if r["symbol"] == sym]
        filled, win, scratch, loss = be_sim(sub, dfs, time_index)
        nl = (win + scratch) / filled if filled else float("nan")
        n, hit = dir_hit(sub)
        print(f"  {sym}: BE计划 fill={filled} 非亏损={nl*100 if filled else 0:.1f}% | 方向1W n={n} 胜率={hit*100 if n else 0:.1f}%")


if __name__ == "__main__":
    main()
