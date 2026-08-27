import asyncio

from fastapi import APIRouter, HTTPException, Query

from services import kline_cache
from services.analysis.context import (
    ALLOWED_INTERVALS,
    NoKlinesError,
    run_analysis,
)

router = APIRouter(prefix="/api")


@router.get("/klines")
async def get_klines(
    symbol: str = Query(...),
    interval: str = Query(default="1h"),
    limit: int = Query(default=500),
    endTime: int | None = Query(default=None),
):
    """Raw klines only (no analysis) - used by the chart for backward history
    paging. Served from the immutable-bar local cache when covered."""
    if interval not in ALLOWED_INTERVALS:
        raise HTTPException(status_code=400, detail=f"interval must be one of {sorted(ALLOWED_INTERVALS)}")
    if not 100 <= limit <= 1000:
        raise HTTPException(status_code=400, detail="limit must be between 100 and 1000")
    symbol = symbol.upper()
    rows = await kline_cache.get_klines(symbol, interval, limit, end_time=endTime)
    candles = [
        {
            "time": int(r[0]),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),
        }
        for r in rows
    ]
    return {"symbol": symbol, "interval": interval, "candles": candles}


@router.get("/analysis")
async def get_analysis(
    symbol: str = Query(...),
    interval: str = Query(default="1h"),
    limit: int = Query(default=500),
    asOf: int | None = Query(default=None, description="replay mode: decision as of this candle open time (ms)"),
):
    if interval not in ALLOWED_INTERVALS:
        raise HTTPException(status_code=400, detail=f"interval must be one of {sorted(ALLOWED_INTERVALS)}")
    if not 100 <= limit <= 1000:
        raise HTTPException(status_code=400, detail="limit must be between 100 and 1000")
    symbol = symbol.upper()
    try:
        return await run_analysis(symbol, interval, limit, as_of=asOf)
    except NoKlinesError as e:
        raise HTTPException(status_code=404, detail=str(e))
