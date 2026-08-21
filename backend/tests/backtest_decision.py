"""Decision-engine historical backtest with anti-overfit protocol (v2).

Protocol (fixed before looking at results):
  1. Sample N decision points per symbol across history (deterministic linspace).
  2. Split ALL points by TIME: earliest 60% = in-sample (IS, tuning),
     latest 40% = out-of-sample (OOS, validation). OOS never used for choosing.
  3. On IS only: diagnostics, direction-gate sweep (small coarse set), and
     trade-plan geometry sweep (fill-then-evaluate limit orders).
  4. Fixed selection rule: pick plan (gate x geometry) with max IS win rate
     subject to filled n >= 40; tie-break larger n. Validate once on OOS.
  5. Report OOS: direction hit, plan win rate, per-symbol, fill rates.

Records are cached to tests/_bt_cache.pkl keyed by data/params/engine source
hash so engine changes automatically invalidate the cache.

Usage:
  PYTHONIOENCODING=utf-8 python tests/backtest_decision.py --points 100
  (--refresh to force recompute)
"""
import argparse
import asyncio
import hashlib
import os
import pickle
import random
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import numpy as np
import pandas as pd

from services.analysis import decision, engine

MIRROR = "https://data-api.binance.vision"
H4_MS = 4 * 3600_000
D1_MS = 24 * 3600_000
HORIZONS = [("1H", 1), ("4H", 4), ("1D", 24), ("1W", 168), ("1M", 720)]
WARMUP = 500
PLAN_FILL_BARS = 24  # limit order valid for 1 day
PLAN_MAX_BARS = 168  # after fill, resolve within 1 week
BARRIER_ATR = 0.5
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_bt_cache.pkl")
HARNESS_VER = 5

COMPONENT_KEYS = [
    ("结构趋势", "structure"), ("多周期", "mtf"), ("EMA", "ema"), ("订单块", "ob"),
    ("FVG", "fvg"), ("价区", "pd"), ("CVD", "cvd"), ("流动性被扫", "sweep"),
    ("RSI", "rsi"), ("图表形态", "chart_pat"), ("K 线形态", "candle"),
    ("Wyckoff", "wyckoff"), ("磁吸", "magnet"),
    ("过热", "extension"), ("超卖", "extension"),
]


async def fetch_klines_1h(symbol: str, total: int) -> pd.DataFrame:
    rows: list = []
    end = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        while len(rows) < total:
            params: dict = {"symbol": symbol, "interval": "1h", "limit": 1000}
            if end is not None:
                params["endTime"] = end
            resp = await client.get(f"{MIRROR}/api/v3/klines", params=params)
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            rows = data + rows
            end = data[0][0] - 1
            await asyncio.sleep(0.05)
    recs = [{
        "time": int(k[0]), "open": float(k[1]), "high": float(k[2]),
        "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
        "takerBuy": float(k[9]) if len(k) > 9 and k[9] not in (None, "") else None,
    } for k in rows]
    return pd.DataFrame(recs).sort_values("time").drop_duplicates("time").reset_index(drop=True)


def resample_from_1h(df1h: pd.DataFrame, span_ms: int) -> pd.DataFrame:
    df = df1h.copy()
    df["bucket"] = (df["time"] // span_ms) * span_ms
    if "takerBuy" in df.columns:
        df["takerBuy"] = df["takerBuy"].fillna(0.0)
    g = df.groupby("bucket", sort=True)
    out = pd.DataFrame({
        "time": g.size().index.to_numpy(),
        "open": g["open"].first().to_numpy(),
        "high": g["high"].max().to_numpy(),
        "low": g["low"].min().to_numpy(),
        "close": g["close"].last().to_numpy(),
        "volume": g["volume"].sum().to_numpy(),
    })
    if "takerBuy" in df.columns:
        out["takerBuy"] = g["takerBuy"].sum().to_numpy()
    return out


def last_valid(series: list) -> float | None:
    for v in reversed(series):
        if v is not None:
            return v
    return None


def mtf_summaries(df4h: pd.DataFrame, df1d: pd.DataFrame, t: int) -> list[dict]:
    out: list[dict] = []
    for itv, dfl in (("4h", df4h), ("1d", df1d)):
        sub = dfl[dfl["time"] <= t].tail(300).reset_index(drop=True)
        if len(sub) < 60:
            continue
        full = engine.full_analysis(sub)
        closes = sub["close"]
        s = decision.build_summary(
            last_close=float(closes.iloc[-1]),
            smc=full["smc"],
            indicators=full["indicators"],
            volume_profile=full["volumeProfile"],
            patterns=full["patterns"],
            wyckoff=full["wyckoff"],
            volatility=full["volatility"],
            cvd_div=full["cvdDivergence"],
            atr=last_valid(full["indicators"]["atr14"]),
        )
        out.append({"interval": itv, "score": s["score"], "bias": s["bias"],
                    "regime": s["regime"], "cvd_div": (full["cvdDivergence"] or {}).get("type"),
                    "cvd_strength": (full["cvdDivergence"] or {}).get("strength")})
    return out


def classify(text: str) -> str | None:
    for key, name in COMPONENT_KEYS:
        if key in text:
            return name
    return None


def decide_at(df1h: pd.DataFrame, df4h: pd.DataFrame, df1d: pd.DataFrame, t: int) -> dict | None:
    sub = df1h[df1h["time"] <= t].tail(WARMUP).reset_index(drop=True)
    if len(sub) < 300:
        return None
    d1 = df1d[df1d["time"] <= t]
    prev_day = None
    if len(d1) >= 2:
        p = d1.iloc[-2]
        prev_day = {"high": float(p["high"]), "low": float(p["low"])}

    full = engine.full_analysis(sub, prev_day)
    closes = sub["close"]
    lookback = min(24, len(closes) - 1)
    base = float(closes.iloc[-1 - lookback])
    pchg = (float(closes.iloc[-1]) - base) / base * 100.0 if base > 0 else None

    mtf = mtf_summaries(df4h, df1d, t)
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
        last_close=float(closes.iloc[-1]),
        smc=full["smc"],
        indicators=full["indicators"],
        volume_profile=full["volumeProfile"],
        price_change_pct=pchg,
        patterns=full["patterns"],
        wyckoff=full["wyckoff"],
        volatility=full["volatility"],
        cvd_div=full["cvdDivergence"],
        mtf=mtf,
        atr=last_valid(full["indicators"]["atr14"]),
    )
    comps: list[tuple[str, float]] = []
    for r in summary["reasons"]:
        c = classify(r["text"])
        if c is not None:
            comps.append((c, float(r["weight"])))
    mtf_scores = {m["interval"]: m["score"] for m in mtf}
    mtf_cvd = {m["interval"]: m["cvd_div"] for m in mtf}
    mtf_cvd_strength = {m["interval"]: m["cvd_strength"] for m in mtf}
    plan = summary.get("tradePlan")
    plan_raw = None
    if plan:
        plan_raw = {"direction": plan["direction"], "entry": plan["entry"], "stop": plan["stop"]}
    e20 = last_valid(full["indicators"]["ema20"])
    atr_v = last_valid(full["indicators"]["atr14"])
    ext = (float(closes.iloc[-1]) - e20) / atr_v if (e20 and atr_v) else None
    # zone context for plan re-parameterization in sweeps
    price_now = float(closes.iloc[-1])
    bull_zones = [z for z in full["smc"]["orderBlocks"] + full["smc"]["fvgs"]
                  if z["type"] == "bullish" and not z["mitigated"] and z["top"] <= price_now]
    near_bull = [z for z in bull_zones if price_now - z["top"] <= 1.0 * (atr_v or 0)]
    zone_bull_top = max(near_bull, key=lambda z: z.get("quality") or 0)["top"] if near_bull else None
    bear_zones = [z for z in full["smc"]["orderBlocks"] + full["smc"]["fvgs"]
                  if z["type"] == "bearish" and not z["mitigated"] and z["bottom"] >= price_now]
    near_bear = [z for z in bear_zones if z["bottom"] - price_now <= 1.0 * (atr_v or 0)]
    zone_bear_bottom = max(near_bear, key=lambda z: z.get("quality") or 0)["bottom"] if near_bear else None
    return {
        "time": t, "score": summary["score"], "bias": summary["bias"],
        "regime": summary["regime"], "alignment": alignment,
        "mtf_scores": mtf_scores, "mtf_cvd": mtf_cvd, "mtf_cvd_strength": mtf_cvd_strength,
        "comps": comps,
        "cvd_div": (full["cvdDivergence"] or {}).get("type"),
        "cvd_strength": (full["cvdDivergence"] or {}).get("strength"),
        "plan": plan_raw, "atr": atr_v,
        "ext": ext, "pd_pct": full["smc"]["premiumDiscount"]["pct"],
        "vol_state": (full["volatility"] or {}).get("state"),
        "zone_bull_top": zone_bull_top, "zone_bear_bottom": zone_bear_bottom,
    }


def attach_forward(rec: dict, df1h: pd.DataFrame, i: int) -> None:
    closes = df1h["close"].to_numpy()
    highs = df1h["high"].to_numpy()
    lows = df1h["low"].to_numpy()
    n = len(df1h)
    for name, h in HORIZONS:
        j = i + h
        rec[f"ret_{name}"] = closes[j] / closes[i] - 1.0 if j < n else float("nan")
    atr = rec.get("atr") or float("nan")
    for name, h in (("1D", 24), ("1W", 168)):
        tu = td = None
        up_lv = closes[i] + BARRIER_ATR * atr
        dn_lv = closes[i] - BARRIER_ATR * atr
        for j in range(i + 1, min(i + 1 + h, n)):
            if tu is None and highs[j] >= up_lv:
                tu = j - i
            if td is None and lows[j] <= dn_lv:
                td = j - i
            if tu is not None and td is not None:
                break
        rec[f"tu_{name}"], rec[f"td_{name}"] = tu, td


def plan_sim(df1h: pd.DataFrame, i: int, plan: dict, target_mult: float) -> str:
    """Limit-order simulation: wait for fill within PLAN_FILL_BARS, then race
    stop vs target (stop-first tie-break). Returns win/loss/nofill/timeout."""
    highs = df1h["high"].to_numpy()
    lows = df1h["low"].to_numpy()
    n = len(df1h)
    long = plan["direction"] == "long"
    entry, stop = plan["entry"], plan["stop"]
    risk = abs(entry - stop)
    if risk <= 0:
        return "invalid"
    target = entry + target_mult * risk if long else entry - target_mult * risk
    fill_bar = None
    for j in range(i + 1, min(i + 1 + PLAN_FILL_BARS, n)):
        if (long and lows[j] <= entry) or (not long and highs[j] >= entry):
            fill_bar = j
            break
    if fill_bar is None:
        return "nofill"
    for j in range(fill_bar, min(fill_bar + PLAN_MAX_BARS, n)):
        hit_stop = lows[j] <= stop if long else highs[j] >= stop
        hit_tg = highs[j] >= target if long else lows[j] <= target
        if hit_stop:
            return "loss"
        if hit_tg:
            return "win"
    return "timeout"


def plan_sim_be(df1h: pd.DataFrame, i: int, plan: dict, mult: float) -> str:
    """Plan sim with breakeven management: stop moves to entry after +0.5R.
    Returns win / scratch (stopped at entry) / loss / nofill / timeout."""
    highs = df1h["high"].to_numpy()
    lows = df1h["low"].to_numpy()
    n = len(df1h)
    long = plan["direction"] == "long"
    entry, stop = plan["entry"], plan["stop"]
    risk = abs(entry - stop)
    if risk <= 0:
        return "invalid"
    target = entry + mult * risk if long else entry - mult * risk
    be_trigger = entry + 0.5 * risk if long else entry - 0.5 * risk
    fill_bar = None
    for j in range(i + 1, min(i + 1 + PLAN_FILL_BARS, n)):
        if (long and lows[j] <= entry) or (not long and highs[j] >= entry):
            fill_bar = j
            break
    if fill_bar is None:
        return "nofill"
    be_active = False
    for j in range(fill_bar, min(fill_bar + PLAN_MAX_BARS, n)):
        hit_stop = lows[j] <= (entry if be_active else stop) if long else highs[j] >= (entry if be_active else stop)
        hit_tg = highs[j] >= target if long else lows[j] <= target
        if hit_stop:
            return "scratch" if be_active else "loss"
        if hit_tg:
            return "win"
        if not be_active and ((long and highs[j] >= be_trigger) or (not long and lows[j] <= be_trigger)):
            be_active = True
    return "timeout"


def cross_sectional_signal(dfs: dict, sym: str, t: int, lookback: int = 720) -> int | None:
    """Rank symbols by trailing 30d return at time t: +1 if `sym` is top, -1 if bottom."""
    rets = {}
    for s, df in dfs.items():
        closes = df["close"].to_numpy()
        times = df["time"].to_numpy()
        idx = int(np.searchsorted(times, t, side="right")) - 1
        if idx < lookback:
            return None
        rets[s] = closes[idx] / closes[idx - lookback] - 1.0
    if len(rets) < 3:
        return None
    own = rets[sym]
    if own == max(rets.values()):
        return 1
    if own == min(rets.values()):
        return -1
    return None


def variant_signal(rec: dict, variant: str, th: float) -> int | None:
    if variant == "base":
        s = rec["score"]
    elif variant == "mtf":
        vals = [rec["score"]] + [rec["mtf_scores"].get(k, 0.0) for k in ("4h", "1d")]
        s = sum(vals) / len(vals)
    elif variant == "d1":
        s = rec["mtf_scores"].get("1d")
        if s is None:
            return None
    elif variant == "cvdconf":
        d, cnt = confluence(rec)
        return (1 if d == "bullish" else -1) if d else None
    elif variant == "cvdconf3":
        d, cnt = confluence(rec)
        return (1 if d == "bullish" else -1) if (d and cnt >= 3) else None
    else:
        raise ValueError(variant)
    if s is None or abs(s) < th:
        return None
    return 1 if s > 0 else -1


def confluence(rec: dict, min_strength: int = 0) -> tuple[str | None, int]:
    dirs: list[str] = []
    if rec["cvd_div"] and (rec.get("cvd_strength") or 0) >= min_strength:
        dirs.append(rec["cvd_div"])
    for k in ("4h", "1d"):
        if rec["mtf_cvd"].get(k) and (rec.get("mtf_cvd_strength", {}).get(k) or 0) >= min_strength:
            dirs.append(rec["mtf_cvd"][k])
    bulls = dirs.count("bullish")
    bears = dirs.count("bearish")
    if bulls >= 2 and bulls > bears:
        return "bullish", bulls
    if bears >= 2 and bears > bulls:
        return "bearish", bears
    return None, max(bulls, bears)


def eval_direction(records: list[dict], sig, horizon: str) -> tuple[int, float, float, float]:
    dirs, rets = [], []
    for r in records:
        d = sig(r)
        if d is None:
            continue
        ret = r.get(f"ret_{horizon}")
        if ret is None or np.isnan(ret) or ret == 0:
            continue
        dirs.append(d)
        rets.append(ret)
    dirs = np.array(dirs)
    rets = np.array(rets)
    n = len(dirs)
    if n == 0:
        return 0, float("nan"), float("nan"), float("nan")
    hit = float(np.mean(np.sign(dirs) == np.sign(rets)))
    bull = rets[dirs > 0]
    bear = rets[dirs < 0]
    return n, hit, (bull.mean() if len(bull) else float("nan")), (bear.mean() if len(bear) else float("nan"))


def eval_barrier(records: list[dict], sig, horizon: str) -> tuple[int, float]:
    wins, total = 0, 0
    for r in records:
        d = sig(r)
        if d is None:
            continue
        tu, td = r.get(f"tu_{horizon}"), r.get(f"td_{horizon}")
        if tu is None and td is None:
            continue
        total += 1
        if d > 0:
            ok = tu is not None and (td is None or tu < td)
        else:
            ok = td is not None and (tu is None or td < tu)
        wins += int(ok)
    return total, (wins / total if total else float("nan"))


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 3:
        return 0.0
    ra = pd.Series(a).rank().to_numpy()
    rb = pd.Series(b).rank().to_numpy()
    if np.std(ra) == 0 or np.std(rb) == 0:
        return 0.0
    return float(np.corrcoef(ra, rb)[0, 1])


def fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def source_hash() -> str:
    parts = []
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "services", "analysis")
    for f in sorted(os.listdir(base)):
        if f.endswith(".py"):
            with open(os.path.join(base, f), "rb") as fh:
                parts.append(f + ":" + hashlib.md5(fh.read()).hexdigest()[:8])
    return ";".join(parts)


def compute_records(symbols: list[str], points: int, total: int, dfs: dict) -> list[dict]:
    records: list[dict] = []
    for sym in symbols:
        df1h = dfs.get(sym)
        if df1h is None or len(df1h) < WARMUP + 721 + 50:
            print(f"[warn] {sym}: skipped")
            continue
        n = len(df1h)
        df4h = resample_from_1h(df1h, H4_MS)
        df1d = resample_from_1h(df1h, D1_MS)
        idxs = sorted(set(int(i) for i in np.linspace(WARMUP, n - 721, points)))
        times = df1h["time"].to_numpy()
        cnt = 0
        t0 = datetime.now()
        for i in idxs:
            t = int(times[i])
            rec = decide_at(df1h, df4h, df1d, t)
            if rec is None:
                continue
            rec["symbol"] = sym
            rec["price"] = float(df1h["close"].to_numpy()[i])
            attach_forward(rec, df1h, i)
            records.append(rec)
            cnt += 1
            if cnt % 100 == 0:
                el = (datetime.now() - t0).total_seconds()
                print(f"[calc] {sym}: {cnt}/{len(idxs)} ({el:.0f}s elapsed)", flush=True)
        print(f"[data] {sym}: {n} bars, {cnt} points")
    records.sort(key=lambda r: r["time"])
    return records


def load_records(symbols: list[str], points: int, total: int, refresh: bool, dfs: dict) -> list[dict]:
    key = {"ver": HARNESS_VER, "symbols": symbols, "points": points, "total": total, "src": source_hash()}
    if not refresh and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "rb") as f:
                cached = pickle.load(f)
            if cached.get("key") == key:
                print(f"[cache] loaded {len(cached['records'])} records")
                return cached["records"]
        except Exception:
            pass
    records = compute_records(symbols, points, total, dfs)
    with open(CACHE_FILE, "wb") as f:
        pickle.dump({"key": key, "records": records}, f)
    print(f"[cache] saved {len(records)} records")
    return records


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    ap.add_argument("--points", type=int, default=150)
    ap.add_argument("--total", type=int, default=17000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    random.seed(args.seed)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    print(f"[setup] symbols={symbols} points/symbol={args.points} split=60/40 by time")
    dfs: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            dfs[sym] = asyncio.run(fetch_klines_1h(sym, args.total))
            print(f"[data] {sym}: {len(dfs[sym])} bars")
        except Exception as exc:
            print(f"[warn] {sym} fetch failed: {exc}")
    records = load_records(symbols, args.points, args.total, args.refresh, dfs)

    k = int(len(records) * 0.6)
    IS, OOS = records[:k], records[k:]
    print(f"[split] total={len(records)}  IS={len(IS)} ({fmt_ts(IS[0]['time'])}..{fmt_ts(IS[-1]['time'])})"
          f"  OOS={len(OOS)} ({fmt_ts(OOS[0]['time'])}..{fmt_ts(OOS[-1]['time'])})")

    # ---------- IS diagnostics: conditional hit rates ----------
    print("\n===== IS 诊断：条件命中率（1D/1W 方向）=====")

    def cond_stats(subset: list[dict]) -> tuple[int, float, float]:
        n1, h1, _, _ = eval_direction(subset, lambda r: (1 if r["score"] >= 15 else (-1 if r["score"] <= -15 else None)), "1D")
        nw, hw, _, _ = eval_direction(subset, lambda r: (1 if r["score"] >= 15 else (-1 if r["score"] <= -15 else None)), "1W")
        return n1, h1, hw

    def sgn(x) -> int:
        return 1 if x > 0 else -1

    diags = [
        ("CVD背离与评分同向", lambda r: r["cvd_div"] is not None and sgn(r["score"]) == (1 if r["cvd_div"] == "bullish" else -1)),
        ("CVD背离与评分反向", lambda r: r["cvd_div"] is not None and sgn(r["score"]) != (1 if r["cvd_div"] == "bullish" else -1)),
        ("MTF aligned", lambda r: r["alignment"] == "aligned"),
        ("MTF conflict", lambda r: r["alignment"] == "conflict"),
        ("trending", lambda r: r["regime"] == "trending"),
        ("ranging", lambda r: r["regime"] == "ranging"),
        ("4h CVD背离存在", lambda r: r["mtf_cvd"].get("4h") is not None),
        ("1d CVD背离存在", lambda r: r["mtf_cvd"].get("1d") is not None),
        ("过热(ext>2.5)", lambda r: r["ext"] is not None and r["ext"] > 2.5),
        ("超卖(ext<-2.5)", lambda r: r["ext"] is not None and r["ext"] < -2.5),
        ("溢价区(pct>0.8)", lambda r: r["pd_pct"] is not None and r["pd_pct"] > 0.8),
        ("折价区(pct<0.2)", lambda r: r["pd_pct"] is not None and r["pd_pct"] < 0.2),
    ]
    for label, fn in diags:
        sub = [r for r in IS if fn(r)]
        n, h1, hw = cond_stats(sub)
        print(f"  {label:<18} n={n:<4} 1D={h1*100 if n else 0:.1f}%  1W={hw*100 if n else 0:.1f}%")

    # standalone signal hit rates (direction = condition-implied direction)
    print("  -- 独立信号方向命中率（信号方向 vs 后续收益方向）--")
    standalone = [
        ("1h CVD背离方向", lambda r: (1 if r["cvd_div"] == "bullish" else -1) if r["cvd_div"] else None),
        ("4h CVD背离方向", lambda r: (1 if r["mtf_cvd"].get("4h") == "bullish" else -1) if r["mtf_cvd"].get("4h") else None),
        ("1d CVD背离方向", lambda r: (1 if r["mtf_cvd"].get("1d") == "bullish" else -1) if r["mtf_cvd"].get("1d") else None),
        ("ext 反转(ext>|2.5|)", lambda r: (-1 if r["ext"] > 2.5 else 1) if r["ext"] is not None and abs(r["ext"]) > 2.5 else None),
        ("pd 边缘反转", lambda r: (-1 if r["pd_pct"] > 0.8 else 1) if r["pd_pct"] is not None and (r["pd_pct"] > 0.8 or r["pd_pct"] < 0.2) else None),
    ]
    for label, fn in standalone:
        for h in ("1D", "1W"):
            n, hit, _, _ = eval_direction(IS, fn, h)
            print(f"  {label:<18} {h}: n={n:<4} 胜率={hit*100 if n else 0:.1f}%")
        print()

    # agreement count of strong components with score direction
    def agree_cnt(r: dict) -> int:
        sd = 1 if r["score"] > 0 else -1
        cnt = 0
        for comp, wgt in r["comps"]:
            if comp in ("structure", "ema", "mtf", "wyckoff", "cvd") and np.sign(wgt) == sd:
                cnt += 1
        return cnt

    print("  -- 按强组件同向数 --")
    for lo, hi in ((0, 1), (2, 3), (4, 5), (6, 99)):
        sub = [r for r in IS if lo <= agree_cnt(r) <= hi and abs(r["score"]) >= 15]
        n, h1, hw = cond_stats(sub)
        print(f"  agree {lo}-{hi}: n={n:<4} 1D={h1*100 if n else 0:.1f}%  1W={hw*100 if n else 0:.1f}%")

    # ---------- extra diagnostics: cross-sectional momentum & BE-managed plans ----------
    print("\n===== IS 附加诊断 =====")
    xs_records = []
    for r in IS:
        d = cross_sectional_signal(dfs, r["symbol"], r["time"])
        if d is not None:
            rr = dict(r)
            rr["_xs"] = d
            xs_records.append(rr)
    for h in ("1D", "1W"):
        n, hit, _, _ = eval_direction(xs_records, lambda r: r["_xs"], h)
        print(f"  横截面动量(30d排名) {h}: n={n} 胜率={hit*100 if n else 0:.1f}%")

    def sim_be(subset: list[dict], mult: float) -> tuple[int, int, float, float]:
        n = filled = win = scratch = 0
        for r in subset:
            if r["plan"] is None:
                continue
            n += 1
            df = dfs.get(r["symbol"])
            i = time_index.get(r["symbol"], {}).get(r["time"])
            if df is None or i is None:
                continue
            out = plan_sim_be(df, i, r["plan"], mult)
            if out in ("win", "loss", "scratch", "timeout"):
                filled += 1
                win += int(out == "win")
                scratch += int(out == "scratch")
        return filled, win, (win / filled if filled else float("nan")), (scratch / filled if filled else float("nan"))

    # NOTE: time_index defined later in plan sweep; define early here instead
    if "time_index" not in dir():
        time_index = {sym: {int(t): i for i, t in enumerate(dfs[sym]["time"].to_numpy())} for sym in dfs}

    for gname in ("all", "aligned25"):
        sub = [r for r in IS if (r["plan"] is not None) and (gname == "all" or (abs(r["score"]) >= 25 and r["alignment"] == "aligned"))]
        filled, win, wr, sr = sim_be(sub, 1.0)
        print(f"  保本管理计划[{gname}] T=1.0x: fill={filled} win={wr*100 if filled else 0:.1f}% scratch={sr*100 if filled else 0:.1f}% 非亏损={(wr+sr)*100 if filled else 0:.1f}%")

    # ---------- IS direction gate sweep ----------
    print("\n===== IS 方向门控扫描 =====")
    for v in ("base", "mtf", "d1", "cvdconf", "cvdconf3"):
        for th in (15, 25, 40):
            sig = lambda r, v=v, th=th: variant_signal(r, v, th)
            n1, h1, _, _ = eval_direction(IS, sig, "1D")
            nw, hw, _, _ = eval_direction(IS, sig, "1W")
            print(f"  {v:<9} th={th:<3} 1D: n={n1:<4} {h1*100 if n1 else 0:.1f}%   1W: n={nw:<4} {hw*100 if nw else 0:.1f}%")

    # ---------- IS plan gate sweep (T=1.0x only, the 1:1 geometry) ----------
    print("\n===== IS 交易计划门控扫描（1:1 目标，限价成交后）=====")
    time_index = {sym: {int(t): i for i, t in enumerate(dfs[sym]["time"].to_numpy())} for sym in dfs}

    def sim_subset(subset: list[dict], mult: float) -> tuple[int, int, int, float]:
        n = filled = win = 0
        for r in subset:
            if r["plan"] is None:
                continue
            n += 1
            df = dfs.get(r["symbol"])
            i = time_index.get(r["symbol"], {}).get(r["time"])
            if df is None or i is None:
                continue
            out = plan_sim(df, i, r["plan"], mult)
            if out in ("win", "loss", "timeout"):
                filled += 1
                win += int(out == "win")
        return n, filled, win, (win / filled if filled else float("nan"))

    def agree_cnt(r: dict) -> int:
        sd = 1 if r["score"] > 0 else -1
        cnt = 0
        for comp, wgt in r["comps"]:
            if comp in ("structure", "ema", "mtf", "wyckoff", "cvd") and np.sign(wgt) == sd:
                cnt += 1
        return cnt

    plan_gates = {
        "all": lambda r: r["plan"] is not None,
        "conf2": lambda r: r["plan"] is not None and confluence(r)[0] is not None,
        "conf2s40": lambda r: r["plan"] is not None and confluence(r, min_strength=40)[0] is not None,
        "conf3": lambda r: r["plan"] is not None and confluence(r)[1] >= 3,
        "aligned25": lambda r: r["plan"] is not None and abs(r["score"]) >= 25 and r["alignment"] == "aligned",
        "cvd25": lambda r: r["plan"] is not None and abs(r["score"]) >= 25 and r["cvd_div"] is not None,
        "conf2_range": lambda r: r["plan"] is not None and confluence(r)[0] is not None and r["regime"] == "ranging",
        "conf2_agree": lambda r: r["plan"] is not None and confluence(r)[0] is not None and agree_cnt(r) >= 4,
    }
    plan_sweep = {}
    for gname, gfn in plan_gates.items():
        sub = [r for r in IS if gfn(r)]
        for mult in (1.0, 1.5, 2.5):
            n, filled, win, wr = sim_subset(sub, mult)
            plan_sweep[(gname, mult)] = (n, filled, wr)
            print(f"  {gname:<12} T={mult}x: n={n:<4} fill={filled:<4} win={wr*100 if filled else 0:.1f}%")

    # ---------- fixed selection rules ----------
    # direction gate: max IS 1W hit with n>=40
    cand = []
    for v in ("base", "mtf", "d1", "cvdconf", "cvdconf3"):
        for th in (15, 25, 40):
            sig = lambda r, v=v, th=th: variant_signal(r, v, th)
            nw, hw, _, _ = eval_direction(IS, sig, "1W")
            if nw >= 40:
                cand.append((hw, nw, f"{v}|th{th}", sig))
    cand.sort(key=lambda x: (-x[0], -x[1]))
    _, _, gname, gsig = cand[0]
    # plan gate: max IS win at T=1.0 with filled>=30
    pcand = [(wr, filled, gn) for (gn, mult), (n, filled, wr) in plan_sweep.items()
             if mult == 1.0 and filled >= 30]
    pcand.sort(key=lambda x: (-x[0], -x[1]))
    _, _, pname = pcand[0]
    pfn = plan_gates[pname]
    print(f"\n[选定方向门控] {gname} (IS 1W)")
    print(f"[选定计划门控] {pname} @T=1.0x (IS)")

    # ---------- OOS evaluation (single shot) ----------
    print("\n===== OOS 验证 =====")
    for h in ("4H", "1D", "1W", "1M"):
        n, hit, bm, em = eval_direction(OOS, gsig, h)
        print(f"  方向 {h}: n={n} 胜率={hit*100 if n else 0:.1f}% 多头均={bm*100 if n else 0:+.2f}% 空头均={em*100 if n else 0:+.2f}%")
    for h in ("1D", "1W"):
        nb, wb = eval_barrier(OOS, gsig, h)
        print(f"  屏障({BARRIER_ATR}×ATR 先触) {h}: n={nb} 先触胜率={wb*100 if nb else 0:.1f}%")
    for sym in symbols:
        sub = [r for r in OOS if r["symbol"] == sym]
        n, hit, _, _ = eval_direction(sub, gsig, "1W")
        if n:
            print(f"  {sym} 1W: n={n} 胜率={hit*100:.1f}%")
    # plan OOS at all geometries for the chosen gate
    oos_sub = [r for r in OOS if pfn(r)]
    for mult in (1.0, 1.5, 2.5):
        n, filled, win, wr = sim_subset(oos_sub, mult)
        print(f"  计划[{pname}] T={mult}x: n={n} fill={filled} win={wr*100 if filled else 0:.1f}%")
    for sym in symbols:
        sub = [r for r in oos_sub if r["symbol"] == sym]
        n, filled, win, wr = sim_subset(sub, 1.0)
        if filled:
            print(f"  {sym} 计划 T=1.0x: fill={filled} win={wr*100:.1f}%")

    # BE-managed plans on OOS (primary deliverable metric)
    print("  -- 保本管理计划（+0.5R 后止损移至入场）--")
    for gname, gfn in (("all", lambda r: r["plan"] is not None),
                       ("aligned25", lambda r: r["plan"] is not None and abs(r["score"]) >= 25 and r["alignment"] == "aligned")):
        sub = [r for r in OOS if gfn(r)]
        filled, win, wr, sr = sim_be(sub, 1.0)
        print(f"  OOS 计划[{gname}] T=1.0x BE管理: fill={filled} win={wr*100 if filled else 0:.1f}% "
              f"保本={sr*100 if filled else 0:.1f}% 非亏损={(wr+sr)*100 if filled else 0:.1f}%")
        for sym in symbols:
            ssub = [r for r in sub if r["symbol"] == sym]
            f2, w2, wr2, sr2 = sim_be(ssub, 1.0)
            if f2:
                print(f"    {sym}: fill={f2} 非亏损={(wr2+sr2)*100:.1f}%")

    # component attribution on IS
    print("\n===== 组件归因（IS）=====")
    for hname in ("1D", "1W"):
        comp_stats: dict[str, dict] = {}
        for r in IS:
            ret = r.get(f"ret_{hname}")
            if ret is None or np.isnan(ret):
                continue
            for comp, wgt in r["comps"]:
                st = comp_stats.setdefault(comp, {"w": [], "r": []})
                st["w"].append(wgt)
                st["r"].append(ret)
        print(f"-- {hname} --")
        rows = []
        for comp, st in comp_stats.items():
            w = np.array(st["w"])
            rr = np.array(st["r"])
            if len(w) < 5:
                continue
            dm = (w != 0) & (rr != 0)
            hit = float(np.mean(np.sign(w[dm]) == np.sign(rr[dm]))) if dm.sum() else float("nan")
            rows.append((comp, len(w), spearman(w, rr), hit))
        for comp, cnt, ic, hit in sorted(rows, key=lambda x: x[2]):
            print(f"  {comp:<12} n={cnt:<5} IC={ic:+.3f} 胜率={hit*100:.1f}%")


if __name__ == "__main__":
    main()
