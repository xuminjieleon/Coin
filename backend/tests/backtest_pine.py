"""Compare the user's TradingView Pine scripts vs CoinLens production (2026-08-26).

User request: backtest three Pine scripts (BTC/ETH/SOL 1h, authored on
TradingView; sources at C:\\Users\\Administrator\\Downloads\\pine\\*.txt),
run concurrently, and compare against the current production strategy.

Faithful Pine replication (details verified line-by-line against the scripts):
  - Signals evaluated on bar close; market entries fill at NEXT bar open
  - No stop on the entry bar (strategy.exit placed at entry-bar close becomes
    active the following bar)
  - Stop levels recomputed every bar from the CURRENT ATR — the initial stop
    drifts with ATR until breakeven triggers, exactly as the scripts do
  - finalStop: long = max(trail, be); short = min(trail, be); no extra ratchet
  - BTC/SOL: short entry commented out (alert only) => LONG-ONLY
  - ETH: long + short live; short KC mult = 0.0 => lower band = EMA(25)
  - SOL long ADX: ta.dmi(14, 14) (smoothing = DI length); all others (len, 10)
  - Long trend filter: BTC/ETH = slope of EMA(680) > 0; SOL = commented out
  - Cooldown counters advance on every signal regardless of position state
  - pyramiding=0: same-direction signal while in position ignored;
    opposite-direction signal reverses at next open (ETH only)
  - commission 0.04% per side; percent-of-equity sizing, compounding
    (BTC/SOL 90% of equity, ETH 1.2*80 = 96%)
  - Short half-close block commented out in all scripts -> ignored
Indicator seeds: EMA via pandas ewm(adjust=False) (same recursive seed as
Pine); RMA/RSI/ATR/ADX via ewm(alpha=1/len) — Pine seeds Wilder RMA with an
SMA; the difference decays to zero within a few hundred bars (warmup 3000).

CoinLens side: 5y 1h decision records cached by backtest_5y.py (engine hash
verified; NO recompute — cache read only), production geometry
(0.5, 2.0, 0.15, 0.5, 96, None), fill=24, native threshold 25,
capacity-constrained serial per symbol (same harness as BACKTEST.md).
Equity model for comparability: 1% of equity risked per trade (the
documented sizing in BACKTEST.md), same 0.04% per-side fee converted to R.
Gross R is also reported (ties back to BACKTEST.md). Both strategies run on
the identical df (newest 43800 1h bars); ~24 early decision records that
pre-date the df start are skipped.

Parallelism (AGENTS.md §7.8): one worker process per symbol; worker results
go to JSON in the kilo temp dir; the main process aggregates and prints.

Usage: .venv/Scripts/python.exe tests/backtest_pine.py [--serial] [--sym BTCUSDT]
"""
import argparse
import asyncio
import json
import multiprocessing as mp
import os
import pickle
import sys
import time
from datetime import datetime, timezone

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

import profit_sweep2 as ps
from profit2_r5 import with_loose_plans
from services import kline_cache

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
WINDOW_1H = 43800
WARMUP_PINE = 3000
FEE = 0.0004  # 0.04% per side, both strategies
INITIAL_EQUITY = 10000.0

# CoinLens production (round-13 geometry) per timeframe; native threshold 25
# for 1h, loosened to 10 for 4h (with_loose_plans) — same harness as backtest_5y
GEO_1H = (0.5, 2.0, 0.15, 0.5, 96, None)
FILL_1H = 24
GEO_4H = (0.75, 1.0, 0.75, None, 48, 0.35)
FILL_4H = 18  # 12 * fill_mult 1.5
WINDOW_4H = 10950

TEMP_DIR = os.environ.get("KILO_TEMP", r"C:\Users\Administrator\AppData\Local\Temp\kilo")

# Per-script Pine config, transcribed from the user's .txt files
PINE = {
    "BTCUSDT": dict(
        qty_pct=90.0,
        long=dict(ema=1000, kc=24, kc_mult=2.7, dmi=(8, 10), adx=(23.5, 59.0),
                  rsi_len=13, rsi_gt=68.5, atr=10, sl=6.95, be=1.18,
                  trail_start=2.03, trail=1.21, vol_ma=35, cooldown=0,
                  trend_ema=680),
        short=None,
    ),
    "ETHUSDT": dict(
        qty_pct=96.0,
        long=dict(ema=425, kc=27, kc_mult=2.7, dmi=(14, 10), adx=(23.5, 100.0),
                  rsi_len=14, rsi_gt=71.0, atr=10, sl=8.9, be=1.04,
                  trail_start=1.05, trail=1.22, vol_ma=38, cooldown=0,
                  trend_ema=680),
        short=dict(ema=680, kc=25, kc_mult=0.0, dmi=(14, 10), adx=(32.5, 59.0),
                   rsi_len=17, rsi_lt=27.0, atr=8, sl=3.14, be=1.0,
                   trail_start=2.3, trail=0.83, vol_ma=39, cooldown=1),
    ),
    "SOLUSDT": dict(
        qty_pct=90.0,
        long=dict(ema=290, kc=21, kc_mult=2.6, dmi=(14, 14), adx=(12.5, 100.0),
                  rsi_len=14, rsi_gt=0.0, atr=11, sl=9.2, be=1.15,
                  trail_start=1.3, trail=0.92, vol_ma=26, cooldown=0,
                  trend_ema=None),
        short=None,
    ),
}


# ---------------------------------------------------------------- indicators
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rma(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    h, l, pc = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.fillna(h - l)


def pine_rsi(close: pd.Series, n: int) -> pd.Series:
    ch = close.diff()
    gain = ch.clip(lower=0.0)
    loss = (-ch).clip(lower=0.0)
    ag = rma(gain, n)
    al = rma(loss, n)
    rs = ag / al.replace(0.0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def pine_adx(df: pd.DataFrame, di_len: int, adx_len: int) -> pd.Series:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = rma(true_range(df), di_len)
    pdi = 100.0 * rma(plus_dm, di_len) / tr
    mdi = 100.0 * rma(minus_dm, di_len) / tr
    dx = 100.0 * (pdi - mdi).abs() / (pdi + mdi).replace(0.0, np.nan)
    return rma(dx.fillna(0.0), adx_len)


def side_arrays(df: pd.DataFrame, side: dict) -> dict:
    close = df["close"]
    atr_s = rma(true_range(df), side["atr"])
    kc_mid = ema(close, side["kc"])
    kc_mid_prev = ema(close.shift(1), side["kc"])  # ta.ema(close[1], len)
    a = {
        "ema": ema(close, side["ema"]).to_numpy(),
        "kc_upper": (kc_mid + side["kc_mult"] * atr_s).to_numpy(),
        "kc_lower": (kc_mid - side["kc_mult"] * atr_s).to_numpy(),
        "kc_last_lower": (kc_mid_prev - side["kc_mult"] * atr_s).to_numpy(),
        "atr": atr_s.to_numpy(),
        "adx": pine_adx(df, *side["dmi"]).to_numpy(),
        "rsi": pine_rsi(close, side["rsi_len"]).to_numpy(),
        "vol_ma": df["volume"].rolling(side["vol_ma"]).mean().to_numpy(),
    }
    if side.get("trend_ema"):
        a["trend_slope"] = (ema(close, side["trend_ema"]).diff() > 0).to_numpy()
    return a


# ------------------------------------------------------------------ pine sim
def run_pine(df: pd.DataFrame, cfg: dict, warmup: int = WARMUP_PINE) -> dict:
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    v = df["volume"].to_numpy()
    t = df["time"].to_numpy()
    n = len(df)

    L = side_arrays(df, cfg["long"])
    S = side_arrays(df, cfg["short"]) if cfg["short"] else None
    lc, sc = cfg["long"], cfg["short"]
    pct, fee = cfg["qty_pct"] / 100.0, FEE

    pos = None            # dict(d=+1/-1, entry, qty, i0, fee_in)
    pending = None        # +1 / -1
    stop_next = None      # stop level active during bar i (set at i-1 close)
    first_stop = None     # first stop that ever applied (risk reference)
    last_long = -1 << 30
    last_short = -1 << 30
    both_signals = 0
    trades: list[dict] = []
    eq = INITIAL_EQUITY
    curve = [(int(t[warmup]), eq)]

    def close_trade(i: int, px: float, reason: str):
        nonlocal eq, pos, stop_next, first_stop
        fee_out = pos["qty"] * px * fee
        pnl = pos["qty"] * (px - pos["entry"]) * pos["d"] - pos["fee_in"] - fee_out
        eq += pnl
        risk_usd = pos["qty"] * abs(pos["entry"] - first_stop) if first_stop else 0.0
        stop_pct = abs(pos["entry"] - first_stop) / pos["entry"] if first_stop else 0.0
        trades.append(dict(t_in=int(t[pos["i0"]]), t_out=int(t[i]), d=pos["d"],
                           entry=float(pos["entry"]), exit=float(px),
                           pnl=float(pnl), eq=float(eq), reason=reason,
                           risk_usd=float(risk_usd), stop_pct=float(stop_pct)))
        curve.append((int(t[i]), eq))
        pos = None
        stop_next = None
        first_stop = None

    def open_pos(i: int, d: int):
        nonlocal pos, stop_next, first_stop
        px = float(o[i])
        qty = eq * pct / px
        pos = dict(d=d, entry=px, qty=qty, i0=i, fee_in=qty * px * fee)
        stop_next = None
        first_stop = None

    for i in range(warmup, n):
        # ---- 1. fills at open (entry / reversal) ----
        if pending is not None:
            d = pending
            pending = None
            if pos is None:
                open_pos(i, d)
            elif pos["d"] != d:
                close_trade(i, float(o[i]), "reversal")
                open_pos(i, d)
            # same direction while in position: ignored (pyramiding=0)

        # ---- 2. intrabar stop check (never on the entry bar) ----
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

        # ---- 3. signals at bar close ----
        sig_l = (
            (c[i] > L["kc_upper"][i] or c[i - 1] > L["kc_upper"][i])
            and (L.get("trend_slope") is None or L["trend_slope"][i])
            and c[i] > L["ema"][i]
            and lc["adx"][0] < L["adx"][i] < lc["adx"][1]
            and L["rsi"][i] > lc["rsi_gt"]
            and v[i] > L["vol_ma"][i]
            and (i - last_long) > lc["cooldown"]
        )
        sig_s = False
        if S is not None:
            sig_s = (
                (c[i] < S["kc_lower"][i] or c[i - 1] < S["kc_last_lower"][i])
                and c[i] < S["ema"][i]
                and sc["adx"][0] < S["adx"][i] < sc["adx"][1]
                and S["rsi"][i] < sc["rsi_lt"]
                and v[i] > S["vol_ma"][i]
                and (i - last_short) > sc["cooldown"]
            )
        if sig_l:
            last_long = i
        if sig_s:
            last_short = i
        if sig_l and sig_s:
            both_signals += 1
        d = None
        if sig_l:
            d = 1
        if sig_s:
            d = -1  # short block runs later in the scripts -> wins
        if d is not None and (pos is None or pos["d"] != d):
            pending = d

        # ---- 4. stop level for the next bar (computed at this close) ----
        if pos is not None:
            a = L if pos["d"] > 0 else S
            g = lc if pos["d"] > 0 else sc
            atr = a["atr"][i]
            entry = pos["entry"]
            if atr > 0:
                profit_atr = (c[i] - entry) / atr if pos["d"] > 0 else (entry - c[i]) / atr
                if pos["d"] > 0:
                    initial = entry - atr * g["sl"]
                    be_stop = entry if profit_atr >= g["be"] else initial
                    trail_stop = (c[i] - atr * g["trail"]) if profit_atr >= g["trail_start"] else be_stop
                    final = max(trail_stop, be_stop)
                else:
                    initial = entry + atr * g["sl"]
                    be_stop = entry if profit_atr >= g["be"] else initial
                    trail_stop = (c[i] + atr * g["trail"]) if profit_atr >= g["trail_start"] else be_stop
                    final = min(trail_stop, be_stop)
                stop_next = float(final)
                if first_stop is None:
                    first_stop = float(final)

    if pos is not None:  # position still open at data end: mark to last close
        close_trade(n - 1, float(c[n - 1]), "open@end")

    stop_pcts = [tr["stop_pct"] for tr in trades]
    return dict(
        trades=trades, curve=curve, both_signals=both_signals,
        avg_stop_pct=float(np.mean(stop_pcts)) if stop_pcts else 0.0,
        warmup_bar=warmup, bars=n,
    )


# -------------------------------------------------------------- coinlens sim
def sim_outcome_fast(highs, lows, closes, n, i, direction, entry, stop,
                     be_frac, tgt_r, texit, fill_bars, trail):
    """Identical logic to backtest_5y.sim_outcome_fast (copied, verified)."""
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


def run_coinlens(df: pd.DataFrame, records: list[dict], geo: tuple,
                 fill_bars: int) -> dict:
    depth, stopw, be_frac, tgt, texit, trail = geo
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    n = len(df)
    tidx = {int(t): i for i, t in enumerate(df["time"].to_numpy())}

    raw: list[tuple] = []
    busy = -1
    skipped = 0
    for r in records:
        if r.get("plan") is None:
            continue
        i = tidx.get(int(r["time"]))
        if i is None:
            skipped += 1
            continue
        if i <= busy:
            continue
        built = ps.build_plan(r, depth, stopw)
        if built is None:
            continue
        direction, entry, stop = built
        out = sim_outcome_fast(highs, lows, closes, n, i, direction, entry,
                               stop, be_frac, tgt, texit, fill_bars, trail)
        if out is None:
            continue
        rr, fill, exit_bar = out
        busy = exit_bar
        raw.append((int(r["time"]), float(rr), float(entry), float(stop)))

    # equity: 1% risk per trade, fee converted to R (0.08% round trip)
    eq = INITIAL_EQUITY
    curve = [(raw[0][0] if raw else int(df["time"].iloc[0]), eq)]
    rr_nets: list[float] = []
    for tm, rr, entry, stop in raw:
        risk = abs(entry - stop)
        fee_r = 2 * FEE * entry / risk if risk > 0 else 0.0
        rr_nets.append(rr - fee_r)
        eq *= 1.0 + 0.01 * (rr - fee_r)
        curve.append((tm, eq))

    rrs = np.array([x[1] for x in raw], dtype=float) if raw else np.array([])
    rr_nets = np.array(rr_nets, dtype=float)
    return dict(raw=raw, curve=curve, skipped_records=skipped, rrs=rrs,
                rr_nets=rr_nets)


# -------------------------------------------------------------------- stats
def year_of(ms: int) -> int:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year


def curve_stats(curve: list[tuple[int, float]], trades: list[dict],
                pnl_key: str = "pnl") -> dict:
    eqs = np.array([e for _, e in curve], dtype=float)
    ts = [int(x) for x, _ in curve]
    peak = np.maximum.accumulate(eqs)
    maxdd = float(np.max(1.0 - eqs / peak)) if len(eqs) else 0.0
    total_ret = float(eqs[-1] / eqs[0] - 1.0) if len(eqs) else 0.0
    years = (ts[-1] - ts[0]) / (365.25 * 86400_000) if len(ts) > 1 else 0.0
    cagr = float((eqs[-1] / eqs[0]) ** (1.0 / years) - 1.0) if years > 0 and eqs[-1] > 0 else 0.0
    mar = cagr / maxdd if maxdd > 1e-9 else 0.0
    pnls = np.array([tr[pnl_key] for tr in trades], dtype=float) if trades else np.array([])
    wins = float(pnls[pnls > 0].sum())
    losses = float(-pnls[pnls < 0].sum())
    pf = wins / losses if losses > 1e-9 else float("inf")
    by_year: dict[int, float] = {}
    for tt, e in curve:
        by_year[year_of(tt)] = float(e)
    years_sorted = sorted(by_year)
    per_year = {}
    prev = eqs[0]
    for y in years_sorted:
        per_year[y] = by_year[y] / prev - 1.0 if prev > 0 else 0.0
        prev = by_year[y]
    return dict(n_trades=len(trades), total_ret=total_ret, maxdd=maxdd,
                years=years, cagr=cagr, mar=mar, pf=pf,
                winrate=float(np.mean(pnls > 0)) if len(pnls) else 0.0,
                avg_pnl_pct=float(np.mean(pnls)) if len(pnls) else 0.0,
                per_year={str(k): v for k, v in per_year.items()})


def jsonable(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = jsonable(v)
        elif isinstance(v, (np.floating, np.integer)):
            out[k] = float(v)
        elif isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
            out[k] = None
        elif isinstance(v, list):
            out[k] = [jsonable(x) if isinstance(x, dict) else x for x in v]
        else:
            out[k] = v
    return out


# ------------------------------------------------------------------- worker
def load_records_5y(sym: str, tf: str, window: int) -> list[dict]:
    cache_file = os.path.join(ps.CACHE_DIR, f"_5y_cache_{sym}_{tf}.pkl")
    key = {"ver": 1, "tf": tf, "symbol": sym, "window": window, "src": ps.source_hash()}
    if not os.path.exists(cache_file):
        raise SystemExit(f"missing 5y record cache: {cache_file}")
    with open(cache_file, "rb") as f:
        entry = pickle.load(f)
    if entry.get("key") != key:
        raise SystemExit(f"stale 5y record cache for {sym} {tf} (engine hash changed); "
                         f"re-run backtest_5y.py --tf {tf} first")
    return entry["records"]


def fetch_df(sym: str, tf: str, window: int) -> pd.DataFrame:
    rows = None
    for attempt in range(3):
        try:
            rows = asyncio.run(kline_cache.get_klines(sym, tf, window))
            break
        except Exception as exc:
            print(f"[warn] {sym} {tf} fetch failed ({exc}); retry in {20*(attempt+1)}s",
                  file=sys.stderr, flush=True)
            time.sleep(20 * (attempt + 1))
    if rows:
        return kline_cache.rows_to_df(rows)
    # fallback: newest bars straight from the local cache (offline)
    print(f"[warn] {sym} {tf}: network unavailable, falling back to local cache only",
          file=sys.stderr, flush=True)
    rows = kline_cache._read_rows(sym, tf, 1 << 62, window)
    if len(rows) < window * 0.95:
        raise SystemExit(f"{sym}: insufficient cached {tf} data ({len(rows)})")
    return kline_cache.rows_to_df(rows)


def coinlens_block(df: pd.DataFrame, records: list[dict], geo: tuple,
                   fill_bars: int, loose_th: int | None) -> dict:
    recs = records if loose_th is None else with_loose_plans(records, loose_th)
    coin = run_coinlens(df, recs, geo, fill_bars)
    coin_rrs = coin["rrs"]
    return dict(
        **curve_stats(coin["curve"],
                      [dict(pnl=float(rr), t_out=tm) for tm, rr, _, _ in coin["raw"]]),
        ev_r=float(coin["rr_nets"].mean()) if len(coin["rr_nets"]) else 0.0,
        total_r=float(coin["rr_nets"].sum()) if len(coin["rr_nets"]) else 0.0,
        ev_r_gross=float(coin_rrs.mean()) if len(coin_rrs) else 0.0,
        total_r_gross=float(coin_rrs.sum()) if len(coin_rrs) else 0.0,
        winrate_gross=float(np.mean(coin_rrs > 0)) if len(coin_rrs) else 0.0,
        nonloss_gross=float(np.mean(coin_rrs >= -1e-9)) if len(coin_rrs) else 0.0,
        maxdd_r_gross=float(np.max(np.maximum.accumulate(np.concatenate(([0.0], np.cumsum(coin_rrs))))[1:] - np.cumsum(coin_rrs))) if len(coin_rrs) else 0.0,
        skipped_records=coin["skipped_records"],
        first_trade=ps.fmt_ts(coin["raw"][0][0]) if coin["raw"] else None,
    )


def worker(sym: str) -> None:
    df = fetch_df(sym, "1h", WINDOW_1H)
    records_1h = load_records_5y(sym, "1h", WINDOW_1H)
    df4 = fetch_df(sym, "4h", WINDOW_4H)
    records_4h = load_records_5y(sym, "4h", WINDOW_4H)

    pine = run_pine(df, PINE[sym])
    coin_1h = coinlens_block(df, records_1h, GEO_1H, FILL_1H, None)
    coin_4h = coinlens_block(df4, records_4h, GEO_4H, FILL_4H, 10)

    # buy & hold over the full df window
    c = df["close"].to_numpy()
    t = df["time"].to_numpy()
    bh_ret = float(c[-1] / c[0] - 1.0)
    # buy & hold drawdown
    peak = np.maximum.accumulate(c)
    bh_dd = float(np.max(1.0 - c / peak))

    pine_tr = pine["trades"]
    pine_r = np.array([tr["pnl"] / tr["risk_usd"] for tr in pine_tr
                       if tr["risk_usd"] > 0], dtype=float)

    res = {
        "symbol": sym,
        "bars": len(df),
        "span": [ps.fmt_ts(int(t[0])), ps.fmt_ts(int(t[-1]))],
        "pine": dict(
            **curve_stats(pine["curve"], pine_tr),
            ev_r=float(pine_r.mean()) if len(pine_r) else 0.0,
            total_r=float(pine_r.sum()) if len(pine_r) else 0.0,
            avg_stop_pct=pine["avg_stop_pct"],
            both_signals=pine["both_signals"],
            first_trade=ps.fmt_ts(pine_tr[0]["t_in"]) if pine_tr else None,
            reasons={},
        ),
        "coinlens": coin_1h,
        "coinlens4h": coin_4h,
        "buyhold": dict(ret=bh_ret, maxdd=bh_dd),
    }
    for tr in pine_tr:
        res["pine"]["reasons"][tr["reason"]] = res["pine"]["reasons"].get(tr["reason"], 0) + 1

    out_file = os.path.join(TEMP_DIR, f"pine_vs_coinlens_{sym}.json")
    os.makedirs(TEMP_DIR, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(jsonable(res), f, ensure_ascii=False)
    print(f"[done] {sym}: pine trades={res['pine']['n_trades']} "
          f"coinlens 1h={res['coinlens']['n_trades']} 4h={res['coinlens4h']['n_trades']}",
          flush=True)


# --------------------------------------------------------------------- main
def fmt_pct(x: float) -> str:
    return f"{x*100:+.1f}%" if x == x else "-"


def print_report(results: list[dict]) -> None:
    for r in sorted(results, key=lambda x: SYMBOLS.index(x["symbol"])):
        sym = r["symbol"]
        p, cl, c4, bh = r["pine"], r["coinlens"], r["coinlens4h"], r["buyhold"]
        print(f"\n{'='*84}")
        print(f"===== {sym} · 1h {r['bars']} 根 / 4h 5 年（{r['span'][0]} .. {r['span'][1]}）"
              f" · 手续费 0.04%/边（双方一致）=====")
        print(f"{'':<15}{'笔数':>6}{'胜率':>8}{'总收益':>10}{'最大回撤':>10}{'MAR':>7}"
              f"{'CAGR':>8}{'PF':>7}{'EV(R)':>8}{'总R':>9}")

        def row(label: str, d: dict) -> None:
            if not d["n_trades"]:
                print(f"{label:<15} 无成交")
                return
            print(f"{label:<15}{d['n_trades']:>6}{d['winrate_gross']*100:>7.1f}%"
                  f"{d['total_ret']*100:>9.1f}%-{d['maxdd']*100:>9.1f}%{d['mar']:>7.2f}"
                  f"{d['cagr']*100:>7.1f}%{d['pf']:>7.2f}{d['ev_r']:>+8.3f}"
                  f"{d['total_r']:>+8.1f}")

        if p["n_trades"]:
            print(f"{'Pine 1h(原始)':<14}{p['n_trades']:>6}{p['winrate']*100:>7.1f}%"
                  f"{p['total_ret']*100:>9.1f}%-{p['maxdd']*100:>9.1f}%{p['mar']:>7.2f}"
                  f"{p['cagr']*100:>7.1f}%{p['pf']:>7.2f}{p['ev_r']:>+8.3f}"
                  f"{p['total_r']:>+8.1f}")
        else:
            print(f"{'Pine 1h(原始)':<14} 无成交")
        row("CoinLens 1h", cl)
        row("CoinLens 4h", c4)
        print(f"{'Buy&Hold':<15}{'':>6}{'':>8}{bh['ret']*100:>9.1f}%-{bh['maxdd']*100:>9.1f}%")
        print(f"  Pine: 名义仓位 {PINE[sym]['qty_pct']:.0f}% 权益/笔，平均止损距离 "
              f"{p['avg_stop_pct']*100:.1f}%，出场 {p['reasons']}，"
              f"首笔 {p['first_trade']}")
        for label, d in (("1h", cl), ("4h", c4)):
            print(f"  CoinLens {label}: 1% 权益风险/笔，EV/总R 扣费净口径（毛 EV "
                  f"{d['ev_r_gross']:+.3f}R / 毛总 {d['total_r_gross']:+.1f}R），"
                  f"非亏损率 {d['nonloss_gross']*100:.1f}%，毛回撤 {d['maxdd_r_gross']:.1f}R，"
                  f"首笔 {d['first_trade']}")
        py = p["per_year"]
        cy = cl["per_year"]
        y4 = c4["per_year"]
        yrs = sorted(set(py) | set(cy) | set(y4))
        line = "  分年:  " + "  ".join(
            f"{y}: {fmt_pct(py.get(y, float('nan'))):>8} / {fmt_pct(cy.get(y, float('nan'))):>8}"
            f" / {fmt_pct(y4.get(y, float('nan'))):>8}" for y in yrs)
        print(line + "   (Pine1h / CL-1h / CL-4h)")

    # aggregate
    print(f"\n{'='*84}\n===== 三币汇总（等权平均）=====")
    rows = [("Pine 1h(原始)", "pine"), ("CoinLens 1h", "coinlens"),
            ("CoinLens 4h", "coinlens4h")]
    for name, key in rows:
        rets = [r[key]["total_ret"] for r in results if r[key]["n_trades"]]
        dds = [r[key]["maxdd"] for r in results if r[key]["n_trades"]]
        trs = [r[key]["n_trades"] for r in results]
        evs = [r[key]["ev_r"] for r in results]
        if rets:
            print(f"  {name:<15} 平均总收益 {np.mean(rets)*100:+.1f}%  "
                  f"平均最大回撤 {np.mean(dds)*100:.1f}%  总笔数 {sum(trs)}  "
                  f"平均EV {np.mean(evs):+.3f}R")
    bhs = [r["buyhold"]["ret"] for r in results]
    print(f"  {'Buy&Hold':<15} 平均总收益 {np.mean(bhs)*100:+.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", action="store_true")
    ap.add_argument("--sym", default=None)
    args = ap.parse_args()
    syms = [args.sym] if args.sym else SYMBOLS

    for s in syms:
        f = os.path.join(TEMP_DIR, f"pine_vs_coinlens_{s}.json")
        if os.path.exists(f):
            os.remove(f)

    if args.serial:
        for s in syms:
            worker(s)
    else:
        ctx = mp.get_context("spawn")
        procs = [ctx.Process(target=worker, args=(s,)) for s in syms]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=600)
        failed = [p.name for p in procs if p.exitcode not in (0, None)]
        if failed:
            print(f"[error] workers failed: {failed}", file=sys.stderr)

    results = []
    for s in syms:
        f = os.path.join(TEMP_DIR, f"pine_vs_coinlens_{s}.json")
        if os.path.exists(f):
            with open(f, encoding="utf-8") as fh:
                results.append(json.load(fh))
        else:
            print(f"[error] missing result for {s}", file=sys.stderr)
    if results:
        print_report(results)


if __name__ == "__main__":
    main()
