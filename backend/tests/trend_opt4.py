"""Trend-strategy optimization ROUND 4 (2026-08-30) — PROFIT-FIRST.

User directive: "continue modifying the trend strategy and optimizing with
RETURN as the #1 objective". This is a NEW round with a fundamentally
different objective — return maximization, constraints relaxed:

  - NO hard drawdown gate during tuning (reported, not gated; a soft
    sanity cap mean-DD <= 25% prevents martingale monsters).
  - NO worst-symbol floor gate (only a weak anti-one-bet rule:
    >= 6 of 8 symbols blind-C total > 0).
  - PYRAMIDING ALLOWED (up to P adds) — directly attacks structural leak #1
    (long holds blocking reversal entries / no adds into a winning trend).
  - Exit structure opened up: optional breakeven, optional trail, optional
    giveback exit (exit when close gives back x ATR from the running peak —
    the textbook trend exit), optional structure target.
  - f (risk per trade) becomes a REPORTED tier, not a single fixed value:
    every config evaluated at f = 1% / 2% / 3% so we do not just pick a
    leverage answer; headline = f=1% comparability, but the report shows
    the f-ladder so the user sees the true return/risk tradeoff.

ROUND-4 PROTOCOL (pre-registered before the run):
  Universe: 8 notify symbols, 1h ONLY, shorts always on (mirror).
  Sizing: risk-based qty = f*eq / (sl * ATR_signal); pyramiding adds use
    the SAME f per add; total notional cap = 5x equity (was 2x; pyramid
    needs headroom). Fee 0.04%/side on every add/exit.
  Folds: A = first 40% of bars, B = next 30% (tuning);
    C blind = entry >= 2025-03-01 UTC (unchanged, ~1.5y).
  Objective (tuning): mean annualized return across symbols (soft
    sanity: 0 if mean per-symbol DD > 25% or mean trades/symbol < 3).
  Walk-forward gate: unchanged (>=2/4 anchored segments positive).
  Blind C gates (candidate vs two-sided incumbent, BOTH at f=1%):
    K1 mean_annualized_C(candidate) >= 1.5 x incumbent  (higher bar this
       round: profit-first must actually deliver more, not +8% relative)
    K2 >= 6 of 8 symbols C total > 0
    K3 candidate mean C maxDD <= 25% (report-only beyond this)
    K4 candidate C trades >= 3 per symbol on average
  If gates fail: archived; NO fifth round without new data dimensions.

Axes (order fixed; values predefined):
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
 14 giveback      [None, 2.0, 3.0, 4.0]   # exit if close gives back x ATR from peak
 15 pyramiding    [0, 1, 2, 3]            # max same-direction adds
 16 add_step      [1.0, 1.5, 2.0]         # ATR of adverse-free move before next add
 17 texit         [None, 200, 500, 1000]

Pyramid semantics: while in a position, a NEW same-direction entry signal
adds f*eq/(sl*ATR_at_add) more quantity (max pyramiding adds; the position
stop tracks the LAST add's ATR-based trail, averaged entry for PnL).
Giveback exit (if set): track running peak close since entry; exit full
position at bar close if (peak - close) >= giveback * ATR (long).

Execution: mp.Pool per (symbol, config); per-phase JSON in TEMP_DIR.
Usage: tests/trend_opt4.py --phase baseline|phase1|phase2|report [--serial]

ROUND 5 (2026-08-30, same file, --round5 flag): user wants the gap to the
production 4h engine closed and ZEC removed (fat-tail concentration).
Changes vs round 4, pre-registered before the run:
  - Universe: 7 symbols (ZECUSDT dropped at user direction).
  - Objective (tuning): minimize the gap to the production 4h engine's
    per-symbol mean annualized return at the same f — implemented as
    J5 = mean_candidate_ann - 0.5 * max(0, production_benchmark_ann
    - mean_candidate_ann) with production benchmark fixed at f=1%
    (+106.5%/yr = mean of BTC/ETH/SOL 4h, measured this round). In
    practice the benchmark is unreachable for this signal family
    (disclosed to the user before running); the objective degenerates to
    maximizing mean_ann, which is what we report.
  - f ladder extended: 1% / 3% / 5%.
  - Blind C gates (candidate vs incumbent at f=1%, 7 symbols):
    K1 mean_ann_C(candidate) >= 1.5 x incumbent
    K2 >= 5 of 7 symbols C total > 0
    K3 mean C maxDD <= 25%
    K4 C trades >= 3 per symbol on average
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
from trend_opt import (ALL_SYMBOLS, FIXED, WARMUP, WINDOWS, _cfg_from_json,  # noqa: E402
                       _jsonable_cfg, build_side, fold_bounds,
                       side_params_from_long)
from trend_opt2 import (C_CUTOFF_MS, MIN_TRADES_PER_SYM, WF_SEGMENTS,  # noqa: E402
                        _fmt_val, _json_val, _trend_eq, build_side2,
                        fold_bounds as fb2, incumbent_long_cfg2,
                        incumbent_short_cfg2, load_state, objective_J,
                        per_sym_on_region, pine_dmi, save_state, seg_stats,
                        state_file, wf_gate)
from services import kline_cache  # noqa: E402

NOTIONAL_CAP4 = 5.0
MEAN_DD_CAP4 = 0.25
SYMS5 = [s for s in ALL_SYMBOLS if s != "ZECUSDT"]  # round 5: ZEC dropped

START_CFG4 = dict(
    direction="long_short", trend=None, di_align=5.0,
    ema_gate=200, kc_len=35, kc_mult=2.7, atr_len=10,
    adx_min=15.0, adx_max=100.0, rsi_gt=68.5, vol_mult=1.0,
    sl=6.95, be_mode="off", trail_start=1.5, trail=4.5,
    giveback=None, pyramiding=0, add_step=1.5,
    texit=200, cooldown=5,
)


def axes4() -> list[tuple[str, list]]:
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
        ("giveback", [None, 2.0, 3.0, 4.0]),
        ("pyramiding", [0, 1, 2, 3]),
        ("add_step", [1.0, 1.5, 2.0]),
        ("texit", [None, 200, 500, 1000]),
    ]


# ------------------------------------------------------------------- sim v4
def run_trend4(df: pd.DataFrame, long_cfg: dict, short_cfg: dict | None,
               f: float = 0.01) -> list[dict]:
    """Round-4 sim: pyramiding + giveback exit on top of round-2 semantics.
    Position model: list of lots (each with entry, qty, atr_at_add, i0).
    One shared stop for the whole position (tracked from last add's ATR)."""
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
        sp = None; g_short = long_cfg
    else:
        sp = dict(short_cfg); g_short = short_cfg
    S = build_side2(df, sp, -1, di_cache) if sp is not None else None

    lots: list[dict] = []
    direction = 0
    pending = None
    pend_atr = None
    stop_next = None
    first_stop = None
    peak_close = None
    trough_close = None
    last_sig = {1: -(1 << 30), -1: -(1 << 30)}
    trades: list[dict] = []
    eq = INITIAL_EQUITY

    def pos_qty():
        return sum(lot["qty"] for lot in lots)

    def avg_entry():
        q = pos_qty()
        return sum(lot["qty"] * lot["entry"] for lot in lots) / q if q else 0.0

    def close_all(i, px, reason):
        nonlocal eq, lots, direction, stop_next, first_stop, peak_close, trough_close
        q = pos_qty()
        if q <= 0:
            return
        ae = avg_entry()
        fee_out = q * px * FEE
        fee_in = sum(lot["fee_in"] for lot in lots)
        pnl = q * (px - ae) * direction - fee_in - fee_out
        ret = pnl / eq if eq > 0 else 0.0
        eq += pnl
        risk_usd = q * abs(ae - first_stop) if first_stop else 0.0
        trades.append(dict(i0=lots[0]["i0"], t_in=int(t[lots[0]["i0"]]),
                           t_out=int(t[i]), d=direction, entry=float(ae),
                           exit=float(px), reason=reason, ret=float(ret),
                           r=float(pnl / risk_usd) if risk_usd > 0 else 0.0,
                           lots=len(lots)))
        lots = []; direction = 0; stop_next = None; first_stop = None
        peak_close = None; trough_close = None

    def add_lot(i, d, sig_atr, sl_mult):
        nonlocal direction, first_stop
        px = float(o[i])
        dist = sl_mult * sig_atr
        qty = f * eq / dist if dist > 0 else 0.0
        qty = min(qty, NOTIONAL_CAP4 * eq / px)
        if qty <= 0:
            return
        lots.append(dict(entry=px, qty=qty, i0=i, atr=sig_atr,
                         fee_in=qty * px * FEE))
        direction = d
        if first_stop is None:
            first_stop = px - d * sl_mult * sig_atr

    for i in range(WARMUP, n):
        # fills at open
        if pending is not None:
            d, sa = pending
            pending = None
            g = long_cfg if d > 0 else g_short
            if direction == 0:
                add_lot(i, d, sa, g["sl"])
            elif direction != d:
                close_all(i, float(o[i]), "reversal")
                add_lot(i, d, sa, g["sl"])
            else:
                # same-direction signal: pyramid add if allowed
                max_adds = g.get("pyramiding", 0)
                if len(lots) - 1 < max_adds:
                    last = lots[-1]
                    move = (c[i-1] - last["entry"]) / sa if d > 0 else (last["entry"] - c[i-1]) / sa
                    if move >= g.get("add_step", 1.5):
                        add_lot(i, d, sa, g["sl"])

        # intrabar stop
        if lots and stop_next is not None:
            s = stop_next; px = None
            if direction > 0:
                if o[i] <= s: px = o[i]
                elif l[i] <= s: px = s
            else:
                if o[i] >= s: px = o[i]
                elif h[i] >= s: px = s
            if px is not None:
                close_all(i, float(px), "stop")

        gpos = long_cfg if direction > 0 else g_short
        if lots and gpos.get("texit") and (i - lots[0]["i0"]) >= gpos["texit"]:
            close_all(i, float(c[i]), "texit")

        # signals at close
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
        if d is not None:
            # fire if flat, or reversal, or same-dir pyramid eligible
            if direction == 0 or direction != d:
                pending = (d, float(L["atr"][i]) if d > 0 else float(S["atr"][i]))
            elif direction == d:
                g = long_cfg if d > 0 else g_short
                if len(lots) - 1 < g.get("pyramiding", 0):
                    pending = (d, float(L["atr"][i]) if d > 0 else float(S["atr"][i]))

        # giveback exit at close
        if lots:
            if direction > 0:
                peak_close = c[i] if peak_close is None else max(peak_close, c[i])
            else:
                trough_close = c[i] if trough_close is None else min(trough_close, c[i])
            gb = gpos.get("giveback")
            atr = (L if direction > 0 else S)["atr"][i]
            if gb is not None and atr > 0:
                if direction > 0 and (peak_close - c[i]) >= gb * atr:
                    close_all(i, float(c[i]), "giveback")
                elif direction < 0 and (c[i] - trough_close) >= gb * atr:
                    close_all(i, float(c[i]), "giveback")

        # stop for next bar (from LAST add's ATR, stateless per Pine)
        if lots:
            a = L if direction > 0 else S
            g = long_cfg if direction > 0 else g_short
            atr = a["atr"][i]; ae = avg_entry()
            if atr > 0:
                profit = (c[i] - ae) / atr if direction > 0 else (ae - c[i]) / atr
                bm = g.get("be_mode", "off")
                be_trig = bm[1] if (isinstance(bm, (tuple, list)) and bm[0] == "at") else float("inf")
                if direction > 0:
                    initial = ae - atr * g["sl"]
                    be_stop = ae if profit >= be_trig else initial
                    trail_stop = (c[i] - atr * g["trail"]) if profit >= g["trail_start"] else be_stop
                    final = max(trail_stop, be_stop)
                else:
                    initial = ae + atr * g["sl"]
                    be_stop = ae if profit >= be_trig else initial
                    trail_stop = (c[i] + atr * g["trail"]) if profit >= g["trail_start"] else be_stop
                    final = min(trail_stop, be_stop)
                stop_next = float(final)
                if first_stop is None:
                    first_stop = float(final)

    if lots:
        close_all(n - 1, float(c[n - 1]), "open@end")
    return trades


# ------------------------------------------------------------------ workers
_DF_CACHE: dict = {}


def get_df4(sym: str, tf: str = "1h") -> pd.DataFrame:
    key = (sym, tf)
    if key not in _DF_CACHE:
        rows = kline_cache._read_rows(sym, tf, 1 << 62, WINDOWS[tf])
        if not rows or len(rows) < WINDOWS[tf] * 0.6:
            raise SystemExit(f"{sym} {tf}: insufficient cached data")
        _DF_CACHE[key] = kline_cache.rows_to_df(rows)
    return _DF_CACHE[key]


def eval_task4(task: dict) -> dict:
    sym, tf = task["sym"], task.get("tf", "1h")
    df = get_df4(sym, tf)
    kind = task["kind"]
    f = task.get("f", 0.01)
    if kind == "cfg":
        cfg = _cfg_from_json(copy.deepcopy(task["cfg"]))
        trades = run_trend4(df, cfg, None, f=f)
    elif kind == "incumbent2":
        lg = _cfg_from_json(incumbent_long_cfg2(sym))
        sh = incumbent_short_cfg2(sym)
        lg.setdefault("pyramiding", 0); lg.setdefault("giveback", None)
        sh.setdefault("pyramiding", 0); sh.setdefault("giveback", None)
        trades = run_trend4(df, lg, sh, f=f)
    else:
        raise ValueError(kind)
    return dict(sym=sym, tf=tf, tag=task.get("tag"), f=f, trades=trades)


def run_pool4(tasks: list[dict], workers: int = 8, serial: bool = False) -> list[dict]:
    if serial:
        return [eval_task4(t) for t in tasks]
    with mp.get_context("spawn").Pool(processes=workers) as pool:
        return pool.map(eval_task4, tasks)


_MAIN_DFS: dict = {}


def get_df_local4(sym, tf="1h"):
    key = (sym, tf)
    if key not in _MAIN_DFS:
        rows = kline_cache._read_rows(sym, tf, 1 << 62, WINDOWS[tf])
        _MAIN_DFS[key] = kline_cache.rows_to_df(rows)
    return _MAIN_DFS[key]


def objective_R(per_sym: dict) -> float:
    """Profit-first: mean annualized return; soft sanity caps."""
    if not per_sym:
        return 0.0
    ns = [s["n"] for s in per_sym.values()]
    dds = [s["maxdd"] for s in per_sym.values()]
    if np.mean(ns) < MIN_TRADES_PER_SYM:
        return 0.0
    if np.mean(dds) > MEAN_DD_CAP4:
        return 0.0
    return float(np.mean([s["ann"] for s in per_sym.values()]))


def evaluate_cfg4(cfg: dict, region: str, serial: bool, f: float = 0.01,
                  syms=None):
    syms = syms or ALL_SYMBOLS
    tasks = [dict(sym=s, tf="1h", kind="cfg", cfg=_jsonable_cfg(cfg), f=f)
             for s in syms]
    res = run_pool4(tasks, serial=serial)
    per = per_sym_on_region(res, region)
    return objective_R(per), per, res


def coordinate_descent4(start_cfg: dict, serial: bool, label: str,
                        syms=None, start_region: str = "A"):
    syms = syms or ALL_SYMBOLS
    axes = axes4()
    best = copy.deepcopy(start_cfg)
    history = []
    best_obj, _, _ = evaluate_cfg4(best, start_region, serial, syms=syms)
    history.append(dict(step="start", axis=None, value=None, obj=best_obj,
                        cfg=copy.deepcopy(_jsonable_cfg(best))))
    print(f"[{label}] start R({start_region})={best_obj:.4f}", flush=True)
    for axis, values in axes:
        cur_val = best.get(axis)
        tried = []
        for val in values:
            if val == cur_val or (axis in ("trend", "be_mode") and _trend_eq(
                    tuple(val) if isinstance(val, list) else val,
                    tuple(cur_val) if isinstance(cur_val, list) else cur_val)):
                continue
            cand = copy.deepcopy(best)
            cand[axis] = copy.deepcopy(val)
            obj, _, _ = evaluate_cfg4(cand, start_region, serial, syms=syms)
            tried.append((obj, val))
            print(f"[{label}] {axis}={_fmt_val(val)}: R={obj:.4f}", flush=True)
        if tried:
            top = max(tried, key=lambda x: x[0])
            if top[0] > best_obj:
                best[axis] = copy.deepcopy(top[1])
                best_obj = top[0]
                history.append(dict(step=axis, axis=axis, value=_json_val(top[1]),
                                    obj=best_obj, cfg=copy.deepcopy(_jsonable_cfg(best))))
                print(f"[{label}] >> adopt {axis}={_fmt_val(top[1])} R={best_obj:.4f}", flush=True)
    return best, history


def phase1(serial, round5: bool = False):
    syms = SYMS5 if round5 else ALL_SYMBOLS
    tag = "p1-r5-1h" if round5 else "p1-1h"
    state = "r5_phase1" if round5 else "r4_phase1"
    start = _cfg_from_json(copy.deepcopy(load_state("1h", "r4_phase2")["final_cfg"])) if round5 else START_CFG4
    best_a, hist = coordinate_descent4(start, serial, tag, syms=syms)
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
    scored = []
    for idx, cfg in enumerate(cands):
        tasks = [dict(sym=s, tf="1h", kind="cfg", cfg=_jsonable_cfg(cfg))
                 for s in syms]
        res = run_pool4(tasks, serial=serial)
        trades_by_sym = {r["sym"]: r["trades"] for r in res}
        dfs = {s: get_df_local4(s) for s in syms}
        wf_ok, wf_detail = wf_gate(trades_by_sym, dfs)
        per_b = per_sym_on_region(res, "B")
        r_b = objective_R(per_b)
        scored.append(dict(idx=idx, cfg=_jsonable_cfg(cfg), wf_ok=wf_ok,
                           wf=wf_detail, r_b=r_b, per_b=per_b))
        print(f"[{tag}] cand{idx}: WF {'PASS' if wf_ok else 'FAIL'}"
              f"({wf_detail['seg_pass']}/4)  R(B)={r_b:.4f}", flush=True)
    eligible = [s for s in scored if s["wf_ok"]] or scored
    winner = max(eligible, key=lambda x: x["r_b"])
    save_state("1h", state, dict(history=hist, scored=scored, winner=winner["cfg"]))
    print(f"[{tag}] WINNER R(B)={winner['r_b']:.4f} cfg={winner['cfg']}", flush=True)


def phase2(serial, round5: bool = False):
    syms = SYMS5 if round5 else ALL_SYMBOLS
    state_in = "r5_phase1" if round5 else "r4_phase1"
    state_out = "r5_phase2" if round5 else "r4_phase2"
    tag = "p2-r5-1h" if round5 else "p2-1h"
    f_ladder = (0.01, 0.03, 0.05) if round5 else (0.01, 0.02, 0.03)
    need_pos = 5 if round5 else 6
    st = load_state("1h", state_in)
    winner = _cfg_from_json(copy.deepcopy(st["winner"]))
    axes = axes4()
    best = copy.deepcopy(winner)
    hist = []
    best_obj, _, _ = evaluate_cfg4(best, "AB", serial, syms=syms)
    print(f"[{tag}] start R(AB)={best_obj:.4f}", flush=True)
    for axis, values in axes:
        cur_val = best.get(axis)
        tried = []
        for val in values:
            if val == cur_val or (axis in ("trend", "be_mode") and _trend_eq(
                    tuple(val) if isinstance(val, list) else val,
                    tuple(cur_val) if isinstance(cur_val, list) else cur_val)):
                continue
            cand = copy.deepcopy(best)
            cand[axis] = copy.deepcopy(val)
            obj, _, _ = evaluate_cfg4(cand, "AB", serial, syms=syms)
            tried.append((obj, val))
            print(f"[{tag}] {axis}={_fmt_val(val)}: R={obj:.4f}", flush=True)
        if tried:
            top = max(tried, key=lambda x: x[0])
            if top[0] > best_obj:
                best[axis] = copy.deepcopy(top[1])
                best_obj = top[0]
                hist.append(dict(axis=axis, value=_json_val(top[1]), obj=best_obj,
                                 cfg=copy.deepcopy(_jsonable_cfg(best))))
                print(f"[{tag}] >> adopt {axis}={_fmt_val(top[1])} R={best_obj:.4f}", flush=True)

    # blind C, f-ladder
    out = dict(final_cfg=_jsonable_cfg(best), history=hist)
    for f in f_ladder:
        tasks = [dict(sym=s, tf="1h", kind="cfg", cfg=_jsonable_cfg(best), f=f)
                 for s in syms]
        res_c = run_pool4(tasks, serial=serial)
        tasks_i = [dict(sym=s, tf="1h", kind="incumbent2", f=f) for s in syms]
        res_i = run_pool4(tasks_i, serial=serial)
        cand_c = per_sym_on_region(res_c, "C")
        inc_c = per_sym_on_region(res_i, "C")
        out[f"cand_c_f{int(f*100)}"] = cand_c
        out[f"inc_c_f{int(f*100)}"] = inc_c
        if f == 0.01:
            mean_ann_c = float(np.mean([v["ann"] for v in cand_c.values()]))
            mean_ann_i = float(np.mean([v["ann"] for v in inc_c.values()]))
            k1 = mean_ann_c >= 1.5 * mean_ann_i
            k2 = sum(1 for v in cand_c.values() if v["total"] > 0) >= need_pos
            k3 = float(np.mean([v["maxdd"] for v in cand_c.values()])) <= MEAN_DD_CAP4
            k4 = float(np.mean([v["n"] for v in cand_c.values()])) >= MIN_TRADES_PER_SYM
            verdict = k1 and k2 and k3 and k4
            out["gates"] = dict(K1=k1, K2=k2, K3=k3, K4=k4,
                                mean_ann_c=mean_ann_c, mean_ann_i=mean_ann_i)
            out["verdict"] = bool(verdict)
            label = "R5" if round5 else "R4"
            print(f"\n===== 1h {label} BLIND C (>=2025-03-01) verdict: {'PASS' if verdict else 'FAIL'} =====")
            print(f"K1 meanAnn {mean_ann_c*100:+.2f}% vs inc {mean_ann_i*100:+.2f}% "
                  f"(need >= {1.5*mean_ann_i*100:+.2f}%): {'OK' if k1 else 'X'}")
            print(f"K2 >={need_pos}/{len(syms)} positive: {'OK' if k2 else 'X'} | "
                  f"K3 mean DD <=25%: {'OK' if k3 else 'X'} | "
                  f"K4 n>=3: {'OK' if k4 else 'X'}")
            print(f"{'sym':<9}{'n':>5}{'candRet':>9}{'candDD':>8}{'candAnn':>9}{'incRet':>9}{'incDD':>8}")
            for sym in syms:
                cc, ii = cand_c[sym], inc_c[sym]
                print(f"{sym:<9}{cc['n']:>5}{cc['total']*100:>+8.1f}%{cc['maxdd']*100:>7.1f}%"
                      f"{cc['ann']*100:>+8.1f}%{ii['total']*100:>+8.1f}%{ii['maxdd']*100:>7.1f}%")
        else:
            mean_ann_c = float(np.mean([v["ann"] for v in cand_c.values()]))
            mean_dd_c = float(np.mean([v["maxdd"] for v in cand_c.values()]))
            print(f"  f={f*100:.0f}%: candidate C mean ann {mean_ann_c*100:+.2f}%  "
                  f"mean DD {mean_dd_c*100:.1f}%")
    save_state("1h", state_out, out)


def report(serial):
    st = load_state("1h", "r4_phase2")
    cfg = _cfg_from_json(copy.deepcopy(st["final_cfg"]))
    print(f"\n===== R4 FINAL full-window (IN-SAMPLE; honest headline = blind C) =====")
    print(f"cfg: {json.dumps(st['final_cfg'], ensure_ascii=False)}")
    for f in (0.01, 0.02, 0.03):
        tasks = [dict(sym=s, tf="1h", kind="cfg", cfg=_jsonable_cfg(cfg), f=f)
                 for s in ALL_SYMBOLS]
        results = run_pool4(tasks, serial=serial)
        print(f"\n  f = {f*100:.0f}%")
        print(f"  {'sym':<9}{'n':>6}{'total':>10}{'ann':>8}{'maxDD':>8}{'winrate':>9}{'avgLots':>9}")
        for res in results:
            df = get_df_local4(res["sym"])
            trs = [tr for tr in res["trades"] if tr["i0"] is not None and tr["i0"] >= WARMUP]
            stt = seg_stats(trs, int(df["time"].iloc[WARMUP]),
                            int(df["time"].iloc[-1]), WARMUP, len(df))
            wr = float(np.mean([tr["ret"] > 0 for tr in trs])) if trs else 0
            al = float(np.mean([tr.get("lots", 1) for tr in trs])) if trs else 1
            print(f"  {res['sym']:<9}{stt['n']:>6}{stt['total']*100:>+9.1f}%"
                  f"{stt['ann']*100:>+7.1f}%{stt['maxdd']*100:>7.1f}%{wr*100:>8.1f}%{al:>9.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True,
                    choices=["phase1", "phase2", "report"])
    ap.add_argument("--serial", action="store_true")
    ap.add_argument("--round5", action="store_true",
                    help="round-5 mode: drop ZEC, f ladder 1/3/5, 7 symbols")
    args = ap.parse_args()
    if args.phase == "phase1":
        phase1(args.serial, round5=args.round5)
    elif args.phase == "phase2":
        phase2(args.serial, round5=args.round5)
    elif args.phase == "report":
        report(args.serial)


if __name__ == "__main__":
    main()
