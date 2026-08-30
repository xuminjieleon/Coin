"""Trend-strategy (user Pine) optimization round — 8 symbols x 1h/4h (2026-08-30).

User request: optimize the TradingView KC-breakout trend strategy (originally
BTC/ETH/SOL 1h, round-14 comparison in backtest_pine.py) to maximize returns;
base logic may be changed, factors added/removed. Goal: good backtest numbers
before considering productization (event pushes). User-chosen scope:
  - universe: the 8 notify symbols (BTC/ETH/SOL/BNB/XRP/ZEC/DOGE/SUI)
  - objective: MAR maximization with a drawdown ceiling
  - timeframes: 1h AND 4h

This is a NEW strategy line — the production-engine freeze (AGENTS §7.3) does
not apply (that freeze covers services/analysis/*). This round still follows
the project's pre-registration discipline (rounds 13/20/33):

PRE-REGISTERED PROTOCOL (written before any optimization run; do not edit
after seeing fold-C results):
  Folds: per symbol-timeframe, time-ordered bar index on [WARMUP, n):
         A = [0,40%) of region, B = [40%,70%), C = [70%,100%].
         Trades attributed by ENTRY bar. Fold stats: equity chain rebuilt from
         trade returns (compounding, INITIAL_EQUITY start, fold duration as
         CAGR basis). Trade-close-event equity convention (same as round 14;
         intratrade drawdown not marked to market — stated limitation).
  Sizing: fixed fractional risk f=1% of equity per trade
         (qty = f*eq / (sl * ATR_signal)); notional cap 2x equity. The
         authored percent-of-equity sizing (90/96%) is reported only in the
         baseline reproduction block, never in gates.
  Fee: 0.04% per side on notional (as authored).
  Candidate params: POOLED — one param set per timeframe across all 8 symbols
         (per-symbol tuning on 8 symbols = overfit; rejected by design).
         Short side, when enabled, = mirror of the long side (same numbers).
  Phase 1: single-pass coordinate descent on fold A over the predefined axes
         below (order fixed). Objective = mean per-symbol fold MAR (symbol
         with <5 trades in fold counts MAR=0). Then the final config and the
         top-2 distinct improving configs seen during the descent are
         evaluated on fold B; best B MAR becomes the Phase-1 winner.
  Phase 2: single-pass coordinate descent restarted from the Phase-1 winner
         on fold A+B (same axes/values). Result evaluated ONCE on fold C.
  Gates on fold C (candidate vs incumbent, both at f=1%):
         K1 mean MAR_C(candidate) >= 1.10 x mean MAR_C(incumbent)
         K2 every symbol C net return (candidate) > 0
         K3 worst-symbol C total return (candidate) >= worst-symbol C total
            return (incumbent)
         K4 candidate per-symbol C maxDD <= 15% and mean maxDD <= 8%
  Incumbent: original per-symbol Pine configs (BTC/ETH/SOL own; the 5 new
         symbols inherit BTC's config), evaluated through the identical sim
         in risk-sizing mode for gate comparability.
  If gates fail: incumbent (or the better gated variant) stays; NO second
  look at C without a new pre-registration.

Axes (order fixed; values predefined):
  1 direction     ['long_only', 'long_short']
  2 trend         [None, ('slope',680), ('slope',200), ('above',680), ('above',200)]
  3 ema_gate      [None, 200, 425, 680, 1000]          (long: close>EMA(n))
  4 kc_len        [21, 24, 35, 50]
  5 kc_mult       [1.5, 2.0, 2.7, 3.5]
  6 adx_min       [0.0, 15.0, 23.5, 30.0]              (dmi fixed (14,10))
  7 adx_max       [59.0, 100.0]
  8 rsi_gt        [None, 55.0, 60.0, 68.5]             (rsi len fixed 14; short: 100-x)
  9 vol_mult      [None, 1.0, 1.5]                     (x SMA(35) volume)
 10 sl            [2.5, 4.0, 6.95, 9.0]                (initial stop, ATR mult)
 11 be            [None, 0.8, 1.2, 1.8]                (breakeven trigger, ATR)
 12 trail_start   [1.0, 1.5, 2.03, 3.0]                (ATR)
 13 trail         [0.8, 1.21, 2.0, 3.0]                (ATR)
 14 texit         1h [None, 200, 500] / 4h [None, 50, 125]  (bars in trade)
 15 cooldown      [0, 5, 12]

Pine-faithful execution semantics (identical for incumbent and candidate):
signal at bar close, market entry at next bar open; no stop on entry bar;
stop levels recomputed EVERY bar from CURRENT ATR (stateless, can loosen —
as the scripts do); final stop = max(trail, breakeven) long / min(...) short;
pyramiding=0; opposite signal reverses at next open; both-signals tie -> short
wins (as authored). New vs round-14 harness: optional time exit at bar close.

Data: local kline_cache only (offline, deterministic); 7 symbols 5y since
2021-08-30, SUI since 2023-05-03 (folds scale to available range).

Parallelism (AGENTS §7.8): mp.Pool workers; worker = one (symbol, tf, config)
eval; dfs cached per worker process. Phases persisted to JSON in TEMP_DIR so
each phase is a separate <15min shell call (background-kill protection).

Usage: .venv/Scripts/python.exe tests/trend_opt.py --phase baseline|phase1|phase2|report [--tf 1h|4h|both] [--serial]
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

from backtest_pine import (FEE, INITIAL_EQUITY, PINE, SYMBOLS,  # noqa: E402
                           ema, pine_adx, pine_rsi, rma, true_range, year_of)
from services import kline_cache  # noqa: E402

# NOTE: backtest_pine.PINE is the authoritative incumbent transcription
# (round-14 numbers were produced with it). btc1h.txt says atrLenLong=9 while
# the transcription uses 10 — immaterial (RMA 9 vs 10); recorded in DEVLOG.

ALL_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
               "XRPUSDT", "ZECUSDT", "DOGEUSDT", "SUIUSDT"]
WINDOWS = {"1h": 43800, "4h": 10950}
WARMUP = 3000
F_RISK = 0.01          # selection/gate risk fraction
NOTIONAL_CAP = 2.0     # x equity
TEXIT_AXIS = {"1h": [None, 200, 500], "4h": [None, 50, 125]}
TEMP_DIR = os.environ.get("KILO_TEMP", r"C:\Users\Administrator\AppData\Local\Temp\kilo")

# Round-14 reference values (BTC/ETH/SOL 1h native sizing, window ended
# 2026-08-26) for baseline cross-check. Data now ends 2026-08-29 — small
# deltas expected, same ballpark required.
R14_REF = {
    "BTCUSDT": dict(trades=150, ret=1.096, dd=0.207),
    "ETHUSDT": dict(trades=265, ret=6.036, dd=0.381),
    "SOLUSDT": dict(trades=223, ret=8.587, dd=0.244),
}

# ------------------------------------------------------- candidate template
START_CFG = dict(  # pooled start point = BTC long config
    direction="long_only",
    trend=("slope", 680), ema_gate=1000,
    kc_len=24, kc_mult=2.7, atr_len=10,
    adx_min=23.5, adx_max=59.0,
    rsi_gt=68.5, vol_mult=1.0,
    sl=6.95, be=1.18, trail_start=2.03, trail=1.21,
    texit=None, cooldown=0,
)
FIXED = dict(dmi=(14, 10), rsi_len=14, vol_len=35)


def axes_for(tf: str) -> list[tuple[str, list]]:
    return [
        ("direction", ["long_only", "long_short"]),
        ("trend", [None, ("slope", 680), ("slope", 200), ("above", 680), ("above", 200)]),
        ("ema_gate", [None, 200, 425, 680, 1000]),
        ("kc_len", [21, 24, 35, 50]),
        ("kc_mult", [1.5, 2.0, 2.7, 3.5]),
        ("adx_min", [0.0, 15.0, 23.5, 30.0]),
        ("adx_max", [59.0, 100.0]),
        ("rsi_gt", [None, 55.0, 60.0, 68.5]),
        ("vol_mult", [None, 1.0, 1.5]),
        ("sl", [2.5, 4.0, 6.95, 9.0]),
        ("be", [None, 0.8, 1.2, 1.8]),
        ("trail_start", [1.0, 1.5, 2.03, 3.0]),
        ("trail", [0.8, 1.21, 2.0, 3.0]),
        ("texit", TEXIT_AXIS[tf]),
        ("cooldown", [0, 5, 12]),
    ]


# ------------------------------------------------------------ side building
def side_params_from_long(lc: dict, direction: int) -> dict:
    """Long side as-is; short side = mirror (direction=-1)."""
    p = dict(lc)
    if direction < 0:
        p["rsi_lt"] = (100.0 - lc["rsi_gt"]) if lc.get("rsi_gt") is not None else None
        p["rsi_gt"] = None
        tr = lc.get("trend")
        if tr and tr[0] == "slope":
            p["trend"] = ("slope_dn", tr[1])
        elif tr and tr[0] == "above":
            p["trend"] = ("below", tr[1])
    return p


def build_side(df: pd.DataFrame, p: dict, direction: int) -> dict:
    close = df["close"]
    atr_s = rma(true_range(df), p["atr_len"])
    kc_mid = ema(close, p["kc_len"])
    kc_mid_prev = ema(close.shift(1), p["kc_len"])
    a = {
        "atr": atr_s.to_numpy(),
        "kc_up": (kc_mid + p["kc_mult"] * atr_s).to_numpy(),
        "kc_lo": (kc_mid - p["kc_mult"] * atr_s).to_numpy(),
        "kc_lo_prev": (kc_mid_prev - p["kc_mult"] * atr_s).to_numpy(),
        "adx": pine_adx(df, *FIXED["dmi"]).to_numpy(),
        "rsi": pine_rsi(close, FIXED["rsi_len"]).to_numpy(),
        "vol_ma": df["volume"].rolling(FIXED["vol_len"]).mean().to_numpy(),
        "ema_gate": ema(close, p["ema_gate"]).to_numpy() if p.get("ema_gate") else None,
        "trend": None,
    }
    tr = p.get("trend")
    if tr:
        kind, n = tr
        e = ema(close, n)
        if kind == "slope":
            a["trend"] = (e.diff() > 0).to_numpy()
        elif kind == "slope_dn":
            a["trend"] = (e.diff() < 0).to_numpy()
        elif kind == "above":
            a["trend"] = (close > e).to_numpy()
        elif kind == "below":
            a["trend"] = (close < e).to_numpy()
    return a


# ---------------------------------------------------------------------- sim
def run_trend(df: pd.DataFrame, long_cfg: dict, sizing: str,
              qty_pct: float = 90.0, f: float = F_RISK) -> list[dict]:
    """Pine-faithful sim. sizing: 'risk' (qty=f*eq/(sl*ATR_sig), cap 2x notional)
    or 'pct' (qty=qty_pct% of equity notional, as authored).
    Returns trades: i0, t_in, t_out, d, entry, exit, reason, ret (pnl/eq_at_entry),
    r (pnl/risk_usd at entry)."""
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    v = df["volume"].to_numpy()
    t = df["time"].to_numpy()
    n = len(df)

    lp = side_params_from_long(long_cfg, +1)
    L = build_side(df, lp, +1)
    sp = side_params_from_long(long_cfg, -1) if long_cfg["direction"] == "long_short" else None
    S = build_side(df, sp, -1) if sp else None

    pos = None
    pending = None
    stop_next = None
    first_stop = None
    last_sig = {1: -(1 << 30), -1: -(1 << 30)}
    trades: list[dict] = []
    eq = INITIAL_EQUITY
    i0_bar = None

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
        pos = None
        stop_next = None
        first_stop = None

    def open_pos(i, d, sig_atr):
        nonlocal eq, pos
        px = float(o[i])
        if sizing == "risk":
            dist = long_cfg["sl"] * sig_atr
            qty = f * eq / dist if dist > 0 else 0.0
            cap = NOTIONAL_CAP * eq / px
            qty = min(qty, cap)
        else:
            qty = qty_pct / 100.0 * eq / px
        if qty <= 0:
            return
        pos = dict(d=d, entry=px, qty=qty, i0=i, fee_in=qty * px * FEE)

    sig_atr = None
    for i in range(WARMUP, n):
        if pending is not None:
            d = pending
            pending = None
            if pos is None:
                open_pos(i, d, sig_atr)
                i0_bar = i
            elif pos["d"] != d:
                close_trade(i, float(o[i]), "reversal")
                open_pos(i, d, sig_atr)

        if pos is not None and stop_next is not None:
            s = stop_next
            px = None
            if pos["d"] > 0:
                if o[i] <= s:
                    px = o[i]
                elif l[i] <= s:
                    px = s
            else:
                if o[i] >= s:
                    px = o[i]
                elif h[i] >= s:
                    px = s
            if px is not None:
                close_trade(i, float(px), "stop")

        # time exit at bar close
        if pos is not None and long_cfg.get("texit") and (i - pos["i0"]) >= long_cfg["texit"]:
            close_trade(i, float(c[i]), "texit")

        # signals at bar close (cooldown counters advance on every signal,
        # regardless of position state — as authored)
        cd = long_cfg["cooldown"]
        sig_l = (
            (c[i] > L["kc_up"][i] or c[i - 1] > L["kc_up"][i])
            and (L["trend"] is None or L["trend"][i])
            and (L["ema_gate"] is None or c[i] > L["ema_gate"][i])
            and long_cfg["adx_min"] < L["adx"][i] < long_cfg["adx_max"]
            and (lp.get("rsi_gt") is None or L["rsi"][i] > lp["rsi_gt"])
            and (lp.get("vol_mult") is None or v[i] > lp["vol_mult"] * L["vol_ma"][i])
            and (i - last_sig[1]) > cd
        )
        sig_s = False
        if S is not None:
            sig_s = (
                (c[i] < S["kc_lo"][i] or c[i - 1] < S["kc_lo_prev"][i])
                and (S["trend"] is None or S["trend"][i])
                and (S["ema_gate"] is None or c[i] < S["ema_gate"][i])
                and long_cfg["adx_min"] < S["adx"][i] < long_cfg["adx_max"]
                and (sp.get("rsi_lt") is None or S["rsi"][i] < sp["rsi_lt"])
                and (sp.get("vol_mult") is None or v[i] > sp["vol_mult"] * S["vol_ma"][i])
                and (i - last_sig[-1]) > cd
            )
        if sig_l:
            last_sig[1] = i
        if sig_s:
            last_sig[-1] = i
        d = None
        if sig_l:
            d = 1
        if sig_s:
            d = -1
        if d is not None and (pos is None or pos["d"] != d):
            pending = d
            sig_atr = float(L["atr"][i]) if d > 0 else float(S["atr"][i])

        # stop level for next bar (recomputed from CURRENT atr, stateless — Pine)
        if pos is not None:
            a = L if pos["d"] > 0 else S
            atr = a["atr"][i]
            entry = pos["entry"]
            g = long_cfg
            if atr > 0:
                profit = (c[i] - entry) / atr if pos["d"] > 0 else (entry - c[i]) / atr
                be_trig = g["be"] if g["be"] is not None else float("inf")
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


# ------------------------------------------------------ incumbent translation
def incumbent_long_cfg(sym: str) -> dict:
    """Original per-symbol Pine (round-14 transcription) -> candidate schema.
    New symbols inherit BTC. ETH's authored short block is kept as-is
    (asymmetric) by monkey-patching direction + short override below."""
    src = PINE.get(sym, PINE["BTCUSDT"])
    lg = src["long"]
    cfg = dict(
        direction="long_short" if src.get("short") else "long_only",
        trend=("slope", lg["trend_ema"]) if lg.get("trend_ema") else None,
        ema_gate=lg["ema"],
        kc_len=lg["kc"], kc_mult=lg["kc_mult"], atr_len=lg["atr"],
        adx_min=lg["adx"][0], adx_max=lg["adx"][1],
        rsi_gt=lg["rsi_gt"] if lg["rsi_gt"] > 0 else None,
        vol_mult=1.0,
        sl=lg["sl"], be=lg["be"], trail_start=lg["trail_start"], trail=lg["trail"],
        texit=None, cooldown=lg["cooldown"],
    )
    return cfg


def run_incumbent(df: pd.DataFrame, sym: str, sizing: str) -> list[dict]:
    """Incumbent via the faithful round-14 sim for native sizing, or via
    run_trend (schema translation) for risk sizing. ETH's asymmetric short is
    preserved in both paths."""
    if sizing == "pct":
        from backtest_pine import run_pine
        src = PINE.get(sym, PINE["BTCUSDT"])
        res = run_pine(df, src)
        out = []
        for tr in res["trades"]:
            out.append(dict(i0=None, t_in=tr["t_in"], t_out=tr["t_out"], d=tr["d"],
                            entry=tr["entry"], exit=tr["exit"], reason=tr["reason"],
                            ret=None, r=None,
                            pnl=tr["pnl"], risk_usd=tr["risk_usd"]))
        return out
    # risk sizing: use schema translation; ETH asymmetric short approximated by
    # its authored short params via a custom direction flag
    cfg = incumbent_long_cfg(sym)
    if sym == "ETHUSDT":
        return run_trend_eth_incumbent(df, cfg)
    return run_trend(df, cfg, sizing="risk")


def run_trend_eth_incumbent(df: pd.DataFrame, long_cfg: dict) -> list[dict]:
    """ETH incumbent with its authored asymmetric short (kc_mult=0 etc.).
    Implemented by running run_trend with a patched mirror via PINE dict."""
    src = PINE["ETHUSDT"]
    sh = src["short"]
    short_cfg = dict(
        direction="long_short",
        trend=None, ema_gate=sh["ema"],
        kc_len=sh["kc"], kc_mult=sh["kc_mult"], atr_len=sh["atr"],
        adx_min=long_cfg["adx_min"], adx_max=long_cfg["adx_max"],
        rsi_gt=None, sl=sh["sl"], be=sh["be"], trail_start=sh["trail_start"],
        trail=sh["trail"], texit=None, cooldown=sh["cooldown"],
    )
    # long pass and short pass share one position stream in the scripts;
    # approximate by running the combined sim with asymmetric sides:
    return _run_trend_asym(df, long_cfg, short_cfg)


def _run_trend_asym(df, long_cfg, short_cfg):
    """Minimal asymmetric two-sided variant: long side from long_cfg, short
    side params from short_cfg (ADX window shared, RSI short = authored 27)."""
    o = df["open"].to_numpy(); h = df["high"].to_numpy()
    l = df["low"].to_numpy(); c = df["close"].to_numpy()
    v = df["volume"].to_numpy(); t = df["time"].to_numpy()
    n = len(df)
    lp = side_params_from_long(long_cfg, +1)
    L = build_side(df, lp, +1)
    sp = dict(short_cfg); sp["rsi_lt"] = 27.0
    S = build_side(df, sp, -1)
    S["_rsi_lt"] = 27.0
    pos = None; pending = None; stop_next = None; first_stop = None
    last_sig = {1: -(1 << 30), -1: -(1 << 30)}
    trades = []; eq = INITIAL_EQUITY; sig_atr = None

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

    def open_pos(i, d, sa, sl_mult):
        nonlocal pos
        px = float(o[i])
        dist = sl_mult * sa
        qty = F_RISK * eq / dist if dist > 0 else 0.0
        qty = min(qty, NOTIONAL_CAP * eq / px)
        if qty <= 0:
            return
        pos = dict(d=d, entry=px, qty=qty, i0=i, fee_in=qty * px * FEE)

    for i in range(WARMUP, n):
        if pending is not None:
            d, sa, slm = pending
            pending = None
            if pos is None:
                open_pos(i, d, sa, slm)
            elif pos["d"] != d:
                close_trade(i, float(o[i]), "reversal")
                open_pos(i, d, sa, slm)
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
        cd_l = long_cfg["cooldown"]; cd_s = short_cfg["cooldown"]
        sig_l = ((c[i] > L["kc_up"][i] or c[i-1] > L["kc_up"][i])
                 and (L["trend"] is None or L["trend"][i])
                 and (L["ema_gate"] is None or c[i] > L["ema_gate"][i])
                 and long_cfg["adx_min"] < L["adx"][i] < long_cfg["adx_max"]
                 and (lp.get("rsi_gt") is None or L["rsi"][i] > lp["rsi_gt"])
                 and v[i] > L["vol_ma"][i] and (i - last_sig[1]) > cd_l)
        sig_s = ((c[i] < S["kc_lo"][i] or c[i-1] < S["kc_lo_prev"][i])
                 and (S["ema_gate"] is None or c[i] < S["ema_gate"][i])
                 and short_cfg["adx_min"] < S["adx"][i] < short_cfg["adx_max"]
                 and S["rsi"][i] < S["_rsi_lt"]
                 and v[i] > S["vol_ma"][i] and (i - last_sig[-1]) > cd_s)
        if sig_l: last_sig[1] = i
        if sig_s: last_sig[-1] = i
        d = None
        if sig_l: d = 1
        if sig_s: d = -1
        if d is not None and (pos is None or pos["d"] != d):
            g = long_cfg if d > 0 else short_cfg
            pending = (d, float((L if d > 0 else S)["atr"][i]), g["sl"])
        if pos is not None:
            a = L if pos["d"] > 0 else S
            g = long_cfg if pos["d"] > 0 else short_cfg
            atr = a["atr"][i]; entry = pos["entry"]
            if atr > 0:
                profit = (c[i]-entry)/atr if pos["d"] > 0 else (entry-c[i])/atr
                if pos["d"] > 0:
                    initial = entry - atr*g["sl"]
                    be_stop = entry if profit >= g["be"] else initial
                    trail_stop = (c[i]-atr*g["trail"]) if profit >= g["trail_start"] else be_stop
                    final = max(trail_stop, be_stop)
                else:
                    initial = entry + atr*g["sl"]
                    be_stop = entry if profit >= g["be"] else initial
                    trail_stop = (c[i]+atr*g["trail"]) if profit >= g["trail_start"] else be_stop
                    final = min(trail_stop, be_stop)
                stop_next = float(final)
                if first_stop is None: first_stop = float(final)
    if pos is not None:
        close_trade(n-1, float(c[n-1]), "open@end")
    return trades


# ------------------------------------------------------------ fold machinery
def fold_bounds(n: int) -> dict:
    region = n - WARMUP
    a1 = WARMUP + int(0.40 * region)
    b1 = WARMUP + int(0.70 * region)
    return {"A": (WARMUP, a1), "B": (a1, b1), "C": (b1, n)}


def fold_stats(trades: list[dict], lo: int, hi: int, t_lo: int, t_hi: int) -> dict:
    trs = [tr for tr in trades if tr["i0"] is not None and lo <= tr["i0"] < hi]
    trs.sort(key=lambda tr: (tr["t_out"], tr["t_in"]))
    eq = INITIAL_EQUITY
    peak = eq
    maxdd = 0.0
    for tr in trs:
        eq *= 1.0 + tr["ret"]
        peak = max(peak, eq)
        maxdd = max(maxdd, 1.0 - eq / peak)
    years = (t_hi - t_lo) / (365.25 * 86400_000)
    total = eq / INITIAL_EQUITY - 1.0
    cagr = (eq / INITIAL_EQUITY) ** (1.0 / years) - 1.0 if years > 0 and eq > 0 else 0.0
    mar = cagr / maxdd if maxdd > 1e-9 else 0.0
    if len(trs) < 5:
        mar = 0.0
    return dict(n=len(trs), total=total, maxdd=maxdd, cagr=cagr, mar=mar,
                winrate=float(np.mean([tr["ret"] > 0 for tr in trs])) if trs else 0.0)


def objective(per_sym_mar: dict) -> float:
    # Winsorize per-symbol fold MAR at 10.0: tiny-sample folds with near-zero
    # DD otherwise produce MAR in the hundreds and hijack the mean (observed:
    # SUI 4h fold A MAR=1048 in the baseline run, before any optimization).
    return float(np.mean([min(m, 10.0) for m in per_sym_mar.values()])) if per_sym_mar else 0.0


# ------------------------------------------------------------------ workers
_DF_CACHE: dict = {}


def get_df(sym: str, tf: str) -> pd.DataFrame:
    key = (sym, tf)
    if key not in _DF_CACHE:
        rows = kline_cache._read_rows(sym, tf, 1 << 62, WINDOWS[tf])
        if not rows or len(rows) < WINDOWS[tf] * 0.6:
            raise SystemExit(f"{sym} {tf}: insufficient cached data ({len(rows) if rows else 0})")
        _DF_CACHE[key] = kline_cache.rows_to_df(rows)
    return _DF_CACHE[key]


def eval_task(task: dict) -> dict:
    sym, tf = task["sym"], task["tf"]
    df = get_df(sym, tf)
    kind = task["kind"]
    if kind == "cfg":
        trades = run_trend(df, task["cfg"], sizing="risk")
    elif kind == "incumbent_risk":
        trades = run_incumbent(df, sym, sizing="risk")
    elif kind == "incumbent_pct":
        trades = run_incumbent(df, sym, sizing="pct")
    else:
        raise ValueError(kind)
    return dict(sym=sym, tf=tf, tag=task.get("tag"), trades=trades)


def run_pool(tasks: list[dict], workers: int = 8, serial: bool = False) -> list[dict]:
    if serial:
        return [eval_task(t) for t in tasks]
    with mp.get_context("spawn").Pool(processes=workers) as pool:
        return pool.map(eval_task, tasks)


# ------------------------------------------------------------------- phases
def state_file(tf: str, phase: str) -> str:
    return os.path.join(TEMP_DIR, f"trend_opt_{tf}_{phase}.json")


def save_state(tf: str, phase: str, data: dict) -> None:
    os.makedirs(TEMP_DIR, exist_ok=True)
    with open(state_file(tf, phase), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)


def load_state(tf: str, phase: str) -> dict:
    with open(state_file(tf, phase), encoding="utf-8") as fh:
        return json.load(fh)


def fold_of_trade(tr: dict, bounds: dict) -> str | None:
    for name, (lo, hi) in bounds.items():
        if tr["i0"] is not None and lo <= tr["i0"] < hi:
            return name
    return None


def phase_baseline(tfs: list[str], serial: bool) -> None:
    print("\n===== BASELINE: incumbent reproduction =====")
    for tf in tfs:
        tasks = []
        for sym in ALL_SYMBOLS:
            tasks.append(dict(sym=sym, tf=tf, kind="incumbent_pct", tag="pct"))
            tasks.append(dict(sym=sym, tf=tf, kind="incumbent_risk", tag="risk"))
        results = run_pool(tasks, serial=serial)
        out = {}
        for res in results:
            sym, tag = res["sym"], res["tag"]
            df = get_df_local(sym, tf)
            n = len(df)
            bounds = fold_bounds(n)
            t0, t1 = int(df["time"].iloc[0]), int(df["time"].iloc[-1])
            trades = res["trades"]
            if tag == "pct":
                eq = INITIAL_EQUITY
                peak = eq; maxdd = 0.0
                for tr in trades:  # pct mode carries absolute pnl
                    eq += tr["pnl"]
                    peak = max(peak, eq)
                    maxdd = max(maxdd, 1 - eq / peak)
                years = (t1 - t0) / (365.25 * 86400_000)
                total = eq / INITIAL_EQUITY - 1
                cagr = (eq / INITIAL_EQUITY) ** (1 / years) - 1 if eq > 0 else 0
                out.setdefault(sym, {})["pct"] = dict(
                    n=len(trades), total=total, maxdd=maxdd,
                    mar=cagr / maxdd if maxdd > 1e-9 else 0)
            else:
                full = fold_stats_risk(trades, 0, n, t0, t1)
                per_fold = {}
                for fname, (lo, hi) in bounds.items():
                    per_fold[fname] = fold_stats(
                        trades, lo, hi,
                        int(df["time"].iloc[lo]), int(df["time"].iloc[min(hi, n) - 1]))
                out.setdefault(sym, {})["risk"] = dict(full=full, folds=per_fold)
        save_state(tf, "baseline", out)
        # print
        print(f"\n--- {tf} incumbent (native pct sizing / f=1% risk sizing) ---")
        print(f"{'sym':<9}{'笔数':>6}{'总收益(原生)':>12}{'DD':>7}{'MAR':>6}"
              f"{'f=1%收益':>10}{'DD':>7}{'MAR':>6}  folds A/B/C MAR")
        for sym in ALL_SYMBOLS:
            r = out[sym]
            p, k = r["pct"], r["risk"]
            fl = k["folds"]
            print(f"{sym:<9}{p['n']:>6}{p['total']*100:>+11.1f}%{p['maxdd']*100:>6.1f}%"
                  f"{p['mar']:>6.2f}{k['full']['total']*100:>+9.1f}%{k['full']['maxdd']*100:>6.1f}%"
                  f"{k['full']['mar']:>6.2f}  {fl['A']['mar']:.2f}/{fl['B']['mar']:.2f}/{fl['C']['mar']:.2f}")
        if tf == "1h":
            print("\n  round-14 cross-check (BTC/ETH/SOL 1h native, ref window -3d):")
            for sym, ref in R14_REF.items():
                got = out[sym]["pct"]
                ok = (abs(got["n"] - ref["trades"]) <= max(5, ref["trades"] * 0.05)
                      and abs(got["total"] - ref["ret"]) < max(0.3, abs(ref["ret"]) * 0.15))
                print(f"    {sym}: trades {got['n']} vs {ref['trades']}  "
                      f"ret {got['total']*100:+.1f}% vs {ref['ret']*100:+.1f}%  "
                      f"dd {got['maxdd']*100:.1f}% vs {ref['dd']*100:.1f}%  -> {'OK' if ok else 'MISMATCH'}")


def fold_stats_risk(trades, lo, hi, t_lo, t_hi):
    trs = [tr for tr in trades]
    eq = INITIAL_EQUITY; peak = eq; maxdd = 0.0
    for tr in trs:
        eq *= 1.0 + tr["ret"]
        peak = max(peak, eq); maxdd = max(maxdd, 1 - eq / peak)
    years = (t_hi - t_lo) / (365.25 * 86400_000)
    total = eq / INITIAL_EQUITY - 1
    cagr = (eq / INITIAL_EQUITY) ** (1 / years) - 1 if years > 0 and eq > 0 else 0
    mar = cagr / maxdd if maxdd > 1e-9 else 0
    return dict(n=len(trs), total=total, maxdd=maxdd, cagr=cagr, mar=mar)


_MAIN_DFS: dict = {}


def get_df_local(sym, tf):
    key = (sym, tf)
    if key not in _MAIN_DFS:
        rows = kline_cache._read_rows(sym, tf, 1 << 62, WINDOWS[tf])
        _MAIN_DFS[key] = kline_cache.rows_to_df(rows)
    return _MAIN_DFS[key]


def fold_objective(results: list[dict], fold: str) -> tuple[float, dict]:
    """results: [{sym, tf, trades}] for ONE config. Returns (mean MAR, per-sym)."""
    per = {}
    for res in results:
        df = get_df_local(res["sym"], res["tf"])
        n = len(df)
        bounds = fold_bounds(n)
        if fold == "AB":
            lo, hi = bounds["A"][0], bounds["B"][1]
            t_lo = int(df["time"].iloc[lo]); t_hi = int(df["time"].iloc[hi - 1])
            st = fold_stats(res["trades"], lo, hi, t_lo, t_hi)
        else:
            lo, hi = bounds[fold]
            t_lo = int(df["time"].iloc[lo]); t_hi = int(df["time"].iloc[min(hi, n) - 1])
            st = fold_stats(res["trades"], lo, hi, t_lo, t_hi)
        per[res["sym"]] = st
    return objective({s: st["mar"] for s, st in per.items()}), per


def coordinate_descent(tf: str, start_cfg: dict, fold: str,
                       serial: bool, label: str) -> tuple[dict, list]:
    """Single pass over predefined axes. Returns (best_cfg, history)."""
    axes = axes_for(tf)
    best = copy.deepcopy(start_cfg)
    history = []

    def cfg_obj(cfg):
        tasks = [dict(sym=s, tf=tf, kind="cfg", cfg=cfg) for s in ALL_SYMBOLS]
        res = run_pool(tasks, serial=serial)
        obj, per = fold_objective(res, fold)
        return obj, per, res

    best_obj, best_per, _ = cfg_obj(best)
    history.append(dict(step="start", axis=None, value=None, obj=best_obj, cfg=copy.deepcopy(best)))
    print(f"[{label}] start obj({fold})={best_obj:.3f}", flush=True)

    for axis, values in axes:
        cur_val = best.get(axis)
        tried = []
        for val in values:
            if val == cur_val or (axis == "trend" and _trend_eq(val, cur_val)):
                continue
            cand = copy.deepcopy(best)
            cand[axis] = copy.deepcopy(val)
            obj, per, _ = cfg_obj(cand)
            tried.append((obj, val, per))
            print(f"[{label}] {axis}={_fmt_val(val)}: obj={obj:.3f}", flush=True)
        if tried:
            top = max(tried, key=lambda x: x[0])
            if top[0] > best_obj:
                best[axis] = copy.deepcopy(top[1])
                best_obj = top[0]
                history.append(dict(step=axis, axis=axis, value=_json_val(top[1]),
                                    obj=best_obj, cfg=copy.deepcopy(best)))
                print(f"[{label}] >> adopt {axis}={_fmt_val(top[1])} obj={best_obj:.3f}", flush=True)
            else:
                history.append(dict(step=axis, axis=axis, value=_json_val(cur_val),
                                    obj=best_obj, cfg=copy.deepcopy(best)))
    return best, history


def _trend_eq(a, b):
    if a is None or b is None:
        return a is None and b is None
    return tuple(a) == tuple(b)


def _fmt_val(v):
    if isinstance(v, tuple):
        return f"{v[0]}{v[1]}"
    return str(v)


def _json_val(v):
    if isinstance(v, tuple):
        return list(v)
    return v


def _cfg_from_json(cfg: dict) -> dict:
    if isinstance(cfg.get("trend"), list):
        cfg["trend"] = tuple(cfg["trend"])
    return cfg


def phase1(tf: str, serial: bool) -> None:
    best_a, hist = coordinate_descent(tf, START_CFG, "A", serial, f"p1-{tf}")
    # candidates: final + top-2 distinct improving configs
    cands = [best_a]
    seen = {json.dumps(_jsonable_cfg(best_a), sort_keys=True)}
    improving = [h for h in hist if h["axis"] is not None]
    for h in sorted(improving, key=lambda x: -x["obj"]):
        key = json.dumps(_jsonable_cfg(h["cfg"]), sort_keys=True)
        if key not in seen:
            cands.append(_cfg_from_json(copy.deepcopy(h["cfg"])))
            seen.add(key)
        if len(cands) >= 3:
            break
    scored = []
    for idx, cfg in enumerate(cands):
        tasks = [dict(sym=s, tf=tf, kind="cfg", cfg=cfg) for s in ALL_SYMBOLS]
        res = run_pool(tasks, serial=serial)
        obj_b, per_b = fold_objective(res, "B")
        scored.append(dict(idx=idx, cfg=_jsonable_cfg(cfg), obj_b=obj_b, per_b=per_b))
        print(f"[p1-{tf}] candidate {idx}: obj(B)={obj_b:.3f}", flush=True)
    winner = max(scored, key=lambda x: x["obj_b"])
    save_state(tf, "phase1", dict(history=[_hist_json(h) for h in hist],
                                  scored=scored, winner=winner["cfg"]))
    print(f"[p1-{tf}] WINNER cfg={winner['cfg']} obj(B)={winner['obj_b']:.3f}", flush=True)


def _jsonable_cfg(cfg):
    out = {}
    for k, v in cfg.items():
        out[k] = list(v) if isinstance(v, tuple) else v
    return out


def _hist_json(h):
    return dict(step=h["step"], axis=h["axis"], value=_json_val(h.get("value")),
                obj=h["obj"], cfg=_jsonable_cfg(h["cfg"]))


def phase2(tf: str, serial: bool) -> None:
    st = load_state(tf, "phase1")
    winner = _cfg_from_json(copy.deepcopy(st["winner"]))
    best_ab, hist = coordinate_descent(tf, winner, "AB", serial, f"p2-{tf}")

    # ---- single blind evaluation on C ----
    tasks = [dict(sym=s, tf=tf, kind="cfg", cfg=best_ab) for s in ALL_SYMBOLS]
    res_cand = run_pool(tasks, serial=serial)
    tasks_inc = [dict(sym=s, tf=tf, kind="incumbent_risk") for s in ALL_SYMBOLS]
    res_inc = run_pool(tasks_inc, serial=serial)

    _, cand_c = fold_objective(res_cand, "C")
    _, inc_c = fold_objective(res_inc, "C")
    _, cand_ab = fold_objective(res_cand, "AB")
    _, inc_ab = fold_objective(res_inc, "AB")

    mean_mar_c = objective({s: x["mar"] for s, x in cand_c.items()})
    mean_mar_i = objective({s: x["mar"] for s, x in inc_c.items()})
    worst_c = min(x["total"] for x in cand_c.values())
    worst_i = min(x["total"] for x in inc_c.values())
    k1 = mean_mar_c >= 1.10 * mean_mar_i
    k2 = all(x["total"] > 0 for x in cand_c.values())
    k3 = worst_c >= worst_i
    k4 = (max(x["maxdd"] for x in cand_c.values()) <= 0.15
          and float(np.mean([x["maxdd"] for x in cand_c.values()])) <= 0.08)
    verdict = k1 and k2 and k3 and k4

    out = dict(
        final_cfg=_jsonable_cfg(best_ab),
        history=[_hist_json(h) for h in hist],
        cand_c=cand_c, inc_c=inc_c, cand_ab=cand_ab, inc_ab=inc_ab,
        gates=dict(K1=k1, K2=k2, K3=k3, K4=k4,
                   mean_mar_c=mean_mar_c, mean_mar_i=mean_mar_i,
                   worst_c=worst_c, worst_i=worst_i),
        verdict=bool(verdict),
    )
    save_state(tf, "phase2", out)

    print(f"\n===== {tf} BLIND C verdict: {'PASS' if verdict else 'FAIL'} =====")
    print(f"K1 meanMAR_C {mean_mar_c:.3f} vs inc {mean_mar_i:.3f} (need >= {1.1*mean_mar_i:.3f}): {'OK' if k1 else 'X'}")
    print(f"K2 all symbols C positive: {'OK' if k2 else 'X'}")
    print(f"K3 worst C total {worst_c*100:+.1f}% vs inc {worst_i*100:+.1f}%: {'OK' if k3 else 'X'}")
    print(f"K4 max DD <=15% & mean <=8%: {'OK' if k4 else 'X'}")
    print(f"{'sym':<9}{'n':>5}{'candRet':>9}{'candDD':>8}{'candMAR':>8}{'incRet':>9}{'incDD':>8}{'incMAR':>8}")
    for sym in ALL_SYMBOLS:
        cc, ii = cand_c[sym], inc_c[sym]
        print(f"{sym:<9}{cc['n']:>5}{cc['total']*100:>+8.1f}%{cc['maxdd']*100:>7.1f}%"
              f"{cc['mar']:>8.2f}{ii['total']*100:>+8.1f}%{ii['maxdd']*100:>7.1f}%{ii['mar']:>8.2f}")


def report(tfs: list[str], serial: bool) -> None:
    """Final machine-checked report: full-window candidate at several f,
    per-year breakdown, all numbers straight from sims."""
    for tf in tfs:
        st = load_state(tf, "phase2")
        cfg = _cfg_from_json(copy.deepcopy(st["final_cfg"]))
        tasks = [dict(sym=s, tf=tf, kind="cfg", cfg=cfg) for s in ALL_SYMBOLS]
        results = run_pool(tasks, serial=serial)
        print(f"\n{'='*88}\n===== FINAL {tf} candidate full-window (IN-SAMPLE A+B included; "
              f"honest headline = fold C above) =====")
        print(f"cfg: {json.dumps(st['final_cfg'], ensure_ascii=False)}")
        for flev in (0.005, 0.01, 0.02, 0.03):
            print(f"\n  f = {flev*100:.1f}% risk/trade")
            print(f"  {'sym':<9}{'n':>6}{'total':>10}{'CAGR':>8}{'maxDD':>8}{'MAR':>7}{'winrate':>9}")
            mets = []
            for res in results:
                df = get_df_local(res["sym"], tf)
                n = len(df)
                t0, t1 = int(df["time"].iloc[WARMUP]), int(df["time"].iloc[-1])
                trs = [tr for tr in res["trades"] if tr["i0"] is not None and tr["i0"] >= WARMUP]
                eq = INITIAL_EQUITY; peak = eq; maxdd = 0.0
                per_year: dict = {}
                prev = INITIAL_EQUITY
                yearly_last: dict = {}
                for tr in trs:
                    eq *= 1.0 + (flev / F_RISK) * tr["ret"]
                    peak = max(peak, eq); maxdd = max(maxdd, 1 - eq / peak)
                    yearly_last[year_of(tr["t_out"])] = eq
                years = (t1 - t0) / (365.25 * 86400_000)
                total = eq / INITIAL_EQUITY - 1
                cagr = (eq / INITIAL_EQUITY) ** (1 / years) - 1 if eq > 0 and years > 0 else 0
                mar = cagr / maxdd if maxdd > 1e-9 else 0
                wr = float(np.mean([tr["ret"] > 0 for tr in trs])) if trs else 0
                mets.append(dict(sym=res["sym"], n=len(trs), total=total, cagr=cagr,
                                 maxdd=maxdd, mar=mar, wr=wr, yearly=yearly_last))
                print(f"  {res['sym']:<9}{len(trs):>6}{total*100:>+9.1f}%{cagr*100:>+7.1f}%"
                      f"{maxdd*100:>7.1f}%{mar:>7.2f}{wr*100:>8.1f}%")
            if mets:
                print(f"  {'MEAN':<9}{sum(m['n'] for m in mets):>6}"
                      f"{np.mean([m['total'] for m in mets])*100:>+9.1f}%"
                      f"{np.mean([m['cagr'] for m in mets])*100:>+7.1f}%"
                      f"{np.mean([m['maxdd'] for m in mets])*100:>7.1f}%"
                      f"{np.mean([m['mar'] for m in mets]):>7.2f}")
        # per-year at f=1%
        print(f"\n  per-year total return by symbol (f=1%):")
        ys = list(range(2021, 2027))
        header = "  " + f"{'sym':<9}" + "".join(f"{y:>9}" for y in ys)
        print(header)
        for res in results:
            trs = [tr for tr in res["trades"] if tr["i0"] is not None and tr["i0"] >= WARMUP]
            yearly_eq: dict = {}
            eq = INITIAL_EQUITY
            for tr in trs:
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
