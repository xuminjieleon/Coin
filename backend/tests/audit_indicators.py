"""Audit script (research only, modifies nothing): numerical verification of
services/analysis/indicators.py against pure-Python reference implementations,
plus determinism / boundary / decision-logic property checks.

Reference conventions:
  (A) "ewm-equivalent": alpha = 1/period (Wilder) or 2/(span+1) (EMA),
      recursion seeded with the first valid sample, first output once
      min_periods valid samples seen — this is exactly what pandas
      .ewm(alpha=..., adjust=False, min_periods=...) computes.
      Expectation: agreement with production to ~1e-12.
  (B) "textbook Wilder": recursion seeded with the SMA of the first `period`
      valid samples (TradingView ta.rma / TA-Lib convention).
      Expectation: initialization difference that decays as (1-alpha)^k —
      reported, not asserted.

Run: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/audit_indicators.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from services.analysis import decision, engine, indicators

SEED = 42
N = 600


# ---------------------------------------------------------------- data gen
def make_df(n: int = N, seed: int = SEED) -> pd.DataFrame:
    rng = np.random.RandomState(seed)
    steps = rng.normal(0.0, 0.012, n)
    close = 100.0 * np.exp(np.cumsum(steps))
    open_ = np.empty(n)
    open_[0] = close[0] * (1 + rng.normal(0, 0.002))
    open_[1:] = close[:-1] * (1 + rng.normal(0, 0.002, n - 1))
    hi_extra = np.abs(rng.normal(0, 0.004, n))
    lo_extra = np.abs(rng.normal(0, 0.004, n))
    high = np.maximum(open_, close) * (1 + hi_extra)
    low = np.minimum(open_, close) * (1 - lo_extra)
    volume = np.exp(rng.normal(7.0, 0.6, n))
    taker = volume * rng.uniform(0.2, 0.8, n)
    return pd.DataFrame({
        "time": (np.arange(n) * 3_600_000).astype(np.int64),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "takerBuy": taker,
    })


# ------------------------------------------------------- reference impls
def ewm_ref(xs, alpha, min_periods):
    """Pure-python equivalent of pandas ewm(alpha, adjust=False, min_periods)."""
    out = [None] * len(xs)
    state = None
    seen = 0
    for i, x in enumerate(xs):
        if x is None or (isinstance(x, float) and math.isnan(x)):
            continue
        seen += 1
        state = float(x) if state is None else (1.0 - alpha) * state + alpha * float(x)
        if seen >= min_periods:
            out[i] = state
    return out


def wilder_ref(xs, period):
    """Textbook Wilder: SMA seed of first `period` valid samples, then recurse."""
    vals = [(i, float(x)) for i, x in enumerate(xs)
            if x is not None and not (isinstance(x, float) and math.isnan(x))]
    out = [None] * len(xs)
    if len(vals) < period:
        return out
    seed = sum(v for _, v in vals[:period]) / period
    out[vals[period - 1][0]] = seed
    state = seed
    for i, v in vals[period:]:
        state = (state * (period - 1) + v) / period
        out[i] = state
    return out


def ref_ema(closes, period):
    return ewm_ref(list(closes), 2.0 / (period + 1), period)


def ref_tr(highs, lows, closes):
    tr = []
    for i in range(len(closes)):
        if i == 0:
            tr.append(highs[i] - lows[i])  # pandas concat.max skipna semantics
        else:
            tr.append(max(highs[i] - lows[i],
                          abs(highs[i] - closes[i - 1]),
                          abs(lows[i] - closes[i - 1])))
    return tr


def ref_rsi(closes, period=14):
    gains, losses = [math.nan], [math.nan]
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = ewm_ref(gains, 1.0 / period, period)
    al = ewm_ref(losses, 1.0 / period, period)
    out = []
    for g, l in zip(ag, al):
        if g is None:
            out.append(None)
        elif l == 0:
            out.append(100.0)  # mirrors production .where(avg_loss != 0, 100)
        else:
            rs = g / l
            out.append(100.0 - 100.0 / (1.0 + rs))
    return out


def ref_rsi_textbook(closes, period=14):
    gains, losses = [math.nan], [math.nan]
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = wilder_ref(gains, period)
    al = wilder_ref(losses, period)
    out = []
    for g, l in zip(ag, al):
        if g is None:
            out.append(None)
        elif l == 0:
            out.append(100.0)
        else:
            rs = g / l
            out.append(100.0 - 100.0 / (1.0 + rs))
    return out


def ref_atr(highs, lows, closes, period=14):
    return ewm_ref(ref_tr(highs, lows, closes), 1.0 / period, period)


def ref_atr_textbook(highs, lows, closes, period=14):
    return wilder_ref(ref_tr(highs, lows, closes), period)


def _dm(highs, lows):
    plus, minus = [0.0], [0.0]
    for i in range(1, len(highs)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus.append(up if (up > down and up > 0) else 0.0)
        minus.append(down if (down > up and down > 0) else 0.0)
    return plus, minus


def ref_adx(highs, lows, closes, period=14, smooth=ewm_ref):
    tr = ref_tr(highs, lows, closes)
    plus, minus = _dm(highs, lows)
    tr_s = smooth(tr, 1.0 / period, period) if smooth is ewm_ref else smooth(tr, period)
    p_s = smooth(plus, 1.0 / period, period) if smooth is ewm_ref else smooth(plus, period)
    m_s = smooth(minus, 1.0 / period, period) if smooth is ewm_ref else smooth(minus, period)
    dx = []
    for t, p, m in zip(tr_s, p_s, m_s):
        if t is None or t == 0:
            dx.append(math.nan if t is None else 0.0)
            continue
        pdi = 100.0 * p / t
        mdi = 100.0 * m / t
        dx.append(100.0 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) != 0 else 0.0)
    if smooth is ewm_ref:
        return ewm_ref(dx, 1.0 / period, period)
    return wilder_ref(dx, period)


def ref_cvd(taker, vol):
    out, acc = [], 0.0
    for t, v in zip(taker, vol):
        acc += 2.0 * t - v
        out.append(acc)
    return out


def ref_boll_bw(closes, period=20, mult=2.0):
    out = []
    for i in range(len(closes)):
        if i < period - 1:
            out.append(None)
            continue
        w = closes[i - period + 1:i + 1]
        mid = sum(w) / period
        sd = math.sqrt(sum((x - mid) ** 2 for x in w) / (period - 1))  # ddof=1
        out.append(2 * mult * sd / mid)
    return out


# ------------------------------------------------------------- comparison
def cmp(name, prod, ref, tol=1e-8):
    assert len(prod) == len(ref), f"{name}: length mismatch {len(prod)} vs {len(ref)}"
    null_mismatch = [(i, prod[i], ref[i]) for i in range(len(prod))
                     if (prod[i] is None) != (ref[i] is None)]
    diffs = [abs(p - r) for p, r in zip(prod, ref) if p is not None and r is not None]
    maxd = max(diffs) if diffs else 0.0
    status = "PASS" if not null_mismatch and maxd <= tol else "FAIL"
    print(f"[{status}] {name}: n={len(prod)} null-mismatch={len(null_mismatch)} "
          f"max|diff|={maxd:.3e} (tol {tol:g})")
    if null_mismatch[:3]:
        print(f"       first null mismatches: {null_mismatch[:3]}")
    return status == "PASS", maxd


def decay_report(name, prod, ref):
    """Diff vs textbook convention at checkpoints after first valid index."""
    first = next((i for i, v in enumerate(ref) if v is not None), None)
    if first is None:
        print(f"[----] {name}: textbook ref all-None")
        return
    pts = [first, first + 50, first + 100, first + 200, len(prod) - 1]
    msg = []
    for i in pts:
        if i < len(prod) and prod[i] is not None and ref[i] is not None:
            msg.append(f"idx{i}:{abs(prod[i] - ref[i]):.2e}")
    print(f"[INFO] {name} vs textbook-Wilder init diff: " + "  ".join(msg))


def section(title):
    print(f"\n{'=' * 72}\n== {title}\n{'=' * 72}")


def main():
    ok = True
    df = make_df()
    closes = list(df["close"])
    highs = list(df["high"])
    lows = list(df["low"])

    section("1. EMA / RSI / ATR / ADX vs ewm-equivalent reference (tol 1e-8)")
    for p in (20, 50, 200):
        p_ok, _ = cmp(f"ema{p}", indicators.ema(df, p), ref_ema(closes, p))
        ok &= p_ok
    p_ok, _ = cmp("rsi14", indicators.rsi(df, 14), ref_rsi(closes, 14))
    ok &= p_ok
    p_ok, _ = cmp("atr14", indicators.atr(df, 14), ref_atr(highs, lows, closes, 14))
    ok &= p_ok
    p_ok, _ = cmp("adx14", indicators.adx(df, 14), ref_adx(highs, lows, closes, 14))
    ok &= p_ok

    section("1b. Initialization convention vs textbook Wilder (SMA-seeded)")
    decay_report("rsi14", indicators.rsi(df, 14), ref_rsi_textbook(closes, 14))
    decay_report("atr14", indicators.atr(df, 14), ref_atr_textbook(highs, lows, closes, 14))
    decay_report("adx14", indicators.adx(df, 14),
                 ref_adx(highs, lows, closes, 14, smooth=wilder_ref))

    section("1c. Bollinger bandwidth / CVD definition")
    p_ok, _ = cmp("bollinger_bandwidth", indicators.bollinger_bandwidth(df),
                  ref_boll_bw(closes, 20, 2.0))
    ok &= p_ok
    cvd_prod = indicators.cvd(df)
    cvd_a = ref_cvd(list(df["takerBuy"]), list(df["volume"]))
    # algebraic identity: 2*taker - vol == taker - (vol - taker)
    acc, cvd_b = 0.0, []
    for t, v in zip(df["takerBuy"], df["volume"]):
        acc += t - (v - t)
        cvd_b.append(acc)
    p_ok, _ = cmp("cvd (2*taker-vol)", cvd_prod, cvd_a)
    ok &= p_ok
    p_ok, _ = cmp("cvd (taker-(vol-taker))", cvd_prod, cvd_b)
    ok &= p_ok
    # None-hole behaviour: one missing takerBuy -> None at that bar, sum continues
    df2 = df.head(30).copy()
    df2.loc[df2.index[5], "takerBuy"] = None
    cvd2 = indicators.cvd(df2)
    hole = cvd2[5] is None and cvd2[4] is not None and cvd2[6] is not None
    cont = abs(cvd2[6] - (cvd_a[6] - (2 * float(df['takerBuy'][5]) - float(df['volume'][5])))) < 1e-6 \
        if cvd2[6] is not None else False
    print(f"[{'PASS' if hole and cont else 'INFO'}] cvd None-hole: bar5 None -> "
          f"{cvd2[5]!r}, bar6 continues (skipna cumsum, missing delta silently dropped): {cont}")

    section("5. Determinism: full_analysis x2 deep-equal")
    import copy

    def deep_eq(a, b, path=""):
        if isinstance(a, dict):
            if set(a) != set(b):
                return f"{path}: key mismatch"
            for k in a:
                r = deep_eq(a[k], b[k], f"{path}.{k}")
                if r:
                    return r
            return None
        if isinstance(a, list):
            if len(a) != len(b):
                return f"{path}: len {len(a)} vs {len(b)}"
            for j, (x, y) in enumerate(zip(a, b)):
                r = deep_eq(x, y, f"{path}[{j}]")
                if r:
                    return r
            return None
        if isinstance(a, float) or isinstance(b, float):
            if a is None or b is None:
                return None if a is b else f"{path}: {a!r} vs {b!r}"
            if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
                return None
            return None if a == b else f"{path}: {a!r} vs {b!r}"
        return None if a == b else f"{path}: {a!r} vs {b!r}"

    r1 = engine.full_analysis(df)
    r2 = engine.full_analysis(copy.deepcopy(df))
    diff = deep_eq(r1, r2)
    print(f"[{'PASS' if diff is None else 'FAIL'}] full_analysis deterministic: {diff or 'identical'}")
    ok &= diff is None
    s1 = decision.build_summary(
        last_close=float(df['close'].iloc[-1]), smc=r1["smc"], indicators=r1["indicators"],
        volume_profile=r1["volumeProfile"], wyckoff=r1["wyckoff"], volatility=r1["volatility"],
        cvd_div=r1["cvdDivergence"], atr=decision._last_valid(r1["indicators"]["atr14"]),
        interval="1h")
    s2 = decision.build_summary(
        last_close=float(df['close'].iloc[-1]), smc=r2["smc"], indicators=r2["indicators"],
        volume_profile=r2["volumeProfile"], wyckoff=r2["wyckoff"], volatility=r2["volatility"],
        cvd_div=r2["cvdDivergence"], atr=decision._last_valid(r2["indicators"]["atr14"]),
        interval="1h")
    diff = deep_eq(s1, s2)
    print(f"[{'PASS' if diff is None else 'FAIL'}] build_summary deterministic: {diff or 'identical'}")
    ok &= diff is None
    # summary shape invariants
    rs = [abs(c["weight"]) for c in s1["reasons"]]
    sorted_ok = all(rs[i] >= rs[i + 1] for i in range(len(rs) - 1)) and len(rs) <= 10
    clamp_ok = -100 <= s1["score"] <= 100
    bias_ok = ((s1["score"] >= 15 and s1["bias"] == "bullish") or
               (s1["score"] <= -15 and s1["bias"] == "bearish") or
               (abs(s1["score"]) < 15 and s1["bias"] == "neutral"))
    print(f"[{'PASS' if sorted_ok else 'FAIL'}] reasons sorted by |weight| desc, <=10: {sorted_ok}")
    print(f"[{'PASS' if clamp_ok else 'FAIL'}] score clamped to +/-100: {s1['score']}")
    print(f"[{'PASS' if bias_ok else 'FAIL'}] bias <-> score +/-15 mapping: {s1['score']} -> {s1['bias']}")
    ok &= sorted_ok and clamp_ok and bias_ok

    section("5b. Boundary: flat line / zero volume / tiny df / no takerBuy / empty df")
    flat = pd.DataFrame({
        "time": (np.arange(100) * 3_600_000).astype(np.int64),
        "open": [100.0] * 100, "high": [100.0] * 100, "low": [100.0] * 100,
        "close": [100.0] * 100, "volume": [100.0] * 100, "takerBuy": [50.0] * 100,
    })
    try:
        fr = engine.full_analysis(flat)
        fs = decision.build_summary(
            last_close=100.0, smc=fr["smc"], indicators=fr["indicators"],
            volume_profile=fr["volumeProfile"], wyckoff=fr["wyckoff"],
            volatility=fr["volatility"], cvd_div=fr["cvdDivergence"],
            atr=decision._last_valid(fr["indicators"]["atr14"]), interval="1h")
        rsi_last = decision._last_valid(fr["indicators"]["rsi14"])
        atr_last = decision._last_valid(fr["indicators"]["atr14"])
        print(f"[PASS] flat line: no crash; rsi_last={rsi_last} atr_last={atr_last} "
              f"plan={fs['tradePlan']} poc={fr['volumeProfile']['poc']:.6f} "
              f"pd.pct={fr['smc']['premiumDiscount']['pct']}")
        print(f"[INFO] flat line RSI convention: avg_gain==avg_loss==0 -> RSI=100 "
              f"(production .where(avg_loss!=0,100); textbook undefined/50) -> "
              f"rsi_last={rsi_last}")
    except Exception as e:
        print(f"[FAIL] flat line raised: {type(e).__name__}: {e}")
        ok = False
    zero_vol = flat.copy()
    zero_vol["volume"] = 0.0
    zero_vol["takerBuy"] = 0.0
    try:
        engine.full_analysis(zero_vol)
        print("[PASS] zero volume: no crash")
    except Exception as e:
        print(f"[FAIL] zero volume raised: {type(e).__name__}: {e}")
        ok = False
    try:
        engine.full_analysis(df.head(61))
        print("[PASS] n=61 (minimal): no crash")
    except Exception as e:
        print(f"[FAIL] n=61 raised: {type(e).__name__}: {e}")
        ok = False
    try:
        no_taker = df.drop(columns=["takerBuy"])
        rn = engine.full_analysis(no_taker)
        all_none = all(v is None for v in rn["indicators"]["cvd"])
        print(f"[{'PASS' if all_none else 'FAIL'}] no takerBuy column: cvd all None={all_none}, "
              f"cvdDivergence={rn['cvdDivergence']}")
        ok &= all_none
    except Exception as e:
        print(f"[FAIL] no takerBuy raised: {type(e).__name__}: {e}")
        ok = False
    empty = df.head(0)
    try:
        re_ = engine.full_analysis(empty)
        rh = re_["smc"]["premiumDiscount"]["rangeHigh"]
        print(f"[INFO] n=0 engine-level: no crash but rangeHigh={rh} "
              f"(NaN -> JSON-unserializable; unreachable via API: run_analysis guards len<60)")
    except Exception as e:
        print(f"[INFO] n=0 engine-level raised {type(e).__name__}: {e} "
              f"(unreachable via API: run_analysis guards len<60)")

    section("2b. Wyckoff spring/utad reachability probe (self-inclusive range)")
    n = 70
    base = np.sin(np.arange(n) * 0.5) * 5 + 100  # oscillation in [95,105]
    wy_open = base.copy()
    wy_close = base + 0.3
    wy_high = np.maximum(wy_open, wy_close) + 0.5
    wy_low = np.minimum(wy_open, wy_close) - 0.5
    # last bar: pierce well below the established range low, close back inside
    wy_open[-1] = 100.0
    wy_close[-1] = 99.5
    wy_high[-1] = 100.5
    wy_low[-1] = 88.0  # deep pierce
    wdf = pd.DataFrame({
        "time": (np.arange(n) * 3_600_000).astype(np.int64),
        "open": wy_open, "high": wy_high, "low": wy_low, "close": wy_close,
        "volume": [100.0] * n, "takerBuy": [50.0] * n,
    })
    from services.analysis import swings as _sw, smc as _smc, wyckoff as _wy
    swl = _sw.detect_swings(wdf)
    sr = _smc.analyze(wdf, swl)
    wy = _wy.analyze(wdf, swl, sr, decision._last_valid(indicators.atr(wdf, 14)))
    springs = [e for e in wy["events"] if e["type"] == "spring"]
    print(f"[INFO] crafted textbook spring (low 88 << range, close 99.5 inside): "
          f"events={wy['events']} -> spring fired: {bool(springs)} "
          f"(rng_lo includes the piercing bar itself, so lows[k] < rng_lo can never be true)")

    section("2c. Causality probe: truncation consistency (no retroactive rewrite)")
    from services.analysis import swings as _sw2, smc as _smc2
    tcut = 500  # decision bar index; truncated window covers bars [0, tcut]
    df_trunc = df.iloc[:tcut + 1].reset_index(drop=True)
    sw_full = _sw2.detect_swings(df)
    sw_trunc = _sw2.detect_swings(df_trunc)
    # swings in the truncated run must equal full-run swings confirmed by tcut (index <= tcut-2)
    full_sw_conf = [(s["index"], s["price"], s["kind"]) for s in sw_full if s["index"] <= tcut - 2]
    trunc_sw = [(s["index"], s["price"], s["kind"]) for s in sw_trunc]
    sw_ok = full_sw_conf == trunc_sw
    print(f"[{'PASS' if sw_ok else 'FAIL'}] swings prefix-consistent: truncated={len(trunc_sw)} "
          f"vs full-confirmed={len(full_sw_conf)} identical={sw_ok}")
    ok &= sw_ok
    r_full = _smc2.analyze(df, sw_full)
    r_trunc = _smc2.analyze(df_trunc, sw_trunc)
    # later window: 41 bars past the cut (small enough that the last-20 cap keeps
    # overlap events); events at/before tcut must match the truncated run
    df_later = df.iloc[:tcut + 41].reset_index(drop=True)
    r_later = _smc2.analyze(df_later, _sw2.detect_swings(df_later))
    ev_trunc = [(e["time"], e["price"], e["kind"], e["direction"]) for e in r_trunc["structureEvents"]]
    ev_overlap = [(e["time"], e["price"], e["kind"], e["direction"])
                  for e in r_later["structureEvents"] if e["index"] <= tcut]
    ev_ok = ev_trunc[-len(ev_overlap):] == ev_overlap if ev_overlap else True
    print(f"[{'PASS' if ev_ok else 'FAIL'}] structure events not retroactively rewritten: "
          f"truncated has {len(ev_trunc)}, later-window overlap {len(ev_overlap)} identical={ev_ok}")
    if not ev_ok:
        print(f"       trunc tail: {ev_trunc[-3:]}\n       later ovlp: {ev_overlap[-3:]}")
    ok &= ev_ok
    # swing CONFIRMATION lag: every event's broken level must be a swing whose
    # index <= event.index - 2 (confirmed at event time). breakFrom is stripped
    # from the public output, so re-derive by matching price+side.
    sw_by_kind = {"high": {}, "low": {}}
    for s in sw_full:
        sw_by_kind[s["kind"]].setdefault(round(s["price"], 8), []).append(s["index"])
    lag_bad = []
    for e in r_full["structureEvents"]:
        kind = "high" if e["direction"] == "bullish" else "low"
        cands = [j for j in sw_by_kind[kind].get(round(e["price"], 8), [])
                 if j <= e["index"] - 2]
        if not cands:
            lag_bad.append(e)
    lag_ok = not lag_bad
    print(f"[{'PASS' if lag_ok else 'FAIL'}] every structure event breaks a swing confirmed "
          f">=2 bars earlier (no unconfirmed swing used): {lag_ok} (bad={len(lag_bad)})")
    ok &= lag_ok

    section("3/6. tradePlan properties (direct _build_trade_plan calls)")
    atr = 2.0
    price = 100.0
    smc_fake = {
        "orderBlocks": [{"top": 99.5, "bottom": 99.0, "startTime": 1, "type": "bullish",
                         "mitigated": False, "quality": 80},
                        {"top": 101.0, "bottom": 100.5, "startTime": 2, "type": "bearish",
                         "mitigated": False, "quality": 80}],
        "fvgs": [],
        "premiumDiscount": {"rangeHigh": 110.0, "rangeLow": 90.0, "equilibrium": 100.0,
                            "position": "equilibrium", "pct": 0.5},
    }
    pools_b = [{"price": 104.0, "type": "buy_side", "touches": 2, "swept": False}]
    pools_s = [{"price": 96.0, "type": "sell_side", "touches": 2, "swept": False}]

    def plan(**kw):
        args = dict(bias="bullish", score=30, price=price, smc=smc_fake, atr=atr,
                    buy_pools=pools_b, sell_pools=pools_s,
                    pd_zone=smc_fake["premiumDiscount"], interval="1h")
        args.update(kw)
        return decision._build_trade_plan(**args)

    p_long = plan()
    chk = (p_long and p_long["direction"] == "long" and p_long["stop"] < p_long["entry"]
           and p_long["beTrigger"] > p_long["entry"] and p_long["target1"] > p_long["entry"])
    print(f"[{'PASS' if chk else 'FAIL'}] long 1h: entry={p_long['entry']} (zone top 99.5) "
          f"stop={p_long['stop']} be={p_long['beTrigger']} t1={p_long['target1']} -> directions ok={chk}")
    ok &= bool(chk)
    exp_stop = 99.5 - 2.0 * atr
    print(f"[{'PASS' if abs(p_long['stop'] - exp_stop) < 1e-8 else 'FAIL'}] long stop = entry - 2.0*ATR "
          f"= {exp_stop} (actual {p_long['stop']})")
    p_short = plan(bias="bearish", score=-30)
    chk = (p_short and p_short["direction"] == "short" and p_short["stop"] > p_short["entry"]
           and p_short["beTrigger"] < p_short["entry"] and p_short["target1"] < p_short["entry"])
    print(f"[{'PASS' if chk else 'FAIL'}] short 1h: entry={p_short['entry']} stop={p_short['stop']} "
          f"be={p_short['beTrigger']} t1={p_short['target1']} -> directions ok={chk}")
    ok &= bool(chk)
    # thresholds: boundary inclusive
    b1 = plan(score=25, interval="1h") is not None
    b2 = plan(score=24, interval="1h") is None
    b3 = plan(score=10, interval="4h") is not None
    b4 = plan(score=9, interval="4h") is None
    b5 = plan(score=-10, interval="1d") is not None
    b6 = plan(score=10, interval="1w") is not None
    print(f"[{'PASS' if all([b1, b2, b3, b4, b5, b6]) else 'FAIL'}] threshold boundaries: "
          f"1h score25->plan({b1}) 24->none({b2}); 4h 10->plan({b3}) 9->none({b4}); "
          f"1d -10->plan({b5}); 1w 10->plan({b6})")
    ok &= all([b1, b2, b3, b4, b5, b6])
    # CVD confluence override: applies at EVERY interval, may contradict score sign
    o1 = plan(score=-40, bias="bearish", interval="4h",
              high_confidence=True, confidence_dir="bullish")
    o2 = plan(score=5, interval="1w", high_confidence=True, confidence_dir="bearish")
    o3 = plan(score=0, interval="1h", high_confidence=True, confidence_dir="bullish")
    ov = (o1 and o1["direction"] == "long" and o2 and o2["direction"] == "short"
          and o3 and o3["direction"] == "long")
    print(f"[{'PASS' if ov else 'FAIL'}] CVD-confluence override at any interval & vs score sign: "
          f"4h score-40+confBull -> {o1['direction']}; 1w score+5+confBear -> {o2['direction']}; "
          f"1h score0+confBull -> {o3['direction']}")
    ok &= bool(ov)
    # guards
    g1 = decision._build_trade_plan(bias="bullish", score=30, price=price, smc=smc_fake,
                                    atr=None, buy_pools=[], sell_pools=[],
                                    pd_zone=smc_fake["premiumDiscount"], interval="1h") is None
    g2 = decision._build_trade_plan(bias="bullish", score=30, price=price, smc=smc_fake,
                                    atr=0.0, buy_pools=[], sell_pools=[],
                                    pd_zone=smc_fake["premiumDiscount"], interval="1h") is None
    print(f"[{'PASS' if g1 and g2 else 'FAIL'}] atr None/0 -> no plan: {g1}, {g2}")
    ok &= g1 and g2
    # unknown interval falls back to 1h defaults
    u1 = decision._build_trade_plan(bias="bullish", score=20, price=price, smc=smc_fake, atr=atr,
                                    buy_pools=[], sell_pools=[],
                                    pd_zone=smc_fake["premiumDiscount"], interval="bogus") is None
    print(f"[{'PASS' if u1 else 'FAIL'}] unknown interval 'bogus' falls back to 1h threshold 25 "
          f"(score20 -> no plan): {u1}")
    ok &= u1

    section("3b. Config tables vs docs")
    print(f"[INFO] PLAN_THRESHOLD = {decision.PLAN_THRESHOLD}  (code: confluence override applies "
          f"to ALL intervals via high_confidence branch, decision.py:393-398)")
    print(f"[INFO] PLAN_GEOMETRY = {decision.PLAN_GEOMETRY}")
    print(f"[INFO] WEIGHTS trending funding/oi = {decision.WEIGHTS['trending']['funding']}/"
          f"{decision.WEIGHTS['trending']['oi']}; ranging = {decision.WEIGHTS['ranging']['funding']}/"
          f"{decision.WEIGHTS['ranging']['oi']} (docstring claims 10/8 and 10/6)")

    print(f"\n{'=' * 72}\nOVERALL: {'ALL HARD CHECKS PASSED' if ok else 'SOME CHECKS FAILED'}\n{'=' * 72}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
