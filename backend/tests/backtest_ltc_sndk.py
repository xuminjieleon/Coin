"""LTC / SNDKB backtest (round 50, 2026-09-01) — R13 production geometry, zero tuning.

User request: backtest LTC and SNDK(SNDKBUSDT) with the current production
strategy and rank. Protocol follows round 36 (SUI) and round 6 (LTC):

  - 口径：R13 生产几何 CONF5（backtest_5y 单源）、5 年窗口（1w 全历史）、
    容量约束串行、1h th=25 原生 / 其余 th=10 放宽；**1h 按下界口径**
    （sim_journal_order，用户裁定；4h/1d/1w 跟踪族两顺序等价用 sim_outcome_fast）
  - LTC：2017-12 上市，窗口足够跑满 5 年四周期；第六轮已跑过（旧几何，
    四周期合计 +296.6R），本轮按 R13 几何重跑以与第三十五/三十六轮口径可比
  - SNDKB：2026-06-11 上市（~2.7 个月），各周期 K 线数：
      1h ~1965 根（warmup 260 后 ~1700 决策点）
      4h ~492 根（warmup 200 后 ~290）
      1d ~83 根（warmup 60+fwd_room 8 → 不足，如实跳过）
      1w ~13 根（远不足 warmup 170 → 如实跳过）
    与第七轮 TUTU "历史短" 同口径如实呈现，年化外推明确标注为小样本
  - 两币均为纯样本外（从未参与任何调参；LTC 第六轮是回测非调参）
  - §7.8：阶段1 单事件循环 Semaphore(3) 并发抓取；阶段2 每 symbol 一个 spawn worker
  - 未计费率为主表口径，费率敏感性单列（feeR=双边×entry/risk）
  - 第四十七轮标准：评估窗口钉 end_time=NOW_MS（活进程实时追加 kline 缓存的坑）

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/backtest_ltc_sndk.py [--fetch-only]
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

SYMBOLS = ["LTCUSDT", "SNDKBUSDT"]
TFS = ("1h", "4h", "1d", "1w")
FEE_NET = 0.0010  # 双边 0.10%
NOW_MS = 1788271200000  # 2026-09-01 14:00 UTC — 评估窗口钉 end_time（第四十七轮标准）
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
                    rows = await ps.kline_cache.get_klines(sym, itv, W5[itv], end_time=NOW_MS)
                    print(f"[fetch-ok] {sym} {itv}: {len(rows)} bars", flush=True)
                    return
                except Exception as exc:
                    print(f"[warn] {sym} {itv}: {exc}", flush=True)
                    await asyncio.sleep(8 * (attempt + 1))
            raise SystemExit(f"{sym} {itv} unavailable")

    jobs = [(s, t) for s in SYMBOLS for t in TFS]
    await asyncio.gather(*[one(s, t) for s, t in jobs])


# ---------------- phase 2: per-symbol worker (pure CPU) ----------------

def load_df(sym, itv):
    rows = ps.kline_cache._read_rows(sym, itv, NOW_MS, W5[itv])
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
            df = dfs[tf]
            if len(df) <= cfg["warmup"] + cfg["fwd_room"] + cfg["spacing"]:
                out[tf] = {"skipped": True, "bars": len(df), "trades": []}
                continue
            records = load_cached_records(sym, tf, dfs)
            recs = records if cfg["th"] == 25 else with_loose_plans(records, cfg["th"])
            sim = sim_journal_order if tf == "1h" else sim_outcome_fast
            out[tf] = {"skipped": False, "bars": len(df),
                       "trades": capacity_trades(recs, cfg, df, sim)}
        return {"sym": sym, "data": out}
    except Exception:
        import traceback
        return {"sym": sym, "error": traceback.format_exc()}


# ---------------- reporting ----------------

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
    with ctx.Pool(len(SYMBOLS)) as pool:
        results = pool.map(worker, SYMBOLS)
    data = {}
    for res in results:
        if "error" in res:
            print(f"[worker-error]\n{res['error']}", flush=True)
        else:
            data[res["sym"]] = res["data"]
    if len(data) != len(SYMBOLS):
        raise SystemExit("worker failed, abort")
    print(f"[pool] done in {time.time()-t0:.0f}s\n")

    spans = {}
    for sym in SYMBOLS:
        df = load_df(sym, "1h")
        spans[sym] = (ps.fmt_ts(int(df["time"].iloc[0])), ps.fmt_ts(int(df["time"].iloc[-1])))
    print(f"[window] LTC {spans['LTCUSDT'][0]}..{spans['LTCUSDT'][1]} | "
          f"SNDKB {spans['SNDKBUSDT'][0]}..{spans['SNDKBUSDT'][1]} (listed 2026-06-11, short history)")

    # ---- per-coin x per-tf detail ----
    print(f"\n{'='*100}")
    print(f"===== LTC / SNDKB per-coin detail (R13 geometry, capacity serial, gross; 1h=lower-bound) =====")
    print(f"{'='*100}")
    coin_tot = {}
    for sym in SYMBOLS:
        print(f"\n{sym} (pure out-of-sample) window {spans[sym][0]}..{spans[sym][1]}")
        tot = 0.0
        for tf in TFS:
            cell = data[sym][tf]
            if cell["skipped"]:
                print(f"  {tf:<3} skipped: short history ({cell['bars']} bars < warmup+fwd_room)")
                continue
            st = stats_of(cell["trades"])
            if st.get("filled"):
                pf = f"{st['pf']:.2f}" if st["pf"] != float("inf") else "inf"
                print(f"  {tf:<3} filled={st['filled']:>5} win={st['winrate']*100:.1f}% "
                      f"nonloss={st['nonloss']*100:.1f}% EV={st['ev']:+.3f}R "
                      f"total={st['totalR']:+.1f}R DD={st['maxdd']:.1f}R PF={pf}")
                tot += st["totalR"]
            else:
                print(f"  {tf:<3} no fills")
        coin_tot[sym] = tot
        print(f"  TOTAL (4 tf gross): {tot:+.1f}R")

    # ---- direction split per coin ----
    print(f"\n-- direction split (long/short, gross R) --")
    for sym in SYMBOLS:
        for tf in TFS:
            cell = data[sym][tf]
            if cell["skipped"] or not cell["trades"]:
                continue
            longs = [t["rr"] for t in cell["trades"] if t["dir"] == "long"]
            shorts = [t["rr"] for t in cell["trades"] if t["dir"] == "short"]
            le = np.mean(longs) if longs else float("nan")
            se = np.mean(shorts) if shorts else float("nan")
            print(f"  {sym:<10} {tf:<3} long: n={len(longs):>4} sum{np.sum(longs):>+8.1f}R EV{le:>+.3f} | "
                  f"short: n={len(shorts):>4} sum{np.sum(shorts):>+8.1f}R EV{se:>+.3f}")

    # ---- yearly pooled ----
    print(f"\n-- yearly (gross R) --")
    for sym in SYMBOLS:
        for tf in TFS:
            cell = data[sym][tf]
            if cell["skipped"] or not cell["trades"]:
                continue
            by_year = defaultdict(list)
            for t in cell["trades"]:
                by_year[year_of(t["entry_t"])].append(t["rr"])
            parts = "  ".join(f"{y}:{np.sum(v):+.1f}(n={len(v)})" for y, v in sorted(by_year.items()))
            print(f"  {sym:<10} {tf:<3} {parts}")

    # ---- fee sensitivity ----
    print(f"\n-- fee sensitivity (feeR=roundtrip*entry/risk) --")
    print(f"  {'symbol':<10}{'tf':<5}{'gross':>10}{'rt0.05%':>11}{'rt0.10%':>11}")
    for sym in SYMBOLS:
        for tf in ("1h", "4h", "1d"):
            cell = data[sym][tf]
            if cell["skipped"] or not cell["trades"]:
                continue
            trades = cell["trades"]
            cells = []
            for fee in (0.0, 0.0005, 0.0010):
                net = [(t["entry_t"], t["rr"] - fee * t["entry_px"] / t["risk_px"]) for t in trades]
                cells.append(trade_stats(net)["totalR"])
            print(f"  {sym:<10}{tf:<5}{cells[0]:>+9.1f}R {cells[1]:>+10.1f}R {cells[2]:>+10.1f}R")

    # ---- annualized (with honest span) ----
    print(f"\n-- annualized (fixed-stake gross R; per-coin actual window; SNDKB small-sample) --")
    print(f"  {'symbol':<10}{'totalR':>10}{'years':>7}{'annR/yr':>9}{'netAnn@0.10%':>13}")
    for sym in SYMBOLS:
        merged = sorted([t for tf in TFS for t in data[sym][tf]["trades"]], key=lambda x: x["entry_t"])
        if not merged:
            print(f"  {sym:<10} no fills")
            continue
        st_c = trade_stats([(t["entry_t"], t["rr"]) for t in merged])
        df1h = load_df(sym, "1h")
        t_start = int(df1h["time"].iloc[min(CONF5["1h"]["warmup"], len(df1h) - 1)])
        t_end = int(df1h["time"].iloc[-1])
        span_y = max(0.1, (t_end - t_start) / YEAR_MS)
        net_tot = sum(t["rr"] - FEE_NET * t["entry_px"] / t["risk_px"] for t in merged)
        print(f"  {sym:<10}{st_c['totalR']:>+9.1f}R {span_y:>6.2f} {st_c['totalR']/span_y:>+8.1f} "
              f"{net_tot/span_y:>+12.1f}")

    print(f"\n[total] {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
