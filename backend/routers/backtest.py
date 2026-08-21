"""Walk-forward backtest of the decision score: IC and directional hit rate."""
import numpy as np
from fastapi import APIRouter, HTTPException, Query

from routers.analysis import ALLOWED_INTERVALS, _klines_df
from services.analysis import indicators, smc, swings

router = APIRouter(prefix="/api")

WARMUP = 210  # ema200 + rolling windows


def _rank(x: np.ndarray) -> np.ndarray:
    order = np.argsort(x)
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(x) + 1, dtype=float)
    return ranks


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 3:
        return 0.0
    ra = _rank(a)
    rb = _rank(b)
    if np.std(ra) == 0 or np.std(rb) == 0:
        return 0.0
    return float(np.corrcoef(ra, rb)[0, 1])


@router.get("/backtest")
async def get_backtest(
    symbol: str = Query(...),
    interval: str = Query(default="1h"),
    limit: int = Query(default=600),
    horizon: int = Query(default=8),
):
    if interval not in ALLOWED_INTERVALS:
        raise HTTPException(status_code=400, detail=f"interval must be one of {sorted(ALLOWED_INTERVALS)}")
    limit = max(300, min(1000, limit))
    horizon = max(2, min(48, horizon))
    symbol = symbol.upper()

    df = await _klines_df(symbol, interval, limit)
    n = len(df)
    if n <= WARMUP + horizon + 30:
        raise HTTPException(status_code=400, detail="not enough candles for backtest")

    closes = df["close"].to_numpy()
    times = df["time"].to_numpy()
    swing_list = swings.detect_swings(df)
    atr_series = indicators.atr(df, 14)
    smc_result = smc.analyze(df, swing_list, atr_series=atr_series)
    events = smc_result["structureEvents"]

    ema20 = np.array([np.nan if v is None else v for v in indicators.ema(df, 20)])
    ema50 = np.array([np.nan if v is None else v for v in indicators.ema(df, 50)])
    ema200 = np.array([np.nan if v is None else v for v in indicators.ema(df, 200)])
    rsi = np.array([np.nan if v is None else v for v in indicators.rsi(df, 14)])
    adx = np.array([np.nan if v is None else v for v in indicators.adx(df, 14)])
    cvd = np.array([np.nan if v is None else v for v in indicators.cvd(df)])
    atrs = np.array([np.nan if v is None else v for v in atr_series])

    # per-bar structure direction from events (events carry 'index')
    trend_dir = np.zeros(n)
    lo = np.full(n, np.nan)
    hi = np.full(n, np.nan)
    for ev in events:
        sgn = 1 if ev["direction"] == "bullish" else -1
        trend_dir[ev["index"]:] = sgn
        if ev.get("rangeHigh") is not None and ev.get("rangeLow") is not None:
            hi[ev["index"]:] = ev["rangeHigh"]
            lo[ev["index"]:] = ev["rangeLow"]

    with np.errstate(invalid="ignore"):
        span = hi - lo
        pd_pct = np.where(span > 0, (closes - lo) / span, 0.5)
        pd_pct = np.clip(pd_pct, 0.0, 1.0)

        # rolling CVD divergence (window 30)
        price_chg = np.full(n, np.nan)
        price_chg[30:] = closes[30:] / closes[:-30] - 1.0
        cvd_chg = np.full(n, np.nan)
        cvd_chg[30:] = cvd[30:] - cvd[:-30]
        bear_div = (price_chg > 0) & (cvd_chg < 0)
        bull_div = (price_chg < 0) & (cvd_chg > 0)

        trending = adx >= 25
        ema_full_bull = (ema20 > ema50) & (ema50 > ema200)
        ema_full_bear = (ema20 < ema50) & (ema50 < ema200)

        score = np.zeros(n)
        for i in range(WARMUP, n):
            s = 0.0
            tr = bool(trending[i]) if np.isfinite(adx[i]) else False
            w_struct = 30.0 if tr else 10.0
            w_stack = 8.0 if tr else 2.0
            w_rsi = 0.0 if tr else 10.0
            w_pd = 2.0 if tr else 5.0
            w_cvd = 14.0 if tr else 16.0
            w_ext = 0.0
            if trend_dir[i] != 0:
                # trend age decay: stale breakouts carry half weight
                s += w_struct * trend_dir[i]
            if ema_full_bull[i]:
                s += w_stack
            elif ema_full_bear[i]:
                s -= w_stack
            elif ema20[i] > ema50[i]:
                s += w_stack * 0.5
            elif ema20[i] < ema50[i]:
                s -= w_stack * 0.5
            if w_rsi > 0 and np.isfinite(rsi[i]):
                if rsi[i] > 70:
                    s -= w_rsi
                elif rsi[i] < 30:
                    s += w_rsi
            if np.isfinite(pd_pct[i]):
                if pd_pct[i] < 0.45:
                    s += w_pd
                elif pd_pct[i] > 0.55:
                    s -= w_pd
            if bear_div[i]:
                s -= w_cvd
            elif bull_div[i]:
                s += w_cvd
            # trend extension guard
            if np.isfinite(ema20[i]) and np.isfinite(atrs[i]) and atrs[i] > 0:
                ext = (closes[i] - ema20[i]) / atrs[i]
                if ext > 2.5:
                    s -= w_ext
                elif ext < -2.5:
                    s += w_ext
            score[i] = max(-100.0, min(100.0, s))

        # forward returns
        fwd = np.full(n, np.nan)
        fwd[:n - horizon] = closes[horizon:] / closes[:n - horizon] - 1.0

        idx = np.arange(WARMUP, n - horizon)
        valid = idx[np.isfinite(score[idx]) & np.isfinite(fwd[idx])]
        if len(valid) < 30:
            raise HTTPException(status_code=400, detail="not enough valid samples")

        s_vals = score[valid]
        f_vals = fwd[valid]
        ic = _spearman(s_vals, f_vals)
        directional = np.abs(s_vals) >= 15
        if directional.any():
            hits = np.mean(np.sign(s_vals[directional]) == np.sign(f_vals[directional]))
            hit_rate = float(hits)
            dir_samples = int(directional.sum())
        else:
            hit_rate = None
            dir_samples = 0

        series_idx = valid[-120:]
        score_series = [
            {"time": int(times[i]), "score": round(float(score[i]), 1)} for i in series_idx
        ]

    return {
        "symbol": symbol,
        "interval": interval,
        "horizon": horizon,
        "samples": int(len(valid)),
        "directionalSamples": dir_samples,
        "ic": round(ic, 4),
        "hitRate": round(hit_rate, 4) if hit_rate is not None else None,
        "scoreSeries": score_series,
    }
