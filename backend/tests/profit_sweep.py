"""Profit-first walk-forward sweep of trade-plan geometry on 4h/1d (2026-08-22).

User spec (declared BEFORE looking at results):
  - Sample decisions from 2-year klines per interval: 4h≈4380 bars, 1d≈730
    bars, BTC/ETH/SOL. Decisions on the interval's own bars; MTF context for
    4h uses only FULLY CLOSED 1d bars (no partial-bar lookahead).
  - Objective order: 1) total profit = sum of R over filled trades;
    2) EV per trade; 3) win rate / direction accuracy. NO 90%+ win-rate
    constraint this round.
  - Only 4h and 1d are optimized (user does not trade intraday).
  - Guards (all stages): filled >= 120 (4h) / >= 30 (1d), fill rate >= 25%,
    EV >= +0.05R gross.
  - Coarse pre-declared grid (144 cells):
      depth {0.75, 1.0} ATR x stop {2.0, 2.5, 3.0} ATR x
      mgmt {plain, be05, scale05, scale10} x tgt {1.5, 2.0, 3.0} R x
      texit {24, 96} bars; limit order valid 12 bars (4h) / 6 bars (1d).
    mgmt: plain = fixed stop; be05 = stop->entry at +0.5R (no partial);
    scale05/scale10 = exit HALF at +0.5R/+1.0R, stop->entry, runner to target.
  - Protocol: time folds A 40% (tune) -> B 30% (blind) -> re-tune A+B ->
    C 30% (blind); leave-one-symbol-out blind check; 1/2/1/4 thinning on C.
    Same-bar conservative order: stop > target > trigger.
  - Fees/slippage NOT modeled (maker entry ~0.02%): all EV is gross.

Usage:
  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/profit_sweep.py \
      [--interval 4h|1d] [--refresh]
"""
import argparse
import asyncio
import hashlib
import os
import pickle
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from services import kline_cache
from services.analysis import decision, engine

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
TWO_YEARS = {"4h": 4380, "1d": 730}
WARMUP = {"4h": 500, "1d": 300}
MIN_BARS = {"4h": 300, "1d": 220}
SPACING = {"4h": 4, "1d": 2}          # decision spacing in bars
FILL_BARS = {"4h": 12, "1d": 6}       # limit order validity window
FWD_ROOM = 110                        # bars reserved for fill + texit
D1_MS = 86_400_000

DEPTHS = (0.75, 1.0)
STOPS = (2.0, 2.5, 3.0)
MGMTS = ("plain", "be05", "scale05", "scale10")
TGTS = (1.5, 2.0, 3.0)
TEXITS = (24, 96)

MGMT_BE = {"plain": None, "be05": 0.5, "scale05": 0.5, "scale10": 1.0, "scale01": 0.10}
MGMT_SCALE = {"plain": False, "be05": False, "scale05": True, "scale10": True, "scale01": True}

BASELINE = (0.75, 2.5, "scale01", 0.75, 96)  # current production geometry (1h-calibrated)

GATES = {
    "all": lambda r: True,
    "aligned25": lambda r: abs(r["score"]) >= 25 and r["alignment"] == "aligned",
    "trending": lambda r: r["regime"] == "trending",
    "ranging": lambda r: r["regime"] == "ranging",
}

MIN_FILLED = {"4h": 120, "1d": 30}
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_profit_cache.pkl")
HARNESS_VER = 1


def last_valid(series: list) -> float | None:
    for v in reversed(series):
        if v is not None:
            return v
    return None


def fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def source_hash() -> str:
    parts = []
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "services", "analysis")
    for f in sorted(os.listdir(base)):
        if f.endswith(".py"):
            with open(os.path.join(base, f), "rb") as fh:
                parts.append(f + ":" + hashlib.md5(fh.read()).hexdigest()[:8])
    return ";".join(parts)


def mtf_summary_closed(df1d, t: int) -> dict | None:
    """1d summary from FULLY CLOSED daily bars only (bar close time <= t)."""
    sub = df1d[df1d["time"] + D1_MS <= t].tail(300).reset_index(drop=True)
    if len(sub) < 60:
        return None
    full = engine.full_analysis(sub)
    closes = sub["close"]
    s = decision.build_summary(
        last_close=float(closes.iloc[-1]),
        smc=full["smc"],
        indicators=full["indicators"],
        volume_profile=full["volumeProfile"],
        wyckoff=full["wyckoff"],
        volatility=full["volatility"],
        cvd_div=full["cvdDivergence"],
        atr=last_valid(full["indicators"]["atr14"]),
    )
    return {"interval": "1d", "score": s["score"], "bias": s["bias"], "regime": s["regime"],
            "cvdDiv": (full["cvdDivergence"] or {}).get("type")}


def decide_at(df, df1d, t: int, warmup: int, min_bars: int) -> dict | None:
    sub = df[df["time"] <= t].tail(warmup).reset_index(drop=True)
    if len(sub) < min_bars:
        return None
    full = engine.full_analysis(sub)
    closes = sub["close"]
    price_now = float(closes.iloc[-1])

    mtf = []
    if df1d is not None:
        m = mtf_summary_closed(df1d, t)
        if m:
            mtf.append(m)
    biases = [m["bias"] for m in mtf if m["bias"] != "neutral"]
    if not mtf:
        alignment = "none"
    elif biases and all(b == biases[0] for b in biases):
        alignment = "aligned"
    elif "bullish" in biases and "bearish" in biases:
        alignment = "conflict"
    else:
        alignment = "mixed"

    summary = decision.build_summary(
        last_close=price_now,
        smc=full["smc"],
        indicators=full["indicators"],
        volume_profile=full["volumeProfile"],
        wyckoff=full["wyckoff"],
        volatility=full["volatility"],
        cvd_div=full["cvdDivergence"],
        mtf=mtf,
        atr=last_valid(full["indicators"]["atr14"]),
    )
    plan = summary.get("tradePlan")
    atr_v = last_valid(full["indicators"]["atr14"])
    zones_bull = [z for z in full["smc"]["orderBlocks"] + full["smc"]["fvgs"]
                  if z["type"] == "bullish" and not z["mitigated"] and z["top"] <= price_now]
    near_bull = [z for z in zones_bull if price_now - z["top"] <= 1.0 * (atr_v or 0)]
    zones_bear = [z for z in full["smc"]["orderBlocks"] + full["smc"]["fvgs"]
                  if z["type"] == "bearish" and not z["mitigated"] and z["bottom"] >= price_now]
    near_bear = [z for z in zones_bear if z["bottom"] - price_now <= 1.0 * (atr_v or 0)]
    return {
        "time": t, "score": summary["score"], "bias": summary["bias"],
        "regime": summary["regime"], "alignment": alignment,
        "cvd_conf": (summary.get("cvdConfluence") or {}).get("direction"),
        "plan": (plan or {}).get("direction"), "atr": atr_v, "price": price_now,
        "vol_state": (full["volatility"] or {}).get("state"),
        "zone_bull_top": max(near_bull, key=lambda z: z.get("quality") or 0)["top"] if near_bull else None,
        "zone_bear_bottom": max(near_bear, key=lambda z: z.get("quality") or 0)["bottom"] if near_bear else None,
    }


def compute_records(itv: str, dfs: dict) -> list[dict]:
    records: list[dict] = []
    for sym in SYMBOLS:
        df = dfs[sym][itv]
        df1d = dfs[sym]["1d"]
        n = len(df)
        times = df["time"].to_numpy()
        closes = df["close"].to_numpy()
        idxs = list(range(WARMUP[itv], n - FWD_ROOM, SPACING[itv]))
        t0 = datetime.now()
        cnt = 0
        for i in idxs:
            t = int(times[i])
            rec = decide_at(df, df1d if itv == "4h" else None, t, WARMUP[itv], MIN_BARS[itv])
            if rec is None:
                continue
            rec["symbol"] = sym
            for h in (6, 24, 48):
                rec[f"ret_{h}"] = float(closes[i + h] / closes[i] - 1.0) if i + h < n else float("nan")
            records.append(rec)
            cnt += 1
            if cnt % 100 == 0:
                el = (datetime.now() - t0).total_seconds()
                print(f"[calc] {sym} {itv}: {cnt}/{len(idxs)} ({el:.0f}s)", flush=True)
        print(f"[data] {sym} {itv}: {n} bars, {cnt} points")
    records.sort(key=lambda r: r["time"])
    return records


def load_records(itv: str, dfs: dict, refresh: bool) -> list[dict]:
    key = {"ver": HARNESS_VER, "itv": itv, "symbols": SYMBOLS, "src": source_hash()}
    cache: dict = {}
    if not refresh and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "rb") as f:
                cache = pickle.load(f)
            if cache.get(itv, {}).get("key") == key:
                print(f"[cache] {itv}: loaded {len(cache[itv]['records'])} records")
                return cache[itv]["records"]
        except Exception:
            cache = {}
    records = compute_records(itv, dfs)
    cache[itv] = {"key": key, "records": records}
    with open(CACHE_FILE, "wb") as f:
        pickle.dump(cache, f)
    print(f"[cache] {itv}: saved {len(records)} records")
    return records


def build_plan(rec: dict, depth: float, stopw: float):
    if rec.get("plan") is None or not rec.get("atr"):
        return None
    long = rec["plan"] == "long"
    price, atr = rec["price"], rec["atr"]
    if long:
        zone_top = rec.get("zone_bull_top")
        entry = min(price, zone_top) if (zone_top and price - zone_top <= depth * atr) else price - depth * atr
        stop = entry - stopw * atr
    else:
        zone_bottom = rec.get("zone_bear_bottom")
        entry = max(price, zone_bottom) if (zone_bottom and zone_bottom - price <= depth * atr) else price + depth * atr
        stop = entry + stopw * atr
    return ("long" if long else "short"), float(entry), float(stop)


def sim_outcome(df, i: int, direction: str, entry: float, stop: float,
                mgmt: str, tgt_r: float, texit: int, fill_bars: int):
    """Outcome in R multiples. Same-bar conservative order: stop > target > trigger."""
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    n = len(df)
    long = direction == "long"
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    be_frac = MGMT_BE[mgmt]
    scale = MGMT_SCALE[mgmt]
    target = entry + tgt_r * risk if long else entry - tgt_r * risk
    be_trig = None
    if be_frac is not None:
        be_trig = entry + be_frac * risk if long else entry - be_frac * risk
    fill = None
    for j in range(i + 1, min(i + 1 + fill_bars, n)):
        if (long and lows[j] <= entry) or ((not long) and highs[j] >= entry):
            fill = j
            break
    if fill is None:
        return "nofill"
    be = False
    locked = 0.0
    for j in range(fill, min(fill + texit, n)):
        stop_lvl = entry if be else stop
        hit_stop = lows[j] <= stop_lvl if long else highs[j] >= stop_lvl
        hit_tg = highs[j] >= target if long else lows[j] <= target
        if hit_stop:
            return -1.0 if not be else locked
        if hit_tg:
            frac = 0.5 if (scale and be) else 1.0
            return locked + frac * tgt_r
        if (not be) and be_trig is not None and (
            (long and highs[j] >= be_trig) or ((not long) and lows[j] <= be_trig)
        ):
            be = True
            if scale:
                locked = 0.5 * be_frac
    j_end = min(fill + texit, n) - 1
    if j_end < fill:
        j_end = fill
    r = (closes[j_end] - entry) / risk if long else (entry - closes[j_end]) / risk
    if scale and be:
        return locked + 0.5 * r
    return float(r)


def evaluate(records, geo, gate_name, dfs, tidx, itv):
    depth, stopw, mgmt, tgt, texit = geo
    gfn = GATES[gate_name]
    outcomes = []
    n_planned = 0
    for r in records:
        if r.get("plan") is None or not gfn(r):
            continue
        n_planned += 1
        built = build_plan(r, depth, stopw)
        if built is None:
            continue
        direction, entry, stop = built
        df = dfs[r["symbol"]][itv]
        i = tidx[r["symbol"]][itv].get(r["time"])
        if i is None:
            continue
        out = sim_outcome(df, i, direction, entry, stop, mgmt, tgt, texit, FILL_BARS[itv])
        if out != "nofill" and out is not None:
            outcomes.append(out)
    if not outcomes:
        return {"n": n_planned, "filled": 0, "fillrate": 0.0, "winrate": float("nan"),
                "nonloss": float("nan"), "ev": float("nan"), "totalR": 0.0}
    arr = np.array(outcomes, dtype=float)
    return {
        "n": n_planned,
        "filled": len(arr),
        "fillrate": len(arr) / max(n_planned, 1),
        "winrate": float(np.mean(arr > 1e-9)),
        "nonloss": float(np.mean(arr >= -1e-9)),
        "ev": float(arr.mean()),
        "totalR": float(arr.sum()),
    }


def fmt(res):
    if not res["filled"]:
        return "n=0"
    return (f"n={res['filled']}/{res['n']} fill={res['fillrate']*100:.0f}% "
            f"胜率={res['winrate']*100:.1f}% 非亏损={res['nonloss']*100:.1f}% "
            f"EV={res['ev']:+.3f}R 总利润={res['totalR']:+.1f}R")


def geo_str(geo):
    depth, stopw, mgmt, tgt, texit = geo
    return f"depth={depth} stop={stopw} mgmt={mgmt} tgt={tgt} texit={texit}"


def passes(res, min_filled):
    return (res["filled"] >= min_filled and res["fillrate"] >= 0.25 and res["ev"] >= 0.05)


def sort_key(res):
    # profit first, then EV, then win rate
    return (-res["totalR"], -res["ev"], -res["winrate"])


def tune(records, tag, dfs, tidx, itv, verbose=True):
    cells = []
    for depth in DEPTHS:
        for stopw in STOPS:
            for mgmt in MGMTS:
                for tgt in TGTS:
                    for texit in TEXITS:
                        geo = (depth, stopw, mgmt, tgt, texit)
                        res = evaluate(records, geo, "all", dfs, tidx, itv)
                        cells.append((geo, res, passes(res, MIN_FILLED[itv])))
    ok = [(g, r) for g, r, p in cells if p]
    ok.sort(key=lambda x: sort_key(x[1]))
    if verbose:
        print(f"\n----- 调参段 [{tag}]：144 格网格（gate=all）-----")
        print(f"  满足约束的格子: {len(ok)}/144；Top-10（总利润优先）：")
        for g, r in ok[:10]:
            print(f"  {geo_str(g)}: {fmt(r)}")
        by_wr = sorted(ok, key=lambda x: (-x[1]["winrate"], -x[1]["totalR"]))[:5]
        print("  按胜率最高的 5 格（参照）：")
        for g, r in by_wr:
            print(f"  {geo_str(g)}: {fmt(r)}")
    if not ok:
        raise SystemExit(f"no grid cell satisfied constraints on tuning fold [{tag}]")
    top3 = ok[:3]
    cand = []
    for g, _ in top3:
        for gname in GATES:
            res = evaluate(records, g, gname, dfs, tidx, itv)
            if passes(res, max(30, MIN_FILLED[itv] // 2)):
                if verbose:
                    print(f"  {geo_str(g)} x {gname}: {fmt(res)}")
                cand.append((sort_key(res), g, gname))
    if not cand:
        return top3[0][0], "all"
    cand.sort(key=lambda x: x[0])
    return cand[0][1], cand[0][2]


def dir_stats(records, horizon: int) -> tuple[int, float]:
    dirs, rets = [], []
    for r in records:
        s = r["score"]
        if abs(s) < 15:
            continue
        ret = r.get(f"ret_{horizon}")
        if ret is None or np.isnan(ret) or ret == 0:
            continue
        dirs.append(1 if s > 0 else -1)
        rets.append(ret)
    if not dirs:
        return 0, float("nan")
    return len(dirs), float(np.mean(np.sign(dirs) == np.sign(rets)))


def run_interval(itv: str, dfs: dict, refresh: bool):
    print(f"\n{'='*70}\n===== 周期 {itv}：2 年采样 · 利润优先回测优化 =====\n{'='*70}")
    tidx: dict = {}
    for sym in SYMBOLS:
        tidx[sym] = {}
        for k in dfs[sym]:
            tidx[sym][k] = {int(t): i for i, t in enumerate(dfs[sym][k]["time"].to_numpy())}
    records = load_records(itv, dfs, refresh)
    print(f"[split] total={len(records)} ({fmt_ts(records[0]['time'])}..{fmt_ts(records[-1]['time'])})")

    a = int(len(records) * 0.4)
    b = int(len(records) * 0.7)
    FA, FB, FC = records[:a], records[a:b], records[b:]
    print(f"folds: A={len(FA)} B={len(FB)} C={len(FC)} (time-ordered)")

    print("\n===== 基线（当前生产几何，1h 校准的保本优先）=====")
    for name, fold in (("A", FA), ("B", FB), ("C", FC)):
        res = evaluate(fold, BASELINE, "all", dfs, tidx, itv)
        print(f"  fold {name}: {fmt(res)}")

    print("\n===== 评分方向准确率（|score|>=15，前向收益）=====")
    for name, fold in (("A", FA), ("B", FB), ("C", FC)):
        parts = []
        for h in (6, 24, 48):
            n, hit = dir_stats(fold, h)
            parts.append(f"{h}根: n={n} {hit*100 if n else 0:.1f}%")
        print(f"  fold {name}: " + "  ".join(parts))

    geo1, gate1 = tune(FA, f"{itv} A", dfs, tidx, itv)
    print(f"\n[Phase1 选定] {geo_str(geo1)} x {gate1}")
    res_b = evaluate(FB, geo1, gate1, dfs, tidx, itv)
    print(f"[Phase1 盲测 B] {fmt(res_b)}")

    geo2, gate2 = tune(FA + FB, f"{itv} A+B", dfs, tidx, itv)
    print(f"\n[Phase2 选定] {geo_str(geo2)} x {gate2}")
    res_c = evaluate(FC, geo2, gate2, dfs, tidx, itv)
    print(f"[Phase2 盲测 C] {fmt(res_c)}")

    print("\n===== 抽稀独立性校验（C 段，Phase2 配置）=====")
    for thin in (1, 2, 4):
        res = evaluate(FC[::thin], geo2, gate2, dfs, tidx, itv)
        print(f"  1/{thin}: {fmt(res)}")

    print("===== 分币种（C 段，Phase2 配置）=====")
    for sym in SYMBOLS:
        res = evaluate([r for r in FC if r["symbol"] == sym], geo2, gate2, dfs, tidx, itv)
        print(f"  {sym}: {fmt(res)}")

    print("\n===== 留一币种交叉验证（LOSO，全时段）=====")
    for held in SYMBOLS:
        others = [r for r in records if r["symbol"] != held]
        g, gn = tune(others, f"LOSO-{held}", dfs, tidx, itv, verbose=False)
        res = evaluate([r for r in records if r["symbol"] == held], g, gn, dfs, tidx, itv)
        print(f"  盲测 {held}: 选 {geo_str(g)} x {gn} -> {fmt(res)}")

    print("\n===== 合并盲测段 B+C（Phase2 配置，仅报告）=====")
    res_bc = evaluate(FB + FC, geo2, gate2, dfs, tidx, itv)
    print(f"  B+C: {fmt(res_bc)}")
    return geo2, gate2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", choices=("4h", "1d"), default=None)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    itvs = [args.interval] if args.interval else ["4h", "1d"]

    dfs: dict = {}
    for sym in SYMBOLS:
        dfs[sym] = {}
        for itv in sorted(set(itvs) | {"1d"}):
            rows = asyncio.run(kline_cache.get_klines(sym, itv, TWO_YEARS[itv]))
            dfs[sym][itv] = kline_cache.rows_to_df(rows)
            print(f"[data] {sym} {itv}: {len(dfs[sym][itv])} bars")

    for itv in itvs:
        run_interval(itv, dfs, args.refresh)


if __name__ == "__main__":
    main()
