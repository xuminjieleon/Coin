"""5-year extended backtest of the production strategy + one pre-registered
optimization check (2026-08-25, round 8).

User request: extend BTC/ETH/BNB/SOL sampling to 5 years, re-run the
backtest, output results to a document, and continue optimizing the strategy
IF headroom remains.

Windows: 1h 43800 bars / 4h 10950 / 1d 1825 (5 years); 1w keeps the full
listing history (520-bar request cap, listing-limited ~260-455 bars, already
longer than 5y where available).

Optimization protocol (pre-registered BEFORE any 5y result was seen; this is
the AGENTS.md §7.3 resumption clause — the new data dimension is the extended
history including the pre-2023 era that round-11 tuning never sampled on 1h/4h):
  - 1w is EXCLUDED from optimization (blind samples far too small; declared
    limitation since round 11). Baseline only.
  - Folds by time on the pooled 4-symbol record stream: A 40% (tune),
    B 30% + C 30% (blind, evaluated ONCE).
  - Single-pass coordinate descent from the incumbent production geometry —
    one axis at a time over a small pre-declared value set, selection on
    fold-A pooled totalR (ties -> EV). NO grid restart, NO threshold/fill
    re-opening (both terminated dimensions in round 11b).
      1h: depth {0.5,1.0} stop {2.0,3.0} be {0.05,0.15} tgt {0.5,1.0} texit {48,144}
      4h: depth {0.5,1.0} stop {1.0,1.5} be {0.25,0.75} trail {0.35,0.75} texit {24,96}
      1d: depth {0.5,1.0} stop {1.2,2.0} be {0.25,0.75} trail {0.35,0.75} texit {12,48}
  - Acceptance of the fold-A winner over the incumbent (ALL required, round-11
    rule): pooled blind B+C totalR improvement > 5%; every symbol's blind
    totalR > 0; worst symbol's blind totalR not worse than the incumbent's
    worst by > 5%. Otherwise: no headroom, incumbent stands, optimization
    stays terminated.
  - Capacity-constrained serial execution (one position per symbol), same-bar
    conservative order stop > target > trigger, trail ratchet prior-bar only.
  - Fees/slippage NOT modeled (maker ~0.02%).

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/backtest_5y.py
       [--refresh] [--tf 1h,4h,1d,1w] [--skip-opt]
"""
import argparse
import asyncio
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
from profit2_r5 import with_loose_plans
from backtest_ltc import CONF, TFS, trade_stats

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
W5 = {"1h": 43800, "4h": 10950, "1d": 1825, "1w": 520}
CONF5 = {tf: dict(CONF[tf], window=W5[tf]) for tf in TFS}

AXIS_IDX = {"depth": 0, "stop": 1, "be": 2, "tgt": 3, "texit": 4, "trail": 5}
AXES = {
    "1h": [("depth", (0.5, 1.0)), ("stop", (2.0, 3.0)), ("be", (0.05, 0.15)),
           ("tgt", (0.5, 1.0)), ("texit", (48, 144))],
    "4h": [("depth", (0.5, 1.0)), ("stop", (1.0, 1.5)), ("be", (0.25, 0.75)),
           ("trail", (0.35, 0.75)), ("texit", (24, 96))],
    "1d": [("depth", (0.5, 1.0)), ("stop", (1.2, 2.0)), ("be", (0.25, 0.75)),
           ("trail", (0.35, 0.75)), ("texit", (12, 48))],
}


def fmt_res(st):
    if not st or not st.get("filled"):
        return "n=0"
    pf = f"{st['pf']:.2f}" if st["pf"] != float("inf") else "inf"
    dd = f"{st['maxdd']:.1f}" if st["maxdd"] == st["maxdd"] else "-"
    return (f"成交={st['filled']} 胜率={st['winrate']*100:.1f}% EV={st['ev']:+.3f}R "
            f"总={st['totalR']:+.1f}R DD={dd}R PF={pf}")


def year_of(ms: int) -> int:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year


def prefetch(sym: str) -> dict:
    dfs: dict = {}
    for itv in TFS:
        rows = None
        for attempt in range(4):
            try:
                rows = asyncio.run(ps.kline_cache.get_klines(sym, itv, W5[itv]))
                break
            except Exception as exc:
                wait = 20 * (attempt + 1)
                print(f"[warn] {sym} {itv} fetch failed ({exc}); retry in {wait}s", flush=True)
                time.sleep(wait)
        if rows is None:
            raise SystemExit(f"{sym} {itv} data unavailable")
        dfs[itv] = ps.kline_cache.rows_to_df(rows)
        print(f"[data] {sym} {itv}: {len(dfs[itv])} bars "
              f"({ps.fmt_ts(int(dfs[itv]['time'].iloc[0]))}..)", flush=True)
    return dfs


def compute_records(sym: str, tf: str, dfs: dict) -> list[dict]:
    cfg = CONF5[tf]
    df = dfs[tf]
    n = len(df)
    times = df["time"].to_numpy()
    closes = df["close"].to_numpy()
    records: list[dict] = []
    t0 = time.time()
    cnt = 0
    for i in range(cfg["warmup"], n - cfg["fwd_room"], cfg["spacing"]):
        t = int(times[i])
        htf = []
        for itv, span in cfg["mtf"]:
            m = ps.tf_summary_closed(dfs[itv], t, span)
            if m:
                htf.append(m)
        rec = ps.decide_at(df, htf, t, cfg["warmup"], cfg["min_bars"])
        if rec is None:
            continue
        rec["symbol"] = sym
        records.append(rec)
        cnt += 1
        if cnt % 1000 == 0:
            print(f"[calc] {sym} {tf}: {cnt} ({time.time()-t0:.0f}s)", flush=True)
    records.sort(key=lambda r: r["time"])
    return records


def load_records(sym: str, tf: str, dfs: dict, refresh: bool) -> list[dict]:
    cache_file = os.path.join(ps.CACHE_DIR, f"_5y_cache_{sym}_{tf}.pkl")
    key = {"ver": 1, "tf": tf, "symbol": sym, "window": W5[tf], "src": ps.source_hash()}
    if not refresh and os.path.exists(cache_file):
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


def sim_outcome_fast(highs, lows, closes, n, i, direction, entry, stop,
                     be_frac, tgt_r, texit, fill_bars, trail):
    """Identical logic to profit2_cap.sim_outcome_full; arrays pre-converted."""
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
        if target is not None and (highs[j] >= target if long else lows[j] <= target):
            frac = 0.5 if be else 1.0
            return (locked + frac * tgt_r, fill, j)
        if not be and ((long and highs[j] >= be_trig) or ((not long) and lows[j] <= be_trig)):
            be = True
            locked = 0.5 * be_frac
        if be and trail is not None:
            mfe = (highs[j] - entry) / risk if long else (entry - lows[j]) / risk
            ratchet = max(ratchet, mfe - trail)
    j_end = min(fill + texit, n) - 1
    if j_end < fill:
        j_end = fill
    r = (closes[j_end] - entry) / risk if long else (entry - closes[j_end]) / risk
    if be:
        return (locked + 0.5 * r, fill, j_end)
    return (float(r), fill, j_end)


def capacity_run_fast(recs, geo, arrs, tidx, fill_bars):
    depth, stopw, be_frac, tgt, texit, trail = geo
    highs, lows, closes, n = arrs
    trades: list[tuple[int, float]] = []
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
        out = sim_outcome_fast(highs, lows, closes, n, i, direction, entry, stop,
                               be_frac, tgt, texit, fill_bars, trail)
        if out is None:
            continue
        rr, fill, exit_bar = out
        busy = exit_bar
        trades.append((r["time"], rr))
    return n_orders, trades


def run_tf(tf: str, dfs_all: dict, refresh: bool, skip_opt: bool):
    cfg = CONF5[tf]
    geo = tuple(cfg["geo"])
    fill_bars = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
    print(f"\n{'='*76}\n===== {tf} 5 年扩展回测（BTC/ETH/BNB/SOL，生产策略）=====\n{'='*76}")
    print(f"几何: {ps.geo_str(geo)} fill={fill_bars} th={cfg['th']}")

    loose: dict = {}
    arrs: dict = {}
    tidx: dict = {}
    for sym in SYMBOLS:
        recs = load_records(sym, tf, dfs_all[sym], refresh)
        loose[sym] = recs if cfg["th"] == 25 else with_loose_plans(recs, cfg["th"])
        df = dfs_all[sym][tf]
        arrs[sym] = (df["high"].to_numpy(), df["low"].to_numpy(),
                     df["close"].to_numpy(), len(df))
        tidx[sym] = {int(t): i for i, t in enumerate(df["time"].to_numpy())}

    print("\n-- 分币种基线（全时段容量约束串行）--")
    pooled_trades: list = []
    for sym in SYMBOLS:
        n_orders, trades = capacity_run_fast(loose[sym], geo, arrs[sym], tidx[sym], fill_bars)
        st = trade_stats(trades)
        pooled_trades.extend(trades)
        by_year = defaultdict(list)
        for t, r in trades:
            by_year[year_of(t)].append(r)
        parts = "  ".join(f"{y}:{np.array(v).sum():+.1f}R(n={len(v)})" for y, v in sorted(by_year.items()))
        print(f"  {sym:<9} {fmt_res(st)}")
        print(f"            分年 {parts}")
    pooled_trades.sort(key=lambda x: x[0])
    base_all = trade_stats(pooled_trades)
    print(f"  四币合计: {fmt_res(base_all)}")

    pooled = sorted(((sym, r) for sym in SYMBOLS for r in loose[sym]), key=lambda x: x[1]["time"])
    a = int(len(pooled) * 0.4)
    b = int(len(pooled) * 0.7)
    FA, FB, FC = pooled[:a], pooled[a:b], pooled[b:]
    t_a = FA[-1][1]["time"]
    t_b = FB[-1][1]["time"] if FB else t_a
    print(f"\n[split] 总决策点 {len(pooled)}；A={len(FA)} B={len(FB)} C={len(FC)}"
          f"（A 止于 {ps.fmt_ts(t_a)}，B 止于 {ps.fmt_ts(t_b)}）")

    def eval_geo(pooled_list):
        by_sym = defaultdict(list)
        for sym, r in pooled_list:
            by_sym[sym].append(r)
        trades: list = []
        orders = 0
        for sym in SYMBOLS:
            if sym not in by_sym:
                continue
            n, tr = capacity_run_fast(by_sym[sym], geo_cur[0], arrs[sym], tidx[sym], fill_bars)
            orders += n
            trades.extend(tr)
        trades.sort(key=lambda x: x[0])
        st = trade_stats(trades)
        st["orders"] = orders
        return st, trades

    geo_cur = [geo]
    base_blind, blind_trades = eval_geo(FB + FC)
    print(f"\n-- 生产配置 盲测 B+C --\n  合计: {fmt_res(base_blind)}")
    blind_by_sym = {}
    for sym in SYMBOLS:
        sub = [(s, r) for s, r in FB + FC if s == sym]
        st, _ = eval_geo(sub)
        blind_by_sym[sym] = st
        print(f"  {sym:<9} {fmt_res(st)}")

    if tf == "1w" or skip_opt:
        print("\n[优化] 1w 样本不足（预登记排除）/ --skip-opt：只做基线，不优化")
        return {"tf": tf, "base_all": base_all, "base_blind": base_blind}

    print(f"\n-- 优化检查（单遍坐标下降，fold A，{ps.geo_str(geo)} 起）--")
    cur = tuple(geo)
    cur_a, _ = eval_geo(FA)
    print(f"  [incumbent A] {ps.geo_str(cur)}: {fmt_res(cur_a)}")
    for axis, values in AXES[tf]:
        idx = AXIS_IDX[axis]
        best_geo, best_st = cur, cur_a
        for v in values:
            cand = list(cur)
            cand[idx] = v
            cand = tuple(cand)
            geo_cur[0] = cand
            st, _ = eval_geo(FA)
            marker = ""
            if st.get("filled") and st["totalR"] > best_st["totalR"] + 1e-9:
                best_geo, best_st, marker = cand, st, "  <- A 段更优"
            print(f"  [{axis}={v}] {fmt_res(st)}{marker}")
        geo_cur[0] = cur
        if best_geo != cur:
            cur, cur_a = best_geo, best_st
            print(f"  [采纳] -> {ps.geo_str(cur)}: {fmt_res(cur_a)}")
    print(f"\n  [A 段最终候选] {ps.geo_str(cur)}")

    if cur == tuple(geo):
        print("  候选与生产配置相同：无优化空间，维持现状")
        return {"tf": tf, "base_all": base_all, "base_blind": base_blind,
                "cand": cur, "cand_blind": None, "accepted": False}

    geo_cur[0] = cur
    cand_blind, _ = eval_geo(FB + FC)
    cand_by_sym = {}
    for sym in SYMBOLS:
        sub = [(s, r) for s, r in FB + FC if s == sym]
        st, _ = eval_geo(sub)
        cand_by_sym[sym] = st
    print(f"\n-- 候选盲测 B+C（一次性）--\n  候选: {ps.geo_str(cur)}")
    print(f"  合计: {fmt_res(cand_blind)}")
    for sym in SYMBOLS:
        print(f"  {sym:<9} {fmt_res(cand_by_sym[sym])}")

    imp = cand_blind["totalR"] / base_blind["totalR"] - 1.0 if base_blind["totalR"] > 0 else float("inf")
    worst_inc = min(blind_by_sym[s]["totalR"] for s in SYMBOLS)
    worst_cand = min(cand_by_sym[s]["totalR"] for s in SYMBOLS)
    ok1 = imp > 0.05
    ok2 = all(cand_by_sym[s]["totalR"] > 0 for s in SYMBOLS)
    ok3 = worst_cand >= worst_inc * 0.95
    print(f"\n  验收: 盲测提升 {imp*100:+.1f}%（需 >5%: {'过' if ok1 else '不过'}）；"
          f"全部币种盲测>0（{'过' if ok2 else '不过'}）；"
          f"最差币种 {worst_cand:+.1f}R vs 生产 {worst_inc:+.1f}R（{'过' if ok3 else '不过'}）")
    accepted = ok1 and ok2 and ok3
    print(f"  结论: {'接受候选（需更新 decision.py PLAN_GEOMETRY 并重跑校验）' if accepted else '拒绝候选：维持生产配置，本轮无优化空间'}")
    return {"tf": tf, "base_all": base_all, "base_blind": base_blind,
            "cand": cur, "cand_blind": cand_blind, "accepted": accepted}


def phase2_run(tf: str, dfs_all: dict, refresh: bool):
    """Round-11 shipping standard: re-tune on A+B (coordinate descent from the
    production geometry), then ONE-SHOT blind validation on fold C. Fold C was
    never used for selection in phase 1 or here (phase 1 selected on A only,
    evaluated B+C once for reporting). If the phase-2 candidate passes the same
    three acceptance criteria on C, it ships; if it fails, NO adoption even if
    phase 1 passed (no cherry-picking between blind results)."""
    cfg = CONF5[tf]
    if tf == "1w":
        print("\n[phase2] 1w excluded (pre-registered)")
        return None
    geo = tuple(cfg["geo"])
    fill_bars = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
    print(f"\n{'='*76}\n===== {tf} Phase 2：A+B 重调参 → C 段一次性盲测 =====\n{'='*76}")

    loose: dict = {}
    arrs: dict = {}
    tidx: dict = {}
    for sym in SYMBOLS:
        recs = load_records(sym, tf, dfs_all[sym], refresh)
        loose[sym] = recs if cfg["th"] == 25 else with_loose_plans(recs, cfg["th"])
        df = dfs_all[sym][tf]
        arrs[sym] = (df["high"].to_numpy(), df["low"].to_numpy(),
                     df["close"].to_numpy(), len(df))
        tidx[sym] = {int(t): i for i, t in enumerate(df["time"].to_numpy())}

    pooled = sorted(((sym, r) for sym in SYMBOLS for r in loose[sym]), key=lambda x: x[1]["time"])
    a = int(len(pooled) * 0.4)
    b = int(len(pooled) * 0.7)
    FA, FB, FC = pooled[:a], pooled[a:b], pooled[b:]

    def eval_geo(pooled_list, geo_val):
        by_sym = defaultdict(list)
        for sym, r in pooled_list:
            by_sym[sym].append(r)
        trades: list = []
        for sym in SYMBOLS:
            if sym not in by_sym:
                continue
            _, tr = capacity_run_fast(by_sym[sym], geo_val, arrs[sym], tidx[sym], fill_bars)
            trades.extend(tr)
        trades.sort(key=lambda x: x[0])
        return trade_stats(trades)

    def per_sym(pooled_list, geo_val):
        out = {}
        for sym in SYMBOLS:
            sub = [(s, r) for s, r in pooled_list if s == sym]
            out[sym] = eval_geo(sub, geo_val)
        return out

    inc_c = eval_geo(FC, geo)
    inc_c_sym = per_sym(FC, geo)
    print(f"\n-- 生产配置 C 段盲测 --\n  合计: {fmt_res(inc_c)}")
    for sym in SYMBOLS:
        print(f"  {sym:<9} {fmt_res(inc_c_sym[sym])}")

    print(f"\n-- A+B 重调参（单遍坐标下降，{ps.geo_str(geo)} 起）--")
    cur = tuple(geo)
    cur_ab = eval_geo(FA + FB, cur)
    print(f"  [incumbent A+B] {ps.geo_str(cur)}: {fmt_res(cur_ab)}")
    for axis, values in AXES[tf]:
        idx = AXIS_IDX[axis]
        best_geo, best_st = cur, cur_ab
        for v in values:
            cand = list(cur)
            cand[idx] = v
            cand = tuple(cand)
            st = eval_geo(FA + FB, cand)
            marker = ""
            if st.get("filled") and st["totalR"] > best_st["totalR"] + 1e-9:
                best_geo, best_st, marker = cand, st, "  <- A+B 更优"
            print(f"  [{axis}={v}] {fmt_res(st)}{marker}")
        if best_geo != cur:
            cur, cur_ab = best_geo, best_st
            print(f"  [采纳] -> {ps.geo_str(cur)}: {fmt_res(cur_ab)}")
    print(f"\n  [A+B 段最终候选] {ps.geo_str(cur)}")

    if cur == geo:
        print("  候选与生产配置相同：Phase 2 无改动，不采纳")
        return {"tf": tf, "cand": cur, "accepted": False, "same": True}

    cand_c = eval_geo(FC, cur)
    cand_c_sym = per_sym(FC, cur)
    print(f"\n-- 候选 C 段盲测（一次性）--\n  候选: {ps.geo_str(cur)}")
    print(f"  合计: {fmt_res(cand_c)}")
    for sym in SYMBOLS:
        print(f"  {sym:<9} {fmt_res(cand_c_sym[sym])}")

    imp = cand_c["totalR"] / inc_c["totalR"] - 1.0 if inc_c["totalR"] > 0 else float("inf")
    worst_inc = min(inc_c_sym[s]["totalR"] for s in SYMBOLS)
    worst_cand = min(cand_c_sym[s]["totalR"] for s in SYMBOLS)
    ok1 = imp > 0.05
    ok2 = all(cand_c_sym[s]["totalR"] > 0 for s in SYMBOLS)
    ok3 = worst_cand >= worst_inc * 0.95
    print(f"\n  验收（C 段）: 提升 {imp*100:+.1f}%（需 >5%: {'过' if ok1 else '不过'}）；"
          f"全部币种>0（{'过' if ok2 else '不过'}）；"
          f"最差币种 {worst_cand:+.1f}R vs 生产 {worst_inc:+.1f}R（{'过' if ok3 else '不过'}）")
    accepted = ok1 and ok2 and ok3
    print(f"  结论: {'接受：更新生产 PLAN_GEOMETRY' if accepted else '拒绝：维持生产配置（Phase 2 一票否决，不回退到 Phase 1 候选）'}")
    return {"tf": tf, "cand": cur, "accepted": accepted, "same": False,
            "inc_c": inc_c, "cand_c": cand_c}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--tf", default=None)
    ap.add_argument("--skip-opt", action="store_true")
    ap.add_argument("--phase2", action="store_true")
    args = ap.parse_args()
    tfs = args.tf.split(",") if args.tf else list(TFS)
    dfs_all = {sym: prefetch(sym) for sym in SYMBOLS}

    if args.phase2:
        for tf in [t for t in tfs if t != "1w"] if args.tf else ("1h", "4h", "1d"):
            phase2_run(tf, dfs_all, args.refresh)
        return

    results = []
    for tf in tfs:
        results.append(run_tf(tf, dfs_all, args.refresh, args.skip_opt))

    print(f"\n{'='*76}\n===== 5 年扩展汇总（生产配置，容量约束串行，未计手续费）=====\n{'='*76}")
    for r in results:
        st = r["base_all"]
        print(f"  {r['tf']:<4} 四币合计: {fmt_res(st)}   盲测B+C: {fmt_res(r['base_blind'])}")


if __name__ == "__main__":
    main()
