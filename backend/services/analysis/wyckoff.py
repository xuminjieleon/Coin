"""Wyckoff phase recognition: range detection, accumulation/distribution,
Spring / UTAD / SOS events. Complementary to SMC structure events."""


def analyze(df, swings: list[dict], smc_result: dict, atr_last: float | None) -> dict:
    n = len(df)
    if n < 30:
        return {"phase": "none", "events": []}
    window = min(60, n)
    w = df.tail(window)
    rng_hi = float(w["high"].max())
    rng_lo = float(w["low"].min())
    width = rng_hi - rng_lo
    atr = atr_last if atr_last and atr_last > 0 else (width / 10.0 or 1e-9)
    closes = df["close"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    vols = df["volume"].to_numpy().astype(float)
    times = df["time"].to_numpy()
    last_close = float(closes[-1])
    events: list[dict] = []
    phase = "none"

    ranging = width <= 3.5 * atr
    if ranging:
        pos = (last_close - rng_lo) / width if width > 0 else 0.5
        half = window // 2
        v1 = float(vols[-window:-half or None].mean()) if window > half else 0.0
        v2 = float(vols[-half:].mean()) if half > 0 else 0.0
        vol_declining = v1 > 0 and v2 < v1 * 0.85
        # Spring / UTAD over the last 15 bars
        for k in range(max(0, n - 15), n):
            if lows[k] < rng_lo and closes[k] > rng_lo:
                events.append({"time": int(times[k]), "type": "spring"})
            if highs[k] > rng_hi and closes[k] < rng_hi:
                events.append({"time": int(times[k]), "type": "utad"})
        # SOS: recent bullish structure break with volume expansion near range high
        structure = smc_result.get("structureEvents") or []
        avg_vol20 = float(vols[-20:].mean()) if n >= 20 else float(vols.mean())
        if (structure and structure[-1]["direction"] == "bullish"
                and last_close > rng_lo + 0.8 * width
                and float(vols[-1]) > 1.5 * avg_vol20):
            events.append({"time": int(times[-1]), "type": "sos"})
        if pos < 0.4:
            phase = "accumulation"
        elif pos > 0.6:
            phase = "distribution"
        # volume declining supports accumulation/disribution reading
        if vol_declining and phase == "accumulation":
            pass
        elif not vol_declining and phase != "none":
            phase = phase  # keep positional reading
    else:
        structure = smc_result.get("structureEvents") or []
        if structure:
            phase = "markup" if structure[-1]["direction"] == "bullish" else "markdown"

    # dedupe events by (time, type), keep last 4
    seen = set()
    uniq = []
    for e in events:
        key = (e["time"], e["type"])
        if key not in seen:
            seen.add(key)
            uniq.append(e)
    return {"phase": phase, "events": uniq[-4:]}
