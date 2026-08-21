from fastapi import APIRouter, Query

from services import binance

router = APIRouter(prefix="/api")


@router.get("/symbols")
async def list_symbols(q: str | None = Query(default=None)):
    info = await binance.get_exchange_info()
    query = (q or "").strip().lower()
    out = []
    for s in info.get("symbols", []):
        # spot exchangeInfo entries have no contractType field (mirror fallback)
        if s.get("contractType", "PERPETUAL") != "PERPETUAL":
            continue
        if s.get("quoteAsset") != "USDT":
            continue
        if s.get("status") != "TRADING":
            continue
        symbol = s.get("symbol", "")
        base = s.get("baseAsset", "")
        if query and query not in symbol.lower() and query not in base.lower():
            continue
        out.append({"symbol": symbol, "base": base})
        if len(out) >= 50:
            break
    return out
