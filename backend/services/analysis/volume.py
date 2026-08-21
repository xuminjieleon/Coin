"""Volume Profile (VPVR) over recent candles + developing POC series."""
import numpy as np
import pandas as pd

BINS = 48
VALUE_AREA_PCT = 0.70


def _bar_bin_matrix(df: pd.DataFrame, bins: int) -> tuple[np.ndarray, float, float, np.ndarray]:
    """Per-bar volume distribution across a global price grid (n x bins)."""
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    vols = df["volume"].to_numpy().astype(float)
    n = len(df)
    p_hi = float(highs.max())
    p_lo = float(lows.min())
    if p_hi <= p_lo:
        p_hi = p_lo + 1e-9
    width = (p_hi - p_lo) / bins
    mat = np.zeros((n, bins))
    for i in range(n):
        first = max(0, int((lows[i] - p_lo) / width))
        last = min(bins - 1, int((highs[i] - p_lo) / width))
        if last < first:
            first, last = last, first
        covered = last - first + 1
        if covered > 0:
            mat[i, first:last + 1] = vols[i] / covered
    return mat, p_lo, width, times_of(df)


def times_of(df: pd.DataFrame) -> np.ndarray:
    return df["time"].to_numpy()


def volume_profile(df: pd.DataFrame, lookback: int = 300) -> dict:
    d = df.tail(min(len(df), lookback)).reset_index(drop=True)
    price_high = float(d["high"].max())
    price_low = float(d["low"].min())
    if price_high <= price_low:
        price_high = price_low + 1e-9
    edges = np.linspace(price_low, price_high, BINS + 1)
    vols = np.zeros(BINS)
    width = (price_high - price_low) / BINS
    for _, row in d.iterrows():
        lo = float(row["low"])
        hi = float(row["high"])
        v = float(row["volume"])
        first = max(0, int((lo - price_low) / width))
        last = min(BINS - 1, int((hi - price_low) / width))
        if last < first:
            first, last = last, first
        covered = last - first + 1
        share = v / covered if covered > 0 else v
        for b in range(first, last + 1):
            vols[b] += share
    total = vols.sum()
    poc_idx = int(np.argmax(vols))
    poc = float((edges[poc_idx] + edges[poc_idx + 1]) / 2)

    # Value area: expand from POC until covering 70% of total volume
    target = total * VALUE_AREA_PCT
    acc = vols[poc_idx]
    up = poc_idx
    down = poc_idx
    while acc < target and (up < BINS - 1 or down > 0):
        up_vol = vols[up + 1] if up < BINS - 1 else -1.0
        down_vol = vols[down - 1] if down > 0 else -1.0
        if up_vol >= down_vol:
            up += 1
            acc += vols[up]
        else:
            down -= 1
            acc += vols[down]
    vah = float(edges[up + 1])
    val = float(edges[down])

    bins = [
        {"priceLow": float(edges[i]), "priceHigh": float(edges[i + 1]), "volume": float(vols[i])}
        for i in range(BINS)
    ]
    return {"poc": poc, "vah": vah, "val": val, "bins": bins}


def developing_poc_series(df: pd.DataFrame, lookback: int = 300, points: int = 120,
                          bins: int = 48) -> list[dict]:
    """Rolling POC over a trailing window, sampled across the recent bars.

    Vectorized via a cumulative bin-volume matrix: window histogram =
    cum[t] - cum[t - lookback]. Returns [{time, poc}] ascending, latest last.
    """
    n = len(df)
    if n < 30:
        return []
    mat, p_lo, width, times = _bar_bin_matrix(df, bins)
    cum = np.cumsum(mat, axis=0)
    start_t = min(lookback, 30)
    step = max(1, (n - start_t) // max(points, 1))
    out: list[dict] = []
    for t in range(start_t, n, step):
        end = t  # window rows [t-lookback, t)
        begin = max(0, t - lookback)
        window_sum = cum[end - 1] - (cum[begin - 1] if begin >= 1 else 0.0)
        idx = int(np.argmax(window_sum))
        out.append({"time": int(times[t]), "poc": float(p_lo + (idx + 0.5) * width)})
    # always include the newest bar
    end = n
    begin = max(0, n - lookback)
    window_sum = cum[end - 1] - (cum[begin - 1] if begin >= 1 else 0.0)
    idx = int(np.argmax(window_sum))
    last = {"time": int(times[n - 1]), "poc": float(p_lo + (idx + 0.5) * width)}
    if not out or out[-1]["time"] != last["time"]:
        out.append(last)
    return out[-points:]
