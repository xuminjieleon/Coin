import asyncio

from fastapi import APIRouter, HTTPException, Query

from services import binance, derivs_store, gateio

router = APIRouter(prefix="/api")

# strong references to background backfill tasks (prevent mid-run GC)
_bg_tasks: set = set()


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


async def _binance_derivatives(symbol: str, result: dict) -> bool:
    """Fill `result` from Binance futures API. Returns True if any field set.

    The five endpoints are independent -> fetched concurrently (they used to
    be sequential awaits; with per-request failover timeouts that alone could
    stack 10s+ when the futures host is slow or blocked)."""
    ok = False

    async def oi():
        try:
            hist = await binance.get_open_interest_hist(symbol)
            return hist
        except (Exception, HTTPException):
            return None

    async def premium():
        try:
            return await binance.get_premium_index(symbol)
        except (Exception, HTTPException):
            return None

    async def funding_hist():
        try:
            return await binance.get_funding_rate_hist(symbol)
        except (Exception, HTTPException):
            return None

    async def lsr():
        try:
            return await binance.get_long_short_ratio(symbol)
        except (Exception, HTTPException):
            return None

    async def taker():
        try:
            return await binance.get_taker_ratio(symbol)
        except (Exception, HTTPException):
            return None

    hist, premium, fhist, lsr_hist, taker = await asyncio.gather(
        oi(), premium(), funding_hist(), lsr(), taker()
    )

    if hist:
        result["openInterest"] = float(hist[-1]["sumOpenInterest"])
        result["openInterestValue"] = float(hist[-1]["sumOpenInterestValue"])
        result["oiChangePct24h"] = compute_oi_change_pct(hist)
        result["oiHistory"] = [
            {"time": int(h["timestamp"]), "value": float(h["sumOpenInterest"])} for h in hist
        ]
        ok = True

    if premium:
        result["fundingRate"] = float(premium["lastFundingRate"])
        ok = True

    if fhist:
        result["fundingHistory"] = [
            {"time": int(f["fundingTime"]), "rate": float(f["fundingRate"])} for f in fhist
        ]
        ok = True

    if lsr_hist:
        result["longShortRatio"] = float(lsr_hist[-1]["longShortRatio"])
        result["longShortHistory"] = [
            {"time": int(r["timestamp"]), "ratio": float(r["longShortRatio"])} for r in lsr_hist
        ]
        ok = True

    if taker:
        result["takerBuySellRatio"] = float(taker[-1]["buySellRatio"])
        ok = True
    return ok


@router.get("/derivatives")
async def get_derivatives(symbol: str = Query(...)):
    symbol = symbol.upper()
    result = {
        "openInterest": None,
        "openInterestValue": None,
        "oiChangePct24h": None,
        "oiHistory": None,
        "fundingRate": None,
        "fundingHistory": None,
        "longShortRatio": None,
        "longShortHistory": None,
        "takerBuySellRatio": None,
        "topTraderRatio": None,
        "historyStats": None,
        "source": None,
        "options": None,
    }

    binance_ok = await _binance_derivatives(symbol, result)
    if binance_ok:
        result["source"] = "binance"

    # Gate.io fallback and the options snapshot are independent -> fetch
    # them concurrently instead of sequentially.
    async def gate_fallback():
        if binance_ok:
            return
        try:
            snap = await gateio.futures_snapshot(symbol)
            if snap:
                for k, v in snap.items():
                    if v is not None:
                        result[k] = v
                result["source"] = "gateio"
        except Exception:
            pass

    async def gate_options():
        try:
            opt = await gateio.options_snapshot(symbol)
            if opt:
                result["options"] = opt
        except Exception:
            pass

    await asyncio.gather(gate_fallback(), gate_options())

    # Persist snapshot inline (fast local write), then percentile context
    # from what is already stored. The network backfill (Gate.io
    # contract_stats 1d x1000 + 1h x720 on first sight, 6h incremental
    # after) runs as a background task so the response is NOT blocked by
    # it — historyStats catches up on the next refresh pass.
    try:
        derivs_store.record_snapshot(symbol, result["source"], result)
        stats = derivs_store.history_stats(symbol)
        if stats:
            result["historyStats"] = stats
    except Exception:
        pass

    async def _background_backfill():
        try:
            await derivs_store.ensure_backfill(symbol)
        except Exception:
            pass
        finally:
            _bg_tasks.discard(task)

    task = asyncio.create_task(_background_backfill())
    _bg_tasks.add(task)

    return result
