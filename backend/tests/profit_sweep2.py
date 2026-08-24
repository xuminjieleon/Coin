"""Multi-round profit-first optimization harness for 4h/1d/1w (2026-08-22, round 11+).

User spec: continuous optimization loop, profit first (then accuracy/win rate),
anti-overfit. May add/remove parameters under consideration.

Protocol (pre-registered before looking at any result):
  - Data: extended windows for stability — 4h 3y (~6570 bars), 1d 4y (~1460),
    1w 10y-capped (~520). BTC/ETH/SOL. MTF context uses ONLY closed higher-TF
    bars. Decision records cached per (tf, variant, engine hash).
  - Folds by time: A 40% (tune), B 30% + C 30% (blind). LOSO across symbols
    as the repeated-peeking guard: tune on 2 symbols, blind the held-out one.
  - Acceptance of a candidate over the incumbent (ALL required):
      1. blind B+C total profit beats incumbent by > 5%
      2. LOSO: min held-out-symbol blind profit > 0 and not worse than
         incumbent's min by > 5%
      3. guards: EV >= +0.05R, fill rate >= 25%, filled >= threshold
    Two consecutive rejected rounds => plateau, stop.
  - Search: staged coordinate descent (one axis at a time, coarse values) —
    far fewer effective hypotheses than full cross-product grids.
  - New parameter dimensions this round: R-ladder trailing stop (runner half
    trails profit by a fixed R gap), unlimited runner target (tgt=None),
    zone-only entries, vol-skip gate, 1w-MTF context for 1d records.
  - Metrics: totalR (primary), EV, win rate, fill rate, max drawdown of the
    time-ordered R curve, profit factor. Same-bar conservative order:
    stop > target > trigger; trail ratchet uses prior bars only.
  - Fees/slippage NOT modeled (maker ~0.02%).

Usage:
  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/profit_sweep2.py --tf 4h --mode prefetch
  ... --mode baseline|gates|refine|validate [--gate NAME] [--geo d,s,be,tgt,texit,trail]
                                       [--mtf1w] [--refresh]
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

# tf -> window bars, warmup, min_bars, spacing (bars between decisions),
#        fill_bars (limit validity), fwd_room (bars reserved), texit options
CFG = {
    "4h": dict(window=6570, warmup=500, min_bars=300, spacing=4, fill_bars=12, fwd_room=64,
               texits=(12, 24, 48)),
    "1d": dict(window=1460, warmup=300, min_bars=220, spacing=2, fill_bars=6, fwd_room=110,
               texits=(24, 48, 96)),
    "1w": dict(window=520, warmup=170, min_bars=120, spacing=1, fill_bars=4, fwd_room=32,
               texits=(6, 12, 24)),
}

D1_MS = 86_400_000
W1_MS = 604_800_000

# incumbent production geometry per tf: (depth, stop, be_frac, tgt, texit, trail)
INCUMBENT = {
    "4h": (1.0, 2.0, 0.5, 3.0, 24, None),
    "1d": (1.0, 2.0, 0.5, 3.0, 96, None),
    "1w": (1.0, 2.0, 0.5, 3.0, 12, None),  # inherited from 4h family (no 1w calibration yet)
}
INCUMBENT_GATE = "base"

MIN_FILLED = {"4h": 150, "1d": 60, "1w": 40}

GATES = {
    "base": lambda r: True,
    "score30": lambda r: abs(r["score"]) >= 30,
    "score35": lambda r: abs(r["score"]) >= 35,
    "conf": lambda r: r.get("cvd_conf") is not None,
    "noexp": lambda r: r.get("vol_state") != "expanded",
    "trend": lambda r: r["regime"] == "trending",
    "range": lambda r: r["regime"] == "ranging",
    "align": lambda r: r.get("alignment") == "aligned",
    "zone": lambda r: r.get("zone_bull_top") is not None or r.get("zone_bear_bottom") is not None,
}

CACHE_DIR = os.path.dirname(os.path.abspath(__file__))
HARNESS_VER = 2

# coordinate-descent axis values (coarse, pre-declared)
AXIS_DEPTH = (0.75, 0.9, 1.0, 1.25, 1.5)
AXIS_STOP = (1.5, 1.8, 2.0, 2.3, 2.5, 3.0)
AXIS_BE = (0.25, 0.35, 0.5, 0.75, 1.0)
AXIS_TGT = (2.0, 2.5, 3.0, 4.0, 5.0, None)  # None = unlimited runner (trail/stop/timeout only)
AXIS_TRAIL = (None, 0.75, 1.0, 1.5)


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


def tf_summary_closed(df_htf, t: int, span_ms: int) -> dict | None:
    """Higher-TF summary from FULLY CLOSED bars only (close time <= t)."""
    sub = df_htf[df_htf["time"] + span_ms <= t].tail(300).reset_index(drop=True)
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
    return {"interval": "htf", "score": s["score"], "bias": s["bias"], "regime": s["regime"],
            "cvdDiv": (full["cvdDivergence"] or {}).get("type")}


def decide_at(df, htf: list, t: int, warmup: int, min_bars: int) -> dict | None:
    sub = df[df["time"] <= t].tail(warmup).reset_index(drop=True)
    if len(sub) < min_bars:
        return None
    full = engine.full_analysis(sub)
    closes = sub["close"]
    price_now = float(closes.iloc[-1])

    biases = [m["bias"] for m in htf if m["bias"] != "neutral"]
    if not htf:
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
        mtf=htf,
        atr=last_valid(full["indicators"]["atr14"]),
    )
    plan = summary.get("tradePlan")
    atr_v = last_valid(full["indicators"]["atr14"])
    zones_bull = [z for z in full["smc"]["orderBlocks"] + full["smc"]["fvgs"]
                  if z["type"] == "bullish" and not z["mitigated"] and z["top"] <= price_now]
    near_bull = [z for z in zones_bull if price_now - z["top"] <= 1.5 * (atr_v or 0)]
    zones_bear = [z for z in full["smc"]["orderBlocks"] + full["smc"]["fvgs"]
                  if z["type"] == "bearish" and not z["mitigated"] and z["bottom"] >= price_now]
    near_bear = [z for z in zones_bear if z["bottom"] - price_now <= 1.5 * (atr_v or 0)]
    return {
        "time": t, "score": summary["score"], "bias": summary["bias"],
        "regime": summary["regime"], "alignment": alignment,
        "cvd_conf": (summary.get("cvdConfluence") or {}).get("direction"),
        "plan": (plan or {}).get("direction"), "atr": atr_v, "price": price_now,
        "vol_state": (full["volatility"] or {}).get("state"),
        "zone_bull_top": max(near_bull, key=lambda z: z.get("quality") or 0)["top"] if near_bull else None,
        "zone_bear_bottom": max(near_bear, key=lambda z: z.get("quality") or 0)["bottom"] if near_bear else None,
    }


def compute_records(tf: str, dfs: dict, mtf1w: bool) -> list[dict]:
    cfg = CFG[tf]
    records: list[dict] = []
    for sym in SYMBOLS:
        df = dfs[sym][tf]
        n = len(df)
        times = df["time"].to_numpy()
        closes = df["close"].to_numpy()
        idxs = list(range(cfg["warmup"], n - cfg["fwd_room"], cfg["spacing"]))
        t0 = datetime.now()
        cnt = 0
        for i in idxs:
            t = int(times[i])
            htf = []
            if tf == "4h" and dfs[sym].get("1d") is not None:
                m = tf_summary_closed(dfs[sym]["1d"], t, D1_MS)
                if m:
                    htf.append(m)
            elif tf == "1d" and mtf1w and dfs[sym].get("1w") is not None:
                m = tf_summary_closed(dfs[sym]["1w"], t, W1_MS)
                if m:
                    htf.append(m)
            rec = decide_at(df, htf, t, cfg["warmup"], cfg["min_bars"])
            if rec is None:
                continue
            rec["symbol"] = sym
            for h in (6, 24, 48):
                rec[f"ret_{h}"] = float(closes[i + h] / closes[i] - 1.0) if i + h < n else float("nan")
            records.append(rec)
            cnt += 1
            if cnt % 150 == 0:
                el = (datetime.now() - t0).total_seconds()
                print(f"[calc] {sym} {tf}: {cnt}/{len(idxs)} ({el:.0f}s)", flush=True)
        print(f"[data] {sym} {tf}: {n} bars, {cnt} points")
    records.sort(key=lambda r: r["time"])
    return records


def load_records(tf: str, dfs: dict, mtf1w: bool, refresh: bool) -> list[dict]:
    variant = "mtf1w" if mtf1w else "plain"
    key = {"ver": HARNESS_VER, "tf": tf, "variant": variant, "symbols": SYMBOLS, "src": source_hash()}
    # per-key cache file: parallel-safe (no read-modify-write clobbering)
    cache_file = os.path.join(CACHE_DIR, f"_profit2_cache_{tf}_{variant}.pkl")
    if not refresh and os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                entry = pickle.load(f)
            if entry.get("key") == key:
                print(f"[cache] {tf}/{variant}: {len(entry['records'])} records")
                return entry["records"]
        except Exception:
            pass
    records = compute_records(tf, dfs, mtf1w)
    tmp = cache_file + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump({"key": key, "records": records}, f)
    os.replace(tmp, cache_file)
    print(f"[cache] {tf}/{variant}: saved {len(records)} records")
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
                be_frac: float, tgt_r, texit: int, fill_bars: int, trail):
    """Outcome in R. Scale-out at be trigger (half out, stop->entry); runner
    optionally trails by a fixed R gap (ratchet from PRIOR bars only).
    Same-bar conservative order: stop > target > trigger."""
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    n = len(df)
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
        return "nofill"
    be = False
    locked = 0.0
    ratchet = 0.0  # runner stop offset in R (after be)
    for j in range(fill, min(fill + texit, n)):
        if be and trail is not None:
            stop_lvl = entry + ratchet * risk if long else entry - ratchet * risk
        else:
            stop_lvl = entry if be else stop
        hit_stop = lows[j] <= stop_lvl if long else highs[j] >= stop_lvl
        if hit_stop:
            if not be:
                return -1.0
            runner_r = ratchet if trail is not None else 0.0
            return locked + 0.5 * runner_r
        if target is not None and (highs[j] >= target if long else lows[j] <= target):
            frac = 0.5 if be else 1.0
            return locked + frac * tgt_r
        if not be and ((long and highs[j] >= be_trig) or ((not long) and lows[j] <= be_trig)):
            be = True
            locked = 0.5 * be_frac
        # ratchet update AFTER bar checks (prior-bar information only)
        if be and trail is not None:
            mfe = (highs[j] - entry) / risk if long else (entry - lows[j]) / risk
            ratchet = max(ratchet, mfe - trail)
    j_end = min(fill + texit, n) - 1
    if j_end < fill:
        j_end = fill
    r = (closes[j_end] - entry) / risk if long else (entry - closes[j_end]) / risk
    if be:
        return locked + 0.5 * r
    return float(r)


def evaluate(records, geo, gate_name, dfs, tidx, tf, fill_mult=1.0):
    depth, stopw, be_frac, tgt, texit, trail = geo
    gfn = GATES[gate_name]
    cfg = CFG[tf]
    fill_bars = max(1, int(round(cfg["fill_bars"] * fill_mult)))
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
        df = dfs[r["symbol"]][tf]
        i = tidx[r["symbol"]][tf].get(r["time"])
        if i is None:
            continue
        out = sim_outcome(df, i, direction, entry, stop, be_frac, tgt, texit, fill_bars, trail)
        if out != "nofill" and out is not None:
            outcomes.append(out)
    if not outcomes:
        return {"n": n_planned, "filled": 0, "fillrate": 0.0, "winrate": float("nan"),
                "nonloss": float("nan"), "ev": float("nan"), "totalR": 0.0,
                "maxdd": float("nan"), "pf": float("nan")}
    arr = np.array(outcomes, dtype=float)
    cum = np.cumsum(arr)
    peak = np.maximum.accumulate(np.concatenate(([0.0], cum)))[1:]
    maxdd = float(np.max(peak - cum)) if len(cum) else 0.0
    wins = arr[arr > 1e-9].sum()
    losses = -arr[arr < -1e-9].sum()
    pf = float(wins / losses) if losses > 1e-9 else float("inf")
    return {
        "n": n_planned,
        "filled": len(arr),
        "fillrate": len(arr) / max(n_planned, 1),
        "winrate": float(np.mean(arr > 1e-9)),
        "nonloss": float(np.mean(arr >= -1e-9)),
        "ev": float(arr.mean()),
        "totalR": float(arr.sum()),
        "maxdd": maxdd,
        "pf": pf,
    }


def fmt(res):
    if not res["filled"]:
        return "n=0"
    dd = f"{res['maxdd']:.1f}" if res["maxdd"] == res["maxdd"] else "-"
    pf = f"{res['pf']:.2f}" if res["pf"] != float("inf") else "inf"
    return (f"n={res['filled']}/{res['n']} fill={res['fillrate']*100:.0f}% "
            f"胜率={res['winrate']*100:.1f}% EV={res['ev']:+.3f}R 总={res['totalR']:+.1f}R "
            f"DD={dd}R PF={pf}")


def geo_str(geo):
    depth, stopw, be, tgt, texit, trail = geo
    return f"depth={depth} stop={stopw} be={be} tgt={tgt} texit={texit} trail={trail}"


def passes(res, min_filled):
    return (res["filled"] >= min_filled and res["fillrate"] >= 0.25 and res["ev"] >= 0.05)


def load_dfs(tf: str, mtf1w: bool) -> dict:
    need = {tf, "1w"} if (tf == "1d" and mtf1w) else ({tf, "1d"} if tf == "4h" else {tf})
    dfs: dict = {}
    for sym in SYMBOLS:
        dfs[sym] = {}
        for itv in sorted(need):
            rows = None
            for attempt in range(4):
                try:
                    rows = asyncio.run(kline_cache.get_klines(sym, itv, CFG[itv]["window"]))
                    break
                except Exception as exc:  # transient network / cooldown short-circuit
                    wait = 20 * (attempt + 1)
                    print(f"[warn] {sym} {itv} fetch failed ({exc}); retry in {wait}s")
                    import time as _t
                    _t.sleep(wait)
            if rows is None:
                raise SystemExit(f"data fetch failed for {sym} {itv}")
            dfs[sym][itv] = kline_cache.rows_to_df(rows)
            print(f"[data] {sym} {itv}: {len(dfs[sym][itv])} bars")
    return dfs


def make_tidx(dfs: dict, tf: str) -> dict:
    tidx: dict = {}
    for sym in SYMBOLS:
        tidx[sym] = {}
        for k in dfs[sym]:
            tidx[sym][k] = {int(t): i for i, t in enumerate(dfs[sym][k]["time"].to_numpy())}
    return tidx


def folds(records):
    a = int(len(records) * 0.4)
    b = int(len(records) * 0.7)
    return records[:a], records[a:b], records[b:]


def report_blind(FA, FB, FC, geo, gate, dfs, tidx, tf, tag=""):
    rb = evaluate(FB, geo, gate, dfs, tidx, tf)
    rc = evaluate(FC, geo, gate, dfs, tidx, tf)
    rbc = evaluate(FB + FC, geo, gate, dfs, tidx, tf)
    print(f"  {tag}B[{fmt(rb)}]")
    print(f"  {tag}C[{fmt(rc)}]")
    print(f"  {tag}B+C[{fmt(rbc)}]")
    return rbc


def loso(records, geo, gate, dfs, tidx, tf, min_filled):
    """Tune nothing here: evaluate the GIVEN config per-symbol-blind.
    Returns per-symbol totalR on that symbol's ALL folds (config never tuned
    on it because we only pass fixed configs here; the caller guarantees the
    config was chosen without the held-out symbol when used for selection)."""
    out = {}
    for sym in SYMBOLS:
        sub = [r for r in records if r["symbol"] == sym]
        res = evaluate(sub, geo, gate, dfs, tidx, tf)
        out[sym] = res
        print(f"  LOSO {sym}: {fmt(res)}")
    return out


# ---------------- modes ----------------

def mode_prefetch(args):
    for tf in ("4h", "1d", "1w"):
        dfs = load_dfs(tf, mtf1w=True)
    print("[prefetch] done")


def mode_baseline(args):
    tf = args.tf
    dfs = load_dfs(tf, args.mtf1w)
    tidx = make_tidx(dfs, tf)
    records = load_records(tf, dfs, args.mtf1w, args.refresh)
    FA, FB, FC = folds(records)
    print(f"\n===== {tf} 基线（扩展窗口）: {len(records)} 点 "
          f"({fmt_ts(records[0]['time'])}..{fmt_ts(records[-1]['time'])}) A/B/C={len(FA)}/{len(FB)}/{len(FC)} =====")
    geo = INCUMBENT[tf]
    print(f"  incumbent {geo_str(geo)} x {INCUMBENT_GATE}")
    for name, fold in (("A", FA), ("B", FB), ("C", FC)):
        res = evaluate(fold, geo, INCUMBENT_GATE, dfs, tidx, tf)
        print(f"  fold {name}: {fmt(res)}")
    report_blind(FA, FB, FC, geo, INCUMBENT_GATE, dfs, tidx, tf, tag="blind-")
    # direction accuracy on blind folds for reference
    for h in (6, 24, 48):
        dirs, rets = [], []
        for r in FB + FC:
            s = r["score"]
            if abs(s) < 15:
                continue
            ret = r.get(f"ret_{h}")
            if ret is None or np.isnan(ret) or ret == 0:
                continue
            dirs.append(1 if s > 0 else -1)
            rets.append(ret)
        if dirs:
            hit = float(np.mean(np.sign(dirs) == np.sign(rets)))
            print(f"  方向 {h}根: n={len(dirs)} 胜率={hit*100:.1f}%")


def mode_gates(args):
    tf = args.tf
    dfs = load_dfs(tf, args.mtf1w)
    tidx = make_tidx(dfs, tf)
    records = load_records(tf, dfs, args.mtf1w, args.refresh)
    FA, FB, FC = folds(records)
    geo = INCUMBENT[tf] if args.geo is None else args.geo
    fill_mult = args.fillmult or 1.0
    print(f"\n===== {tf} 门控扫描（几何固定 {geo_str(geo)}，fill×{fill_mult}）=====")
    print("-- 调参段 A+B --")
    results = {}
    for gname in GATES:
        res = evaluate(FA + FB, geo, gname, dfs, tidx, tf, fill_mult)
        ok = passes(res, MIN_FILLED[tf] // 2)
        results[gname] = (res, ok)
        print(f"  {gname:<8}: {'OK ' if ok else '   '}{fmt(res)}")
    ranked = sorted([(r, g) for g, (r, ok) in results.items() if ok],
                    key=lambda x: (-x[0]["totalR"], -x[0]["ev"]))
    if not ranked:
        print("  [!] 无门控满足约束")
        return
    print("-- 盲测段 B+C（Top-3 门控）--")
    for _, gname in ranked[:3]:
        print(f"  [{gname}]")
        report_blind(FA, FB, FC, geo, gname, dfs, tidx, tf, tag="    ")


def mode_refine(args):
    tf = args.tf
    dfs = load_dfs(tf, args.mtf1w)
    tidx = make_tidx(dfs, tf)
    records = load_records(tf, dfs, args.mtf1w, args.refresh)
    FA, FB, FC = folds(records)
    gate = args.gate or INCUMBENT_GATE
    geo = list(INCUMBENT[tf]) if args.geo is None else list(args.geo)
    print(f"\n===== {tf} 坐标下降精调（gate={gate}, 起点 {geo_str(tuple(geo))}）=====")
    tune_fold = FA + FB
    best_total = None
    for pass_no in (1, 2):
        print(f"-- 第 {pass_no} 轮坐标下降（调参段 A+B）--")
        base = evaluate(tune_fold, tuple(geo), gate, dfs, tidx, tf)
        best_total = base["totalR"]
        print(f"  当前: {fmt(base)}")
        for axis, values, idx in (
            ("depth", AXIS_DEPTH, 0), ("stop", AXIS_STOP, 1), ("be", AXIS_BE, 2),
            ("tgt", AXIS_TGT, 3), ("texit", CFG[tf]["texits"], 4), ("trail", AXIS_TRAIL, 5),
        ):
            best_v, best_r = geo[idx], None
            for v in values:
                if v == geo[idx]:
                    continue
                cand = list(geo)
                cand[idx] = v
                res = evaluate(tune_fold, tuple(cand), gate, dfs, tidx, tf)
                if passes(res, MIN_FILLED[tf] // 2) and (best_r is None or res["totalR"] > best_r["totalR"]):
                    best_r, best_v = res, v
            if best_r is not None and best_r["totalR"] > best_total + 1e-9:
                print(f"  axis {axis}: {geo[idx]} -> {best_v}  {fmt(best_r)}")
                geo[idx] = best_v
                best_total = best_r["totalR"]
            else:
                print(f"  axis {axis}: 保持 {geo[idx]}")
    geo = tuple(geo)
    print(f"\n[精调结果] {geo_str(geo)} x {gate}")
    print("-- 盲测段 B+C --")
    report_blind(FA, FB, FC, geo, gate, dfs, tidx, tf, tag="  ")
    print("-- 分币种（全时段，该配置）--")
    loso(records, geo, gate, dfs, tidx, tf, MIN_FILLED[tf])


def mode_validate(args):
    tf = args.tf
    dfs = load_dfs(tf, args.mtf1w)
    tidx = make_tidx(dfs, tf)
    records = load_records(tf, dfs, args.mtf1w, args.refresh)
    FA, FB, FC = folds(records)
    if args.geo is None:
        raise SystemExit("--geo required for validate")
    geo = args.geo
    gate = args.gate or INCUMBENT_GATE
    print(f"\n===== {tf} 最终验证 {geo_str(geo)} x {gate} =====")
    for name, fold in (("A", FA), ("B", FB), ("C", FC)):
        res = evaluate(fold, geo, gate, dfs, tidx, tf)
        print(f"  fold {name}: {fmt(res)}")
    report_blind(FA, FB, FC, geo, gate, dfs, tidx, tf, tag="  ")
    print("-- 抽稀（C 段）--")
    for thin in (1, 2, 4):
        res = evaluate(FC[::thin], geo, gate, dfs, tidx, tf)
        print(f"  1/{thin}: {fmt(res)}")
    print("-- 分币种（全时段）--")
    loso(records, geo, gate, dfs, tidx, tf, MIN_FILLED[tf] // 2)
    print("-- 对照 incumbent --")
    report_blind(FA, FB, FC, INCUMBENT[tf], INCUMBENT_GATE, dfs, tidx, tf, tag="  inc-")


def parse_geo(s: str):
    if s is None:
        return None
    parts = s.split(",")
    depth, stop, be, texit = float(parts[0]), float(parts[1]), float(parts[2]), int(parts[4])
    tgt = None if parts[3].lower() in ("none", "n") else float(parts[3])
    trail = None if parts[5].lower() in ("none", "n") else float(parts[5])
    return (depth, stop, be, tgt, texit, trail)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", choices=("4h", "1d", "1w"), required=True)
    ap.add_argument("--mode", choices=("prefetch", "baseline", "gates", "refine", "validate"), required=True)
    ap.add_argument("--gate")
    ap.add_argument("--geo")
    ap.add_argument("--mtf1w", action="store_true", help="1d records with 1w MTF context")
    ap.add_argument("--fillmult", type=float, default=None, help="fill window multiplier (gates mode)")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    args.geo = parse_geo(args.geo)
    {"prefetch": mode_prefetch, "baseline": mode_baseline, "gates": mode_gates,
     "refine": mode_refine, "validate": mode_validate}[args.mode](args)


if __name__ == "__main__":
    main()
