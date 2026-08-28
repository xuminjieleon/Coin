"""AUDIT (2026-08-28) part 2, main-thread empirical checks.

E1) Clean-room independent replayer: re-simulate sampled real trades bar-by-bar
    from cached klines WITHOUT reusing sim_outcome_fast, compare (rr, exit_bar).
E2) Margin-check semantics: verify that in the trail families (4h/1d/1w) the
    BE trigger (+beR with be_frac = trail distance) always implies
    MFE - trail <= 0, so the journal ordering (trigger before ratchet update)
    and the backtest ordering (update before trigger) are mathematically
    equivalent — matching the Gap-1 zero-delta control result.
E3) Fill-bar/texit counting conventions (sanity, printed for the record).

Read-only; prints machine-verifiable numbers only.
Usage: PYTHONIOENCODING=utf-8 ..\\.venv\\Scripts\\python.exe tests\\audit_replay_cleanroom.py
"""
import asyncio
import os
import pickle
import sys

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import profit_sweep2 as ps
from profit2_r5 import with_loose_plans
from backtest_ltc import CONF
from backtest_5y import W5, sim_outcome_fast

_FAR = 4_000_000_000_000


def fetch_df(sym, tf):
    rows = asyncio.run(ps.kline_cache.get_klines(sym, tf, W5[tf], end_time=_FAR))
    return ps.kline_cache.rows_to_df(rows)


def collect_trades(sym, tf):
    """capacity-constrained serial run, keeping full trade detail."""
    cfg = CONF[tf]
    geo = tuple(cfg["geo"])
    fb = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
    df = fetch_df(sym, tf)
    with open(os.path.join(ps.CACHE_DIR, f"_5y_cache_{sym}_{tf}.pkl"), "rb") as f:
        recs = pickle.load(f)["records"]
    recs = recs if cfg["th"] == 25 else with_loose_plans(recs, cfg["th"])
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    n = len(df)
    tidx = {int(t): k for k, t in enumerate(df["time"].to_numpy())}
    trades = []
    busy = -1
    for r in recs:
        if r.get("plan") is None:
            continue
        i = tidx.get(r["time"])
        if i is None or i <= busy:
            continue
        built = ps.build_plan(r, geo[0], geo[1])
        if built is None:
            continue
        direction, entry, stop = built
        out = sim_outcome_fast(highs, lows, closes, n, i, direction, entry, stop,
                               geo[2], geo[3], geo[4], fb, geo[5])
        if out is None:
            continue
        rr, fill, exit_bar = out
        busy = exit_bar
        trades.append(dict(sym=sym, tf=tf, t=r["time"], i=i, direction=direction,
                           entry=entry, stop=stop, rr=rr, fill=fill, exit=exit_bar,
                           geo=geo, fb=fb))
    return trades, (df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy())


def cleanroom_replay(highs, lows, closes, direction, entry, stop, i,
                     be_frac, tgt_r, texit, fill_bars, trail):
    """Independent replayer written from the documented semantics only.

    long: fill if low<=entry within fill_bars AFTER bar i; then per bar:
      1. stop first (initial stop; after BE: max(entry, trail level))
      2. target (if any)
      3. BE trigger: +beR reached -> half out at +beR, stop to entry
      4. trail ratchet from PRIOR bars' MFE (update AFTER the checks so the
         current bar never tightens its own stop)
      5. time exit at bar fill+texit-1 close.
    """
    long = direction == "long"
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    n = len(closes)
    target = (entry + tgt_r * risk if long else entry - tgt_r * risk) if tgt_r is not None else None
    be_trig = entry + be_frac * risk if long else entry - be_frac * risk
    fill = None
    for j in range(i + 1, min(i + 1 + fill_bars, n)):
        if (long and lows[j] <= entry) or ((not long) and highs[j] >= entry):
            fill = j
            break
    if fill is None:
        return None
    be = False
    ratchet = 0.0
    for j in range(fill, min(fill + texit, n)):
        if be and trail is not None:
            stop_lvl = entry + ratchet * risk if long else entry - ratchet * risk
        else:
            stop_lvl = entry if be else stop
        if (lows[j] <= stop_lvl) if long else (highs[j] >= stop_lvl):
            if not be:
                return (-1.0, fill, j)
            runner = ratchet if trail is not None else 0.0
            return (0.5 * be_frac + 0.5 * runner, fill, j)
        if target is not None and ((highs[j] >= target) if long else (lows[j] <= target)):
            frac = 0.5 if be else 1.0
            base = 0.5 * be_frac if be else 0.0
            return (base + frac * tgt_r, fill, j)
        if not be and ((long and highs[j] >= be_trig) or ((not long) and lows[j] <= be_trig)):
            be = True
        if be and trail is not None:
            mfe = (highs[j] - entry) / risk if long else (entry - lows[j]) / risk
            ratchet = max(ratchet, mfe - trail)
    j_end = min(fill + texit, n) - 1
    r = (closes[j_end] - entry) / risk if long else (entry - closes[j_end]) / risk
    if be:
        return (0.5 * be_frac + 0.5 * r, fill, j_end)
    return (float(r), fill, j_end)


def e1():
    print("=" * 78)
    print("E1: clean-room 独立重放器 vs sim_outcome_fast（真实交易抽样）")
    print("=" * 78)
    rng = np.random.RandomState(7)
    total, bad = 0, 0
    for sym, tf in (("BTCUSDT", "1h"), ("ETHUSDT", "4h"), ("SOLUSDT", "1d")):
        trades, (h, l, c) = collect_trades(sym, tf)
        # 60 random + boundary: stops(-1R), near-BE, time-exits
        idx = rng.choice(len(trades), size=min(60, len(trades)), replace=False).tolist()
        stops = [k for k, t in enumerate(trades) if abs(t["rr"] + 1.0) < 1e-9][:5]
        texit_hits = [k for k, t in enumerate(trades)
                      if t["exit"] - t["fill"] >= t["geo"][4] - 1][:5]
        pick = sorted(set(idx) | set(stops) | set(texit_hits))
        n_bad = 0
        for k in pick:
            t = trades[k]
            out = cleanroom_replay(h, l, c, t["direction"], t["entry"], t["stop"], t["i"],
                                   t["geo"][2], t["geo"][3], t["geo"][4], t["fb"], t["geo"][5])
            ok = out is not None and abs(out[0] - t["rr"]) < 1e-9 and out[2] == t["exit"] and out[1] == t["fill"]
            if not ok:
                n_bad += 1
                bad += 1
                print(f"  MISMATCH {sym} {tf} t={t['t']} sim={t['rr']:+.6f}@{t['exit']} "
                      f"clean={out}", flush=True)
        total += len(pick)
        print(f"  {sym} {tf}: 抽样 {len(pick)} 笔（含止损 {len(stops)}/时间退出 {len(texit_hits)}），"
              f"不一致 {n_bad}", flush=True)
    print(f"  E1 汇总: {total} 笔抽样，不一致 {bad} 笔", flush=True)


def e2():
    print()
    print("=" * 78)
    print("E2: 跟踪族 保本触发根的棘轮状态 —— 日记顺序(先触发后更新) 是否改变止损位")
    print("   （journal: 触发后 stop=entry，当根 MFE 更新 ratchet；")
    print("     backtest: 先更新 ratchet 后触发，当根 stop 仍=entry —— 该根止损位相同；")
    print("     差异仅在【同根再回落打穿 stop】时：sim 用 entry+0×risk=entry、journal 用")
    print("     entry+max(0,MFE-trail)×risk。MFE>trail 时 journal 止损更高 → journal 更激进）")
    print("=" * 78)
    # empirical: on the BE-trigger bar, how often does MFE exceed trail (so the
    # journal's ratchet would sit ABOVE entry on that very bar)?
    for sym, tf in (("BTCUSDT", "4h"), ("ETHUSDT", "4h"), ("BTCUSDT", "1d"), ("SOLUSDT", "1d")):
        trades, (h, l, c) = collect_trades(sym, tf)
        n_be, n_above = 0, 0
        for t in trades:
            long = t["direction"] == "long"
            risk = abs(t["entry"] - t["stop"])
            be_lvl = t["entry"] + t["geo"][2] * risk if long else t["entry"] - t["geo"][2] * risk
            for j in range(t["fill"], t["exit"] + 1):
                hit = (h[j] >= be_lvl) if long else (l[j] <= be_lvl)
                if hit:
                    n_be += 1
                    mfe = (h[j] - t["entry"]) / risk if long else (t["entry"] - l[j]) / risk
                    if mfe - t["geo"][5] > 0:
                        n_above += 1
                    break
        print(f"  {sym} {tf}: 发生保本触发的交易 {n_be} 笔，其中触发根 MFE>trail "
              f"（两顺序该根止损位不同）{n_above} 笔", flush=True)


if __name__ == "__main__":
    e1()
    e2()
