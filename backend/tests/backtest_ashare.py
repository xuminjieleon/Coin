"""A-share ETF feasibility backtest (2026-08-27, round 18, PRE-REGISTERED).

User direction: A股策略可以与币圈策略不同。本轮不调任何参数，只做**可行性
基线**——把生产 1d 决策引擎（评分/SMC/区域）原样用于 A 股 ETF，执行层按
A 股规则重写（这是与币圈回测的本质差异）：

  - **long-only**：ETF 无融券，short 计划全部丢弃
  - **T+1**：A 股 ETF 买入当日不可卖 → 止损/保本/跟踪全部从成交次日生效；
    跨境/黄金 ETF（513100/513330/518880）为 T+0，当日可止损（同币圈口径）
  - **跳空真实成交**：限价单开盘低于挂单价→按开盘价成交（优于挂单价）；
    止损日开盘击穿止损位→按开盘价离场（劣于止损位，隔夜跳空风险的直接度量）
  - **手续费**：双边 0.06%（佣金万2.5/边×2+滑点，ETF 免印花税），以 R 计逐笔扣除
  - CVD 组件中性降级（东财无 takerBuy，见 ashare_data.py）——如实声明

几何 = 生产 1d 原封不动（depth 1.0 / stop 1.2×ATR / be 0.5R / 无固定目标 /
trail 0.35R / texit 12 根 / 挂单窗口 9 根 / 阈值放宽 th=10），只做多过滤。
A 股是全新数据维度；若基线可行，后续调参必须走 §7.3 两阶段盲测协议。

**预登记验收（可行 vs 不可行，写于跑数之前）**——全部按扣费后净值：
  G1: 12 个 ETF 合并 net totalR > 0
  G2: 净 totalR > 0 的 ETF 占比 ≥ 2/3
  G3: 最差单个 ETF 的 net EV > −0.15R
三关全过 = 可行（进入下一轮预登记调参）；任一不过 = A 股仅做展示层，
不进决策引擎（结论如实写入 DEVLOG，不回捞参数）。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/backtest_ashare.py [--refresh]
"""
import argparse
import os
import pickle
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from multiprocessing import Pool

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import profit_sweep2 as ps
from profit2_r5 import with_loose_plans
from backtest_ltc import trade_stats
import ashare_data as ad

# production 1d config, unchanged (CONF['1d'] in backtest_ltc)
WARMUP, MIN_BARS, SPACING, FWD_ROOM = 300, 220, 2, 110
GEO = (1.0, 1.2, 0.50, None, 12, 0.35)  # depth, stop, be, tgt, texit, trail
FILL_BARS = 9   # round(6 * fill_mult 1.5)
TH = 10
FEE_RT = 0.0006  # round-trip notional cost


def year_of(ms: int) -> int:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year


def fmt_res(st):
    if not st or not st.get("filled"):
        return "n=0"
    pf = f"{st['pf']:.2f}" if st["pf"] != float("inf") else "inf"
    dd = f"{st['maxdd']:.1f}" if st["maxdd"] == st["maxdd"] else "-"
    return (f"成交={st['filled']} 胜率={st['winrate']*100:.1f}% EV={st['ev']:+.3f}R "
            f"总={st['totalR']:+.1f}R DD={dd}R PF={pf}")


def load_records(code: str, df, refresh: bool) -> list[dict]:
    cache_file = os.path.join(ps.CACHE_DIR, f"_ashare_rec_{code}.pkl")
    key = {"ver": 1, "code": code, "tf": "1d", "src": ps.source_hash()}
    if not refresh and os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                entry = pickle.load(f)
            if entry.get("key") == key:
                return entry["records"]
        except Exception:
            pass
    n = len(df)
    times = df["time"].to_numpy()
    closes = df["close"].to_numpy()
    records: list[dict] = []
    for i in range(WARMUP, n - FWD_ROOM, SPACING):
        rec = ps.decide_at(df, [], int(times[i]), WARMUP, MIN_BARS)
        if rec is None:
            continue
        rec["symbol"] = code
        for h in (6, 24, 48):
            rec[f"ret_{h}"] = float(closes[i + h] / closes[i] - 1.0) if i + h < n else float("nan")
        records.append(rec)
    records.sort(key=lambda r: r["time"])
    tmp = cache_file + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump({"key": key, "records": records}, f)
    os.replace(tmp, cache_file)
    return records


def sim_ashare(opens, highs, lows, closes, n, i, entry, stop, be_frac, texit,
               fill_bars, trail, same_day_stop):
    """Long-only A-share execution. Returns (rr, fill, exit_bar) or None.

    Fill: limit at `entry`; open <= entry -> fill at open (gap improvement).
    Stop: open <= stop_lvl -> exit at open (gap-through, worse than stop);
          else low <= stop_lvl -> exit at stop_lvl. T+1: management starts
    the day AFTER fill. Same-bar conservative order: stop > be-trigger.
    R denominator = planned risk (entry - stop); P&L on actual fill price.
    """
    risk = entry - stop
    if risk <= 0:
        return None
    be_trig = entry + be_frac * risk
    fill = fill_px = None
    for j in range(i + 1, min(i + 1 + fill_bars, n)):
        if opens[j] <= entry:
            fill, fill_px = j, opens[j]
            break
        if lows[j] <= entry:
            fill, fill_px = j, entry
            break
    if fill is None:
        return None
    be = False
    locked = 0.0
    ratchet = 0.0
    start = fill if same_day_stop else fill + 1
    for j in range(start, min(fill + texit, n)):
        if be and trail is not None:
            stop_lvl = entry + ratchet * risk
        elif be:
            stop_lvl = entry
        else:
            stop_lvl = stop
        if opens[j] <= stop_lvl:
            exit_px = opens[j]
        elif lows[j] <= stop_lvl:
            exit_px = stop_lvl
        else:
            exit_px = None
        if exit_px is not None:
            gross = (exit_px - fill_px) / risk
            if be:
                gross = locked + 0.5 * (exit_px - entry) / risk
            return gross, fill, j
        if not be and highs[j] >= be_trig:
            be = True
            locked = 0.5 * be_frac
        if be and trail is not None:
            mfe = (highs[j] - entry) / risk
            ratchet = max(ratchet, mfe - trail)
    j_end = min(fill + texit, n) - 1
    exit_px = closes[j_end]
    gross = (exit_px - fill_px) / risk
    if be:
        gross = locked + 0.5 * (exit_px - entry) / risk
    return gross, fill, j_end


def capacity_run(recs, geo, opens, highs, lows, closes, n, tidx, same_day_stop):
    depth, stopw, be_frac, tgt, texit, trail = geo
    trades_gross: list[tuple[int, float, float]] = []  # time, grossR, feeR
    n_orders = 0
    busy = -1
    for r in recs:
        if r.get("plan") != "long":
            continue
        i = tidx.get(r["time"])
        if i is None or i <= busy:
            continue
        built = ps.build_plan(r, depth, stopw)
        if built is None:
            continue
        direction, entry, stop = built
        n_orders += 1
        out = sim_ashare(opens, highs, lows, closes, n, i, entry, stop,
                         be_frac, texit, FILL_BARS, trail, same_day_stop)
        if out is None:
            continue
        rr, fill, exit_bar = out
        busy = exit_bar
        risk = entry - stop
        fill_px = min(entry, opens[fill]) if opens[fill] <= entry else entry
        fee_r = FEE_RT * fill_px / risk
        trades_gross.append((int(times_all[fill]), rr, fee_r))
    return n_orders, trades_gross


times_all = None  # set in worker


def worker(code: str) -> dict:
    global times_all
    df = ad.load_df(code)
    times_all = df["time"].to_numpy()
    records = load_records(code, df, _STATE["refresh"])
    recs = with_loose_plans(records, TH)
    n_total = len(records)
    n_long = sum(1 for r in recs if r.get("plan") == "long")
    opens = df["open"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    n = len(df)
    tidx = {int(t): k for k, t in enumerate(times_all)}
    n_orders, trades = capacity_run(recs, GEO, opens, highs, lows, closes, n,
                                    tidx, ad.ETFS[code][2])
    # buy & hold benchmark over the decision window (close-based maxdd)
    bh_ret = float(closes[-1] / closes[WARMUP] - 1.0)
    seg = closes[WARMUP:]
    peak = np.maximum.accumulate(seg)
    bh_dd = float(np.max((peak - seg) / peak)) if len(seg) else float("nan")
    # direction accuracy
    diracc = {}
    for h in (6, 24, 48):
        dirs, rets = [], []
        for r in records:
            s = r["score"]
            if abs(s) < 15:
                continue
            ret = r.get(f"ret_{h}")
            if ret is None or np.isnan(ret) or ret == 0:
                continue
            dirs.append(1 if s > 0 else -1)
            rets.append(ret)
        diracc[h] = (len(dirs), float(np.mean(np.sign(dirs) == np.sign(rets))) if dirs else float("nan"))
    return {"code": code, "name": ad.etf_name(code), "bars": n, "decisions": n_total,
            "long_plans": n_long, "orders": n_orders, "trades": trades,
            "bh_ret": bh_ret, "bh_dd": bh_dd, "diracc": diracc,
            "t0": ad.ETFS[code][2]}


_STATE = {"refresh": False}


def _init_worker(refresh: bool):
    _STATE["refresh"] = refresh


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    codes = list(ad.ETFS)
    t0 = time.time()
    with Pool(processes=min(6, len(codes)), initializer=_init_worker,
              initargs=(args.refresh,)) as pool:
        results = pool.map(worker, codes, chunksize=1)
    print(f"[pool] {len(results)} workers done in {time.time()-t0:.0f}s")

    print(f"\n{'='*88}")
    print(f"===== A股 ETF 1d 生产引擎可行性回测（long-only, T+1/跳空真实口径, 双边费 {FEE_RT*100:.2f}%）=====")
    print(f"几何: {ps.geo_str(GEO)} fill={FILL_BARS} th={TH}（生产 1d 原封不动）")
    print(f"{'='*88}")
    print(f"\n{'代码':<7} {'名称':<12} {'bar':>5} {'挂单':>4} {'成交':>4} {'净EV':>8} {'净总R':>8} "
          f"{'grossR':>8} {'DD':>6} {'B&H':>8} {'B&H_DD':>7} {'T+0':>4}")

    all_trades: list[tuple[int, float, float]] = []
    per_etf = {}
    for r in sorted(results, key=lambda x: x["code"]):
        net = [(t, g - f) for t, g, f in r["trades"]]
        st = trade_stats(net)
        gsum = sum(g for _, g, _ in r["trades"])
        per_etf[r["code"]] = (st, gsum)
        all_trades.extend(r["trades"])
        dd = f"{st['maxdd']:.1f}" if st["maxdd"] == st["maxdd"] else "-"
        pf = ""
        print(f"{r['code']:<7} {r['name'][:6]:<12} {r['bars']:>5} {r['orders']:>4} {st.get('filled',0):>4} "
              f"{st['ev'] if st.get('filled') else 0:>+8.3f} {st['totalR']:>+8.1f} {gsum:>+8.1f} "
              f"{dd:>6} {r['bh_ret']*100:>+7.1f}% {r['bh_dd']*100:>6.1f}% {'T+0' if r['t0'] else '':>4}")
        acc = "  ".join(f"{h}d: n={r['diracc'][h][0]} {r['diracc'][h][1]*100:.1f}%" for h in (6, 24, 48))
        print(f"        方向准确率(|score|>=15): {acc}")

    all_trades.sort(key=lambda x: x[0])
    pooled_net = [(t, g - f) for t, g, f in all_trades]
    st_all = trade_stats(pooled_net)
    gross_all = sum(g for _, g, _ in all_trades)
    print(f"\n-- 12 ETF 合并（净）--\n  {fmt_res(st_all)}  gross={gross_all:+.1f}R "
          f"费用拖累={gross_all - st_all['totalR']:+.1f}R")

    by_year = defaultdict(list)
    for t, r in pooled_net:
        by_year[year_of(t)].append(r)
    parts = "  ".join(f"{y}:{np.array(v).sum():+.1f}R(n={len(v)})" for y, v in sorted(by_year.items()))
    print(f"  分年: {parts}")

    # pre-registered gate (net)
    pos = sum(1 for c, (st, _) in per_etf.items() if st.get("filled") and st["totalR"] > 0)
    n_with_trades = sum(1 for c, (st, _) in per_etf.items() if st.get("filled"))
    worst_ev = min((st["ev"] for st, _ in per_etf.values() if st.get("filled")), default=float("nan"))
    g1 = st_all.get("filled", 0) > 0 and st_all["totalR"] > 0
    g2 = n_with_trades > 0 and pos / n_with_trades >= 2 / 3
    g3 = worst_ev == worst_ev and worst_ev > -0.15
    print(f"\n-- 预登记验收（净口径，写于跑数前）--")
    print(f"  G1 合并净 totalR>0: {st_all['totalR']:+.1f}R -> {'过' if g1 else '不过'}")
    print(f"  G2 正收益占比>=2/3: {pos}/{n_with_trades} -> {'过' if g2 else '不过'}")
    print(f"  G3 最差ETF净EV>-0.15R: {worst_ev:+.3f}R -> {'过' if g3 else '不过'}")
    verdict = all((g1, g2, g3))
    print(f"  结论: {'可行 —— 下一轮按 §7.3 两阶段盲测协议预登记调参' if verdict else '不可行 —— A股仅展示层，不进决策引擎'}")


if __name__ == "__main__":
    main()
