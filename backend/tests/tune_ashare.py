"""A股 ETF 预登记调参轮（2026-08-27，第二十轮，§7.3 协议）。

用户目标：收敛 2018/2023 亏损年 + 按波动率分闸；产出收益表格供产品化决策。

**预登记协议（写于跑数之前，任何结果不可回捞）**：
  - 数据：第十九轮 12 ETF 决策记录缓存（生产 1d 引擎，几何原封不动起点）
  - 折分：池化决策点按时间 A 40% / B 30% / C 30%（A≈2006-2015，
    B≈2015-2021 含 2018，C≈2021-2026 含 2023——2023 留在盲测段）
  - 两阶段：Phase1 在 A 单遍坐标下降 → B+C 只报一次不选择；
    Phase2 从生产配置重启在 A+B 下降 → **C 段一次性盲测**
  - 轴（预声明值集，depth=1.0/trail=0.35/fill=9 固定不动）：
      volgate:  none | noexp（vol_state=='expanded' 跳过——波动率分闸）
      bullgate: none | ema200（收盘<EMA200 跳过——熊市年收敛主假设）
      th:       10 | 20
      stop:     1.2 | 1.5 | 2.0
      be:       0.5 | 0.75
      texit:    12 | 24
  - 选择守卫（防小样本退化）：候选在调参段 filled ≥ 基线同段 50%
    且净 EV ≥ +0.03R，否则不可选；同 totalR 取 EV 高者
  - 验收（Phase2 候选 vs 生产基线，全部满足才采纳，C 段一次性）：
      K1: C 段池化净 totalR 提升 > 5%
      K2: C 段盈利 ETF 数 ≥ 基线
      K3: C 段最差 ETF ≥ 基线最差 × 0.95
      K4: C 段内 2023 年净 R 优于基线（用户目标的盲测度量）
      任一不过 = 拒绝，维持生产配置，A 股维持展示层结论
  - 报告项（非验收）：2018（A+B 段内样本）收敛情况、逐 ETF 变化、逐年表
  - 执行口径同第十九轮：long-only/T+1/T+0 当日止损/跳空按开盘/双边 0.06%
  - §7.8 说明：重 CPU 段（引擎决策记录）已在第十九轮多进程算毕并缓存，
    本轮仅加载缓存做轻量模拟（单次 eval <1s），单进程运行

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/tune_ashare.py
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
from services.analysis import indicators as ind_mod
from backtest_ashare import (GEO, FILL_BARS, FEE_RT, load_records, sim_ashare,
                             year_of, fmt_res)
import ashare_data as ad

BASE = {"volgate": "none", "bullgate": "none", "th": 10,
        "stop": GEO[1], "be": GEO[2], "texit": GEO[4]}  # 生产基线 = 1.2/0.5/12
AXES = [("volgate", ("none", "noexp")),
        ("bullgate", ("none", "ema200")),
        ("th", (10, 20)),
        ("stop", (1.2, 1.5, 2.0)),
        ("be", (0.5, 0.75)),
        ("texit", (12, 24))]
FIXED_GEO = (GEO[0], None, None, GEO[3], None, GEO[5])  # depth/tgt/trail 固定


def cfg_str(c):
    return (f"vol={c['volgate']:<5} bull={c['bullgate']:<6} th={c['th']:<2} "
            f"stop={c['stop']:<3} be={c['be']:<4} texit={c['texit']}")


def gate_ok(r, cfg):
    if cfg["volgate"] == "noexp" and r.get("vol_state") == "expanded":
        return False
    if cfg["bullgate"] == "ema200" and not r.get("ema200_ok"):
        return False
    return True


class Book:
    """Per-ETF arrays + gate-annotated records."""

    def __init__(self, code):
        self.code = code
        df = ad.load_df(code)
        self.times = df["time"].to_numpy()
        self.opens = df["open"].to_numpy()
        self.highs = df["high"].to_numpy()
        self.lows = df["low"].to_numpy()
        self.closes = df["close"].to_numpy()
        self.n = len(df)
        self.tidx = {int(t): k for k, t in enumerate(self.times)}
        self.same_day = ad.ETFS[code][2]
        # ema200 gate annotation (full-history EMA, standard)
        ema = ind_mod.ema(df, 200)
        arr = np.full(self.n, np.nan)
        last = np.nan
        for k, v in enumerate(ema):
            if v is not None:
                last = v
            arr[k] = last
        self.ema200 = arr
        records = load_records(code, df, False)
        for r in records:
            i = self.tidx[r["time"]]
            r["ema200_ok"] = bool(self.closes[i] > self.ema200[i]) if not np.isnan(self.ema200[i]) else False
        self.records = records


def run_book(book, cfg):
    recs = with_loose_plans(book.records, cfg["th"])
    geo = (FIXED_GEO[0], cfg["stop"], cfg["be"], FIXED_GEO[3], cfg["texit"], FIXED_GEO[5])
    trades = []
    n_orders = 0
    busy = -1
    for r in recs:
        if r.get("plan") != "long" or not gate_ok(r, cfg):
            continue
        i = book.tidx.get(r["time"])
        if i is None or i <= busy:
            continue
        built = ps.build_plan(r, geo[0], cfg["stop"])
        if built is None:
            continue
        _, entry, stop = built
        n_orders += 1
        out = sim_ashare(book.opens, book.highs, book.lows, book.closes, book.n,
                         i, entry, stop, cfg["be"], cfg["texit"], FILL_BARS,
                         geo[5], book.same_day)
        if out is None:
            continue
        rr, fill, exit_bar = out
        busy = exit_bar
        risk = entry - stop
        fill_px = book.opens[fill] if book.opens[fill] <= entry else entry
        trades.append((int(book.times[fill]), rr - FEE_RT * fill_px / risk))
    return n_orders, trades


def eval_cfg(cfg, books, fold=None):
    """fold: None=all, or (t0,t1) ms bounds. Returns pooled net stats+trades."""
    trades = []
    orders = 0
    for b in books.values():
        n_o, tr = run_book(b, cfg)
        orders += n_o
        if fold:
            tr = [(t, r) for t, r in tr if fold[0] <= t < fold[1]]
        trades.extend(tr)
    trades.sort(key=lambda x: x[0])
    st = trade_stats(trades)
    st["orders"] = orders
    st["trades"] = trades
    return st


def eval_per_etf(cfg, books, fold=None):
    out = {}
    for code, b in books.items():
        _, tr = run_book(b, cfg)
        if fold:
            tr = [(t, r) for t, r in tr if fold[0] <= t < fold[1]]
        out[code] = trade_stats(tr)
    return out


def year_table(trades):
    by = defaultdict(list)
    for t, r in trades:
        by[year_of(t)].append(r)
    return {y: (len(v), float(np.sum(v))) for y, v in sorted(by.items())}


def descend(cfg, books, fold, tag):
    cur = dict(cfg)
    base = eval_cfg(cur, books, fold)
    print(f"  [incumbent] {cfg_str(cur)}\n    {fmt_res(base)}")
    guard_filled = base["filled"]
    for axis, values in AXES:
        best_v, best_st = cur[axis], None
        for v in values:
            if v == cur[axis]:
                continue
            cand = dict(cur)
            cand[axis] = v
            st = eval_cfg(cand, books, fold)
            ok = (st.get("filled", 0) >= guard_filled * 0.5
                  and st.get("ev", float("nan")) == st.get("ev", float("nan"))
                  and st["ev"] >= 0.03)
            mark = ""
            if ok and st["totalR"] > base["totalR"] + 1e-9:
                if best_st is None or st["totalR"] > best_st["totalR"]:
                    best_st, best_v, mark = st, v, "  <- 更优"
            print(f"    [{axis}={v}] {fmt_res(st)}{mark}")
        if best_st is not None:
            cur[axis] = best_v
            base = best_st
            print(f"    [采纳] {axis} -> {best_v}")
        else:
            print(f"    [保持] {axis}={cur[axis]}")
    print(f"  [{tag} 最终] {cfg_str(cur)}\n    {fmt_res(base)}")
    return cur


def main():
    t0 = time.time()
    codes = list(ad.ETFS)
    books = {c: Book(c) for c in codes}
    pooled = sorted((r for b in books.values() for r in b.records), key=lambda r: r["time"])
    a, b = int(len(pooled) * 0.4), int(len(pooled) * 0.7)
    t_a, t_b = pooled[a]["time"], pooled[b]["time"]
    t_end = pooled[-1]["time"] + 1
    FA, FB, FC = (0, t_a), (t_a, t_b), (t_b, t_end)
    print(f"[folds] 决策点 {len(pooled)}；A 止于 {ps.fmt_ts(t_a)}，B 止于 {ps.fmt_ts(t_b)}")

    inc = dict(BASE)
    print(f"\n{'='*84}\n===== Phase 1：A 段坐标下降（B+C 只报不选）=====\n{'='*84}")
    p1 = descend(inc, books, FA, "Phase1-A")
    for name, fold in (("B", FB), ("C", FC), ("B+C", (t_a, t_end))):
        st = eval_cfg(p1, books, fold)
        print(f"  P1候选 {name}: {fmt_res(st)}")

    print(f"\n{'='*84}\n===== Phase 2：A+B 段重启下降 → C 一次性盲测 =====\n{'='*84}")
    p2 = descend(inc, books, (0, t_b), "Phase2-A+B")

    inc_c = eval_cfg(inc, books, FC)
    inc_c_etf = eval_per_etf(inc, books, FC)
    if p2 == inc:
        print("\n[Phase2] 候选与基线相同：无改动，不盲测")
        cand_c = inc_c
        cand_c_etf = inc_c_etf
        same = True
    else:
        cand_c = eval_cfg(p2, books, FC)
        cand_c_etf = eval_per_etf(p2, books, FC)
        same = False

    inc_all = eval_cfg(inc, books)
    cand_all = eval_cfg(p2, books)

    print(f"\n{'='*84}\n===== C 段盲测验收（预登记 K1-K4）=====\n{'='*84}")
    pos_i = sum(1 for s in inc_c_etf.values() if s.get("filled") and s["totalR"] > 0)
    pos_c = sum(1 for s in cand_c_etf.values() if s.get("filled") and s["totalR"] > 0)
    worst_i = min((s["totalR"] for s in inc_c_etf.values() if s.get("filled")), default=0.0)
    worst_c = min((s["totalR"] for s in cand_c_etf.values() if s.get("filled")), default=0.0)
    yt_i = year_table(inc_c["trades"])
    yt_c = year_table(cand_c["trades"])
    y2023_i = yt_i.get(2023, (0, 0.0))[1]
    y2023_c = yt_c.get(2023, (0, 0.0))[1]
    k1 = cand_c["totalR"] > inc_c["totalR"] * 1.05 if inc_c["totalR"] > 0 else cand_c["totalR"] > inc_c["totalR"]
    k2 = pos_c >= pos_i
    k3 = worst_c >= worst_i * 0.95
    k4 = y2023_c > y2023_i
    print(f"  基线 C: {fmt_res(inc_c)}")
    print(f"  候选 C: {fmt_res(cand_c)}")
    print(f"  K1 提升>5%: {cand_c['totalR']:+.1f} vs {inc_c['totalR']:+.1f} -> {'过' if k1 else '不过'}")
    print(f"  K2 盈利数: {pos_c} vs {pos_i} -> {'过' if k2 else '不过'}")
    print(f"  K3 最差: {worst_c:+.1f} vs {worst_i:+.1f} -> {'过' if k3 else '不过'}")
    print(f"  K4 2023: {y2023_c:+.1f} vs {y2023_i:+.1f} -> {'过' if k4 else '不过'}")
    accepted = all((k1, k2, k3, k4)) and not same
    print(f"  结论: {'接受候选' if accepted else '拒绝——维持生产配置（A 股维持第十九轮结论）'}")

    # 诊断输出（跑数后补写，仅记录候选明细，不参与选择/不重跑验收）
    print(f"\n  [诊断] 候选 C 段逐 ETF（基线R | 候选R）:")
    for c in codes:
        si, sc = inc_c_etf[c], cand_c_etf[c]
        print(f"    {c} {ad.etf_name(c)[:5]:<8} {si['totalR']:>+7.1f} | {sc['totalR']:>+7.1f}"
              f"{'  <- K3 违规者' if sc['totalR'] < worst_i * 0.95 and sc['totalR'] == worst_c else ''}")
    ytab_c = year_table(cand_c["trades"])
    print(f"  [诊断] 候选 C 段逐年: " + "  ".join(f"{y}:{v[1]:+.1f}R(n={v[0]})" for y, v in sorted(ytab_c.items())))

    # ---- 收益表格（用户决策产品化用）----
    final = p2 if accepted else inc
    tag = "候选(采纳)" if accepted else "基线(候选被拒)"
    fin_all = cand_all if accepted else inc_all
    print(f"\n{'='*84}\n===== 收益表格（净口径，双边费 0.06%；最终配置 = {tag}）=====\n{'='*84}")
    print(f"配置: {cfg_str(final)}")
    print(f"\n-- 逐年净 R（12 ETF 合并；1R=单笔计划风险；1%风险/笔≈年收益%粗算）--")
    ya, yb = year_table(inc_all["trades"]), year_table(fin_all["trades"])
    print(f"{'年份':<6} {'基线笔':>6} {'基线R':>8} {'最终笔':>6} {'最终R':>8} {'差':>7}")
    for y in sorted(set(ya) | set(yb)):
        na, ra = ya.get(y, (0, 0.0))
        nb, rb = yb.get(y, (0, 0.0))
        blind = " (盲C)" if y >= 2021 else ""
        print(f"{y:<6} {na:>6} {ra:>+8.1f} {nb:>6} {rb:>+8.1f} {rb-ra:>+7.1f}{blind}")
    print(f"{'合计'::<6} {inc_all['filled']:>6} {inc_all['totalR']:>+8.1f} "
          f"{fin_all['filled']:>6} {fin_all['totalR']:>+8.1f} {fin_all['totalR']-inc_all['totalR']:>+7.1f}")
    for st, name in ((inc_all, "基线"), (fin_all, "最终")):
        dd = f"{st['maxdd']:.1f}" if st["maxdd"] == st["maxdd"] else "-"
        print(f"  {name}: {fmt_res(st)}")
    print(f"\n-- 2018（A+B 段内）目标复核 --")
    print(f"  基线 2018: {ya.get(2018, (0,0.0))[1]:+.1f}R -> 最终 2018: {yb.get(2018, (0,0.0))[1]:+.1f}R")
    print(f"-- 2023（盲 C 段内）--")
    print(f"  基线 2023: {ya.get(2023, (0,0.0))[1]:+.1f}R -> 最终 2023: {yb.get(2023, (0,0.0))[1]:+.1f}R")

    print(f"\n-- 逐 ETF（全时段净 totalR / 净EV）--")
    fin_etf = eval_per_etf(final, books)
    inc_etf = eval_per_etf(inc, books)
    print(f"{'代码':<7} {'名称':<10} {'基线R':>8} {'最终R':>8} {'差':>7} {'最终EV':>8} {'最终笔':>6}")
    for c in codes:
        si, sf = inc_etf[c], fin_etf[c]
        print(f"{c:<7} {ad.etf_name(c)[:5]:<10} {si['totalR']:>+8.1f} {sf['totalR']:>+8.1f} "
              f"{sf['totalR']-si['totalR']:>+7.1f} {sf['ev'] if sf.get('filled') else 0:>+8.3f} {sf.get('filled',0):>6}")
    print(f"\n[耗时] {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
