"""Liquidation data.

Real feed endpoints (Binance forceOrders / Gate.io liq orders) need signed
access; Gate.io contract_stats aggregates exactly what desks look at:
long/short liquidation USD size per hour/day — the only free reachable
source for this. We persist that history (services/derivs_store, which
itself falls back to Binance stats for OI/funding/LSR — but liquidation
sizes exist only on Gate) and derive:
  - 24h long/short liquidation totals + ratio
  - 48h hourly series (for the panel chart)
  - percentile of today's total vs the stored ~1y daily distribution
  - estimated liquidation levels (leverage map) from the latest price —
    a locally computed approximation, clearly labelled as such.
When Gate.io is unreachable everything degrades honestly to null except
the leverage map (only needs a price).
"""
from __future__ import annotations

from services import derivs_store, kline_cache

LEVERAGES = (10, 25, 50, 100)


def _sum_usd(points: list[dict], key: str) -> float:
    return sum(p.get(key) or 0.0 for p in points)


async def liquidation_snapshot(symbol: str) -> dict:
    symbol = symbol.upper()
    await derivs_store.ensure_backfill(symbol)

    hourly = derivs_store.hourly_series(symbol, 168)  # last 7 days of 1h bars
    has_feed = any((p.get("longLiqUsd") or 0) + (p.get("shortLiqUsd") or 0) > 0 for p in hourly)
    if has_feed:
        hist24 = hourly[-24:] if len(hourly) >= 24 else hourly
        long_24 = _sum_usd(hist24, "longLiqUsd")
        short_24 = _sum_usd(hist24, "shortLiqUsd")
    else:
        long_24 = short_24 = None  # Gate.io stats unreachable and no alternative free source

    series = [
        {
            "time": int(p["time"]) * 1000,
            "longUsd": p.get("longLiqUsd") or 0.0,
            "shortUsd": p.get("shortLiqUsd") or 0.0,
        }
        for p in hourly[-48:]
    ]

    pctile = None
    total_24 = (long_24 or 0) + (short_24 or 0)
    stats = derivs_store.history_stats(symbol) or {}
    base = stats.get("liqDayPctlBase")
    if base and total_24 > 0 and base.get("max"):
        # rank today's running total against the daily distribution
        pctile = min(100.0, total_24 / base["max"] * 100.0)

    # estimated liquidation levels from the latest hourly mark price
    price = hourly[-1].get("mark") if hourly else None
    if price is None:
        try:
            rows = await kline_cache.get_klines(symbol, "1h", 2)
            if rows:
                price = float(rows[-1][4])
        except Exception:
            price = None
    estimated = []
    if price:
        for lev in LEVERAGES:
            estimated.append({
                "leverage": lev,
                "longLiq": price * (1 - 1.0 / lev),
                "shortLiq": price * (1 + 1.0 / lev),
            })

    if has_feed:
        note = ("清算金额为 Gate.io 合约统计口径（每小时清算的 USD 名义值，按小时累计为24h）。"
                "估算强平位为隔离保证金近似（入场价×(1∓1/杠杆)，未计维持保证金），仅供参考。")
    else:
        note = ("Gate.io 合约统计不可达，且无其他免费清算聚合源——多空清算金额暂缺（不编造）。"
                "估算强平位仍可用（隔离保证金近似，未计维持保证金），仅供参考。")

    return {
        "symbol": symbol,
        "long24hUsd": long_24,
        "short24hUsd": short_24,
        "total24hUsd": total_24 if has_feed else None,
        "longShortRatio": (long_24 / short_24) if (has_feed and short_24 and short_24 > 0) else None,
        "percentileVsYear": round(pctile, 1) if pctile is not None else None,
        "history": series,
        "estimated": estimated,
        "price": price,
        "source": "gateio" if has_feed else None,
        "note": note,
    }
