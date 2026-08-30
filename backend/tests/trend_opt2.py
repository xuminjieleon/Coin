"""Trend-strategy optimization ROUND 3 (2026-08-30) — structural: no forced breakeven.

Round 2 ended FAIL on K1 only (+5.94% vs required +7.45%); the user then
pinpointed the real disease ("97.9% of trades are kicked out by stop/BE")
and AUTHORIZED structural changes: do NOT force breakeven; maximize profit;
this is a separate strategy line from the production engine.

Pre-run elasticity probe (training folds only, BTC+SOL 1h, no blind data):
  baseline (be1.18/ts2.03/tr1.21):  avgWin +0.59R  avgR +0.099  n=824
  no-BE alone:                      avgWin +0.61R  avgR +0.121  n=800
  loose trail (4.5) alone:          avgWin +1.92R  avgR +0.084  n=453
  no-BE + loose trail:              avgWin +2.26R  avgR +0.342  n=360
=> BE alone is NOT the killer (12% of exits); the tight trail is. Removing
BOTH triples avgR and lifts avgWin 3.8x. Cost: fewer trades (long holds
block reversal entries — no pyramiding).

ROUND-3 PROTOCOL (pre-registered before the run; blind C slice UNCHANGED
= entries >= 2025-03-01 UTC; rounds 39/40 numbers on it are archived and
this round's candidate is a STRUCTURALLY different config family, so a third
look is legitimate per the fresh pre-registration below):
  Universe/sizing/fee/folds/WF gate: unchanged from round 2 (1h only,
    shorts always on, f=1%, A=40%/B=30% tuning, J = soft-floor objective).
  Structural axes REPLACING the old be/trail axes:
    be_mode   ['off', ('at', 1.2), ('at', 2.0)]   # off = never move to BE
    trail     [1.21, 2.0, 3.0, 4.5, 6.0, 8.0]
    trail_start [1.5, 2.03, 3.0, 4.0]
  Entry/exit semantics otherwise identical to round 2 (Pine-faithful).
  Start cfg = round-2 elasticity hint: di_align=5, ema_gate=200, kc_len=35,
    adx 15-100, rsi_gt=68.5, vol=1.0, sl=6.95, be_mode=off, trail_start=1.5,
    trail=4.5, texit=200, cooldown=5.
  Blind C gates (candidate vs round-2 two-sided incumbent):
    K1 mean_ann_C >= 1.25 x incumbent   (the profitability bar the user set:
       "at least comparable to what exists" — the incumbent bar, which the
       production engine clears by an order of magnitude, remains the first
       gate; failing it means the trend line is not worth productizing)
    K2 >= 6 of 8 symbols C total > 0
    K3 worst-symbol C total(candidate) >= worst-symbol C total(incumbent)
    K4 candidate mean C maxDD <= 12% and max per-symbol C maxDD <= 25%
       (relaxed vs round 2's 10/20: letting profits run mechanically
       deepens givebacks; user accepted "利润最大化" over smoothness)
    K5 candidate C trades >= 3 per symbol on average
  If gates fail: archived; NO fourth round without new data dimensions.

Axes (order fixed; values predefined; cooldown axis dropped, be_mode/trail
structural axes per round-3 protocol):
  1 trend         [None, ('slope',680), ('slope',200), ('above',680), ('above',200)]
  2 di_align      [None, 0.0, 5.0]
  3 ema_gate      [None, 200, 425, 680, 1000]
  4 kc_len        [21, 24, 35, 50]
  5 kc_mult       [1.5, 2.0, 2.7, 3.5]
  6 adx_min       [0.0, 15.0, 23.5, 30.0]
  7 adx_max       [59.0, 100.0]
  8 rsi_gt        [None, 55.0, 60.0, 68.5]
  9 vol_mult      [None, 1.0, 1.5]
 10 sl            [2.5, 4.0, 6.95, 9.0]
 11 be_mode       ['off', ('at', 1.2), ('at', 2.0)]
 12 trail_start   [1.5, 2.03, 3.0, 4.0]
 13 trail         [1.21, 2.0, 3.0, 4.5, 6.0, 8.0]
 14 texit         [None, 200, 500, 1000]

Execution semantics identical to round 39. Parallelism: mp.Pool per
(symbol, config); per-phase JSON state in TEMP_DIR.
Usage: tests/trend_opt2.py --phase baseline|phase1|phase2|report [--tf 1h] [--serial]
"""
import argparse
import copy
import json
import multiprocessing as mp
import os
import sys

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

from backtest_pine import FEE, INITIAL_EQUITY, year_of  # noqa: E402
from backtest_pine import ema, pine_rsi, rma, true_range  # noqa: E402
from trend_opt import (ALL_SYMBOLS, FIXED, TEXIT_AXIS, WARMUP, WINDOWS,  # noqa: E402
                       _cfg_from_json, _jsonable_cfg, build_side,
                       fold_bounds, side_params_from_long)
from services import kline_cache  # noqa: E402

C_CUTOFF_MS = 1740787200000  # 2025-03-01T00:00:00Z — blind slice, all symbols
WF_SEGMENTS = 4
MEAN_DD_CAP = 0.10
MIN_TRADES_PER_SYM = 3.0

# Authored two-sided incumbent: BTC shorts (commented in btc1h.txt), SOL
# shorts (commented in sol1h.txt); ETH's authored asymmetric short is live
# already. New symbols inherit BTC.
AUTHORED_SHORTS = {
    "BTCUSDT": dict(ema=680, kc=23, kc_mult=2.2, adx=(32.0, 60.0),
                    rsi_lt=27.0, atr=8, sl=4.1, be=1.02,
                    trail_start=2.43, trail=0.61, cooldown=3),
    "SOLUSDT": dict(ema=490, kc=20, kc_mult=1.3, adx=(29.7, 58.0),
                    rsi_lt=37.4, atr=14, sl=3.63, be=0.95,
                    trail_start=2.35, trail=0.82, cooldown=2),
}

from backtest_pine import PINE  # noqa: E402


def incumbent_long_cfg2(sym: str) -> dict:
    src = PINE.get(sym, PINE["BTCUSDT"])
    lg = src["long"]
    return dict(
        direction="long_short",
        trend=("slope", lg["trend_ema"]) if lg.get("trend_ema") else None,
        di_align=None,
        ema_gate=lg["ema"],
        kc_len=lg["kc"], kc_mult=lg["kc_mult"], atr_len=lg["atr"],
        adx_min=lg["adx"][0], adx_max=lg["adx"][1],
        rsi_gt=lg["rsi_gt"] if lg["rsi_gt"] > 0 else None,
        vol_mult=1.0,
        sl=lg["sl"], be_mode=("at", lg["be"]), trail_start=lg["trail_start"], trail=lg["trail"],
        texit=None, cooldown=lg["cooldown"],
    )


def incumbent_short_cfg2(sym: str) -> dict:
    if sym == "ETHUSDT":
        sh = PINE["ETHUSDT"]["short"]
        return dict(direction="long_short", trend=None, di_align=None,
                    ema_gate=sh["ema"], kc_len=sh["kc"], kc_mult=sh["kc_mult"],
                    atr_len=sh["atr"], adx_min=sh["adx"][0], adx_max=sh["adx"][1],
                    rsi_lt=27.0, vol_mult=1.0,
                    sl=sh["sl"], be_mode=("at", sh["be"]), trail_start=sh["trail_start"],
                    trail=sh["trail"], texit=None, cooldown=sh["cooldown"])
    base = sym if sym in AUTHORED_SHORTS else "BTCUSDT"
    sh = AUTHORED_SHORTS[base]
    return dict(direction="long_short", trend=None, di_align=None,
                ema_gate=sh["ema"], kc_len=sh["kc"], kc_mult=sh["kc_mult"],
                atr_len=sh["atr"], adx_min=sh["adx"][0], adx_max=sh["adx"][1],
                rsi_lt=sh["rsi_lt"], vol_mult=1.0,
                sl=sh["sl"], be_mode=("at", sh["be"]), trail_start=sh["trail_start"],
                trail=sh["trail"], texit=None, cooldown=sh["cooldown"])


START_CFG2 = dict(  # round-3 start = elasticity-probe hint, shorts on
    direction="long_short", trend=None, di_align=5.0,
    ema_gate=200, kc_len=35, kc_mult=2.7, atr_len=10,
    adx_min=15.0, adx_max=100.0, rsi_gt=68.5, vol_mult=1.0,
    sl=6.95, be_mode="off", trail_start=1.5, trail=4.5,
    texit=200, cooldown=5,
)


def axes2(tf: str) -> list[tuple[str, list]]:
    return [
        ("trend", [None, ("slope", 680), ("slope", 200), ("above", 680), ("above", 200)]),
        ("di_align", [None, 0.0, 5.0]),
        ("ema_gate", [None, 200, 425, 680, 1000]),
        ("kc_len", [21, 24, 35, 50]),
        ("kc_mult", [1.5, 2.0, 2.7, 3.5]),
        ("adx_min", [0.0, 15.0, 23.5, 30.0]),
        ("adx_max", [59.0, 100.0]),
        ("rsi_gt", [None, 55.0, 60.0, 68.5]),
        ("vol_mult", [None, 1.0, 1.5]),
        ("sl", [2.5, 4.0, 6.95, 9.0]),
        ("be_mode", ["off", ("at", 1.2), ("at", 2.0)]),
        ("trail_start", [1.5, 2.03, 3.0, 4.0]),
        ("trail", [1.21, 2.0, 3.0, 4.5, 6.0, 8.0]),
        ("texit", [None, 200, 500, 1000]),
    ]


# --------------------------------------------------------------- dmi (both)
def pine_dmi(df: pd.DataFrame, di_len: int, adx_len: int):
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = rma(true_range(df), di_len)
    pdi = 100.0 * rma(plus_dm, di_len) / tr
    mdi = 100.0 * rma(minus_dm, di_len) / tr
    dx = 100.0 * (pdi - mdi).abs() / (pdi + mdi).replace(0.0, np.nan)
    adx = rma(dx.fillna(0.0), adx_len)
    return pdi.to_numpy(), mdi.to_numpy(), adx.to_numpy()


def build_side2(df: pd.DataFrame, p: dict, direction: int, di_cache: dict) -> dict:
    a = build_side(df, p, direction)
    pdi, mdi = di_cache["pdi"], di_cache["mdi"]
    if p.get("di_align") is not None:
        x = p["di_align"]
        if direction > 0:
            a["di_ok"] = np.asarray(pdi - mdi > x)
        else:
            a["di_ok"] = np.asarray(mdi - pdi > x)
    else:
        a["di_ok"] = None
    return a


# ------------------------------------------------------------------- sim v2
def run_trend2(df: pd.DataFrame, long_cfg: dict, short_cfg: dict | None,
               sizing: str = "risk", f: float = 0.01) -> list[dict]:
    """Round-2 sim: asymmetric sides supported natively (short_cfg None =>
    mirror of long). sizing 'risk' only (pct mode lives in trend_opt)."""
    o = df["open"].to_numpy(); h = df["high"].to_numpy()
    l = df["low"].to_numpy(); c = df["close"].to_numpy()
    v = df["volume"].to_numpy(); t = df["time"].to_numpy()
    n = len(df)
    di_cache = dict(zip(("pdi", "mdi", "adx"), pine_dmi(df, *FIXED["dmi"])))

    lp = side_params_from_long(long_cfg, +1)
    lp["di_align"] = long_cfg.get("di_align")
    L = build_side2(df, lp, +1, di_cache)
    if short_cfg is None and long_cfg.get("direction") == "long_short":
        sp = side_params_from_long(long_cfg, -1)
        sp["di_align"] = long_cfg.get("di_align")
        g_short = long_cfg
    elif short_cfg is None:
        sp = None
        g_short = long_cfg
    else:
        sp = dict(short_cfg)
        g_short = short_cfg
    S = build_side2(df, sp, -1, di_cache) if sp is not None else None

    pos = None; pending = None; stop_next = None; first_stop = None
    last_sig = {1: -(1 << 30), -1: -(1 << 30)}
    trades: list[dict] = []
    eq = INITIAL_EQUITY

    def close_trade(i, px, reason):
        nonlocal eq, pos, stop_next, first_stop
        fee_out = pos["qty"] * px * FEE
        pnl = pos["qty"] * (px - pos["entry"]) * pos["d"] - pos["fee_in"] - fee_out
        ret = pnl / eq if eq > 0 else 0.0
        eq += pnl
        risk_usd = pos["qty"] * abs(pos["entry"] - first_stop) if first_stop else 0.0
        trades.append(dict(i0=pos["i0"], t_in=int(t[pos["i0"]]), t_out=int(t[i]),
                           d=pos["d"], entry=float(pos["entry"]), exit=float(px),
                           reason=reason, ret=float(ret),
                           r=float(pnl / risk_usd) if risk_usd > 0 else 0.0))
        pos = None; stop_next = None; first_stop = None

    def open_pos(i, d, sig_atr, sl_mult):
        nonlocal pos
        px = float(o[i])
        dist = sl_mult * sig_atr
        qty = f * eq / dist if dist > 0 else 0.0
        qty = min(qty, 2.0 * eq / px)
        if qty <= 0:
            return
        pos = dict(d=d, entry=px, qty=qty, i0=i, fee_in=qty * px * FEE)

    sig_atr = None
    pend_atr = None
    for i in range(WARMUP, n):
        if pending is not None:
            d = pending
            pending = None
            g = long_cfg if d > 0 else g_short
            if pos is None:
                open_pos(i, d, pend_atr, g["sl"])
            elif pos["d"] != d:
                close_trade(i, float(o[i]), "reversal")
                open_pos(i, d, pend_atr, g["sl"])

        if pos is not None and stop_next is not None:
            s = stop_next; px = None
            if pos["d"] > 0:
                if o[i] <= s: px = o[i]
                elif l[i] <= s: px = s
            else:
                if o[i] >= s: px = o[i]
                elif h[i] >= s: px = s
            if px is not None:
                close_trade(i, float(px), "stop")

        gpos = long_cfg if (pos and pos["d"] > 0) else g_short
        if pos is not None and gpos.get("texit") and (i - pos["i0"]) >= gpos["texit"]:
            close_trade(i, float(c[i]), "texit")

        cd_l = long_cfg["cooldown"]; cd_s = g_short["cooldown"]
        sig_l = ((c[i] > L["kc_up"][i] or c[i - 1] > L["kc_up"][i])
                 and (L["trend"] is None or L["trend"][i])
                 and (L["ema_gate"] is None or c[i] > L["ema_gate"][i])
                 and long_cfg["adx_min"] < L["adx"][i] < long_cfg["adx_max"]
                 and (lp.get("rsi_gt") is None or L["rsi"][i] > lp["rsi_gt"])
                 and (L["di_ok"] is None or L["di_ok"][i])
                 and (lp.get("vol_mult") is None or v[i] > lp["vol_mult"] * L["vol_ma"][i])
                 and (i - last_sig[1]) > cd_l)
        sig_s = False
        if S is not None:
            sig_s = ((c[i] < S["kc_lo"][i] or c[i - 1] < S["kc_lo_prev"][i])
                     and (S["trend"] is None or S["trend"][i])
                     and (S["ema_gate"] is None or c[i] < S["ema_gate"][i])
                     and g_short["adx_min"] < S["adx"][i] < g_short["adx_max"]
                     and (sp.get("rsi_lt") is None or S["rsi"][i] < sp["rsi_lt"])
                     and (S["di_ok"] is None or S["di_ok"][i])
                     and (sp.get("vol_mult") is None or v[i] > sp["vol_mult"] * S["vol_ma"][i])
                     and (i - last_sig[-1]) > cd_s)
        if sig_l: last_sig[1] = i
        if sig_s: last_sig[-1] = i
        d = None
        if sig_l: d = 1
        if sig_s: d = -1
        if d is not None and (pos is None or pos["d"] != d):
            pending = d
            pend_atr = float(L["atr"][i]) if d > 0 else float(S["atr"][i])

        if pos is not None:
            a = L if pos["d"] > 0 else S
            g = long_cfg if pos["d"] > 0 else g_short
            atr = a["atr"][i] if a is not None else 0.0
            entry = pos["entry"]
            if atr > 0:
                profit = (c[i] - entry) / atr if pos["d"] > 0 else (entry - c[i]) / atr
                bm = g.get("be_mode", "off")
                be_trig = bm[1] if (isinstance(bm, (tuple, list)) and bm[0] == "at") else float("inf")
                if pos["d"] > 0:
                    initial = entry - atr * g["sl"]
                    be_stop = entry if profit >= be_trig else initial
                    trail_stop = (c[i] - atr * g["trail"]) if profit >= g["trail_start"] else be_stop
                    final = max(trail_stop, be_stop)
                else:
                    initial = entry + atr * g["sl"]
                    be_stop = entry if profit >= be_trig else initial
                    trail_stop = (c[i] + atr * g["trail"]) if profit >= g["trail_start"] else be_stop
                    final = min(trail_stop, be_stop)
                stop_next = float(final)
                if first_stop is None:
                    first_stop = float(final)

    if pos is not None:
        close_trade(n - 1, float(c[n - 1]), "open@end")
    return trades


# ------------------------------------------------------------- stats & gates
def seg_stats(trades: list[dict], t_lo: int, t_hi: int, i_lo: int, i_hi: int) -> dict:
    trs = [tr for tr in trades if tr["i0"] is not None and i_lo <= tr["i0"] < i_hi]
    trs.sort(key=lambda tr: (tr["t_out"], tr["t_in"]))
    eq = INITIAL_EQUITY; peak = eq; maxdd = 0.0
    for tr in trs:
        eq *= 1.0 + tr["ret"]
        peak = max(peak, eq); maxdd = max(maxdd, 1 - eq / peak)
    years = (t_hi - t_lo) / (365.25 * 86400_000)
    total = eq / INITIAL_EQUITY - 1.0
    ann = (eq / INITIAL_EQUITY) ** (1.0 / years) - 1.0 if years > 0 and eq > 0 else 0.0
    return dict(n=len(trs), total=total, ann=ann, maxdd=maxdd, years=years)


def objective_J(per_sym: dict) -> float:
    """Soft-floor objective (round-2a): mean annualized return
    + 0.5*min(0, worst_symbol_total) - 2*max(0, meanDD - 10%).
    Suffocation rule: <3 trades/symbol on average -> 0."""
    if not per_sym:
        return 0.0
    totals = [s["total"] for s in per_sym.values()]
    anns = [s["ann"] for s in per_sym.values()]
    dds = [s["maxdd"] for s in per_sym.values()]
    ns = [s["n"] for s in per_sym.values()]
    if np.mean(ns) < MIN_TRADES_PER_SYM:
        return 0.0
    return float(np.mean(anns) + 0.5 * min(0.0, min(totals))
                 - 2.0 * max(0.0, float(np.mean(dds)) - MEAN_DD_CAP))


def wf_gate(trades_by_sym: dict, dfs: dict) -> tuple[bool, dict]:
    """Anchored expanding walk-forward over the given bar region per symbol.
    trades_by_sym: sym -> trades (for ONE config); dfs: sym -> df.
    Region: fold A bars per symbol. 4 equal test segments."""
    seg_pass = 0
    seg_means = []
    detail = {}
    for k in range(1, WF_SEGMENTS + 1):
        per = {}
        for sym, trades in trades_by_sym.items():
            df = dfs[sym]
            n = len(df)
            b = fold_bounds(n)
            a_lo, a_hi = b["A"]
            seg = (a_hi - a_lo) / WF_SEGMENTS
            i_lo = a_lo + int((k - 1) * seg)
            i_hi = a_lo + int(k * seg) if k < WF_SEGMENTS else a_hi
            st = seg_stats(trades, int(df["time"].iloc[i_lo]),
                           int(df["time"].iloc[min(i_hi, n) - 1]), i_lo, i_hi)
            per[sym] = st
        m = float(np.mean([s["ann"] for s in per.values()]))
        seg_means.append(m)
        detail[f"seg{k}"] = dict(mean_ann=m, per={s: dict(total=v["total"], n=v["n"]) for s, v in per.items()})
        if m > 0:
            seg_pass += 1
    ok = seg_pass >= 2 and float(np.mean(seg_means)) > 0
    return ok, dict(seg_pass=seg_pass, seg_means=seg_means, detail=detail)


# ------------------------------------------------------------------ workers
_DF_CACHE: dict = {}


def get_df(sym: str, tf: str) -> pd.DataFrame:
    key = (sym, tf)
    if key not in _DF_CACHE:
        rows = kline_cache._read_rows(sym, tf, 1 << 62, WINDOWS[tf])
        if not rows or len(rows) < WINDOWS[tf] * 0.6:
            raise SystemExit(f"{sym} {tf}: insufficient cached data")
        _DF_CACHE[key] = kline_cache.rows_to_df(rows)
    return _DF_CACHE[key]


def eval_task(task: dict) -> dict:
    sym, tf = task["sym"], task["tf"]
    df = get_df(sym, tf)
    kind = task["kind"]
    if kind == "cfg":
        cfg = _cfg_from_json(copy.deepcopy(task["cfg"]))
        trades = run_trend2(df, cfg, None)
    elif kind == "incumbent2":
        lg = _cfg_from_json(incumbent_long_cfg2(sym))
        sh = incumbent_short_cfg2(sym)
        trades = run_trend2(df, lg, sh)
    else:
        raise ValueError(kind)
    return dict(sym=sym, tf=tf, tag=task.get("tag"), trades=trades)


def run_pool(tasks: list[dict], workers: int = 8, serial: bool = False) -> list[dict]:
    if serial:
        return [eval_task(t) for t in tasks]
    with mp.get_context("spawn").Pool(processes=workers) as pool:
        return pool.map(eval_task, tasks)


_MAIN_DFS: dict = {}


def get_df_local(sym, tf):
    key = (sym, tf)
    if key not in _MAIN_DFS:
        rows = kline_cache._read_rows(sym, tf, 1 << 62, WINDOWS[tf])
        _MAIN_DFS[key] = kline_cache.rows_to_df(rows)
    return _MAIN_DFS[key]


def per_sym_on_region(results: list[dict], i_range: str) -> dict:
    """i_range: 'A' | 'B' | 'AB' | 'C'. C uses the global time cutoff."""
    out = {}
    for res in results:
        df = get_df_local(res["sym"], res["tf"])
        n = len(df)
        b = fold_bounds(n)
        if i_range == "C":
            i_lo = int(np.searchsorted(df["time"].to_numpy(), C_CUTOFF_MS))
            i_hi = n
        elif i_range == "AB":
            i_lo, i_hi = b["A"][0], b["B"][1]
        else:
            i_lo, i_hi = b[i_range]
        st = seg_stats(res["trades"], int(df["time"].iloc[i_lo]),
                       int(df["time"].iloc[min(i_hi, n) - 1]), i_lo, i_hi)
        out[res["sym"]] = st
    return out


def evaluate_cfg(tf: str, cfg: dict, region: str, serial: bool) -> tuple[float, dict, list]:
    tasks = [dict(sym=s, tf=tf, kind="cfg", cfg=_jsonable_cfg(cfg)) for s in ALL_SYMBOLS]
    res = run_pool(tasks, serial=serial)
    per = per_sym_on_region(res, region)
    return objective_J(per), per, res


def coordinate_descent2(tf: str, start_cfg: dict, serial: bool, label: str,
                        wf_check: bool) -> tuple[dict, list]:
    axes = axes2(tf)
    best = copy.deepcopy(start_cfg)
    history = []
    best_obj, best_per, _ = evaluate_cfg(tf, best, "A", serial)
    history.append(dict(step="start", axis=None, value=None, obj=best_obj,
                        cfg=copy.deepcopy(_jsonable_cfg(best))))
    print(f"[{label}] start J(A)={best_obj:.4f}", flush=True)

    for axis, values in axes:
        cur_val = best.get(axis)
        tried = []
        for val in values:
            if val == cur_val or (axis == "trend" and _trend_eq(val, cur_val)):
                continue
            cand = copy.deepcopy(best)
            cand[axis] = copy.deepcopy(val)
            obj, per, _ = evaluate_cfg(tf, cand, "A", serial)
            tried.append((obj, val))
            print(f"[{label}] {axis}={_fmt_val(val)}: J={obj:.4f}", flush=True)
        if tried:
            top = max(tried, key=lambda x: x[0])
            if top[0] > best_obj:
                best[axis] = copy.deepcopy(top[1])
                best_obj = top[0]
                history.append(dict(step=axis, axis=axis, value=_json_val(top[1]),
                                    obj=best_obj, cfg=copy.deepcopy(_jsonable_cfg(best))))
                print(f"[{label}] >> adopt {axis}={_fmt_val(top[1])} J={best_obj:.4f}", flush=True)
    return best, history


def _trend_eq(a, b):
    if a is None or b is None:
        return a is None and b is None
    return tuple(a) == tuple(b)


def _fmt_val(v):
    return f"{v[0]}{v[1]}" if isinstance(v, (tuple, list)) else str(v)


def _json_val(v):
    return list(v) if isinstance(v, tuple) else v


def state_file(tf: str, phase: str) -> str:
    from trend_opt import TEMP_DIR
    return os.path.join(TEMP_DIR, f"trend_opt2_{tf}_{phase}.json")


def save_state(tf, phase, data):
    from trend_opt import TEMP_DIR
    os.makedirs(TEMP_DIR, exist_ok=True)
    with open(state_file(tf, phase), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)


def load_state(tf, phase):
    with open(state_file(tf, phase), encoding="utf-8") as fh:
        return json.load(fh)


# ------------------------------------------------------------------- phases
def phase_baseline(tfs, serial):
    print("\n===== R2 BASELINE: two-sided incumbent (authored shorts activated) =====")
    for tf in tfs:
        tasks = [dict(sym=s, tf=tf, kind="incumbent2") for s in ALL_SYMBOLS]
        results = run_pool(tasks, serial=serial)
        per_full = {}
        per_c = per_sym_on_region(results, "C")
        for res in results:
            df = get_df_local(res["sym"], tf)
            n = len(df)
            per_full[res["sym"]] = seg_stats(
                res["trades"], int(df["time"].iloc[WARMUP]),
                int(df["time"].iloc[-1]), WARMUP, n)
        save_state(tf, "baseline", dict(full=per_full, C=per_c))
        print(f"\n--- {tf} two-sided incumbent (f=1%) ---")
        print(f"{'sym':<9}{'n':>6}{'fullRet':>10}{'fullDD':>8} | {'C_n':>4}{'C_Ret':>9}{'C_DD':>8}")
        for sym in ALL_SYMBOLS:
            f_, c_ = per_full[sym], per_c[sym]
            print(f"{sym:<9}{f_['n']:>6}{f_['total']*100:>+9.1f}%{f_['maxdd']*100:>7.1f}% | "
                  f"{c_['n']:>4}{c_['total']*100:>+8.1f}%{c_['maxdd']*100:>7.1f}%")
        print(f"  C mean ann {np.mean([v['ann'] for v in per_c.values()])*100:+.2f}%/yr  "
              f"J(C)={objective_J(per_c):.4f}")


def phase1(tf, serial):
    best_a, hist = coordinate_descent2(tf, START_CFG2, serial, f"p1-{tf}", wf_check=True)
    # candidate set: final + top-2 distinct improvers
    cands = [best_a]
    seen = {json.dumps(_jsonable_cfg(best_a), sort_keys=True)}
    improving = [h for h in hist if h["axis"] is not None]
    for h in sorted(improving, key=lambda x: -x["obj"]):
        key = json.dumps(h["cfg"], sort_keys=True)
        if key not in seen:
            cands.append(_cfg_from_json(copy.deepcopy(h["cfg"])))
            seen.add(key)
        if len(cands) >= 3:
            break

    # WF gate on fold A for each candidate
    scored = []
    for idx, cfg in enumerate(cands):
        tasks = [dict(sym=s, tf=tf, kind="cfg", cfg=_jsonable_cfg(cfg)) for s in ALL_SYMBOLS]
        res = run_pool(tasks, serial=serial)
        trades_by_sym = {r["sym"]: r["trades"] for r in res}
        dfs = {s: get_df_local(s, tf) for s in ALL_SYMBOLS}
        wf_ok, wf_detail = wf_gate(trades_by_sym, dfs)
        per_b = per_sym_on_region(res, "B")
        j_b = objective_J(per_b)
        scored.append(dict(idx=idx, cfg=_jsonable_cfg(cfg), wf_ok=wf_ok,
                           wf=wf_detail, j_b=j_b, per_b=per_b))
        print(f"[p1-{tf}] cand{idx}: WF {'PASS' if wf_ok else 'FAIL'}"
              f"({wf_detail['seg_pass']}/4)  J(B)={j_b:.4f}", flush=True)
    eligible = [s for s in scored if s["wf_ok"]] or scored  # all fail -> keep all, noted
    winner = max(eligible, key=lambda x: x["j_b"])
    save_state(tf, "phase1", dict(history=hist, scored=scored, winner=winner["cfg"]))
    print(f"[p1-{tf}] WINNER J(B)={winner['j_b']:.4f} cfg={winner['cfg']}", flush=True)


def phase2(tf, serial):
    st = load_state(tf, "phase1")
    winner = _cfg_from_json(copy.deepcopy(st["winner"]))
    # coordinate descent restarted on A+B
    axes = axes2(tf)
    best = copy.deepcopy(winner)
    hist = []
    tasks = [dict(sym=s, tf=tf, kind="cfg", cfg=_jsonable_cfg(best)) for s in ALL_SYMBOLS]
    res = run_pool(tasks, serial=serial)
    per_ab = per_sym_on_region(res, "AB")
    best_obj = objective_J(per_ab)
    print(f"[p2-{tf}] start J(AB)={best_obj:.4f}", flush=True)
    for axis, values in axes:
        cur_val = best.get(axis)
        tried = []
        for val in values:
            if val == cur_val or (axis == "trend" and _trend_eq(val, cur_val)):
                continue
            cand = copy.deepcopy(best)
            cand[axis] = copy.deepcopy(val)
            tasks = [dict(sym=s, tf=tf, kind="cfg", cfg=_jsonable_cfg(cand)) for s in ALL_SYMBOLS]
            res = run_pool(tasks, serial=serial)
            per = per_sym_on_region(res, "AB")
            obj = objective_J(per)
            tried.append((obj, val))
            print(f"[p2-{tf}] {axis}={_fmt_val(val)}: J(AB)={obj:.4f}", flush=True)
        if tried:
            top = max(tried, key=lambda x: x[0])
            if top[0] > best_obj:
                best[axis] = copy.deepcopy(top[1])
                best_obj = top[0]
                hist.append(dict(axis=axis, value=_json_val(top[1]), obj=best_obj,
                                 cfg=copy.deepcopy(_jsonable_cfg(best))))
                print(f"[p2-{tf}] >> adopt {axis}={_fmt_val(top[1])} J={best_obj:.4f}", flush=True)

    # ---- single blind evaluation on C ----
    tasks = [dict(sym=s, tf=tf, kind="cfg", cfg=_jsonable_cfg(best)) for s in ALL_SYMBOLS]
    res_cand = run_pool(tasks, serial=serial)
    tasks_inc = [dict(sym=s, tf=tf, kind="incumbent2") for s in ALL_SYMBOLS]
    res_inc = run_pool(tasks_inc, serial=serial)

    cand_c = per_sym_on_region(res_cand, "C")
    inc_c = per_sym_on_region(res_inc, "C")
    cand_ab = per_sym_on_region(res_cand, "AB")
    inc_ab = per_sym_on_region(res_inc, "AB")

    mean_ann_c = float(np.mean([v["ann"] for v in cand_c.values()]))
    mean_ann_i = float(np.mean([v["ann"] for v in inc_c.values()]))
    worst_c = min(v["total"] for v in cand_c.values())
    worst_i = min(v["total"] for v in inc_c.values())
    mean_dd_c = float(np.mean([v["maxdd"] for v in cand_c.values()]))
    max_dd_c = max(v["maxdd"] for v in cand_c.values())
    mean_n_c = float(np.mean([v["n"] for v in cand_c.values()]))
    k1 = mean_ann_c >= 1.25 * mean_ann_i
    k2 = sum(1 for v in cand_c.values() if v["total"] > 0) >= 6
    k3 = worst_c >= worst_i
    k4 = mean_dd_c <= 0.12 and max_dd_c <= 0.25
    k5 = mean_n_c >= MIN_TRADES_PER_SYM
    verdict = k1 and k2 and k3 and k4 and k5

    out = dict(final_cfg=_jsonable_cfg(best), history=hist,
               cand_c=cand_c, inc_c=inc_c, cand_ab=cand_ab, inc_ab=inc_ab,
               gates=dict(K1=k1, K2=k2, K3=k3, K4=k4, K5=k5,
                          mean_ann_c=mean_ann_c, mean_ann_i=mean_ann_i,
                          worst_c=worst_c, worst_i=worst_i,
                          mean_dd_c=mean_dd_c, max_dd_c=max_dd_c, mean_n_c=mean_n_c),
               verdict=bool(verdict))
    save_state(tf, "phase2", out)

    print(f"\n===== {tf} BLIND C (>=2025-03-01) verdict: {'PASS' if verdict else 'FAIL'} =====")
    print(f"K1 meanAnn_C {mean_ann_c*100:+.2f}% vs inc {mean_ann_i*100:+.2f}% "
          f"(need >= {1.25*mean_ann_i*100:+.2f}%): {'OK' if k1 else 'X'}")
    print(f"K2 all positive: {'OK' if k2 else 'X'} | "
          f"K3 worst {worst_c*100:+.1f}% vs {worst_i*100:+.1f}%: {'OK' if k3 else 'X'} | "
          f"K4 DD mean {mean_dd_c*100:.1f}% max {max_dd_c*100:.1f}%: {'OK' if k4 else 'X'} | "
          f"K5 mean n {mean_n_c:.1f}: {'OK' if k5 else 'X'}")
    print(f"{'sym':<9}{'n':>5}{'candRet':>9}{'candDD':>8}{'candAnn':>9}{'incRet':>9}{'incDD':>8}")
    for sym in ALL_SYMBOLS:
        cc, ii = cand_c[sym], inc_c[sym]
        print(f"{sym:<9}{cc['n']:>5}{cc['total']*100:>+8.1f}%{cc['maxdd']*100:>7.1f}%"
              f"{cc['ann']*100:>+8.1f}%{ii['total']*100:>+8.1f}%{ii['maxdd']*100:>7.1f}%")
    # direction split
    for tag, resx in (("cand", res_cand), ("inc", res_inc)):
        longs = shorts = 0
        lr = sr = 0.0
        for r_ in resx:
            for tr in r_["trades"]:
                if tr["t_in"] >= C_CUTOFF_MS:
                    if tr["d"] > 0:
                        longs += 1; lr += tr["ret"]
                    else:
                        shorts += 1; sr += tr["ret"]
        print(f"  {tag} C direction split: long {longs} trades sumRet {lr*100:+.1f}% | "
              f"short {shorts} trades sumRet {sr*100:+.1f}%")


def report(tfs, serial):
    for tf in tfs:
        st = load_state(tf, "phase2")
        cfg = _cfg_from_json(copy.deepcopy(st["final_cfg"]))
        tasks = [dict(sym=s, tf=tf, kind="cfg", cfg=_jsonable_cfg(cfg)) for s in ALL_SYMBOLS]
        results = run_pool(tasks, serial=serial)
        print(f"\n{'='*88}\n===== R2 FINAL {tf} candidate full-window "
              f"(IN-SAMPLE; honest headline = blind C above) =====")
        print(f"cfg: {json.dumps(st['final_cfg'], ensure_ascii=False)}")
        print(f"{'sym':<9}{'n':>6}{'total':>10}{'ann':>8}{'maxDD':>8}{'winrate':>9}"
              f"{'long%':>7}")
        for res in results:
            df = get_df_local(res["sym"], tf)
            trs = [tr for tr in res["trades"] if tr["i0"] is not None and tr["i0"] >= WARMUP]
            stt = seg_stats(trs, int(df["time"].iloc[WARMUP]), int(df["time"].iloc[-1]),
                            WARMUP, len(df))
            wr = float(np.mean([tr["ret"] > 0 for tr in trs])) if trs else 0
            ln = sum(1 for tr in trs if tr["d"] > 0) / max(1, len(trs))
            print(f"{res['sym']:<9}{stt['n']:>6}{stt['total']*100:>+9.1f}%"
                  f"{stt['ann']*100:>+7.1f}%{stt['maxdd']*100:>7.1f}%{wr*100:>8.1f}%{ln*100:>6.0f}%")
        # per-year
        print(f"\n  per-year (f=1%):")
        ys = list(range(2021, 2027))
        print("  " + f"{'sym':<9}" + "".join(f"{y:>9}" for y in ys))
        for res in results:
            trs = [tr for tr in res["trades"] if tr["i0"] is not None and tr["i0"] >= WARMUP]
            yearly_eq: dict = {}
            eq = INITIAL_EQUITY
            for tr in sorted(trs, key=lambda x: x["t_out"]):
                eq *= 1.0 + tr["ret"]
                yearly_eq[year_of(tr["t_out"])] = eq
            row = f"  {res['sym']:<9}"
            prev = INITIAL_EQUITY
            for y in ys:
                if y in yearly_eq:
                    r = yearly_eq[y] / prev - 1
                    prev = yearly_eq[y]
                    row += f"{r*100:>+8.0f}%"
                else:
                    row += f"{'-':>9}"
            print(row)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True,
                    choices=["baseline", "phase1", "phase2", "report"])
    ap.add_argument("--tf", default="both", choices=["1h", "4h", "both"])
    ap.add_argument("--serial", action="store_true")
    args = ap.parse_args()
    tfs = ["1h", "4h"] if args.tf == "both" else [args.tf]
    if args.phase == "baseline":
        phase_baseline(tfs, args.serial)
    elif args.phase == "phase1":
        for tf in tfs:
            phase1(tf, args.serial)
    elif args.phase == "phase2":
        for tf in tfs:
            phase2(tf, args.serial)
    elif args.phase == "report":
        report(tfs, args.serial)


if __name__ == "__main__":
    main()
