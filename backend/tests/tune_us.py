"""US 1d pre-registered tuning round (2026-08-29, round 33, PRE-REGISTERED).

User direction: 为美股调参（只 1d），把年/月化收益率排序列出。美股 1d 已过
第三十二轮可行性三关（+1205.3R），是全新数据维度 —— 按 §7.3 条款本轮是合法
调参轮。协议写于跑数之前（本 docstring 定稿后才开始跑任何 fold）：

  - 数据/池：第三十二轮 15 只美股 1d 全历史复权数据 + 既有决策记录缓存
    `_us_rec_*_1d.pkl`（记录与几何无关，直接复用；载入时校验 src hash）
  - 费率：全程净口径，双边 0.06%（第三十二轮主口径；费率是真实约束，
    调参在净口径下选择 —— 同第二十轮 A 股做法）
  - 折分：池化决策点按时间排 A 40% / B 30% / C 30%
  - incumbent = 生产 1d 配置（depth 1.0 / stop 1.2 / be 0.50 / trail 0.35 /
    texit 12 / th 10 / fill 9 不动——fill 为 11b 轮终结维度，不重开）
  - 坐标轴（预定义值集，单遍坐标下降，一轴一轮，A 段池化净 totalR 选择，
    平手取 EV；任何值都不含 above-set 的新值）：
      depth {0.5, 0.75, 1.0, 1.25}   stop {1.2, 1.5, 2.0}
      be    {0.25, 0.50, 0.75}       trail {0.35, 0.5, 0.75}
      texit {12, 24, 48}            th {10, 15, 20}
  - 阶段 1：A 段坐标下降 → 候选；B+C 一次性盲测（只报告，不采纳）
  - 阶段 2（最终采纳判据，第 13 轮 phase2 标准）：从 incumbent 起在 A+B 重调
    （同轴同单遍）→ 若无改动维持现状；若有改动 → C 段一次性盲测三关：
      K1: C 段池化净 totalR 提升 > 5%
      K2: 每个标的 C 段净 totalR > 0
      K3: 最差标的 C 段净 totalR 不比 incumbent 的最差差 > 5%
    三关全过 → 采纳候选（美股专用几何，生产币圈几何不动）；任一不过 →
    一票否决维持 incumbent，不回捞不重试
  - 排序输出（用户要求）：最终生效配置全时段净口径逐标的
      年化净 R/年（= totalR / 跨度年数，排序键，降序）
      近 10 年年化净 R/年
      月均净 R
      复利年化 % / 复利月均 %（f=1% 每笔风险注额、单标的独立资金流——
      第二十九轮口径：数学上限，不含容量/市场冲击，如实声明）
      组合等权月度再平衡复利年化 %（15 只等权、月度 rebalance）
  - 声明：R 为风险单位（固定注额），% 为复利假设口径；两者排序可能不同
  - §7.8：本脚本全程 <10 分钟（记录已缓存、单评估 ~1.5s×~45 次），单进程
    合规；若重算记录仍走 backtest_us.py 的多进程缓存

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/tune_us.py
"""
import os
import pickle
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import profit_sweep2 as ps
from profit2_r5 import with_loose_plans
from backtest_ltc import trade_stats
import us_data
from backtest_us import capacity_run, netize, stats3, CONF_US, FEES, NOW_MS, TEN_Y_MS

FEE = 0.0006  # net canon for all selection
WARMUP_1D = CONF_US["1d"]["warmup"]
FILL = 9       # frozen (round-11b terminated dimension)

INCUMBENT = dict(depth=1.0, stop=1.2, be=0.50, trail=0.35, texit=12, th=10)
AXES = [
    ("depth", (0.5, 0.75, 1.0, 1.25)),
    ("stop", (1.2, 1.5, 2.0)),
    ("be", (0.25, 0.50, 0.75)),
    ("trail", (0.35, 0.5, 0.75)),
    ("texit", (12, 24, 48)),
    ("th", (10, 15, 20)),
]


def geo_of(cfg):
    return (cfg["depth"], cfg["stop"], cfg["be"], None, cfg["texit"], cfg["trail"])


def year_of(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year


def fmt_res(st):
    if not st or not st.get("filled"):
        return "n=0"
    dd = f"{st['maxdd']:.1f}" if st["maxdd"] == st["maxdd"] else "-"
    pf = f"{st['pf']:.2f}" if st["pf"] != float("inf") else "inf"
    return (f"成交={st['filled']} 胜率={st['winrate']*100:.1f}% EV={st['ev']:+.3f}R "
            f"总={st['totalR']:+.1f}R DD={dd}R PF={pf}")


# ---------------- load books ----------------

def load_books():
    books = {}
    cur_src = ps.source_hash()
    for sym in us_data.POOL:
        cache_file = os.path.join(ps.CACHE_DIR, f"_us_rec_{sym}_1d.pkl")
        with open(cache_file, "rb") as f:
            entry = pickle.load(f)
        if entry["key"].get("src") != cur_src:
            raise SystemExit(f"{sym}: record cache src hash mismatch — rerun backtest_us.py --refresh")
        records = entry["records"]
        df = us_data.load_df(sym, "1d")
        times = df["time"].to_numpy()
        tidx = {int(t): k for k, t in enumerate(times)}
        books[sym] = {
            "records": records,
            "times": times,
            "opens": df["open"].to_numpy(),
            "highs": df["high"].to_numpy(),
            "lows": df["low"].to_numpy(),
            "closes": df["close"].to_numpy(),
            "n": len(df),
            "tidx": tidx,
            "t0": int(times[WARMUP_1D]),
            "t1": int(times[-1]),
        }
    return books


# ---------------- evaluation ----------------

def eval_cfg(books, cfg, t_from=0, t_to=None):
    """Per-symbol net stats for records in [t_from, t_to). Returns (pooled_stats, per_sym)."""
    per = {}
    all_trades = []
    for sym, b in books.items():
        recs = [r for r in b["records"] if r["time"] >= t_from and (t_to is None or r["time"] < t_to)]
        if cfg["th"] == 25:
            loose = recs
        else:
            loose = with_loose_plans(recs, cfg["th"])
        cap_cfg = {"geo": geo_of(cfg), "fill": FILL}
        _n_orders, trades = capacity_run(loose, cap_cfg, b["times"], b["opens"],
                                         b["highs"], b["lows"], b["closes"], b["n"], b["tidx"])
        net = netize(trades, FEE)
        net.sort(key=lambda x: x[0])
        per[sym] = stats3(net)
        per[sym]["trades"] = net
        all_trades.extend(net)
    all_trades.sort(key=lambda x: x[0])
    return stats3(all_trades), per


def gate_check(st_cand, st_inc, per_cand, per_inc):
    ok1 = st_inc["totalR"] > 0 and st_cand["totalR"] / st_inc["totalR"] - 1.0 > 0.05
    ok2 = all(s.get("filled") and s["totalR"] > 0 for s in per_cand.values())
    worst_inc = min(s["totalR"] for s in per_inc.values())
    worst_cand = min(s["totalR"] for s in per_cand.values())
    ok3 = worst_cand >= worst_inc * 0.95
    return ok1, ok2, ok3, worst_inc, worst_cand


def coordinate_descent(books, t0, t1, label):
    cur = dict(INCUMBENT)
    st_cur, _ = eval_cfg(books, cur, t0, t1)
    print(f"\n-- 坐标下降 [{label}]（incumbent {ps.geo_str(geo_of(cur))} th={cur['th']}）--")
    print(f"  [incumbent] {fmt_res(st_cur)}")
    for axis, values in AXES:
        best_cfg, best_st = cur, st_cur
        for v in values:
            cand = dict(cur)
            cand[axis] = v
            st, _ = eval_cfg(books, cand, t0, t1)
            marker = ""
            if st.get("filled") and (st["totalR"] > best_st["totalR"] + 1e-9 or
                                     (abs(st["totalR"] - best_st["totalR"]) <= 1e-9 and
                                      st.get("ev", -9) > best_st.get("ev", -9))):
                best_cfg, best_st, marker = cand, st, "  <- 更优"
            print(f"  [{axis}={v}] {fmt_res(st)}{marker}")
        cur, st_cur = best_cfg, best_st
        print(f"  [采纳] {axis} -> {cur[axis]}")
    return cur, st_cur


# ---------------- annualized ranking table ----------------

def equity_path(trades, f=0.01):
    eq = 1.0
    for t, r, _d in sorted(trades, key=lambda x: x[0]):
        eq *= (1.0 + f * r)
    return eq


def month_factors(trades, f=0.01):
    """month -> compounded factor for this symbol's trades in that month."""
    by_month = defaultdict(list)
    for t, r, _d in trades:
        by_month[datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m")].append(r)
    return {m: float(np.prod([1.0 + f * r for r in rs])) for m, rs in by_month.items()}


def rank_table(books, cfg, title):
    st_all, per = eval_cfg(books, cfg)
    print(f"\n{'='*112}")
    print(f"===== {title}：年/月化收益率排序（净口径 双边0.06%；复利=f=1%每笔风险注额，数学上限口径）=====")
    print(f"{'='*112}")
    cfg_geo = geo_of(cfg)
    print(f"配置: {ps.geo_str(cfg_geo)} th={cfg['th']} fill={FILL}")
    header = (f"{'标的':<6} {'全史净R':>9} {'年化R/年':>9} {'月均R':>8} {'近10年年化':>10} "
              f"{'复利年化%':>10} {'复利月均%':>10} {'DD(R)':>7} {'胜率%':>6}")
    print(f"\n{header}")
    rows = []
    for sym, b in books.items():
        st = per[sym]
        trades = st["trades"]
        span_years = max(0.5, (b["t1"] - b["t0"]) / (365.25 * 86400000))
        span_months = max(6.0, span_years * 12)
        annual_r = st["totalR"] / span_years
        monthly_r = st["totalR"] / span_months
        tr10 = [(t, r, d) for t, r, d in trades if t >= NOW_MS - TEN_Y_MS]
        st10 = stats3(tr10)
        annual10 = st10["totalR"] / min(10.0, span_years) if st10.get("filled") else 0.0
        eq = equity_path(trades, 0.01)
        comp_annual = (eq ** (1.0 / span_years) - 1.0) * 100
        comp_month = (eq ** (1.0 / span_months) - 1.0) * 100
        rows.append((annual_r, sym, st, monthly_r, annual10, comp_annual, comp_month))
    for annual_r, sym, st, monthly_r, annual10, ca, cm in sorted(rows, key=lambda x: -x[0]):
        dd = f"{st['maxdd']:.1f}" if st["maxdd"] == st["maxdd"] else "-"
        print(f"{sym:<6} {st['totalR']:>+9.1f} {annual_r:>+9.1f} {monthly_r:>+8.2f} "
              f"{annual10:>+10.1f} {ca:>+10.1f} {cm:>+10.2f} {dd:>7} {st['winrate']*100 if st.get('filled') else 0:>6.1f}")
    # pooled + portfolio
    pooled = []
    for sym, b in books.items():
        pooled.extend(per[sym]["trades"])
    pooled.sort(key=lambda x: x[0])
    st_pool = stats3(pooled)
    span_years = max(0.5, (max(b["t1"] for b in books.values()) - min(b["t0"] for b in books.values())) / (365.25 * 86400000))
    print(f"\n  池化合计: {fmt_res(st_pool)}  年化净R = {st_pool['totalR']/span_years:+.1f}R/年（未除重叠）")
    # equal-weight monthly-rebalanced portfolio compounding
    all_factors = {sym: month_factors(per[sym]["trades"]) for sym in books}
    months = sorted({m for f in all_factors.values() for m in f})
    eq = 1.0
    eq_path = []
    for m in months:
        rets = [all_factors[sym][m] - 1.0 for sym in books if m in all_factors[sym]]
        if rets:
            eq *= (1.0 + float(np.mean(rets)))
        eq_path.append(eq)
    n_months = max(1, len(months))
    pf_annual = (eq ** (12.0 / n_months) - 1.0) * 100
    pf_monthly = (eq ** (1.0 / n_months) - 1.0) * 100
    peak = np.maximum.accumulate(np.array(eq_path))
    pf_dd = float(np.max((peak - np.array(eq_path)) / peak)) * 100
    m0 = datetime.strptime(months[0], "%Y-%m")
    m1 = datetime.strptime(months[-1], "%Y-%m")
    print(f"  等权组合（月度再平衡，f=1%/笔）: 年化 {pf_annual:+.1f}%/年 | 月均 {pf_monthly:+.2f}%/月 | "
          f"复利月度DD {pf_dd:.1f}% | {m0.strftime('%Y-%m')}..{m1.strftime('%Y-%m')} 共 {n_months} 月 | 期末 {eq:.2f}x")
    # fee sensitivity on pooled annualized (raw trades recomputed once for cfg)
    raw = []
    for sym, b in books.items():
        loose = b["records"] if cfg["th"] == 25 else with_loose_plans(b["records"], cfg["th"])
        cap_cfg = {"geo": geo_of(cfg), "fill": FILL}
        _n, trades = capacity_run(loose, cap_cfg, b["times"], b["opens"],
                                  b["highs"], b["lows"], b["closes"], b["n"], b["tidx"])
        raw.extend(trades)
    print(f"  费率敏感性（池化年化净R）: ", end="")
    for fee in FEES:
        stf = stats3(netize(raw, fee))
        print(f"双边{fee*100:.2f}% = {stf['totalR']/span_years:+.1f}R/年", end="  ")
    print()
    return st_pool, per


# ---------------- main ----------------

def main():
    t0 = time.time()
    books = load_books()
    print(f"[load] 15 books, records={sum(len(b['records']) for b in books.values())} ({time.time()-t0:.0f}s)")

    # time folds on pooled decision stream
    pooled = sorted(r["time"] for b in books.values() for r in b["records"])
    t_a = pooled[int(len(pooled) * 0.4)]
    t_b = pooled[int(len(pooled) * 0.7)]
    t_end = pooled[-1] + 1
    print(f"[folds] 决策点 {len(pooled)}；A 止于 {ps.fmt_ts(t_a)}，B 止于 {ps.fmt_ts(t_b)}")

    # ---- phase 1: tune on A, one-shot blind on B+C (report only) ----
    cand1, _ = coordinate_descent(books, 0, t_a, "Phase1 fold-A")
    st_inc_bc, per_inc_bc = eval_cfg(books, INCUMBENT, t_b, t_end)
    st_cand_bc, per_cand_bc = eval_cfg(books, cand1, t_b, t_end)
    print(f"\n-- Phase 1 B+C 一次性盲测（只报告）--")
    print(f"  incumbent: {fmt_res(st_inc_bc)}")
    print(f"  候选:      {fmt_res(st_cand_bc)}")
    ok1, ok2, ok3, wi, wc = gate_check(st_cand_bc, st_inc_bc, per_cand_bc, per_inc_bc)
    print(f"  K1 提升>5%: {'过' if ok1 else '不过'} | K2 逐标的>0: {'过' if ok2 else '不过'} | "
          f"K3 最差标的 {wc:+.1f} vs {wi:+.1f}: {'过' if ok3 else '不过'}")

    # ---- phase 2: re-tune on A+B, one-shot blind on C (FINAL acceptance) ----
    cand2, _ = coordinate_descent(books, 0, t_b, "Phase2 fold-A+B")
    if cand2 == INCUMBENT:
        print("\n[Phase2] A+B 重调与生产配置相同：无改动，维持现状")
        final = dict(INCUMBENT)
        accepted = False
    else:
        st_inc_c, per_inc_c = eval_cfg(books, INCUMBENT, t_b, t_end)
        st_cand_c, per_cand_c = eval_cfg(books, cand2, t_b, t_end)
        print(f"\n-- Phase 2 C 段一次性盲测（最终采纳判据）--")
        print(f"  incumbent: {fmt_res(st_inc_c)}")
        print(f"  候选:      {fmt_res(st_cand_c)}")
        for sym in us_data.POOL:
            si, sc = per_inc_c[sym], per_cand_c[sym]
            print(f"    {sym:<6} inc {si['totalR']:+8.1f}R  cand {sc['totalR']:+8.1f}R")
        ok1, ok2, ok3, wi, wc = gate_check(st_cand_c, st_inc_c, per_cand_c, per_inc_c)
        print(f"  K1 C段提升>5%: {'过' if ok1 else '不过'} | K2 逐标的C段>0: {'过' if ok2 else '不过'} | "
              f"K3 最差标的 {wc:+.1f} vs {wi:+.1f}: {'过' if ok3 else '不过'}")
        accepted = ok1 and ok2 and ok3
        final = cand2 if accepted else dict(INCUMBENT)
        print(f"  结论: {'采纳候选（美股专用几何；生产币圈几何不动）' if accepted else '一票否决——维持生产配置，不回捞'}")

    # ---- final ranking table (user request) ----
    title = "最终采纳配置" if (cand2 != INCUMBENT and accepted) else "维持生产配置"
    rank_table(books, final, f"美股 1d {title}")

    print(f"\n[耗时] {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()