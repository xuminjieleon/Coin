"""Technical indicators: EMA, RSI, ATR, ADX, Bollinger bandwidth, CVD. Pure pandas/numpy."""
import numpy as np
import pandas as pd


def ema(df: pd.DataFrame, period: int) -> list:
    s = df["close"].ewm(span=period, adjust=False, min_periods=period).mean()
    return [None if pd.isna(v) else float(v) for v in s]


def rsi(df: pd.DataFrame, period: int = 14) -> list:
    close = df["close"]
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder smoothing == ewm(alpha=1/period, adjust=False)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    out = 100.0 - 100.0 / (1.0 + rs)
    out = out.where(avg_loss != 0, 100.0)
    out = out.where(~(avg_gain.isna()), np.nan)
    return [None if pd.isna(v) else float(v) for v in out]


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> list:
    tr = _true_range(df)
    val = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return [None if pd.isna(v) else float(v) for v in val]


def adx(df: pd.DataFrame, period: int = 14) -> list:
    high = df["high"]
    low = df["low"]
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = _true_range(df)
    tr_s = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100.0 * pd.Series(plus_dm, index=df.index).ewm(
        alpha=1 / period, adjust=False, min_periods=period
    ).mean() / tr_s
    minus_di = 100.0 * pd.Series(minus_dm, index=df.index).ewm(
        alpha=1 / period, adjust=False, min_periods=period
    ).mean() / tr_s
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    val = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return [None if pd.isna(v) else float(v) for v in val]


def bollinger_bandwidth(df: pd.DataFrame, period: int = 20, mult: float = 2.0) -> list:
    """Bandwidth = (upper - lower) / middle, as a fraction."""
    mid = df["close"].rolling(period).mean()
    sd = df["close"].rolling(period).std()
    bw = (2 * mult * sd) / mid
    return [None if pd.isna(v) else float(v) for v in bw]


def cvd(df: pd.DataFrame) -> list:
    """Cumulative Volume Delta from taker buy volume (kline field 9).

    Per-bar delta = 2*takerBuy - volume (aggressive buys minus aggressive sells).
    Returns None list when the source does not provide taker volume.
    """
    if "takerBuy" not in df.columns:
        return [None] * len(df)
    taker = df["takerBuy"].astype(float)
    vol = df["volume"].astype(float)
    delta = (taker * 2.0 - vol).cumsum()
    return [None if pd.isna(v) else float(v) for v in delta]


def cvd_divergence(df: pd.DataFrame, cvd_series: list, window: int = 30) -> dict | None:
    """Detect price/CVD divergence over the recent window.

    bearish: price up over window while CVD down (rally without aggressive buyers).
    bullish: price down while CVD up (selloff absorbed by aggressive buyers).
    """
    n = len(df)
    if n < window + 2 or len(cvd_series) < window + 1:
        return None
    closes = df["close"].to_numpy()
    price_chg = closes[-1] / closes[-1 - window] - 1.0
    cvd_now = cvd_series[-1]
    cvd_then = cvd_series[-1 - window]
    if cvd_now is None or cvd_then is None or abs(price_chg) < 1e-9:
        return None
    cvd_chg = cvd_now - cvd_then
    if abs(cvd_chg) < 1e-9:
        return None
    # strength: |cvd move| in units of per-bar cvd delta std over ~2 windows
    vals = [v for v in cvd_series[-(2 * window + 1):] if v is not None]
    if len(vals) > 2:
        diffs = np.diff(np.array(vals))
        std = float(np.std(diffs))
        if std > 0:
            z = abs(cvd_chg) / (std * np.sqrt(window))
            strength = int(min(100, z * 40))
        else:
            strength = 50
    else:
        strength = 50
    if price_chg > 0 and cvd_chg < 0:
        return {"type": "bearish", "strength": strength}
    if price_chg < 0 and cvd_chg > 0:
        return {"type": "bullish", "strength": strength}
    return None


def _percentile_of_last(series: list, lookback: int = 200) -> float | None:
    vals = [v for v in series[-(lookback + 1):] if v is not None]
    if len(vals) < 10 or vals[-1] is None:
        return None
    last = vals[-1]
    rank = sum(1 for v in vals if v <= last) / len(vals) * 100.0
    return float(rank)


def volatility_state(df: pd.DataFrame, atr_series: list) -> dict:
    """ATR percentile + Bollinger squeeze detection."""
    atr_pct = _percentile_of_last(atr_series, 200)
    bw_series = bollinger_bandwidth(df)
    bw_pct = _percentile_of_last(bw_series, 200)
    if atr_pct is None or bw_pct is None:
        return {"atrPct": None, "bandwidthPct": None, "squeeze": False, "state": "normal"}
    squeeze = bw_pct < 20.0 and atr_pct < 30.0
    if bw_pct < 20.0:
        state = "compressed"
    elif bw_pct > 80.0:
        state = "expanded"
    else:
        state = "normal"
    return {
        "atrPct": atr_pct,
        "bandwidthPct": bw_pct,
        "squeeze": bool(squeeze),
        "state": state,
    }
