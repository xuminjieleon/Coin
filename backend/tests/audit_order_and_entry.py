"""AUDIT (2026-08-28): quantify two suspected backtest-vs-production gaps.

Gap 1 (same-bar ordering, 1h target family):
  backtest_5y.sim_outcome_fast checks TARGET before the BE trigger on a bar.
  journal_store.replay_plan checks BE trigger before TARGET (plan semantics:
  resting +beR scale-out limit fills before price reaches the target).
  When one bar spans both +beR and +targetR with BE not yet done, the
  backtest credits the FULL target (1.0*tgtR) while the journal/realistic
  convention credits 0.5*beR + 0.5*tgtR. Difference = +0.5*(tgt-be) per
  such trade, i.e. the backtest is OPTIMISTIC on those trades.
  Control: trail families (4h/1d) where both orderings are argued equivalent
  - the journal-order variant must reproduce the baseline exactly.

Gap 2 (entry-zone selection):
  Production decision._build_trade_plan picks the BEST-QUALITY zone within
  depth*ATR and enters at its edge; the harness record stores only the
  best-quality zone within 1.5*ATR and ps.build_plan falls back to
  price -/+ depth*ATR when that zone is beyond depth*ATR - even when a
  DIFFERENT (lower-quality) zone sits within depth*ATR. Recompute the true
  production entry on sampled decisions and measure incidence + R impact.

Read-only audit: no production files modified. Prints machine-verifiable
numbers only.

Usage: PYTHONIOENCODING=utf-8 ..\\.venv\\Scripts\\python.exe tests\\audit_order_and_entry.py
"""
import asyncio
import os
import pickle
import sys
from multiprocessing import Pool

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import profit_sweep2 as ps
from profit2_r5 import with_loose_plans
from backtest_ltc import CONF, TFS, trade_stats
from backtest_5y import W5, capacity_run_fast, sim_outcome_fast

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]


def sim_journal_order(highs, lows, closes, n, i, direction, entry, stop,
                      be_frac, tgt_r, texit, fill_bars, trail):
    """sim_outcome_fast with journal_store ordering: stop -> BE trigger ->
    trail update (current bar) -> target -> time exit."""
    long = direction == "long"
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    target = None
    if tgt_r is not None:
        target = entry + tgt_r * risk if long else entry - tgt_r * risk
    be_trig = entry + be_frac * risk if long else entry - be_frac * risk
    fill = None
    for j in range(i + 1, min(i + 1 + fill_bars, n)):
        if (long and lows[j] <= entry) or ((not long) and highs[j] >= entry):
            fill = j
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
        hit_stop = lows[j] <= stop_lvl if long else highs[j] >= stop_lvl
        if hit_stop:
            if not be:
                return (-1.0, fill, j)
            runner_r = ratchet if trail is not None else 0.0
            return (locked + 0.5 * runner_r, fill, j)
        if not be and ((long and highs[j] >= be_trig) or ((not long) and lows[j] <= be_trig)):
            be = True
            locked = 0.5 * be_frac
        if be and trail is not None:
            mfe = (highs[j] - entry) / risk if long else (entry - lows[j]) / risk
            ratchet = max(ratchet, mfe - trail)
        if target is not None and (highs[j] >= target if long else lows[j] <= target):
            frac = 0.5 if be else 1.0
            return (locked + frac * tgt_r, fill, j)
    j_end = min(fill + texit, n) - 1
    if j_end < fill:
        j_end = fill
    r = (closes[j_end] - entry) / risk if long else (entry - closes[j_end]) / risk
    if be:
        return (locked + 0.5 * r, fill, j_end)
    return (float(r), fill, j_end)


def capacity_run(recs, geo, arrs, tidx, fill_bars, sim):
    depth, stopw, be_frac, tgt, texit, trail = geo
    highs, lows, closes, n = arrs
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
        trades.append((r["time"], rr, direction, i, fill, exit_bar, entry, stop))
    return trades


_FAR_FUTURE = 4_000_000_000_000  # anchor at newest cached bar: cache-only read


def fetch_df(sym, tf):
    # end_time set -> _get_history reads SQLite first; fully-covered 5y windows
    # never touch the network (also avoids the shared-httpx-client-across-
    # asyncio.run-loops pitfall).
    rows = asyncio.run(ps.kline_cache.get_klines(sym, tf, W5[tf], end_time=_FAR_FUTURE))
    return ps.kline_cache.rows_to_df(rows)


def gap1(tf):
    cfg = CONF[tf]
    geo = tuple(cfg["geo"])
    fill_bars = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
    tot_base, tot_alt = [], []
    diff_trades = 0
    delta_sum = 0.0
    for sym in SYMBOLS:
        df = fetch_df(sym, tf)
        cache_file = os.path.join(ps.CACHE_DIR, f"_5y_cache_{sym}_{tf}.pkl")
        with open(cache_file, "rb") as f:
            recs = pickle.load(f)["records"]
        recs = recs if cfg["th"] == 25 else with_loose_plans(recs, cfg["th"])
        arrs = (df["high"].to_numpy(), df["low"].to_numpy(),
                df["close"].to_numpy(), len(df))
        tidx = {int(t): k for k, t in enumerate(df["time"].to_numpy())}
        tb = capacity_run(recs, geo, arrs, tidx, fill_bars, sim_outcome_fast)
        ta = capacity_run(recs, geo, arrs, tidx, fill_bars, sim_journal_order)
        assert len(tb) == len(ta), f"{sym} {tf}: trade count {len(tb)} vs {len(ta)}"
        db = sum(t[1] for t in tb)
        da = sum(t[1] for t in ta)
        same_bar = sum(1 for x, y in zip(tb, ta)
                       if abs(x[1] - y[1]) > 1e-12 and x[4] == x[5])
        later_bar = sum(1 for x, y in zip(tb, ta)
                        if abs(x[1] - y[1]) > 1e-12 and x[4] != x[5])
        chg = same_bar + later_bar
        dsum = sum(x[1] - y[1] for x, y in zip(tb, ta))
        diff_trades += chg
        delta_sum += dsum
        tot_base.extend(tb)
        tot_alt.extend(ta)
        print(f"  {sym:<9} 基准={db:+9.1f}R 日记口径={da:+9.1f}R 差异={dsum:+7.2f}R "
              f"(变动 {chg}/{len(tb)} 笔: 成交根本身 {same_bar}, 后续单根爆发 {later_bar})",
              flush=True)
    sb = trade_stats([(t[0], t[1]) for t in tot_base])
    sa = trade_stats([(t[0], t[1]) for t in tot_alt])
    print(f"  合计: 基准 {sb['totalR']:+.1f}R (n={sb['filled']}) vs "
          f"日记口径 {sa['totalR']:+.1f}R (n={sa['filled']}) "
          f"=> 同根K线顺序差异 {delta_sum:+.2f}R / {diff_trades} 笔", flush=True)
    return sb, sa


# ---------------- Gap 2: entry-zone selection ----------------

def prod_entry(zones, long, price, depth, atr):
    """Production decision._build_trade_plan entry formula."""
    if long:
        zs = [z for z in zones if z["type"] == "bullish" and not z["mitigated"] and z["top"] <= price]
        near = [z for z in zs if price - z["top"] <= depth * atr]
        if near:
            zone = max(near, key=lambda z: z.get("quality") or 0)
            return min(price, zone["top"])
        return price - depth * atr
    zs = [z for z in zones if z["type"] == "bearish" and not z["mitigated"] and z["bottom"] >= price]
    near = [z for z in zs if z["bottom"] - price <= depth * atr]
    if near:
        zone = max(near, key=lambda z: z.get("quality") or 0)
        return max(price, zone["bottom"])
    return price + depth * atr


def gap2_worker(args):
    sym, tf, sample_idxs = args
    from services.analysis import engine  # noqa: E402  (worker-local import)
    cfg = CONF[tf]
    depth = cfg["geo"][0]
    stopw = cfg["geo"][1]
    df = fetch_df(sym, tf)
    cache_file = os.path.join(ps.CACHE_DIR, f"_5y_cache_{sym}_{tf}.pkl")
    with open(cache_file, "rb") as f:
        recs = pickle.load(f)["records"]
    out = []
    for k in sample_idxs:
        r = recs[k]
        if r.get("plan") is None or not r.get("atr"):
            continue
        t = int(r["time"])
        sub = df[df["time"] <= t].tail(cfg["warmup"]).reset_index(drop=True)
        if len(sub) < cfg["min_bars"]:
            continue
        full = engine.full_analysis(sub)
        zones = full["smc"]["orderBlocks"] + full["smc"]["fvgs"]
        long = r["plan"] == "long"
        price, atr = float(r["price"]), float(r["atr"])
        built = ps.build_plan(r, depth, stopw)
        if built is None:
            continue
        _, e_bt, s_bt = built
        e_prod = prod_entry(zones, long, price, depth, atr)
        s_prod = (e_prod - stopw * atr) if long else (e_prod + stopw * atr)
        mismatch = abs(e_prod - e_bt) > 1e-9
        rec_out = {"sym": sym, "t": t, "long": long, "e_bt": e_bt, "e_prod": e_prod,
                   "atr": atr, "mismatch": mismatch}
        if mismatch:
            highs = df["high"].to_numpy()
            lows = df["low"].to_numpy()
            closes = df["close"].to_numpy()
            n = len(df)
            i = int(np.searchsorted(df["time"].to_numpy(), t))
            fb = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
            _, _, be_f, tgt, texit, trail = cfg["geo"]
            o_bt = sim_outcome_fast(highs, lows, closes, n, i, "long" if long else "short",
                                    e_bt, s_bt, be_f, tgt, texit, fb, trail)
            o_pd = sim_outcome_fast(highs, lows, closes, n, i, "long" if long else "short",
                                    e_prod, s_prod, be_f, tgt, texit, fb, trail)
            rec_out["r_bt"] = o_bt[0] if o_bt else None
            rec_out["r_prod"] = o_pd[0] if o_pd else None
        out.append(rec_out)
    return sym, tf, out


def gap2():
    rng = np.random.RandomState(42)
    jobs = []
    for sym in SYMBOLS:
        for tf in ("1h", "4h", "1d"):
            cache_file = os.path.join(ps.CACHE_DIR, f"_5y_cache_{sym}_{tf}.pkl")
            with open(cache_file, "rb") as f:
                recs = pickle.load(f)["records"]
            plan_idxs = [k for k, r in enumerate(recs) if r.get("plan") is not None]
            take = min(400, len(plan_idxs))
            sample = sorted(rng.choice(plan_idxs, size=take, replace=False).tolist())
            jobs.append((sym, tf, sample))
    all_rows = []
    with Pool(6) as pool:
        for sym, tf, rows in pool.map(gap2_worker, jobs):
            mm = [x for x in rows if x["mismatch"]]
            all_rows.extend(rows)
            both = [x for x in mm if x.get("r_bt") is not None and x.get("r_prod") is not None]
            only_bt = [x for x in mm if x.get("r_bt") is not None and x.get("r_prod") is None]
            only_pd = [x for x in mm if x.get("r_bt") is None and x.get("r_prod") is not None]
            d_r = sum(x["r_bt"] - x["r_prod"] for x in both)
            d_r += sum(x["r_bt"] for x in only_bt) - sum(x["r_prod"] for x in only_pd)
            print(f"  {sym:<9} {tf:<3} 样本={len(rows):>4} 入场价不一致={len(mm):>4} "
                  f"({100.0*len(mm)/max(1,len(rows)):.2f}%) "
                  f"|不一致|/ATR 均值={np.mean([abs(x['e_bt']-x['e_prod'])/x['atr'] for x in mm]) if mm else 0:.3f} "
                  f"独立重放ΔR(回测-生产)={d_r:+.2f}R", flush=True)
    tot = len(all_rows)
    mm = [x for x in all_rows if x["mismatch"]]
    print(f"  总计: 样本 {tot}，入场不一致 {len(mm)} ({100.0*len(mm)/max(1,tot):.2f}%)", flush=True)


if __name__ == "__main__":
    print("=" * 78)
    print("Gap 1: 同根K线 目标vs保本触发 顺序差异（1h=目标族；4h/1d=跟踪族对照组应≈0）")
    print("=" * 78)
    for tf in ("1h", "4h", "1d"):
        print(f"\n-- {tf} --", flush=True)
        gap1(tf)
    print()
    print("=" * 78)
    print("Gap 2: 入场区域选择差异（生产=depth内最优质量区域 vs 回测=1.5ATR内最优+超界回退）")
    print("=" * 78)
    gap2()
