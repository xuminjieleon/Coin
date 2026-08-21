"""SMC engine: market structure (BOS/CHoCH), order blocks with quality,
FVGs with quality, liquidity pools, sweep events, premium/discount zones."""
import numpy as np
import pandas as pd

CLUSTER_TOL_PCT = 0.0015  # 0.15% of price
OB_MIN_DISPLACEMENT = 0.8  # impulse must exceed 0.8 * ATR
FVG_MIN_ATR = 0.1  # gap must exceed 0.1 * ATR


def _atr_or_default(df: pd.DataFrame, atr_series: list | None) -> np.ndarray:
    if atr_series is not None:
        arr = np.array([v if v is not None else np.nan for v in atr_series], dtype=float)
        if np.isfinite(arr).any():
            return arr
    tr = (df["high"] - df["low"]).astype(float).to_numpy()
    tr = np.where(tr <= 0, 1e-9, tr)
    # simple rolling mean ATR as fallback
    atr = np.full(len(tr), np.nan)
    if len(tr) >= 14:
        cum = np.cumsum(tr)
        atr[13:] = (cum[13:] - np.concatenate([[0.0], cum[:-14]])) / 14.0
        atr[:13] = atr[13]
    else:
        atr[:] = tr.mean()
    return atr


def _cluster_levels(prices: list[float], members: list[dict]) -> list[dict]:
    """Greedy clustering of sorted prices with 0.15% tolerance."""
    clusters: list[dict] = []
    for price, member in zip(prices, members):
        if clusters and price - clusters[-1]["_last"] <= clusters[-1]["_last"] * CLUSTER_TOL_PCT:
            c = clusters[-1]
            c["prices"].append(price)
            c["members"].append(member)
            c["_last"] = price
        else:
            clusters.append({"prices": [price], "members": [member], "_last": price})
    return clusters


def analyze(
    df: pd.DataFrame,
    swings: list[dict],
    prev_day: dict | None = None,
    atr_series: list | None = None,
) -> dict:
    n = len(df)
    opens = df["open"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    times = df["time"].to_numpy()
    volumes = df["volume"].to_numpy().astype(float)
    atrs = _atr_or_default(df, atr_series)
    avg_vol = float(volumes.mean()) if n else 0.0

    # ---------------- 1. Structure events (BOS / CHoCH) ----------------
    events: list[dict] = []
    trend: str | None = None
    swing_highs: list[dict] = []  # confirmed, each with 'used' flag
    swing_lows: list[dict] = []
    ordered = sorted(swings, key=lambda s: s["index"])
    ptr = 0
    for i in range(n):
        # a swing at index j is confirmed once candle j+2 closes
        while ptr < len(ordered) and ordered[ptr]["index"] <= i - 2:
            s = dict(ordered[ptr])
            s["used"] = False
            (swing_highs if s["kind"] == "high" else swing_lows).append(s)
            ptr += 1
        close = closes[i]
        ref_high = next((s for s in reversed(swing_highs) if not s["used"]), None)
        if ref_high is not None and close > ref_high["price"]:
            ref_high["used"] = True
            kind = "CHoCH" if trend == "bearish" else "BOS"
            recent_low = next((s for s in reversed(swing_lows)), None)
            events.append({
                "time": int(times[i]), "price": float(ref_high["price"]), "kind": kind,
                "direction": "bullish", "index": i, "breakFrom": ref_high["index"],
                "rangeHigh": float(ref_high["price"]),
                "rangeLow": float(recent_low["price"]) if recent_low else None,
            })
            trend = "bullish"
            continue
        ref_low = next((s for s in reversed(swing_lows) if not s["used"]), None)
        if ref_low is not None and close < ref_low["price"]:
            ref_low["used"] = True
            kind = "CHoCH" if trend == "bullish" else "BOS"
            recent_high = next((s for s in reversed(swing_highs)), None)
            events.append({
                "time": int(times[i]), "price": float(ref_low["price"]), "kind": kind,
                "direction": "bearish", "index": i, "breakFrom": ref_low["index"],
                "rangeLow": float(ref_low["price"]),
                "rangeHigh": float(recent_high["price"]) if recent_high else None,
            })
            trend = "bearish"

    # ---------------- 2. Order blocks (displacement-filtered, quality) ----------------
    order_blocks: list[dict] = []
    for ev in events:
        i = ev["index"]
        target = "bullish" if ev["direction"] == "bullish" else "bearish"
        found = None
        for j in range(i - 1, ev["breakFrom"] - 1, -1):
            if target == "bullish" and closes[j] < opens[j]:
                found = j
                break
            if target == "bearish" and closes[j] > opens[j]:
                found = j
                break
        if found is None:
            continue
        top = float(highs[found])
        bottom = float(lows[found])
        # mitigation checked on candles after the breakout candle
        mitigated = False
        first_touch = None
        for k in range(i + 1, n):
            if target == "bullish" and lows[k] <= top:
                mitigated = True
                first_touch = k if first_touch is None else first_touch
                break
            if target == "bearish" and highs[k] >= bottom:
                mitigated = True
                first_touch = k if first_touch is None else first_touch
                break
        # quality: displacement strength + impulse volume + held-after-touch bonus
        atr_at = float(atrs[i]) if np.isfinite(atrs[i]) and atrs[i] > 0 else 1e-9
        impulse = abs(float(closes[i]) - float(closes[found]))
        disp = impulse / atr_at
        if disp < OB_MIN_DISPLACEMENT:
            continue  # low-quality origin, drop
        seg_vol = volumes[found:i + 1]
        vol_ratio = float(seg_vol.mean()) / avg_vol if avg_vol > 0 else 1.0
        quality = int(100 * (0.6 * min(1.0, disp / 3.0) + 0.4 * min(1.0, vol_ratio / 2.0)))
        if mitigated and first_touch is not None:
            # held: price respected the zone after the first revisit
            if target == "bullish" and closes[-1] > top:
                quality = min(100, quality + 15)
            if target == "bearish" and closes[-1] < bottom:
                quality = min(100, quality + 15)
        order_blocks.append({
            "top": top, "bottom": bottom, "startTime": int(times[found]),
            "type": target, "mitigated": mitigated, "quality": max(0, min(100, quality)),
            "_seq": i,
        })
    # keep latest 10, prefer unmitigated, then by quality
    order_blocks.sort(key=lambda ob: -ob["_seq"])
    order_blocks.sort(key=lambda ob: ob["mitigated"])  # stable: unmitigated first
    order_blocks.sort(key=lambda ob: -ob["quality"])
    order_blocks = order_blocks[:10]
    order_blocks.sort(key=lambda ob: ob["startTime"])
    for ob in order_blocks:
        ob.pop("_seq", None)

    # ---------------- 3. Fair value gaps (ATR-filtered, quality) ----------------
    fvgs: list[dict] = []
    for i in range(1, n - 1):
        atr_at = float(atrs[i]) if np.isfinite(atrs[i]) and atrs[i] > 0 else 1e-9
        if lows[i + 1] > highs[i - 1]:  # bullish FVG
            bottom = float(highs[i - 1])
            top = float(lows[i + 1])
            mitigated = any(lows[k] <= bottom for k in range(i + 2, n))
        elif highs[i + 1] < lows[i - 1]:  # bearish FVG
            bottom = float(highs[i + 1])
            top = float(lows[i - 1])
            mitigated = any(highs[k] >= top for k in range(i + 2, n))
        else:
            continue
        size = top - bottom
        if size < FVG_MIN_ATR * atr_at:
            continue  # noise gap
        vol_ratio = float(volumes[i]) / avg_vol if avg_vol > 0 else 1.0
        quality = int(100 * (0.6 * min(1.0, size / (1.5 * atr_at)) + 0.4 * min(1.0, vol_ratio / 2.0)))
        fvgs.append({"top": top, "bottom": bottom, "startTime": int(times[i]),
                     "type": "bullish" if lows[i + 1] > highs[i - 1] else "bearish",
                     "mitigated": mitigated, "quality": max(0, min(100, quality))})
    fvgs.sort(key=lambda f: -f["quality"])
    fvgs = fvgs[:10]
    fvgs.sort(key=lambda f: f["startTime"])

    # ---------------- 4. Liquidity pools + sweep events ----------------
    pools: list[dict] = []
    sweep_events: list[dict] = []

    def _check_sweep(level: float, side: str, scan_from: int, members_last: int) -> dict | None:
        """Find the most recent sweep of `level`; classify reclaimed vs broken."""
        sweep_bar = None
        for k in range(max(scan_from, members_last + 1), n):
            if side == "buy_side" and highs[k] > level and closes[k] < level:
                sweep_bar = k
            elif side == "sell_side" and lows[k] < level and closes[k] > level:
                sweep_bar = k
        if sweep_bar is None:
            return None
        # outcome within 5 bars after the sweep
        outcome = "reclaimed"
        for j in range(sweep_bar + 1, min(sweep_bar + 6, n)):
            if side == "buy_side" and closes[j] > level:
                outcome = "broken"
                break
            if side == "sell_side" and closes[j] < level:
                outcome = "broken"
                break
        return {
            "time": int(times[sweep_bar]), "price": float(level), "side": side,
            "outcome": outcome, "barsToResolve": 5,
        }

    high_swings = sorted([s for s in swings if s["kind"] == "high"], key=lambda s: s["price"])
    low_swings = sorted([s for s in swings if s["kind"] == "low"], key=lambda s: s["price"])
    for cluster in _cluster_levels([s["price"] for s in high_swings], high_swings):
        if len(cluster["prices"]) < 2:
            continue
        level = sum(cluster["prices"]) / len(cluster["prices"])
        last_idx = max(m["index"] for m in cluster["members"])
        ev = _check_sweep(level, "buy_side", 0, last_idx)
        pools.append({"price": float(level), "type": "buy_side",
                      "touches": len(cluster["prices"]), "swept": ev is not None})
        if ev:
            sweep_events.append(ev)
    for cluster in _cluster_levels([s["price"] for s in low_swings], low_swings):
        if len(cluster["prices"]) < 2:
            continue
        level = sum(cluster["prices"]) / len(cluster["prices"])
        last_idx = max(m["index"] for m in cluster["members"])
        ev = _check_sweep(level, "sell_side", 0, last_idx)
        pools.append({"price": float(level), "type": "sell_side",
                      "touches": len(cluster["prices"]), "swept": ev is not None})
        if ev:
            sweep_events.append(ev)
    # previous day high / low act as liquidity references too (scan recent bars only)
    if prev_day:
        if prev_day.get("high") is not None:
            pdh = float(prev_day["high"])
            ev = _check_sweep(pdh, "buy_side", max(0, n - 30), 0)
            pools.append({"price": pdh, "type": "buy_side", "touches": 1, "swept": ev is not None})
            if ev:
                sweep_events.append(ev)
        if prev_day.get("low") is not None:
            pdl = float(prev_day["low"])
            ev = _check_sweep(pdl, "sell_side", max(0, n - 30), 0)
            pools.append({"price": pdl, "type": "sell_side", "touches": 1, "swept": ev is not None})
            if ev:
                sweep_events.append(ev)
    pools.sort(key=lambda p: -p["touches"])
    pools = pools[:8]
    # only keep sweeps from the last 24 bars: stale sweeps carry no signal
    recent_threshold = int(times[max(0, n - 24)])
    sweep_events = [e for e in sweep_events if e["time"] >= recent_threshold]
    sweep_events = sweep_events[-6:]

    # ---------------- 5. Premium / discount ----------------
    range_high = range_low = None
    if events:
        last_ev = events[-1]
        range_high = last_ev.get("rangeHigh")
        range_low = last_ev.get("rangeLow")
    if range_high is None or range_low is None:
        sh = [s for s in swings if s["kind"] == "high"]
        sl = [s for s in swings if s["kind"] == "low"]
        if sh and sl:
            range_high = sh[-1]["price"]
            range_low = sl[-1]["price"]
    if range_high is None or range_low is None or range_high <= range_low:
        window = df.tail(min(n, 50))
        range_high = float(window["high"].max())
        range_low = float(window["low"].min())
    equilibrium = (range_high + range_low) / 2
    last_close = float(closes[-1])
    span = range_high - range_low
    pct = (last_close - range_low) / span if span > 0 else 0.5
    pct = max(0.0, min(1.0, pct))
    if pct > 0.55:
        position = "premium"
    elif pct < 0.45:
        position = "discount"
    else:
        position = "equilibrium"
    premium_discount = {
        "rangeHigh": float(range_high), "rangeLow": float(range_low),
        "equilibrium": float(equilibrium), "position": position, "pct": float(pct),
    }

    structure_events = [
        {"time": e["time"], "price": e["price"], "kind": e["kind"], "direction": e["direction"],
         "index": e["index"]}
        for e in events[-20:]
    ]
    trend_age = (n - 1) - events[-1]["index"] if events else None
    return {
        "swings": [{"index": s["index"], "time": s["time"], "price": s["price"], "kind": s["kind"]}
                    for s in ordered],
        "structureEvents": structure_events,
        "orderBlocks": order_blocks,
        "fvgs": fvgs,
        "liquidityPools": pools,
        "sweepEvents": sweep_events,
        "premiumDiscount": premium_discount,
        "trendAge": trend_age,
    }
