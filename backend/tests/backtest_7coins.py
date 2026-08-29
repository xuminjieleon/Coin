"""Push-list 5y backtest with per-coin detail (round 35: 7 coins; round 36: +SUI = 8).

推送事件列表币种（BTC/ETH/SOL/BNB + XRP/ZEC/DOGE + SUI[第三十六轮加入]）
按 BACKTEST §4 同口径重算，并输出报告此前缺失的分币明细：

  - 口径：R13 生产几何 CONF5（backtest_5y 单源）、5 年窗口（1w 全历史）、
    容量约束串行、1h th=25 原生 / 其余 th=10 放宽；**1h 按下界口径**
    （sim_journal_order，用户裁定；4h/1d/1w 跟踪族两顺序等价用 sim_outcome_fast）
  - SUI 合约 2023-05 上线：1h/4h/1d 为上市起全历史（~3.3 年），1w 仅 ~174 根
    不足 warmup(170)+fwd_room(32)——如实无成交，与第七轮"1w 历史不足跳过"同口径
  - 记录缓存 _5y_cache_*（四币命中 08-28 快照；XRP/ZEC/DOGE 第三十五轮落盘；
    SUI 新算）——窗口尾差如实声明：四币尾部 2026-08-28 / 新币 2026-08-29
  - §7.8：分两阶段——阶段1 单事件循环 Semaphore(3) 并发抓取（--fetch-only）；
    阶段2 每 symbol 一个 spawn worker 纯 CPU
  - 复利段复用第二十九轮 harness（compound + 共享预算 scale 参数化）
  - 未计费率为主表口径，费率敏感性单列（feeR=双边×entry/risk）

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/backtest_7coins.py [--fetch-only]
"""
import argparse
import asyncio
import multiprocessing as mp
import os
import pickle
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import profit_sweep2 as ps
from backtest_5y import W5, CONF5, compute_records, sim_outcome_fast
from audit_order_and_entry import sim_journal_order
from backtest_ltc import trade_stats
from profit2_r5 import with_loose_plans
import compound_backtest as cb

SYMBOLS8 = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
            "XRPUSDT", "ZECUSDT", "DOGEUSDT", "SUIUSDT"]
OLD4 = SYMBOLS8[:4]
NEW4 = SYMBOLS8[4:]
TFS = ("1h", "4h", "1d", "1w")
FEE_NET = 0.0010  # 双边 0.10%（单边 0.05%，既有报告净口径）
FAR = 4_000_000_000_000
YEAR_MS = 365.25 * 86400 * 1000


def year_of(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year


# ---------------- phase 1: fetch (single event loop) ----------------

async def fetch_all():
    sem = asyncio.Semaphore(3)

    async def one(sym, itv):
        async with sem:
            for attempt in range(4):
                try:
                    rows = await ps.kline_cache.get_klines(sym, itv, W5[itv], end_time=FAR)
                    print(f"[fetch-ok] {sym} {itv}: {len(rows)} bars", flush=True)
                    return
                except Exception as exc:
                    print(f"[warn] {sym} {itv}: {exc}", flush=True)
                    await asyncio.sleep(8 * (attempt + 1))
            raise SystemExit(f"{sym} {itv} unavailable")

    jobs = [(s, t) for s in SYMBOLS8 for t in TFS]
    await asyncio.gather(*[one(s, t) for s, t in jobs])


# ---------------- phase 2: per-symbol worker (pure CPU) ----------------

def load_df(sym, itv):
    rows = ps.kline_cache._read_rows(sym, itv, FAR, W5[itv])
    return ps.kline_cache.rows_to_df(rows)


def load_cached_records(sym, tf, dfs):
    """_5y_cache_* convention (window-keyed, src-hash guarded)."""
    cache_file = os.path.join(ps.CACHE_DIR, f"_5y_cache_{sym}_{tf}.pkl")
    key = {"ver": 1, "tf": tf, "symbol": sym, "window": W5[tf], "src": ps.source_hash()}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                entry = pickle.load(f)
            if entry.get("key") == key:
                return entry["records"]
        except Exception:
            pass
    records = compute_records(sym, tf, dfs)
    tmp = cache_file + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump({"key": key, "records": records}, f)
    os.replace(tmp, cache_file)
    print(f"[rec] {sym} {tf}: {len(records)} records computed", flush=True)
    return records


def capacity_trades(recs, cfg, df, sim):
    """Trades in dict format (compound-compatible) + (time, rr) list."""
    depth, stopw, be_frac, tgt, texit, trail = tuple(cfg["geo"])
    fill_bars = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    times = df["time"].to_numpy()
    n = len(df)
    tidx = {int(t): i for i, t in enumerate(times)}
    trades = []
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
        out = sim(highs, lows, closes, n, i, direction, entry, stop,
                  be_frac, tgt, texit, fill_bars, trail)
        if out is None:
            continue
        rr, fill, exit_bar = out
        busy = exit_bar
        trades.append({"entry_t": int(times[fill]), "exit_t": int(times[exit_bar]),
                       "dir": direction, "rr": float(rr),
                       "entry_px": float(entry), "risk_px": float(abs(entry - stop)),
                       "scale": 1.0})
    return trades


def worker(sym):
    try:
        dfs = {itv: load_df(sym, itv) for itv in TFS}
        out = {}
        for tf in TFS:
            cfg = CONF5[tf]
            records = load_cached_records(sym, tf, dfs)
            recs = records if cfg["th"] == 25 else with_loose_plans(records, cfg["th"])
            sim = sim_journal_order if tf == "1h" else sim_outcome_fast
            out[tf] = capacity_trades(recs, cfg, dfs[tf], sim)
        return {"sym": sym, "data": out}
    except Exception as exc:
        import traceback
        return {"sym": sym, "error": traceback.format_exc()}


# ---------------- shared-budget scale (round-22 rule, parameterized) ----------------

def shared_budget_portfolio(data, symbols):
    out = []
    for sym in symbols:
        merged = sorted(data[sym]["1h"] + data[sym]["4h"], key=lambda t: t["entry_t"])
        ent = np.array([t["entry_t"] for t in merged], dtype=np.int64)
        ext = np.array([t["exit_t"] for t in merged], dtype=np.int64)
        for t in merged:
            span = t["exit_t"] - t["entry_t"]
            sc = 1.0
            if span > 0:
                lo = np.maximum(t["entry_t"], ent)
                hi = np.minimum(t["exit_t"], ext)
                ov = float(np.maximum(0, hi - lo).sum()) - span
                sc = 1.0 - 0.5 * min(1.0, ov / span)
            c = dict(t)
            c["scale"] = sc
            out.append(c)
    return out


# ---------------- reporting ----------------

def fmt_x(x):
    if x != x or x == float("inf"):
        return "n/a"
    if x >= 1000:
        return f"{x/1000:.1f}千×" if x < 1e6 else f"{x:.2e}×"
    return f"{x:.2f}×"


def stats_of(trades):
    return trade_stats([(t["entry_t"], t["rr"]) for t in trades])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch-only", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    asyncio.run(fetch_all())
    print(f"[fetch phase] {time.time()-t0:.0f}s", flush=True)
    if args.fetch_only:
        return

    ctx = mp.get_context("spawn")
    with ctx.Pool(len(SYMBOLS8)) as pool:
        results = pool.map(worker, SYMBOLS8)
    data = {}
    for res in results:
        if "error" in res:
            print(f"[worker-error]\n{res['error']}", flush=True)
        else:
            data[res["sym"]] = res["data"]
    if len(data) != len(SYMBOLS8):
        raise SystemExit("worker 失败，中止")
    print(f"[pool] done in {time.time()-t0:.0f}s\n")

    # data windows (honest span note)
    spans = {}
    for sym in SYMBOLS8:
        df = load_df(sym, "1h")
        spans[sym] = (ps.fmt_ts(int(df["time"].iloc[0])), ps.fmt_ts(int(df["time"].iloc[-1])))
    print(f"[窗口] 四币尾部 {spans['BTCUSDT'][1]} / XRP尾部 {spans['XRPUSDT'][1]}"
          f"（1h 起点差 {spans['BTCUSDT'][0]} vs {spans['XRPUSDT'][0]}，分年表 2021 年内影响 <0.4%；"
          f"SUI 上市 2023-05 窗口 {spans['SUIUSDT'][0]}..{spans['SUIUSDT'][1]}，1w 不足 warmup 无成交）")

    # ---- per-coin x per-tf detail ----
    print(f"\n{'='*108}")
    print(f"===== 8 币分币明细（R13 几何、容量约束串行、毛口径；1h=下界口径（用户裁定），4h/1d/1w 两顺序等价）=====")
    print(f"{'='*108}")
    coin_tot = {}
    for sym in SYMBOLS8:
        tag = "调参" if sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT") else ("样本外" if sym in OLD4 else "新加入")
        print(f"\n{sym}（{tag}） 窗口 {spans[sym][0]}..{spans[sym][1]}")
        tot = 0.0
        for tf in TFS:
            st = stats_of(data[sym][tf])
            if st.get("filled"):
                pf = f"{st['pf']:.2f}" if st["pf"] != float("inf") else "inf"
                print(f"  {tf:<3} 成交={st['filled']:>5} 胜率={st['winrate']*100:.1f}% "
                      f"非亏={st['nonloss']*100:.1f}% EV={st['ev']:+.3f}R "
                      f"总={st['totalR']:+.1f}R DD={st['maxdd']:.1f}R PF={pf}")
                tot += st["totalR"]
            else:
                print(f"  {tf:<3} 无成交")
        coin_tot[sym] = tot
        print(f"  合计（四周期毛）: {tot:+.1f}R")
    print(f"\n-- 分币合计排名（四周期毛 R）--")
    for sym, tot in sorted(coin_tot.items(), key=lambda x: -x[1]):
        print(f"  {sym:<9} {tot:>+8.1f}R")

    # ---- pooled 8 vs old-4 vs new-4, per tf ----
    print(f"\n-- 池化对比（同 sim 口径重评）--")
    print(f"{'周期':<4} {'8币合计':>10} {'原4币':>10} {'新加入4币':>10}")
    for tf in TFS:
        st8 = stats_of([t for s in SYMBOLS8 for t in data[s][tf]])
        st4 = stats_of([t for s in OLD4 for t in data[s][tf]])
        stn = stats_of([t for s in NEW4 for t in data[s][tf]])
        print(f"{tf:<4} {st8['totalR']:>+9.1f}R {st4['totalR']:>+9.1f}R {stn['totalR']:>+9.1f}R "
              f"(n={st8['filled']})")

    # ---- yearly pooled (1h/4h/1d) ----
    print(f"\n-- 池化逐年（毛 R）--")
    for tf in ("1h", "4h", "1d"):
        by_year = defaultdict(list)
        for s in SYMBOLS8:
            for t in data[s][tf]:
                by_year[year_of(t["entry_t"])].append(t["rr"])
        parts = "  ".join(f"{y}:{np.sum(v):+.1f}(n={len(v)})" for y, v in sorted(by_year.items()))
        print(f"  {tf:<3} {parts}")

    # ---- per-coin yearly total (all four tfs) ----
    print(f"\n-- 分币逐年合计（1h+4h+1d+1w 毛 R）--")
    years = sorted({year_of(t["entry_t"]) for s in SYMBOLS8 for tf in TFS for t in data[s][tf]})
    print(f"  {'币种':<9}" + "".join(f"{y:>9}" for y in years))
    for sym in SYMBOLS8:
        by_year = defaultdict(float)
        for tf in TFS:
            for t in data[sym][tf]:
                by_year[year_of(t["entry_t"])] += t["rr"]
        cells = "".join(f"{by_year.get(y, 0.0):>+9.1f}" for y in years)
        print(f"  {sym:<9}{cells}")

    # ---- fee sensitivity ----
    print(f"\n-- 费率敏感性（8 币池化，feeR=双边×entry/risk）--")
    print(f"  {'周期':<4} {'毛':>10} {'双边0.05%':>11} {'双边0.06%':>11} {'双边0.10%':>11}")
    for tf in ("1h", "4h", "1d"):
        trades = [t for s in SYMBOLS8 for t in data[s][tf]]
        cells = []
        for fee in (0.0, 0.0005, 0.0006, 0.0010):
            net = [(t["entry_t"], t["rr"] - fee * t["entry_px"] / t["risk_px"]) for t in trades]
            cells.append(trade_stats(net)["totalR"])
        print(f"  {tf:<4} {cells[0]:>+9.1f}R {cells[1]:>+10.1f}R {cells[2]:>+10.1f}R {cells[3]:>+10.1f}R")

    # ---- compound (round-29 harness, 8 coins) ----
    ports = {
        "仅1h(下界)": [t for s in SYMBOLS8 for t in data[s]["1h"]],
        "仅4h": [t for s in SYMBOLS8 for t in data[s]["4h"]],
        "仅1d": [t for s in SYMBOLS8 for t in data[s]["1d"]],
        "1h+4h共享": shared_budget_portfolio(data, SYMBOLS8),
    }
    print(f"\n{'='*100}")
    print(f"===== 复利口径（8 币，f=1%，净@双边0.10%，事件账户=第二十九轮 harness）=====")
    print(f"{'='*100}")
    print(f"{'组合':<13}{'复利期末':>12} {'年化':>9} {'最大回撤':>9} {'非复利对照':>12} {'笔数':>7}")
    for name, tr in ports.items():
        st = cb.compound(tr, 0.01, FEE_NET)
        ann_pct = (st["cagr"] - 1.0) * 100 if st["cagr"] == st["cagr"] else float("nan")
        dd = f"{st['maxdd']*100:.1f}%"
        print(f"{name:<13}{fmt_x(st['multiple']):>12} {ann_pct:>+8.1f}% {dd:>9} "
              f"{fmt_x(st['flat']):>12} {st['n']:>7}")
    # yearly compound for shared budget
    st = cb.compound(ports["1h+4h共享"], 0.01, FEE_NET)
    yparts = "  ".join(f"{y}:{(v-1)*100:+.0f}%" for y, v in sorted(st["yearly"].items()))
    print(f"  共享预算分年: {yparts}")

    # ---- per-coin annualized ranking (round 37, user request) ----
    def fmt_pct(x):
        if x != x:
            return "n/a"
        if abs(x) >= 100000:
            return f"{x:+.1e}%"
        return f"{x:+.1f}%"

    print(f"\n{'='*110}")
    print(f"===== 分币年/月化排序（固定注额毛 R；复利=f=1% 每笔风险注额、净@双边0.10%、单币独立资金流=数学上限口径）=====")
    print(f"{'='*110}")
    print(f"{'币种':<9} {'全史总R':>9} {'年化R/年':>9} {'月均R':>7} {'净年化R/年':>11} "
          f"{'复利年化%':>13} {'复利月均%':>11} {'DD(R)':>6}")
    rank_rows = []
    for sym in SYMBOLS8:
        merged = sorted([t for tf in TFS for t in data[sym][tf]], key=lambda x: x["entry_t"])
        st_c = trade_stats([(t["entry_t"], t["rr"]) for t in merged])
        df1h = load_df(sym, "1h")
        t0 = int(df1h["time"].iloc[CONF5["1h"]["warmup"]])
        t1 = int(df1h["time"].iloc[-1])
        span_y = max(0.5, (t1 - t0) / YEAR_MS)
        annual = st_c["totalR"] / span_y
        monthly = annual / 12.0
        net_tot = sum(t["rr"] - FEE_NET * t["entry_px"] / t["risk_px"] for t in merged)
        net_annual = net_tot / span_y
        eq = 1.0
        for t in merged:
            eq *= (1.0 + 0.01 * (t["rr"] - FEE_NET * t["entry_px"] / t["risk_px"]))
        comp_a = (eq ** (1.0 / span_y) - 1.0) * 100 if eq > 0 else float("nan")
        comp_m = (eq ** (1.0 / (span_y * 12)) - 1.0) * 100 if eq > 0 else float("nan")
        rank_rows.append((annual, sym, st_c, monthly, net_annual, comp_a, comp_m))
    for annual, sym, st_c, monthly, na, ca, cm in sorted(rank_rows, key=lambda x: -x[0]):
        dd = f"{st_c['maxdd']:.1f}" if st_c["maxdd"] == st_c["maxdd"] else "-"
        print(f"{sym:<9} {st_c['totalR']:>+9.1f} {annual:>+9.1f} {monthly:>+7.2f} {na:>+11.1f} "
              f"{fmt_pct(ca):>13} {fmt_pct(cm):>11} {dd:>6}")
    print(f"\n[总耗时] {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()