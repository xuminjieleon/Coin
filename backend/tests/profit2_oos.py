"""Round 11b: expanded-universe out-of-sample validation.

User requirement: >= 100 blind sample points per scenario (ideally 1000);
the 1w capacity-constrained blind had only 46 trades. Fix: expand the symbol
cross-section. The round-11 geometry/threshold were tuned ONLY on BTC/ETH/SOL,
so 10 additional long-history symbols are PURE out-of-sample (their timelines
never informed any choice). This also addresses why 1d/1w total profit is
lower than 4h: total = EV x trade count, and count scales with bar count /
symbol count, not years.

Per timeframe (4h/1d/1w), production geometry + th=10:
  1. Tuned trio blind B+C (capacity) - consistency check vs round 11
  2. Fresh symbols full timeline (capacity, per symbol) - pure OOS
  3. All-symbols aggregate (capacity + unconstrained statistics)

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/profit2_oos.py
"""
import asyncio
import os
import pickle
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import profit_sweep2 as ps
from profit2_cap import sim_outcome_full

TUNED = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
FRESH = ["BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT", "LTCUSDT",
         "DOTUSDT", "TRXUSDT", "ETCUSDT", "BCHUSDT"]
ALL = TUNED + FRESH

GEOS = {
    "4h": ((0.75, 1.2, 0.5, None, 48, 0.5), 1.5),
    "1d": ((0.75, 1.5, 0.5, None, 24, 0.5), 1.5),
    "1w": ((0.75, 1.5, 0.5, None, 24, 0.75), 2.0),
}


def fetch_dfs(tf: str, symbols: list) -> dict:
    need = {tf} | ({"1d"} if tf == "4h" else set())
    dfs: dict = {}
    for sym in symbols:
        entry: dict = {}
        ok = True
        for itv in sorted(need):
            rows = None
            for attempt in range(4):
                try:
                    rows = asyncio.run(ps.kline_cache.get_klines(sym, itv, ps.CFG[itv]["window"]))
                    break
                except Exception as exc:
                    wait = 20 * (attempt + 1)
                    print(f"[warn] {sym} {itv} fetch failed ({exc}); retry in {wait}s", flush=True)
                    time.sleep(wait)
            if rows is None:
                print(f"[skip] {sym}: {itv} data unavailable")
                ok = False
                break
            entry[itv] = ps.kline_cache.rows_to_df(rows)
            print(f"[data] {sym} {itv}: {len(entry[itv])} bars")
        if ok:
            dfs[sym] = entry
    return dfs


def compute_fresh(tf: str, dfs: dict, symbols: list) -> list:
    cfg = ps.CFG[tf]
    records: list = []
    for sym in symbols:
        df = dfs[sym][tf]
        n = len(df)
        if n <= cfg["warmup"] + cfg["fwd_room"] + cfg["spacing"]:
            print(f"[skip] {sym} {tf}: too few bars ({n})")
            continue
        times = df["time"].to_numpy()
        closes = df["close"].to_numpy()
        idxs = range(cfg["warmup"], n - cfg["fwd_room"], cfg["spacing"])
        cnt = 0
        t0 = time.time()
        for i in idxs:
            t = int(times[i])
            htf = []
            if tf == "4h" and dfs[sym].get("1d") is not None:
                m = ps.tf_summary_closed(dfs[sym]["1d"], t, ps.D1_MS)
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
            if cnt % 150 == 0:
                print(f"[calc] {sym} {tf}: {cnt} ({time.time()-t0:.0f}s)", flush=True)
        print(f"[data] {sym} {tf}: {n} bars, {cnt} points")
    records.sort(key=lambda r: r["time"])
    return records


def load_fresh_records(tf: str, dfs: dict, symbols: list) -> list:
    cache_file = os.path.join(ps.CACHE_DIR, f"_profit2_oos_cache_{tf}.pkl")
    key = {"ver": 1, "tf": tf, "symbols": symbols, "src": ps.source_hash()}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                entry = pickle.load(f)
            if entry.get("key") == key:
                print(f"[cache] {tf} OOS: {len(entry['records'])} records")
                return entry["records"]
        except Exception:
            pass
    records = compute_fresh(tf, dfs, symbols)
    tmp = cache_file + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump({"key": key, "records": records}, f)
    os.replace(tmp, cache_file)
    print(f"[cache] {tf} OOS: saved {len(records)} records")
    return records


def capacity_eval(records, geo, dfs, tidx, tf, fill_mult=1.0):
    depth, stopw, be_frac, tgt, texit, trail = geo
    cfg = ps.CFG[tf]
    fill_bars = max(1, int(round(cfg["fill_bars"] * fill_mult)))
    trades: list = []
    n_orders = 0
    busy: dict = {}
    for r in records:
        if r.get("plan") is None:
            continue
        sym = r["symbol"]
        df = dfs[sym][tf]
        i = tidx[sym][tf].get(r["time"])
        if i is None:
            continue
        if i <= busy.get(sym, -1):
            continue
        built = ps.build_plan(r, depth, stopw)
        if built is None:
            continue
        direction, entry, stop = built
        n_orders += 1
        out = sim_outcome_full(df, i, direction, entry, stop, be_frac, tgt, texit, fill_bars, trail)
        if out is None:
            continue
        rr, fill, exit_bar = out
        busy[sym] = exit_bar
        trades.append((r["time"], rr))
    if not trades:
        return {"orders": n_orders, "filled": 0, "winrate": float("nan"), "ev": float("nan"),
                "totalR": 0.0, "maxdd": float("nan"), "pf": float("nan")}
    arr = np.array([t[1] for t in trades], dtype=float)
    cum = np.cumsum(arr)
    peak = np.maximum.accumulate(np.concatenate(([0.0], cum)))[1:]
    maxdd = float(np.max(peak - cum))
    wins = arr[arr > 1e-9].sum()
    losses = -arr[arr < -1e-9].sum()
    pf = float(wins / losses) if losses > 1e-9 else float("inf")
    return {"orders": n_orders, "filled": len(arr), "winrate": float(np.mean(arr > 1e-9)),
            "ev": float(arr.mean()), "totalR": float(arr.sum()), "maxdd": maxdd, "pf": pf}


def fmt(res):
    if not res["filled"]:
        return "n=0"
    dd = f"{res['maxdd']:.1f}"
    pf = f"{res['pf']:.2f}" if res["pf"] != float("inf") else "inf"
    return (f"成交={res['filled']}/{res['orders']} 胜率={res['winrate']*100:.1f}% "
            f"EV={res['ev']:+.3f}R 总={res['totalR']:+.1f}R DD={dd}R PF={pf}")


def main():
    for tf in ("4h", "1d", "1w"):
        geo, fill = GEOS[tf]
        dfs = fetch_dfs(tf, ALL)
        fresh_syms = [s for s in FRESH if s in dfs]
        trio_syms = [s for s in TUNED if s in dfs]
        tidx = {s: {k: {int(t): i for i, t in enumerate(dfs[s][k]["time"].to_numpy())}
                    for k in dfs[s]} for s in dfs}

        tuned_recs = ps.load_records(tf, {s: dfs[s] for s in trio_syms}, mtf1w=False, refresh=False)
        FA, FB, FC = ps.folds(tuned_recs)
        fresh_recs = load_fresh_records(tf, dfs, fresh_syms)

        print(f"\n{'='*72}\n===== {tf} 扩展样本验证（{ps.geo_str(geo)} fill×{fill} th=10）=====\n{'='*72}")
        print(f"决策点：调参三币 {len(tuned_recs)}（盲测 B+C {len(FB)+len(FC)}）"
              f"+ 新增 {len(fresh_syms)} 币 {len(fresh_recs)} = {len(tuned_recs)+len(fresh_recs)}")

        print("\n-- 调参三币 盲测 B+C（容量约束，对照第 11 轮）--")
        r = capacity_eval(FB + FC, geo, dfs, tidx, tf, fill)
        print(f"   {fmt(r)}")

        print(f"\n-- 新增 {len(fresh_syms)} 币种 纯样本外 全时段（容量约束，分币种）--")
        neg_syms = []
        for s in fresh_syms:
            sub = [x for x in fresh_recs if x["symbol"] == s]
            rr = capacity_eval(sub, geo, dfs, tidx, tf, fill)
            mark = "" if rr["filled"] == 0 or rr["ev"] > 0 else "  <- EV转负"
            if rr["filled"] > 0 and rr["ev"] <= 0:
                neg_syms.append(s)
            print(f"   {s:<9} 决策点 {len(sub):>5}  {fmt(rr)}{mark}")
        ra = capacity_eval(fresh_recs, geo, dfs, tidx, tf, fill)
        print(f"   新增合计: {fmt(ra)}")

        all_recs = tuned_recs + fresh_recs
        rc = capacity_eval(all_recs, geo, dfs, tidx, tf, fill)
        print(f"\n-- 全部 {len(dfs)} 币种 全时段（容量约束）--")
        print(f"   {fmt(rc)}")
        ru = ps.evaluate(all_recs, geo, "base", dfs, tidx, tf, fill)
        print(f"   无约束统计口径: 成交={ru['filled']} 胜率={ru['winrate']*100:.1f}% "
              f"EV={ru['ev']:+.3f}R 总={ru['totalR']:+.1f}R")
        if neg_syms:
            print(f"   [注意] 新增币种中 EV 为负: {', '.join(neg_syms)}")


if __name__ == "__main__":
    main()
