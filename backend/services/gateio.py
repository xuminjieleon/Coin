"""Gate.io futures + options data client (derivatives fallback source).

Works in networks where Binance fapi is blocked but api.gateio.ws is
reachable. Provides OI history, funding, account long/short ratio, taker
ratio, and an options snapshot (ATM IV / put-call ratio).
"""
import httpx

GATE_API = "https://api.gateio.ws"
_TIMEOUT = httpx.Timeout(8.0)


def to_gate_symbol(symbol: str) -> str:
    """BTCUSDT -> BTC_USDT"""
    if symbol.endswith("USDT") and len(symbol) > 4:
        return symbol[:-4] + "_USDT"
    return symbol


async def _get(path: str, params: dict | None = None):
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{GATE_API}{path}", params=params)
        resp.raise_for_status()
        return resp.json()


async def futures_snapshot(symbol: str) -> dict | None:
    """Funding, OI history/value, long/short account ratio, taker ratio."""
    g = to_gate_symbol(symbol)
    tickers = await _get("/api/v4/futures/usdt/tickers", {"contract": g})
    if not tickers:
        return None
    ticker = tickers[0]
    stats = await _get(
        "/api/v4/futures/usdt/contract_stats",
        {"contract": g, "interval": "1h", "limit": 30},
    )
    if not stats:
        return None
    spec = await _get(f"/api/v4/futures/usdt/contracts/{g}")
    mult = float(spec.get("quanto_multiplier") or 1.0)

    mark = float(ticker.get("mark_price") or 0.0)
    funding = float(ticker.get("funding_rate") or 0.0)
    last_oi = float(stats[-1].get("open_interest") or 0.0)
    oi_usd = last_oi * mult * mark
    base = stats[-25] if len(stats) >= 25 else stats[0]
    base_oi = float(base.get("open_interest") or 0.0)
    oi_change = ((last_oi - base_oi) / base_oi * 100.0) if base_oi > 0 else None

    oi_history = [
        {"time": int(s["time"]) * 1000, "value": float(s.get("open_interest") or 0.0) * mult}
        for s in stats
    ]
    ls_hist = []
    for s in stats:
        if s.get("lsr_account"):
            ls_hist.append({"time": int(s["time"]) * 1000, "ratio": float(s["lsr_account"])})
    lsr_account = ls_hist[-1]["ratio"] if ls_hist else None
    lsr_taker = float(stats[-1]["lsr_taker"]) if stats[-1].get("lsr_taker") else None

    return {
        "openInterest": last_oi * mult,
        "openInterestValue": oi_usd,
        "oiChangePct24h": oi_change,
        "oiHistory": oi_history,
        "fundingRate": funding,
        "fundingHistory": None,
        "longShortRatio": lsr_account,
        "longShortHistory": ls_hist,
        "takerBuySellRatio": lsr_taker,
    }


async def options_snapshot(symbol: str) -> dict | None:
    """ATM implied vol (nearest expiry) and put/call OI ratio."""
    g = to_gate_symbol(symbol)
    tickers = await _get("/api/v4/options/tickers", {"underlying": g})
    if not tickers:
        return None
    valid = [t for t in tickers if t.get("mark_iv") is not None and t.get("expiration_time")]
    if not valid:
        return None
    # put/call ratio by position size (open interest)
    call_oi = sum(float(t.get("position_size") or 0.0) for t in valid if t["name"].endswith("-C"))
    put_oi = sum(float(t.get("position_size") or 0.0) for t in valid if t["name"].endswith("-P"))
    pcr = (put_oi / call_oi) if call_oi > 0 else None
    # nearest expiry group
    nearest_exp = min(int(t["expiration_time"]) for t in valid)
    group = [t for t in valid if int(t["expiration_time"]) == nearest_exp]
    calls = [t for t in group if t["name"].endswith("-C") and t.get("delta") is not None]
    puts = [t for t in group if t["name"].endswith("-P") and t.get("delta") is not None]
    atm_iv = None
    if calls and puts:
        call_atm = min(calls, key=lambda t: abs(abs(float(t["delta"])) - 0.5))
        put_atm = min(puts, key=lambda t: abs(abs(float(t["delta"])) - 0.5))
        atm_iv = (float(call_atm["mark_iv"]) + float(put_atm["mark_iv"])) / 2.0
    return {
        "atmIv": atm_iv,
        "putCallRatio": pcr,
        "contracts": len(valid),
        "expiry": nearest_exp,
    }
