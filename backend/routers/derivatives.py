from fastapi import APIRouter, HTTPException, Query

from services import binance, gateio

router = APIRouter(prefix="/api")


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
    """Fill `result` from Binance futures API. Returns True if any field set."""
    ok = False
    try:
        hist = await binance.get_open_interest_hist(symbol)
        if hist:
            result["openInterest"] = float(hist[-1]["sumOpenInterest"])
            result["openInterestValue"] = float(hist[-1]["sumOpenInterestValue"])
            result["oiChangePct24h"] = compute_oi_change_pct(hist)
            result["oiHistory"] = [
                {"time": int(h["timestamp"]), "value": float(h["sumOpenInterest"])} for h in hist
            ]
            ok = True
    except (Exception, HTTPException):
        pass

    try:
        premium = await binance.get_premium_index(symbol)
        result["fundingRate"] = float(premium["lastFundingRate"])
        ok = True
    except (Exception, HTTPException):
        pass

    try:
        fhist = await binance.get_funding_rate_hist(symbol)
        result["fundingHistory"] = [
            {"time": int(f["fundingTime"]), "rate": float(f["fundingRate"])} for f in fhist
        ]
        ok = True
    except (Exception, HTTPException):
        pass

    try:
        lsr = await binance.get_long_short_ratio(symbol)
        if lsr:
            result["longShortRatio"] = float(lsr[-1]["longShortRatio"])
            result["longShortHistory"] = [
                {"time": int(r["timestamp"]), "ratio": float(r["longShortRatio"])} for r in lsr
            ]
            ok = True
    except (Exception, HTTPException):
        pass

    try:
        taker = await binance.get_taker_ratio(symbol)
        if taker:
            result["takerBuySellRatio"] = float(taker[-1]["buySellRatio"])
            ok = True
    except (Exception, HTTPException):
        pass
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
        "source": None,
        "options": None,
    }

    binance_ok = await _binance_derivatives(symbol, result)
    if binance_ok:
        result["source"] = "binance"
    else:
        # Gate.io fallback (Binance futures API unreachable in this network)
        try:
            snap = await gateio.futures_snapshot(symbol)
            if snap:
                for k, v in snap.items():
                    if v is not None:
                        result[k] = v
                result["source"] = "gateio"
        except Exception:
            pass

    # Options snapshot from Gate.io (best effort, majors only)
    try:
        opt = await gateio.options_snapshot(symbol)
        if opt:
            result["options"] = opt
    except Exception:
        pass

    return result
