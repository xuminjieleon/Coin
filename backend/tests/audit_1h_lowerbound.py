"""AUDIT-followup (2026-08-28): 1h journal-order lower-bound + fee-net table.

User directive: 回测报告 1h 利润按 -20% 口径（日记/计划语义的保本挂单先于
目标成交）呈现。本脚本产出 BACKTEST/STRATEGY 修订要抄的机器数字：
  T8  1h 日记口径（下界）全时段四币分年 + 合计 + 非亏损率/EV/DD/PF
  T9  1h 日记口径 × 费率场景（毛/双边0.05/0.07/0.10/0.12%）
  T10 1h 日记口径 C 段盲测（pooled A40/B30/C30，新几何）
  T11 4h 盲测段 DD 漂移核对（文档 5.0R vs 当前缓存）

口径：sim_journal_order = 同根K线 止损→保本触发→棘轮更新→目标（journal/
计划语义）；其余与 backtest_5y.capacity_run_fast 完全一致（容量串行、
fill 窗口、texit、feeR=双边费率×entry/risk）。

Usage: PYTHONIOENCODING=utf-8 ..\\.venv\\Scripts\\python.exe tests\\audit_1h_lowerbound.py
"""
import os
import pickle
import sys
from collections import defaultdict
from datetime import datetime, timezone

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import profit_sweep2 as ps
from profit2_r5 import with_loose_plans
from backtest_ltc import CONF, trade_stats
from backtest_5y import W5, sim_outcome_fast
from audit_order_and_entry import sim_journal_order, capacity_run, fetch_df

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
FEES = [("毛(无费用)", 0.0), ("双边0.05%", 0.0005), ("双边0.07%(maker入+taker出)", 0.0007),
        ("双边0.10%(单边0.05%)", 0.0010), ("双边0.12%(单边0.06%)", 0.0012)]


def year_of(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year


def load(sym, tf):
    df = fetch_df(sym, tf)
    with open(os.path.join(ps.CACHE_DIR, f"_5y_cache_{sym}_{tf}.pkl"), "rb") as f:
        recs = pickle.load(f)["records"]
    cfg = CONF[tf]
    recs = recs if cfg["th"] == 25 else with_loose_plans(recs, cfg["th"])
    arrs = (df["high"].to_numpy(), df["low"].to_numpy(),
            df["close"].to_numpy(), len(df))
    tidx = {int(t): k for k, t in enumerate(df["time"].to_numpy())}
    fb = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
    return recs, arrs, tidx, fb, cfg


def run_all(sim, tf):
    out = {}
    for sym in SYMBOLS:
        recs, arrs, tidx, fb, cfg = load(sym, tf)
        out[sym] = (capacity_run(recs, tuple(cfg["geo"]), arrs, tidx, fb, sim), recs, cfg)
    return out


def t8(results):
    print("=" * 78)
    print("T8: 1h 日记口径（下界）全时段（2021-08-29..2026-08-28 缓存窗口）")
    print("=" * 78)
    pooled = []
    for sym in SYMBOLS:
        trades = results[sym][0]
        st = trade_stats([(t[0], t[1]) for t in trades])
        by_year = defaultdict(float)
        for t in trades:
            by_year[year_of(t[0])] += t[1]
        yrs = "  ".join(f"{y}:{v:+.1f}" for y, v in sorted(by_year.items()))
        print(f"  {sym:<9} 成交={st['filled']} 非亏损={st['nonloss']*100:.1f}% "
              f"EV={st['ev']:+.3f}R 总={st['totalR']:+.1f}R DD={st['maxdd']:.1f}R PF={st['pf']:.2f}")
        print(f"            分年 {yrs}")
        pooled.extend(trades)
    st = trade_stats([(t[0], t[1]) for t in pooled])
    print(f"  >> 四币合计: 成交={st['filled']} 非亏损={st['nonloss']*100:.1f}% "
          f"EV={st['ev']:+.3f}R 总={st['totalR']:+.1f}R DD={st['maxdd']:.1f}R PF={st['pf']:.2f}")
    neg = [(s, y) for s in SYMBOLS for y in range(2021, 2027)
           if sum(t[1] for t in results[s][0] if year_of(t[0]) == y) < 0]
    print(f"  >> 负值单元(币×年): {neg if neg else '无（逐年全正）'}")


def t9(results):
    print()
    print("=" * 78)
    print("T9: 1h 日记口径 × 费率场景（feeR=双边费率×entry/risk）")
    print("=" * 78)
    rows = []
    for label, fee in FEES:
        pooled = []
        for sym in SYMBOLS:
            trades = results[sym][0]
            pooled.extend((t[0], t[1] - fee * t[6] / abs(t[6] - t[7])) for t in trades)
        st = trade_stats(pooled)
        rows.append((label, st))
        print(f"  {label:<26} 成交={st['filled']} 非亏损={st['nonloss']*100:.1f}% "
              f"EV={st['ev']:+.3f}R 总={st['totalR']:+.1f}R DD={st['maxdd']:.1f}R PF={st['pf']:.2f}")
    return rows


def t10():
    print()
    print("=" * 78)
    print("T10: 1h 日记口径 C 段盲测（pooled 40/30/30，新几何）")
    print("=" * 78)
    for sym_tf in ("1h",):
        pooled_recs = []
        arrs_d, tidx_d, fb_d, cfg_d = {}, {}, {}, {}
        for sym in SYMBOLS:
            recs, arrs, tidx, fb, cfg = load(sym, sym_tf)
            pooled_recs += [(sym, r) for r in recs]
            arrs_d[sym], tidx_d[sym], fb_d[sym], cfg_d[sym] = arrs, tidx, fb, cfg
        pooled_recs.sort(key=lambda x: x[1]["time"])
        b = int(len(pooled_recs) * 0.7)
        FC = pooled_recs[b:]
        for tag, sim in (("基准(sim原口径)", sim_outcome_fast), ("日记口径(下界)", sim_journal_order)):
            trades = []
            per_sym = {}
            for sym in SYMBOLS:
                sub = [r for s, r in FC if s == sym]
                # 与 backtest_5y.phase2 / audit_baseline T7 同口径：fold-C 决策
                # 记录喂入完整 K 线数组做容量串行（busy 在 fold 起点干净重置），
                # 统计时按决策时间在 fold 内的交易（与 T7 的 eval_geo(FC) 一致）
                tr = capacity_run(sub, tuple(cfg_d[sym]["geo"]), arrs_d[sym],
                                  tidx_d[sym], fb_d[sym], sim)
                per_sym[sym] = sum(t[1] for t in tr)
                trades.extend(tr)
            st = trade_stats([(t[0], t[1]) for t in trades])
            worst = min(per_sym.values())
            print(f"  [{tag}] 成交={st['filled']} 非亏损={st['nonloss']*100:.1f}% "
                  f"EV={st['ev']:+.3f}R 总={st['totalR']:+.1f}R DD={st['maxdd']:.1f}R "
                  f"PF={st['pf']:.2f} 最差币={worst:+.1f}R")


def t11():
    print()
    print("=" * 78)
    print("T11: 4h 盲测段 DD 核对（STRATEGY §4 用 5.0R）")
    print("=" * 78)
    pooled_recs = []
    arrs_d, tidx_d, fb_d, cfg_d = {}, {}, {}, {}
    for sym in SYMBOLS:
        recs, arrs, tidx, fb, cfg = load(sym, "4h")
        pooled_recs += [(sym, r) for r in recs]
        arrs_d[sym], tidx_d[sym], fb_d[sym], cfg_d[sym] = arrs, tidx, fb, cfg
    pooled_recs.sort(key=lambda x: x[1]["time"])
    b = int(len(pooled_recs) * 0.7)
    FC = pooled_recs[b:]
    trades = []
    for sym in SYMBOLS:
        sub = [r for s, r in FC if s == sym]
        trades.extend(capacity_run(sub, tuple(cfg_d[sym]["geo"]), arrs_d[sym],
                                   tidx_d[sym], fb_d[sym], sim_outcome_fast))
    st = trade_stats([(t[0], t[1]) for t in trades])
    print(f"  4h 盲测段(当前缓存): 成交={st['filled']} 非亏损={st['nonloss']*100:.1f}% "
          f"EV={st['ev']:+.3f}R 总={st['totalR']:+.1f}R DD={st['maxdd']:.1f}R PF={st['pf']:.2f}")


if __name__ == "__main__":
    r1h = run_all(sim_journal_order, "1h")
    t8(r1h)
    t9(r1h)
    t10()
    t11()
