"""US equity feasibility backtest (2026-08-29, round 32, PRE-REGISTERED).

User direction: 回测美股值得用策略的标的（纳指标普等等），4h/1d；策略可以与
目前的不同。本轮不调任何参数——生产 4h/1d 决策引擎与几何原封不动（同第
十九轮 A 股可行性基线做法），执行层按美股规则重写：

  - 双向：美股 T+0，ETF/大盘股可做空；plan long/short 全部执行
  - 跳空真实成交：开盘价穿越挂单/止损按开盘成交（隔夜 gap 风险直接定价）
  - 同根保守盘口顺序：止损先判（journal 重放口径）；跟踪族 4h/1d 两顺序
    等价（第三十轮已证），无 1h 上界问题
  - trail ratchet prior-bar、容量约束串行（一仓一标的）
  - 费率主口径双边 0.06%（与第二十一轮 T+0 结论可比）；敏感性 0.02%
    （零佣金+紧点差 ETF 场景）与 0.12%（高频保守）
  - 做空借券成本不计（大盘股/ETF borrow ~0.3~1%/年，texit 中位几周 →
    每笔 <0.03R，如实声明）
  - CVD 组件中性降级（无 takerBuy → volume/2，同 A 股口径）
  - 4h = RTH 两段制（09:30-13:30 + 13:30-16:00 ET）由 Yahoo 60m 聚合，
    窗口仅 730 天（Yahoo 硬限制，2023-10 起）——4h 结果为短窗参考，
    如实声明；1d 为全历史（各标的上市起，复权后）

预登记验收（写于跑数之前，净口径，1d 与 4h 分别判）：
  G1: 池内合并净 totalR > 0
  G2: 净 totalR > 0 标的占比 >= 2/3
  G3: 最差标的净 EV > -0.15R
三关全过 = 可行（美股为策略适用市场；后续若要调参必须走 §7.3 两阶段盲测）；
任一不过 = 如实记录不足。4h 因窗口短只作方向参考（额外声明）。

朴素对照（预声明参数，非生产、不调优，只为参照系；若占优如实报告）：
  - B&H：区间买入持有（扣一次双边费），报告 % 收益与 close 口径 maxDD
  - Donchian 55/20 双向 + 2×ATR 止损：收盘确认次日开盘成交、gap-aware，
    R 分母 = 2×ATR
  - EMA50/200 交叉双向：金叉次日开盘多/死叉空，持有到反向，risk=入场时
    2×ATR 冻结，R 口径
对照与生产同费率、同 gap 成交、同容量约束（一仓一标的）。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/backtest_us.py [--refresh]
       [--tf 1d,4h] [--no-bench]
"""
import argparse
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

from multiprocessing import Pool

import numpy as np

import profit_sweep2 as ps
from profit2_r5 import with_loose_plans
from backtest_ltc import trade_stats
import us_data

FEES = (0.0002, 0.0006, 0.0012)  # sensitivity: rt notional fee per trade
D1_MS = 86_400_000
NOW_MS = int(datetime.now(timezone.utc).timestamp() * 1000)
TEN_Y_MS = int(10 * 365.25 * 86400000)

# production geometry, unchanged (CONF in backtest_ltc / backtest_5y round-13 canon)
CONF_US = {
    "4h": dict(warmup=500, min_bars=300, spacing=4, fill=18, fwd_room=130,
               geo=(0.75, 1.0, 0.75, None, 48, 0.35), th=10,
               mtf=(("1d", D1_MS),)),
    "1d": dict(warmup=300, min_bars=220, spacing=2, fill=9, fwd_room=110,
               geo=(1.0, 1.2, 0.50, None, 12, 0.35), th=10, mtf=()),
}

STATE = {"refresh": False}


def year_of(ms: int) -> int:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year


def fmt_res(st):
    if not st or not st.get("filled"):
        return "n=0"
    pf = f"{st['pf']:.2f}" if st["pf"] != float("inf") else "inf"
    dd = f"{st['maxdd']:.1f}" if st["maxdd"] == st["maxdd"] else "-"
    return (f"成交={st['filled']} 胜率={st['winrate']*100:.1f}% 非亏={st['nonloss']*100:.1f}% "
            f"EV={st['ev']:+.3f}R 总={st['totalR']:+.1f}R DD={dd}R PF={pf}")


# ---------------- decision records (cached; key includes harness params) ----------------

def load_records(sym: str, tf: str, df, df1d, refresh: bool) -> list[dict]:
    cfg = CONF_US[tf]
    key = {"ver": 1, "sym": sym, "tf": tf, "src": ps.source_hash(), **{
        k: v for k, v in cfg.items() if k != "mtf"}}
    cache_file = os.path.join(ps.CACHE_DIR, f"_us_rec_{sym}_{tf}.pkl")
    if not refresh and os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                entry = pickle.load(f)
            if entry.get("key") == key:
                return entry["records"]
        except Exception:
            pass
    times = df["time"].to_numpy()
    closes = df["close"].to_numpy()
    n = len(df)
    records = []
    t0 = time.time()
    cnt = 0
    for i in range(cfg["warmup"], n - cfg["fwd_room"], cfg["spacing"]):
        t = int(times[i])
        htf = []
        for itv, span in cfg["mtf"]:
            m = ps.tf_summary_closed(df1d, t, span)
            if m:
                htf.append(m)
        rec = ps.decide_at(df, htf, t, cfg["warmup"], cfg["min_bars"])
        if rec is None:
            continue
        rec["symbol"] = sym
        for h in (6, 24, 48):
            rec[f"ret_{h}"] = float(closes[i + h] / closes[i] - 1.0) if i + h < n else float("nan")
        records.append(rec)
        cnt += 1
        if cnt % 1000 == 0:
            print(f"[calc] {sym} {tf}: {cnt} ({time.time()-t0:.0f}s)", flush=True)
    records.sort(key=lambda r: r["time"])
    tmp = cache_file + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump({"key": key, "records": records}, f)
    os.replace(tmp, cache_file)
    print(f"[rec] {sym} {tf}: {len(records)} records cached", flush=True)
    return records


# ---------------- gap-aware dual-direction execution (journal-replay order) ----------------

def sim_us(opens, highs, lows, closes, n, i, direction, entry, stop,
           be_frac, texit, fill_bars, trail):
    """Dual-direction gap-aware sim. Returns (grossR, fill, exit_bar, fill_px) or None.

    Fill: limit at `entry`; open beyond entry (gap) -> fill at open (better);
    else intrabar touch -> fill at entry. Management starts same bar (T+0).
    Stop: open beyond stop -> exit at open (gap-through, worse); else touch -> stop.
    Same-bar conservative order: stop evaluated BEFORE be-trigger (journal canon).
    Trail ratchet updated after exit checks (prior-bar semantics).
    """
    long = direction == "long"
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    be_trig = entry + be_frac * risk if long else entry - be_frac * risk
    fill = fill_px = None
    for j in range(i + 1, min(i + 1 + fill_bars, n)):
        if long:
            if opens[j] <= entry:
                fill, fill_px = j, opens[j]
                break
            if lows[j] <= entry:
                fill, fill_px = j, entry
                break
        else:
            if opens[j] >= entry:
                fill, fill_px = j, opens[j]
                break
            if highs[j] >= entry:
                fill, fill_px = j, entry
                break
    if fill is None:
        return None
    be = False
    locked = 0.0
    ratchet = 0.0
    for j in range(fill, min(fill + texit, n)):
        if be and trail is not None:
            stop_lvl = entry + ratchet * risk if long else entry - ratchet * risk
        else:
            stop_lvl = entry if be else stop
        exit_px = None
        if long:
            if opens[j] <= stop_lvl:
                exit_px = opens[j]
            elif lows[j] <= stop_lvl:
                exit_px = stop_lvl
        else:
            if opens[j] >= stop_lvl:
                exit_px = opens[j]
            elif highs[j] >= stop_lvl:
                exit_px = stop_lvl
        if exit_px is not None:
            if not be:
                gross = (exit_px - fill_px) / risk if long else (fill_px - exit_px) / risk
            else:
                runner = (exit_px - entry) / risk if long else (entry - exit_px) / risk
                gross = locked + 0.5 * runner
            return gross, fill, j, fill_px
        if not be and ((long and highs[j] >= be_trig) or ((not long) and lows[j] <= be_trig)):
            be = True
            locked = 0.5 * be_frac
        if be and trail is not None:
            mfe = (highs[j] - entry) / risk if long else (entry - lows[j]) / risk
            ratchet = max(ratchet, mfe - trail)
    j_end = min(fill + texit, n) - 1
    if j_end < fill:
        j_end = fill
    exit_px = closes[j_end]
    if not be:
        gross = (exit_px - fill_px) / risk if long else (fill_px - exit_px) / risk
    else:
        runner = (exit_px - entry) / risk if long else (entry - exit_px) / risk
        gross = locked + 0.5 * runner
    return gross, fill, j_end, fill_px


def capacity_run(recs, cfg, times, opens, highs, lows, closes, n, tidx):
    """Serial one-position execution. Returns trades (time_ms, gross, fill_px, risk, dir)."""
    depth, stopw, be_frac, tgt, texit, trail = cfg["geo"]
    trades = []
    n_orders = 0
    busy = -1
    for r in recs:
        if r.get("plan") is None:
            continue
        i = tidx.get(r["time"])
        if i is None or i <= busy:
            continue
        built = ps.build_plan(r, depth, stopw)
        if built is None:
            continue
        direction, entry, stop = built
        n_orders += 1
        out = sim_us(opens, highs, lows, closes, n, i, direction, entry, stop,
                     be_frac, texit, cfg["fill"], trail)
        if out is None:
            continue
        gross, fill, exit_bar, fill_px = out
        busy = exit_bar
        risk = abs(entry - stop)
        trades.append((int(times[fill]), gross, fill_px, risk, direction))
    return n_orders, trades


# ---------------- naive benchmarks (pre-declared, not production) ----------------

def atr14_wilder(highs, lows, closes):
    n = len(closes)
    tr = np.empty(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]))
    atr = np.empty(n)
    atr[:14] = tr[:14].mean()
    a = atr[13]
    for i in range(14, n):
        a = (a * 13 + tr[i]) / 14.0
        atr[i] = a
    return atr


def bench_donchian(opens, highs, lows, closes, n, w_in=55, w_out=20, atr_mult=2.0, fee_rt=0.0006):
    """Dual-direction Donchian breakout with 2xATR stop, gap-aware, one position.
    Entry: close beyond prior N-bar extreme -> next bar open. Exit stop: 2xATR
    (gap-through at open); reverse: close beyond opposite 20-bar extreme -> next open."""
    atr = atr14_wilder(highs, lows, closes)
    trades = []  # (bar_idx, R_net, direction)
    pos = 0  # 0 flat, +1 long, -1 short
    entry_px = risk = stop = 0.0
    pending = 0  # +1/-1 order active for next bar open
    for i in range(max(w_in, 60), n - 1):
        if pos == 0:
            if pending:
                entry_px = opens[i + 1]
                risk = atr_mult * atr[i]
                if risk <= 0:
                    pending = 0
                    continue
                stop = entry_px - risk if pending > 0 else entry_px + risk
                pos = pending
                pending = 0
                continue
            if closes[i] > highs[i - w_in:i].max():
                pending = 1
            elif closes[i] < lows[i - w_in:i].min():
                pending = -1
            continue
        # manage position
        exited = False
        if pos > 0:
            if opens[i] <= stop:
                px = opens[i]
                r = (px - entry_px) / risk
                trades.append((i, r - fee_rt * px / risk, "long"))
                pos, exited = 0, True
            elif lows[i] <= stop:
                r = (stop - entry_px) / risk
                trades.append((i, r - fee_rt * stop / risk, "long"))
                pos, exited = 0, True
        else:
            if opens[i] >= stop:
                px = opens[i]
                r = (entry_px - px) / risk
                trades.append((i, r - fee_rt * px / risk, "short"))
                pos, exited = 0, True
            elif highs[i] >= stop:
                r = (entry_px - stop) / risk
                trades.append((i, r - fee_rt * stop / risk, "short"))
                pos, exited = 0, True
        if exited:
            continue
        # reverse exit: close beyond opposite 20-bar extreme -> exit next open
        want_exit = False
        if pos > 0 and closes[i] < lows[i - w_out:i].min():
            want_exit = True
        elif pos < 0 and closes[i] > highs[i - w_out:i].max():
            want_exit = True
        if want_exit:
            px = opens[i + 1]
            r = (px - entry_px) / risk if pos > 0 else (entry_px - px) / risk
            trades.append((i + 1, r - fee_rt * px / risk, "long" if pos > 0 else "short"))
            pos = 0
    return trades


def bench_ema(opens, highs, lows, closes, n, fee_rt=0.0006):
    """Dual-direction EMA50/200 cross, next-bar open, hold until reverse.
    Risk frozen at entry = 2xATR14 (R denominator only)."""
    import pandas as pd
    s = pd.Series(closes)
    e50 = s.ewm(span=50, adjust=False).mean().to_numpy()
    e200 = s.ewm(span=200, adjust=False).mean().to_numpy()
    atr = atr14_wilder(highs, lows, closes)
    trades = []
    pos = 0
    entry_px = risk = 0.0
    for i in range(210, n - 1):
        cross_up = e50[i] > e200[i] and e50[i - 1] <= e200[i - 1]
        cross_dn = e50[i] < e200[i] and e50[i - 1] >= e200[i - 1]
        if pos == 0:
            if cross_up or cross_dn:
                entry_px = opens[i + 1]
                risk = 2.0 * atr[i]
                pos = 1 if cross_up else -1
            continue
        if (pos > 0 and cross_dn) or (pos < 0 and cross_up):
            px = opens[i + 1]
            r = (px - entry_px) / risk if pos > 0 else (entry_px - px) / risk
            trades.append((i + 1, r - fee_rt * px / risk, "long" if pos > 0 else "short"))
            pos = 0
    return trades


# ---------------- worker ----------------

def worker(job):
    sym, tf = job
    cfg = CONF_US[tf]
    df = us_data.load_df(sym, tf)
    df1d = us_data.load_df(sym, "1d") if tf == "4h" else None
    records = load_records(sym, tf, df, df1d, STATE["refresh"])
    recs = with_loose_plans(records, cfg["th"])
    times = df["time"].to_numpy()
    opens = df["open"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    n = len(df)
    tidx = {int(t): k for k, t in enumerate(times)}
    n_orders, trades = capacity_run(recs, cfg, times, opens, highs, lows, closes, n, tidx)
    out = {"sym": sym, "tf": tf, "n_bars": n, "orders": n_orders, "trades": trades,
           "t0": int(times[0]), "t1": int(times[-1])}
    if tf == "1d":
        out["bh"] = float(closes[-1] / closes[cfg["warmup"]] - 1.0)
        seg = closes[cfg["warmup"]:]
        peak = np.maximum.accumulate(seg)
        out["bh_dd"] = float(np.max((peak - seg) / peak))
        idx10 = int(np.searchsorted(times, NOW_MS - TEN_Y_MS))
        if idx10 > cfg["warmup"]:
            seg10 = closes[idx10:]
            peak10 = np.maximum.accumulate(seg10)
            out["bh10"] = float(closes[-1] / closes[idx10] - 1.0)
            out["bh10_dd"] = float(np.max((peak10 - seg10) / peak10))
        else:
            out["bh10"] = out["bh"]
            out["bh10_dd"] = out["bh_dd"]
        out["donchian"] = bench_donchian(opens, highs, lows, closes, n)
        out["ema"] = bench_ema(opens, highs, lows, closes, n)
        # direction accuracy on records
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
        out["diracc"] = diracc
    return out


# ---------------- main ----------------

def netize(trades, fee):
    return [(t, g - fee * px / rk, d) for (t, g, px, rk, d) in trades]


def stats3(trades3):
    """trade_stats adapter for (time, r, direction) 3-tuples."""
    return trade_stats([(t, r) for t, r, _d in trades3])


def _init_worker(refresh: bool):
    STATE["refresh"] = refresh


def main():
    global STATE
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--tf", default="1d,4h")
    ap.add_argument("--no-bench", action="store_true")
    args = ap.parse_args()
    STATE["refresh"] = args.refresh
    tfs = args.tf.split(",")
    jobs = [(sym, tf) for tf in tfs for sym in us_data.POOL]
    t0 = time.time()
    with Pool(processes=min(8, len(jobs)), initializer=_init_worker,
              initargs=(args.refresh,)) as pool:
        results = pool.map(worker, jobs, chunksize=1)
    print(f"[pool] {len(jobs)} jobs done in {time.time()-t0:.0f}s")

    for tf in tfs:
        cfg = CONF_US[tf]
        rs = [r for r in results if r["tf"] == tf]
        print(f"\n{'='*100}")
        print(f"===== 美股 {tf}：生产引擎可行性回测（双向，gap 保守口径，主费率双边 0.06%）=====")
        print(f"几何: {ps.geo_str(tuple(cfg['geo']))} fill={cfg['fill']} th={cfg['th']}"
              f" warmup={cfg['warmup']} spacing={cfg['spacing']}")
        print(f"{'='*100}")

        header = (f"{'标的':<6} {'bar':>5} {'起':>6} {'挂单':>4} {'成交':>4} {'胜率':>6} "
                  f"{'非亏':>6} {'净EV':>8} {'净总R':>8} {'DD':>6} {'多R':>8} {'空R':>8} {'B&H%':>8}")
        print(f"\n-- 逐标的（净口径）--\n{header}")
        per = {}
        for r in sorted(rs, key=lambda x: x["sym"]):
            net = netize(r["trades"], FEES[1])
            st = stats3(net)
            per[r["sym"]] = (st, net)
            lo = sum(rr for _, rr, d in net if d == "long")
            sh = sum(rr for _, rr, d in net if d == "short")
            y0 = datetime.fromtimestamp(r["t0"] / 1000, tz=timezone.utc).year
            dd = f"{st['maxdd']:.1f}" if st["maxdd"] == st["maxdd"] else "-"
            bh = f"{r.get('bh', float('nan'))*100:+.1f}" if r.get("bh") is not None else "-"
            print(f"{r['sym']:<6} {r['n_bars']:>5} {y0:>6} {r['orders']:>4} {st.get('filled',0):>4} "
                  f"{st['winrate']*100 if st.get('filled') else 0:>6.1f} "
                  f"{st['nonloss']*100 if st.get('filled') else 0:>6.1f} "
                  f"{st['ev'] if st.get('filled') else 0:>+8.3f} {st['totalR']:>+8.1f} {dd:>6} "
                  f"{lo:>+8.1f} {sh:>+8.1f} {bh:>8}")

        all_trades = []
        for r in rs:
            all_trades.extend(netize(r["trades"], FEES[1]))
        all_trades.sort(key=lambda x: x[0])
        st_all = stats3(all_trades)
        t0_pool = min(r["t0"] for r in rs)
        t1_pool = max(r["t1"] for r in rs)
        span_years = max(0.5, (t1_pool - t0_pool) / (365.25 * 86400000)) if rs else 1
        print(f"\n-- 合并（净，双边 0.06%）--\n  {fmt_res(st_all)}")
        if st_all.get("filled"):
            print(f"  年化净R = {st_all['totalR']/span_years:+.1f}R/年（池化，未除重叠）")

        by_year = defaultdict(list)
        for t, rr, d in all_trades:
            by_year[year_of(t)].append(rr)
        parts = "  ".join(f"{y}:{np.sum(v):+.1f}(n={len(v)})" for y, v in sorted(by_year.items()))
        print(f"  分年: {parts}")
        tr10 = [(t, rr, d) for t, rr, d in all_trades if t >= NOW_MS - TEN_Y_MS]
        st10 = stats3(tr10)
        print(f"  近10年: {fmt_res(st10)}")
        lo_all = sum(rr for _, rr, d in all_trades if d == "long")
        sh_all = sum(rr for _, rr, d in all_trades if d == "short")
        print(f"  方向: 多 {lo_all:+.1f}R / 空 {sh_all:+.1f}R")

        # pre-registered gates
        pos = sum(1 for s, _ in per.values() if s.get("filled") and s["totalR"] > 0)
        n_traded = sum(1 for s, _ in per.values() if s.get("filled"))
        worst_ev = min((s["ev"] for s, _ in per.values() if s.get("filled")), default=float("nan"))
        g1 = st_all.get("filled", 0) > 0 and st_all["totalR"] > 0
        g2 = n_traded > 0 and pos / n_traded >= 2 / 3
        g3 = worst_ev == worst_ev and worst_ev > -0.15
        verdict = all((g1, g2, g3))
        print(f"\n-- 预登记验收（净口径，写于跑数前）--")
        print(f"  G1 合并净totalR>0: {st_all['totalR']:+.1f}R -> {'过' if g1 else '不过'}")
        print(f"  G2 正收益占比>=2/3: {pos}/{n_traded} -> {'过' if g2 else '不过'}")
        print(f"  G3 最差标的净EV>-0.15R: {worst_ev:+.3f}R -> {'过' if g3 else '不过'}")
        if tf == "4h":
            print(f"  [声明] 4h 窗口仅 730 天（Yahoo 60m 硬限制，2023-10 起），样本短，三关只作方向参考")
        print(f"  结论: {'可行 —— 美股为策略适用市场' if verdict else '不足 —— 如实记录，不回捞参数'}")

        # fee sensitivity
        print(f"\n-- 费率敏感性（合并净）--")
        for fee in FEES:
            stf = stats3(netize([tr for r in rs for tr in r["trades"]], fee))
            print(f"  双边 {fee*100:.2f}%: {fmt_res(stf)}")

        if tf == "1d" and not args.no_bench:
            print(f"\n-- 朴素对照（预声明参数，同费率双边 0.06%，非生产）--")
            print(f"  {'标的':<6} {'B&H%':>8} {'B&H_DD%':>8} {'DC55/20 R':>10} {'DC笔':>5} "
                  f"{'EMA50/200 R':>11} {'EMA笔':>5} {'引擎净R':>9}")
            dc_all, ema_all = [], []
            for r in sorted(rs, key=lambda x: x["sym"]):
                # convert benchmark bar idx -> ms
                df = us_data.load_df(r["sym"], "1d")
                times = df["time"].to_numpy()
                dc_t = [(int(times[i]), rr, d) for i, rr, d in r["donchian"] if i < len(times)]
                ema_t = [(int(times[i]), rr, d) for i, rr, d in r["ema"] if i < len(times)]
                st_dc = stats3(dc_t)
                st_ema = stats3(ema_t)
                dc_all.extend(dc_t)
                ema_all.extend(ema_t)
                st_eng = per[r["sym"]][0]
                print(f"  {r['sym']:<6} {r['bh']*100:>+8.1f} {r['bh_dd']*100:>8.1f} "
                      f"{st_dc['totalR']:>+10.1f} {st_dc.get('filled',0):>5} "
                      f"{st_ema['totalR']:>+11.1f} {st_ema.get('filled',0):>5} {st_eng['totalR']:>+9.1f}")
            dc_all.sort(key=lambda x: x[0])
            ema_all.sort(key=lambda x: x[0])
            print(f"  合并: B&H n/a | Donchian {fmt_res(stats3(dc_all))} | "
                  f"EMA {fmt_res(stats3(ema_all))} | 引擎 {fmt_res(st_all)}")
            # benchmarks 10y slice
            dc10 = [(t, rr, d) for t, rr, d in dc_all if t >= NOW_MS - TEN_Y_MS]
            ema10 = [(t, rr, d) for t, rr, d in ema_all if t >= NOW_MS - TEN_Y_MS]
            print(f"  近10年: Donchian {fmt_res(stats3(dc10))} | EMA {fmt_res(stats3(ema10))}")
            # direction accuracy (informational)
            print(f"\n-- 方向准确率(|score|>=15，决策点前瞻收益符号）--")
            for r in sorted(rs, key=lambda x: x["sym"]):
                acc = r["diracc"]
                parts = "  ".join(f"{h}d n={acc[h][0]} {acc[h][1]*100:.1f}%" for h in (6, 24, 48))
                print(f"  {r['sym']:<6} {parts}")


if __name__ == "__main__":
    main()