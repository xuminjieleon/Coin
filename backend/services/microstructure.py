"""Order-book microstructure metrics from a snapshot of the book.

Snapshot-based (the app has no real-time push): spread, depth imbalance by
price band, cumulative depth in USD and visible wall detection. Source is
picked by runtime reachability (Binance futures -> Gate.io futures ->
Binance spot mirror); metrics describe the *current resting liquidity*,
not a stream — good enough for "how deep is the book / which side is
heavier / where are the walls" style checks.
"""
from __future__ import annotations

import time

from services import binance, gateio

# in-memory result cache: symbol -> (expires, data)
_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 60.0  # short: order book state matters

BANDS = (0.001, 0.0025, 0.005, 0.01)  # ±0.1% / ±0.25% / ±0.5% / ±1%


def compute_metrics(bids: list, asks: list, mult: float) -> dict:
    """bids/asks: [[price, size_contracts]...] descending/ascending."""
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2.0
    spread_bps = (best_ask - best_bid) / mid * 10_000.0 if mid > 0 else None

    def _usd(price: float, size: float) -> float:
        return price * size * mult

    bands = []
    for band in BANDS:
        lo, hi = mid * (1 - band), mid * (1 + band)
        bid_usd = sum(_usd(p, s) for p, s in bids if p >= lo)
        ask_usd = sum(_usd(p, s) for p, s in asks if p <= hi)
        imb = (bid_usd - ask_usd) / (bid_usd + ask_usd) if (bid_usd + ask_usd) > 0 else None
        bands.append({
            "bandPct": round(band * 100, 2),
            "bidUsd": bid_usd,
            "askUsd": ask_usd,
            "imbalance": round(imb, 4) if imb is not None else None,
        })

    # top-of-book pressure: first 20 levels
    top_bids = bids[:20]
    top_asks = asks[:20]
    tb = sum(_usd(p, s) for p, s in top_bids)
    ta = sum(_usd(p, s) for p, s in top_asks)
    top_imb = (tb - ta) / (tb + ta) if (tb + ta) > 0 else None

    # wall detection within ±1%: single level > 5x median visible level size
    near = [s * mult * p for p, s in bids if p >= mid * 0.99] + \
           [s * mult * p for p, s in asks if p <= mid * 1.01]
    walls = []
    if near and len(near) >= 10:
        near_sorted = sorted(near)
        median = near_sorted[len(near_sorted) // 2]
        if median > 0:
            for side, levels in (("bid", bids), ("ask", asks)):
                for p, s in levels[:60]:
                    usd = _usd(p, s)
                    if usd >= median * 5 and usd > 0:
                        walls.append({
                            "side": side,
                            "price": p,
                            "usd": usd,
                            "distBps": round(abs(p - mid) / mid * 10_000.0, 1),
                        })
        walls = sorted(walls, key=lambda w: -w["usd"])[:5]

    return {
        "mid": mid,
        "bestBid": best_bid,
        "bestAsk": best_ask,
        "spreadBps": round(spread_bps, 2) if spread_bps is not None else None,
        "topImbalance": round(top_imb, 4) if top_imb is not None else None,
        "bands": bands,
        "walls": walls,
        "levels": len(bids) + len(asks),
    }


async def orderbook_snapshot(symbol: str) -> dict:
    """Priority chain (adapts to whatever network this runs in):
      1. Binance USDT-M futures depth (deepest perp book when reachable)
      2. Gate.io USDT perp aggregated book (quanto multiplier -> USD)
      3. Binance spot-mirror depth (spot book, labelled as such)
    Failed hosts enter cooldown so subsequent calls fail fast to the next
    source in the chain."""
    symbol = symbol.upper()
    cached = _cache.get(symbol)
    if cached and cached[0] > time.monotonic():
        return cached[1]

    result: dict = {
        "symbol": symbol, "source": None, "mid": None, "bestBid": None,
        "bestAsk": None, "spreadBps": None, "topImbalance": None,
        "bands": None, "walls": None, "levels": 0, "note": None,
    }

    def _fill(raw: dict, source: str, mult: float = 1.0) -> None:
        bids = [[float(p), float(s)] for p, s in raw["bids"]]
        asks = [[float(p), float(s)] for p, s in raw["asks"]]
        result.update(compute_metrics(bids, asks, mult))
        result["source"] = source

    # 1) Binance official futures depth only (no mirror here — perp sources
    #    rank above any spot book)
    try:
        raw, source = await binance.get_depth(symbol, 100, allow_mirror=False)
        _fill(raw, source)
    except Exception:
        pass
    # 2) Gate.io perp aggregated book
    if result["source"] is None:
        try:
            book = await gateio.order_book(symbol, 100)
            mult = await gateio.contract_multiplier(symbol)
            _fill(book, "gateio_perp", mult)
            result["ts"] = book.get("ts")
        except Exception:
            pass
    # 3) Binance spot mirror (last resort; fapi host is in cooldown by now)
    if result["source"] is None:
        try:
            raw, source = await binance.get_depth(symbol, 100)
            _fill(raw, source)
            result["note"] = "币安合约与 Gate.io 不可达，本快照为现货盘（杠杆盘口可能不同）"
        except Exception:
            result["note"] = "订单簿数据源不可达（币安合约/现货与 Gate.io 均失败）"

    _cache[symbol] = (time.monotonic() + _CACHE_TTL, result)
    return result
