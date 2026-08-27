"""Shared analysis context pipeline (klines + prev-day + MTF + derivatives).

Extracted from routers/analysis.py so the hourly WeChat notifier
(services/notifier.py) runs the EXACT same decision path as the API —
single source of truth, zero behavioural drift between the decision card
and pushed messages.
"""

import asyncio

import pandas as pd

from services import binance, derivs_store, kline_cache
from services.analysis import decision, engine

ALLOWED_INTERVALS = {"1h", "4h", "1d", "1w"}

# Higher timeframes used for MTF resonance per current interval.
MTF_MAP = {
    "1h": ["4h", "1d"],
    "4h": ["1d"],
    "1d": [],
    "1w": [],
}

STEP_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}


class NoKlinesError(Exception):
    """Raised when no kline data is available for the request."""


def compute_oi_change_pct(hist: list) -> float | None:
    """OI change % between latest entry and the one ~24h before (1h period, limit 30)."""
    if not hist or len(hist) < 2:
        return None
    latest = float(hist[-1]["sumOpenInterest"])
    idx = -25 if len(hist) >= 25 else 0
    base = float(hist[idx]["sumOpenInterest"])
    if base == 0:
        return None
    return (latest - base) / base * 100.0


def _klines_to_df(raw: list) -> pd.DataFrame:
    rows = []
    for k in raw:
        rows.append({
            "time": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "takerBuy": float(k[9]) if len(k) > 9 and k[9] not in (None, "") else None,
        })
    df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
    return df


async def klines_df(symbol: str, interval: str, limit: int, cache_ttl: float = 0,
                    end_time: int | None = None) -> pd.DataFrame:
    if end_time is not None:
        rows = await kline_cache.get_klines(symbol, interval, limit, end_time=end_time)
        df = kline_cache.rows_to_df(rows)
    else:
        raw = await binance.get_klines(symbol, interval, limit, cache_ttl=cache_ttl)
        if not raw:
            raise NoKlinesError(f"no klines for {symbol}")
        df = _klines_to_df(raw)
    if df.empty:
        raise NoKlinesError(f"no klines for {symbol}")
    return df


async def prev_day_levels(symbol: str, interval: str, as_of_ms: int | None = None) -> dict | None:
    if interval == "1d":
        return None
    try:
        if as_of_ms is not None:
            rows = await kline_cache.get_klines(symbol, "1d", 3, end_time=as_of_ms)
            if len(rows) < 2:
                return None
            prev = rows[-2]
            return {"high": float(prev[2]), "low": float(prev[3])}
        raw = await binance.get_klines(symbol, "1d", 2, cache_ttl=60)
        if len(raw) < 2:
            return None
        prev = raw[-2]
        return {"high": float(prev[2]), "low": float(prev[3])}
    except Exception:
        return None


async def derivatives_context(symbol: str, as_of_ms: int | None = None) -> tuple[float | None, float | None]:
    """(oi_change_pct_24h, funding_rate) for the WEIGHTED funding/OI decision
    components. Live mode: Binance first, Gate.io daily-history fallback.
    Replay mode (as_of set): ONLY as-of fully-closed daily rows — live values
    would be lookahead. (The derivs/macro factor-context chips were removed in
    round 12b after failing the profit-first acceptance bar.)"""
    if as_of_ms is not None:
        try:
            daily = derivs_store.daily_rates(symbol, as_of_ms) or {}
        except Exception:
            daily = {}
        return daily.get("oiChangePct"), daily.get("fundingRate")
    oi_change = None
    funding = None

    async def _oi():
        try:
            hist = await binance.get_open_interest_hist(symbol)
            return compute_oi_change_pct(hist)
        except Exception:
            return None

    async def _funding():
        try:
            premium = await binance.get_premium_index(symbol)
            return float(premium["lastFundingRate"])
        except Exception:
            return None

    oi_change, funding = await asyncio.gather(_oi(), _funding())
    # Gate.io daily-history fallback when Binance live data is missing
    try:
        daily = derivs_store.daily_rates(symbol) or {}
    except Exception:
        daily = {}
    if funding is None:
        funding = daily.get("fundingRate")
    if oi_change is None:
        oi_change = daily.get("oiChangePct")
    return oi_change, funding


async def mtf_context(symbol: str, interval: str, as_of_ms: int | None = None) -> dict:
    """Summaries of higher timeframes for resonance display. In replay mode
    (as_of set) only FULLY CLOSED higher-TF bars are used (no lookahead;
    consistent with the backtest harness)."""
    tf_list = MTF_MAP.get(interval, [])

    async def one_tf(itv: str) -> dict | None:
        try:
            end_time = None
            if as_of_ms is not None:
                # higher-TF bar fully closed by the selected candle's close
                end_time = as_of_ms + STEP_MS[interval] - STEP_MS[itv]
            rows = await kline_cache.get_klines(symbol, itv, 300, end_time=end_time)
            df = kline_cache.rows_to_df(rows)
            if len(df) < 60:
                return None
            full = engine.full_analysis(df)
            closes = df["close"]
            lookback = min(24, len(closes) - 1)
            price_change_pct = None
            if lookback > 0:
                base = float(closes.iloc[-1 - lookback])
                if base > 0:
                    price_change_pct = (float(closes.iloc[-1]) - base) / base * 100.0
            summary = decision.build_summary(
                last_close=float(closes.iloc[-1]),
                smc=full["smc"],
                indicators=full["indicators"],
                volume_profile=full["volumeProfile"],
                wyckoff=full["wyckoff"],
                volatility=full["volatility"],
                cvd_div=full["cvdDivergence"],
                price_change_pct=price_change_pct,
                atr=next((v for v in reversed(full["indicators"]["atr14"]) if v is not None), None),
                interval=itv,
            )
            return {
                "interval": itv,
                "score": summary["score"],
                "bias": summary["bias"],
                "regime": summary["regime"],
                "cvdDiv": (full["cvdDivergence"] or {}).get("type"),
            }
        except Exception:
            return None

    results = await asyncio.gather(*[one_tf(i) for i in tf_list])
    summaries = [r for r in results if r]
    biases = [s["bias"] for s in summaries if s["bias"] != "neutral"]
    if not summaries:
        alignment = "none"
    elif biases and all(b == biases[0] for b in biases):
        alignment = "aligned"
    elif "bullish" in biases and "bearish" in biases:
        alignment = "conflict"
    else:
        alignment = "mixed"
    return {"list": summaries, "alignment": alignment}


async def run_analysis(symbol: str, interval: str, limit: int = 500,
                       as_of: int | None = None) -> dict:
    """Full analysis response (identical shape to GET /api/analysis). Raises
    NoKlinesError when data is missing."""
    df_task = klines_df(symbol, interval, limit, end_time=as_of)
    prev_day_task = prev_day_levels(symbol, interval, as_of_ms=as_of)
    mtf_task = mtf_context(symbol, interval, as_of_ms=as_of)
    deriv_task = derivatives_context(symbol, as_of_ms=as_of)
    df, prev_day, mtf, (oi_change, funding) = await asyncio.gather(
        df_task, prev_day_task, mtf_task, deriv_task
    )
    if len(df) < 60:
        raise NoKlinesError(f"no klines for {symbol} before asOf")

    full = engine.full_analysis(df, prev_day)
    closes = df["close"]
    lookback = min(24, len(closes) - 1)
    price_change_pct = None
    if lookback > 0:
        base = float(closes.iloc[-1 - lookback])
        if base > 0:
            price_change_pct = (float(closes.iloc[-1]) - base) / base * 100.0

    summary = decision.build_summary(
        last_close=float(closes.iloc[-1]),
        smc=full["smc"],
        indicators=full["indicators"],
        volume_profile=full["volumeProfile"],
        oi_change_pct=oi_change,
        price_change_pct=price_change_pct,
        funding_rate=funding,
        wyckoff=full["wyckoff"],
        volatility=full["volatility"],
        cvd_div=full["cvdDivergence"],
        mtf=mtf["list"],
        atr=next((v for v in reversed(full["indicators"]["atr14"]) if v is not None), None),
        interval=interval,
    )

    candles = [
        {
            "time": int(r.time),
            "open": float(r.open),
            "high": float(r.high),
            "low": float(r.low),
            "close": float(r.close),
            "volume": float(r.volume),
        }
        for r in df.itertuples()
    ]
    return {
        "symbol": symbol,
        "interval": interval,
        "candles": candles,
        "smc": full["smc"],
        "indicators": full["indicators"],
        "volumeProfile": full["volumeProfile"],
        "wyckoff": full["wyckoff"],
        "volatility": full["volatility"],
        "cvdDivergence": full["cvdDivergence"],
        "mtf": mtf,
        "summary": summary,
        "replay": {"asOf": as_of} if as_of is not None else None,
    }
