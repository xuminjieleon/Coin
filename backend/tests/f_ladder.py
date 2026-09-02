"""f 峰值审计（第五十一轮，2026-09-02）——生产引擎 f 阶梯与模型内几何增长峰值

背景：QA 2026-09-02「高 R 还是低 R」裁定"生产引擎 f 峰值未测、勿外推"；用户追问
"f 的峰值是多少"。本脚本把它测出来。

口径（与 BACKTEST §2/§4 同引擎同几何，窗口按本机数据可用性收缩并如实披露）：
- 9 币（推送列表）、R13 几何 CONF5、容量约束串行、1h=下界口径（sim_journal_order）、
  4h/1d=sim_outcome_fast、净@双边0.10%、窗口钉 NOW_MS=2026-09-01 14:00 UTC；
- **1h/4h = 2 年统一窗（尾对齐 18168/4380 根，≈2024-08-05 起）**：本机 K 线缓存对
  XRP/ZEC/SUI/LTC 无 5 年深度，全量重算 1h 5 年每币 30-90 分钟不可行；decide_at/
  tf_summary_closed 均为有界回看（≤500/300 根），2 年窗内决策与 5 年窗同码同值；
- **1d = 5 年全窗（1825 根；SUI 上市起 1218）**：本机数据全满，且与 BACKTEST §4.1
  同口径——f=1% 基线应复现 §4.1 的 53.55×（机器锚点交叉验证，允许 R47 登记的
  跨机 K 线历史漂移 ±1~2%）；
- 记录缓存**独立文件**：1h/4h→`_2y_cache_*`（window 键=18168/4380），
  1d→`_5y_cache_{sym}_1d.pkl`（window 键=1825，与 backtest_5y.load_records 键兼容，
  本机从此拥有九币 1d 当前源码哈希的 5 年记录缓存）；
- 基线 / 休息8h（北京 00:00~08:00 信号错过，与 sleep_discount 同口径）两臂，
  记录只算一次、两臂复用；
- f 阶梯 0.5%~20% 跑 compound_backtest.compound（事件账户、权益仅平仓时点记账），
  输出 CAGR-argmax（模型内峰值）、半峰/四分之一峰、回撤穿越线、峰值处聚合风险。

诚实口径（§4.4 全部适用且更强）：权益仅平仓时点记账（MAE 未计）→ 回撤偏浅 →
模型内峰值偏乐观；未建模容量/市场冲击/杠杆保证金（f 大时单仓名义=(f/止损%)×权益）；
亏损截断于 −1R 无跳空穿仓。峰值≠建议值：fractional（1/4~1/2 峰值）是常规实践，
回撤承受力才是真约束。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/f_ladder.py
"""
import multiprocessing as mp
import os
import pickle
import sys
import time
import traceback

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import profit_sweep2 as ps
import compound_9coins as c9
import compound_backtest as cb
from backtest_5y import CONF5, compute_records, sim_outcome_fast
from audit_order_and_entry import sim_journal_order
from profit2_r5 import with_loose_plans

SYMBOLS = c9.SYMBOLS
TFS3 = ("1h", "4h", "1d")
NOW_MS = c9.NOW_MS
W2 = {"1h": 18168, "4h": 4380}
W1D = 1825
LADDER = (0.005, 0.01, 0.015, 0.02, 0.025, 0.03, 0.035, 0.04, 0.05, 0.06, 0.08, 0.10,
          0.125, 0.15, 0.175, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50)
DD_MARKS = (0.10, 0.20, 0.30, 0.50)
EXPECT = {sym: {"1h": 18168, "4h": 4380, "1d": 1825} for sym in SYMBOLS}
EXPECT["SUIUSDT"] = {"1h": 18167, "4h": 4380, "1d": 1218}


def load_df(sym, itv, limit):
    rows = ps.kline_cache._read_rows(sym, itv, NOW_MS, limit)
    return ps.kline_cache.rows_to_df(rows)


def load_records_win(sym, tf, dfs):
    lim = W2[tf] if tf in W2 else W1D
    fname = f"_2y_cache_{sym}_{tf}.pkl" if tf in W2 else f"_5y_cache_{sym}_1d.pkl"
    cache_file = os.path.join(ps.CACHE_DIR, fname)
    key = {"ver": 1, "tf": tf, "symbol": sym, "window": lim, "src": ps.source_hash()}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                entry = pickle.load(f)
            if entry.get("key") == key:
                print(f"[cache] {sym} {tf}: {len(entry['records'])} records", flush=True)
                return entry["records"]
        except Exception:
            pass
    records = compute_records(sym, tf, dfs)
    tmp = cache_file + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump({"key": key, "records": records}, f)
    os.replace(tmp, cache_file)
    print(f"[cache] {sym} {tf}: saved {len(records)} records", flush=True)
    return records


def worker(sym):
    try:
        dfs = {}
        for itv in TFS3:
            lim = EXPECT[sym][itv]
            df = load_df(sym, itv, lim)
            if len(df) != lim:
                return {"sym": sym, "error": f"{itv} rows {len(df)} != expected {lim}"}
            dfs[itv] = df
        print(f"[data] {sym}: 1h={len(dfs['1h'])} 4h={len(dfs['4h'])} 1d={len(dfs['1d'])}", flush=True)
        recs = {tf: load_records_win(sym, tf, dfs) for tf in TFS3}
        arms = {}
        for arm, sh in (("base", None), ("sleep8h", c9.SLEEP_HOURS)):
            d = {}
            for tf in TFS3:
                cfg = dict(CONF5[tf])
                cfg["_tf"] = tf
                rr = recs[tf] if cfg["th"] == 25 else with_loose_plans(recs[tf], cfg["th"])
                sim = sim_journal_order if tf == "1h" else sim_outcome_fast
                d[tf] = c9.capacity_trades(rr, cfg, dfs[tf], sim, sh)
            arms[arm] = d
        return {"sym": sym, "arms": arms}
    except Exception:
        return {"sym": sym, "error": traceback.format_exc()}


def ports_of(data):
    return {
        "仅1h(下界)": [t for s in SYMBOLS for t in data[s]["1h"]],
        "仅4h": [t for s in SYMBOLS for t in data[s]["4h"]],
        "仅1d(5年)": [t for s in SYMBOLS for t in data[s]["1d"]],
        "1h+4h共享": c9.shared_budget_portfolio(data, SYMBOLS),
    }


def fmt_x(x):
    if x != x or x == float("inf"):
        return "n/a"
    if x >= 1000:
        return f"{x/1000:.1f}千×" if x < 1e6 else f"{x:.2e}×"
    return f"{x:.2f}×"


def fmt_cagr(m):
    if m is None or m != m or m == float("inf"):
        return "-"
    if m < 10:
        return f"+{(m-1)*100:.0f}%/年"
    return f"{m:.1f}×/年"


def main():
    t0 = time.time()
    ctx = mp.get_context("spawn")
    with ctx.Pool(len(SYMBOLS)) as pool:
        res = pool.map(worker, SYMBOLS)
    results = {}
    for r in res:
        if "error" in r:
            print(f"[worker-error] {r['sym']}\n{r['error']}", flush=True)
        else:
            results[r["sym"]] = r["arms"]
    if len(results) != len(SYMBOLS):
        raise SystemExit("worker failed")
    print(f"[pool] {time.time()-t0:.0f}s", flush=True)

    summary = []
    for arm, tag in (("base", "基线"), ("sleep8h", "休息8h")):
        data = {s: results[s][arm] for s in SYMBOLS}
        ports = ports_of(data)
        win = "1h/4h=2年窗、1d=5年窗"
        print(f"\n{'='*104}\n== f 阶梯（9 币，净@双边0.10%，{tag}臂；{win}；权益仅平仓时点记账）==\n{'='*104}")
        for name, tr in ports.items():
            stop_pct = float(np.median([t["risk_px"] / t["entry_px"] for t in tr]))
            mc = cb.max_concurrent(tr)
            span = (max(t["exit_t"] for t in tr) - min(t["entry_t"] for t in tr)) / c9.YEAR_MS
            print(f"\n-- {name}：n={len(tr)} 峰值并发={mc} 止损距中位={stop_pct*100:.2f}% 流span={span:.2f}年")
            print(f"{'f':>6} {'单仓名义×权益':>13} {'复利期末':>13} {'CAGR':>13} {'maxDD':>8} {'非复利':>13}")
            rows = []
            for f in LADDER:
                st = cb.compound(tr, f, c9.FEE_NET)
                rows.append((f, st))
                ruin = " RUIN(权益转负)" if st["maxdd"] >= 1.0 else ""
                print(f"{f*100:>5.1f}% {f/stop_pct:>11.1f}× {fmt_x(st['multiple']):>13} "
                      f"{fmt_cagr(st['cagr']):>13} {st['maxdd']*100:>7.1f}% {fmt_x(st['flat']):>13}{ruin}")
            valid = [(f, st) for f, st in rows
                      if st["cagr"] == st["cagr"] and st["cagr"] != float("inf")
                      and st["maxdd"] < 1.0]
            if valid:
                bf, bs = max(valid, key=lambda r: r[1]["cagr"])

                def at(target):
                    rung = min(LADDER, key=lambda x: abs(x - target))
                    return [r for r in rows if r[0] == rung][0]

                hf, hs = at(bf / 2)
                qf, qs = at(bf / 4)
                cross = []
                for mark in DD_MARKS:
                    hit = next((f for f, st in rows if st["maxdd"] >= mark), None)
                    cross.append(f"DD≥{int(mark*100)}%: {hit*100:.1f}%" if hit
                                 else f"DD≥{int(mark*100)}%: 阶梯内未达")
                ruin_f = next((f for f, st in rows if st["maxdd"] >= 1.0), None)
                cross.append("RUIN: " + (f"{ruin_f*100:.1f}%" if ruin_f else "阶梯内未达"))
                print(f"  ▶ 模型内峰值(非RUIN区): f={bf*100:.1f}%（CAGR {fmt_cagr(bs['cagr'])}，"
                      f"maxDD {bs['maxdd']*100:.1f}%，期末 {fmt_x(bs['multiple'])}）")
                print(f"  ▶ 半峰值 f={hf*100:.1f}%（CAGR {fmt_cagr(hs['cagr'])}，maxDD {hs['maxdd']*100:.1f}%）；"
                      f"四分之一峰 f={qf*100:.1f}%（CAGR {fmt_cagr(qs['cagr'])}，maxDD {qs['maxdd']*100:.1f}%）")
                print(f"  ▶ 回撤穿越线: " + "  ".join(cross))
                print(f"  ▶ 峰值 f 下单仓名义≈{bf/stop_pct:.1f}×权益、"
                      f"瞬时最大理论亏损≈{mc}仓×f×1.07≈{mc*bf*107:.0f}%")
                summary.append((tag, name, bf, bs["cagr"], bs["maxdd"]))

    st1d = cb.compound(ports_of({s: results[s]["base"] for s in SYMBOLS})["仅1d(5年)"], 0.01, c9.FEE_NET)
    print(f"\n== 锚点交叉验证（仅1d f=1% 基线，净@0.10%）==")
    print(f"  本机本轮: 期末 {fmt_x(st1d['multiple'])} / CAGR {fmt_cagr(st1d['cagr'])} / maxDD {st1d['maxdd']*100:.1f}% / n={st1d['n']}")
    print(f"  BACKTEST §4.1（第五十轮机器输出）: 53.55× / +179.4%/年 / DD 4.3% / n=1124")
    print(f"  偏差在 R47 登记的跨机 K 线历史漂移（池化 ±1~2%）内即视为锚点成立")

    print(f"\n{'='*104}\n== 峰值汇总（模型内 CAGR-argmax，净@0.10%）==\n{'='*104}")
    for tag, name, bf, cagr, maxdd in summary:
        print(f"  {tag:<7}{name:<12} 峰值 f={bf*100:>5.1f}%  CAGR={fmt_cagr(cagr):>13}  maxDD={maxdd*100:>6.1f}%")

    print(f"""
注（诚实口径）：
1. 模型内峰值偏乐观：权益仅平仓时点记账（MAE 未计）→ 回撤偏浅；未建模容量/市场冲击/
   杠杆保证金（f 大时单仓名义=(f/止损%)×权益，需要杠杆）；亏损截断于 −1R 无跳空穿仓。
2. 峰值≠建议值：在峰值处回撤已深到多数人会在谷底弃用系统；fractional（1/4~1/2 峰值）
   是常规实践；BACKTEST §4.1 的 1d f=1% 仍是最可实现锚点。
3. 1h/4h 为 2 年窗（本机数据边界），样本约为 5 年口径的 40%，峰值估计精度按梯级读、
   勿加假精度；1d 为 5 年全窗。
4. 梯级精度：≤4% 区间 0.5% 步长。""")
    print(f"\n[total] {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()