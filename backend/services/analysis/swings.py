"""Swing high/low detection using fractal method (n=2)."""
import pandas as pd

N = 2  # fractal order


def detect_swings(df: pd.DataFrame) -> list[dict]:
    """Return confirmed swing points sorted by index.

    high[i] strictly greater than high[i-2..i-1] and >= high[i+1..i+2] -> swing high.
    Symmetric for swing low. Last N candles never produce swings (unconfirmed).
    Each item: {"index", "time", "price", "kind"} kind in {"high", "low"}.
    """
    swings: list[dict] = []
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    times = df["time"].to_numpy()
    n = len(df)
    for i in range(N, n - N):
        h = highs[i]
        if h > highs[i - 1] and h > highs[i - 2] and h >= highs[i + 1] and h >= highs[i + 2]:
            swings.append({"index": i, "time": int(times[i]), "price": float(h), "kind": "high"})
        lo = lows[i]
        if lo < lows[i - 1] and lo < lows[i - 2] and lo <= lows[i + 1] and lo <= lows[i + 2]:
            swings.append({"index": i, "time": int(times[i]), "price": float(lo), "kind": "low"})
    swings.sort(key=lambda s: s["index"])
    return swings
