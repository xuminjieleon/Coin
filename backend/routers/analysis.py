import asyncio

import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from routers.derivatives import compute_oi_change_pct
from services import binance
from services.analysis import decision, engine

router = APIRouter(prefix="/api")

ALLOWED_INTERVALS = {"15m", "1h", "4h", "1d"}

# Higher timeframes used for MTF resonance per current interval.
MTF_MAP = {
    "15m": ["1h", "4h"],
    "1h": ["4h", "1d"],
    "4h": ["1d"],
    "1d": [],
}


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


async def _klines_df(symbol: str, interval: str, limit: int, cache_ttl: float = 0) -> pd.DataFrame:
    raw = await binance.get_klines(symbol, interval, limit, cache_ttl=cache_ttl)
    if not raw:
        raise HTTPException(status_code=404, detail=f"no klines for {symbol}")
    return _klines_to_df(raw)


async def _prev_day_levels(symbol: str, interval: str) -> dict | None:
    if interval == "1d":
        return None
    try:
        raw = await binance.get_klines(symbol, "1d", 2, cache_ttl=60)
        if len(raw) < 2:
            return None
        prev = raw[-2]
        return {"high": float(prev[2]), "low": float(prev[3])}
    except Exception:
        return None


async def _derivatives_context(symbol: str) -> tuple[float | None, float | None]:
    """Return (oi_change_pct_24h, funding_rate); None on failure."""
    oi_change = None
    funding = None
    try:
        hist = await binance.get_open_interest_hist(symbol)
        oi_change = compute_oi_change_pct(hist)
    except Exception:
        pass
    try:
        premium = await binance.get_premium_index(symbol)
        funding = float(premium["lastFundingRate"])
    except Exception:
        pass
    return oi_change, funding


async def _mtf_context(symbol: str, interval: str) -> dict:
    """Summaries of higher timeframes for resonance display."""
    tf_list = MTF_MAP.get(interval, [])

    async def one_tf(itv: str) -> dict | None:
        try:
            df = await _klines_df(symbol, itv, 300, cache_ttl=60)
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
                patterns=full["patterns"],
                wyckoff=full["wyckoff"],
                volatility=full["volatility"],
                cvd_div=full["cvdDivergence"],
                price_change_pct=price_change_pct,
                atr=next((v for v in reversed(full["indicators"]["atr14"]) if v is not None), None),
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


@router.get("/analysis")
async def get_analysis(
    symbol: str = Query(...),
    interval: str = Query(default="1h"),
    limit: int = Query(default=500),
):
    if interval not in ALLOWED_INTERVALS:
        raise HTTPException(status_code=400, detail=f"interval must be one of {sorted(ALLOWED_INTERVALS)}")
    if not 100 <= limit <= 1000:
        raise HTTPException(status_code=400, detail="limit must be between 100 and 1000")
    symbol = symbol.upper()

    df_task = _klines_df(symbol, interval, limit)
    prev_day_task = _prev_day_levels(symbol, interval)
    mtf_task = _mtf_context(symbol, interval)
    deriv_task = _derivatives_context(symbol)
    df, prev_day, mtf, (oi_change, funding) = await asyncio.gather(
        df_task, prev_day_task, mtf_task, deriv_task
    )

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
        patterns=full["patterns"],
        wyckoff=full["wyckoff"],
        volatility=full["volatility"],
        cvd_div=full["cvdDivergence"],
        mtf=mtf["list"],
        atr=next((v for v in reversed(full["indicators"]["atr14"]) if v is not None), None),
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
        "patterns": full["patterns"],
        "wyckoff": full["wyckoff"],
        "volatility": full["volatility"],
        "cvdDivergence": full["cvdDivergence"],
        "mtf": mtf,
        "summary": summary,
    }
