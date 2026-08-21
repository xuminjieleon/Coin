"""Price action patterns: candlestick signals and classical chart patterns."""
import numpy as np
import pandas as pd


def detect_candle_patterns(df: pd.DataFrame, lookback: int = 60) -> list[dict]:
    """Scan recent candles for engulfing / pin bar / inside bar / star patterns."""
    n = len(df)
    if n < 10:
        return []
    start = max(4, n - lookback)
    opens = df["open"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    times = df["time"].to_numpy()
    out: list[dict] = []
    for i in range(start, n):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        body = abs(c - o)
        rng = h - l + 1e-12
        upper = h - max(o, c)
        lower = min(o, c) - l
        # engulfing
        if i > 0:
            po, pc = opens[i - 1], closes[i - 1]
            pbody = abs(pc - po)
            if pbody > 0 and body > pbody * 1.05 and body > rng * 0.5:
                if c > o and pc < po:
                    out.append({"time": int(times[i]), "index": i, "type": "bullish_engulfing",
                                "direction": "bullish", "price": float(h)})
                elif c < o and pc > po:
                    out.append({"time": int(times[i]), "index": i, "type": "bearish_engulfing",
                                "direction": "bearish", "price": float(l)})
        # pin bar
        if body > 0:
            if lower >= body * 2 and upper <= body and c >= l + rng * 0.6:
                out.append({"time": int(times[i]), "index": i, "type": "bullish_pinbar",
                            "direction": "bullish", "price": float(l)})
            elif upper >= body * 2 and lower <= body and c <= h - rng * 0.6:
                out.append({"time": int(times[i]), "index": i, "type": "bearish_pinbar",
                            "direction": "bearish", "price": float(h)})
        # inside bar
        if i > 0 and h < highs[i - 1] and l > lows[i - 1]:
            out.append({"time": int(times[i]), "index": i, "type": "inside_bar",
                        "direction": "neutral", "price": float(c)})
        # morning / evening star (three-bar)
        if i >= 2:
            c1o, c1c = opens[i - 2], closes[i - 2]
            c2o, c2c = opens[i - 1], closes[i - 1]
            mid1 = (c1o + c1c) / 2
            if abs(c1c - c1o) > 0:
                if c1c < c1o and abs(c2c - c2o) <= abs(c1c - c1o) * 0.5 and c > o and c > mid1:
                    out.append({"time": int(times[i]), "index": i, "type": "morning_star",
                                "direction": "bullish", "price": float(h)})
                elif c1c > c1o and abs(c2c - c2o) <= abs(c1c - c1o) * 0.5 and c < o and c < mid1:
                    out.append({"time": int(times[i]), "index": i, "type": "evening_star",
                                "direction": "bearish", "price": float(l)})
    return out[-12:]


def _conf(diff: float, tol: float) -> float:
    """0.5..1.0 confidence from a normalized distance."""
    if tol <= 0:
        return 0.5
    return max(0.5, min(1.0, 1.0 - 0.5 * diff / tol))


def detect_chart_patterns(df: pd.DataFrame, swings: list[dict], atr_last: float | None) -> list[dict]:
    """Heuristic classical chart patterns from recent swing points."""
    out: list[dict] = []
    n = len(df)
    if n < 30:
        return out
    atr = atr_last if atr_last and atr_last > 0 else float((df["high"] - df["low"]).mean())
    closes = df["close"].to_numpy()
    times = df["time"].to_numpy()
    last_close = float(closes[-1])
    sh = [s for s in swings if s["kind"] == "high"][-5:]
    sl = [s for s in swings if s["kind"] == "low"][-5:]

    # ---- double top / bottom ----
    if len(sh) >= 2:
        h1, h2 = sh[-2], sh[-1]
        tol = 0.6 * atr
        diff = abs(h1["price"] - h2["price"])
        if h2["index"] - h1["index"] >= 5 and diff <= tol:
            between = [s for s in sl if h1["index"] < s["index"] < h2["index"]]
            if between:
                neck = min(between, key=lambda s: s["price"])
                if last_close < neck["price"]:
                    out.append({
                        "type": "double_top", "direction": "bearish",
                        "startTime": int(times[h1["index"]]), "endTime": int(times[h2["index"]]),
                        "confidence": round(_conf(diff, tol), 2),
                        "keyLevel": float(neck["price"]),
                    })
    if len(sl) >= 2:
        l1, l2 = sl[-2], sl[-1]
        tol = 0.6 * atr
        diff = abs(l1["price"] - l2["price"])
        if l2["index"] - l1["index"] >= 5 and diff <= tol:
            between = [s for s in sh if l1["index"] < s["index"] < l2["index"]]
            if between:
                neck = max(between, key=lambda s: s["price"])
                if last_close > neck["price"]:
                    out.append({
                        "type": "double_bottom", "direction": "bullish",
                        "startTime": int(times[l1["index"]]), "endTime": int(times[l2["index"]]),
                        "confidence": round(_conf(diff, tol), 2),
                        "keyLevel": float(neck["price"]),
                    })

    # ---- head & shoulders (and inverse) ----
    if len(sh) >= 3:
        h1, h2, h3 = sh[-3], sh[-2], sh[-1]
        tol = 1.2 * atr
        if (h2["price"] - h1["price"] > 0.5 * atr and h2["price"] - h3["price"] > 0.5 * atr
                and abs(h1["price"] - h3["price"]) <= tol):
            lows_between = [s for s in sl if h1["index"] < s["index"] < h3["index"]]
            if len(lows_between) >= 2:
                neckline = min(s["price"] for s in lows_between)
                if last_close < neckline:
                    out.append({
                        "type": "head_shoulders_top", "direction": "bearish",
                        "startTime": int(times[h1["index"]]), "endTime": int(times[h3["index"]]),
                        "confidence": round(_conf(abs(h1["price"] - h3["price"]), tol), 2),
                        "keyLevel": float(neckline),
                    })
    if len(sl) >= 3:
        l1, l2, l3 = sl[-3], sl[-2], sl[-1]
        tol = 1.2 * atr
        if (l1["price"] - l2["price"] > 0.5 * atr and l3["price"] - l2["price"] > 0.5 * atr
                and abs(l1["price"] - l3["price"]) <= tol):
            highs_between = [s for s in sh if l1["index"] < s["index"] < l3["index"]]
            if len(highs_between) >= 2:
                neckline = max(s["price"] for s in highs_between)
                if last_close > neckline:
                    out.append({
                        "type": "head_shoulders_bottom", "direction": "bullish",
                        "startTime": int(times[l1["index"]]), "endTime": int(times[l3["index"]]),
                        "confidence": round(_conf(abs(l1["price"] - l3["price"]), tol), 2),
                        "keyLevel": float(neckline),
                    })

    # ---- triangles (converging / ascending / descending) ----
    if len(sh) >= 3 and len(sl) >= 3:
        h1, h2, h3 = sh[-3], sh[-2], sh[-1]
        l1, l2, l3 = sl[-3], sl[-2], sl[-1]
        highs_desc = h1["price"] > h2["price"] > h3["price"]
        lows_asc = l1["price"] < l2["price"] < l3["price"]
        flat_tol = 0.5 * atr
        highs_flat = abs(h1["price"] - h3["price"]) <= flat_tol
        lows_flat = abs(l1["price"] - l3["price"]) <= flat_tol
        kind = None
        direction = "neutral"
        if highs_desc and lows_asc:
            kind = "symmetric_triangle"
        elif lows_asc and highs_flat:
            kind = "ascending_triangle"
            direction = "bullish"
        elif highs_desc and lows_flat:
            kind = "descending_triangle"
            direction = "bearish"
        if kind:
            out.append({
                "type": kind, "direction": direction,
                "startTime": int(times[min(h1["index"], l1["index"])]),
                "endTime": int(times[max(h3["index"], l3["index"])]),
                "confidence": 0.6,
                "keyLevel": None,
            })
    return out
