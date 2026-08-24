"""Gate.io futures + options data client (derivatives fallback source).

Works in networks where Binance fapi is blocked but api.gateio.ws is
reachable. Provides OI history, funding, account long/short ratio, taker
ratio, an options snapshot (ATM IV / put-call ratio / RR25 / term
structure / max pain), the aggregated order book and contract_stats
history (incl. liquidation USD sizes) for persistence.
"""
import time

import httpx

GATE_API = "https://api.gateio.ws"
_TIMEOUT = httpx.Timeout(8.0)

# contract multiplier cache: symbol -> (expires, multiplier)
_mult_cache: dict[str, tuple[float, float]] = {}
_MULT_TTL = 3600.0


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


async def contract_multiplier(symbol: str) -> float:
    """Quanto multiplier (USD per contract) for a USDT-margined perp."""
    cached = _mult_cache.get(symbol)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    g = to_gate_symbol(symbol)
    spec = await _get(f"/api/v4/futures/usdt/contracts/{g}")
    mult = float(spec.get("quanto_multiplier") or 1.0)
    _mult_cache[symbol] = (time.monotonic() + _MULT_TTL, mult)
    return mult


async def order_book(symbol: str, limit: int = 100) -> dict | None:
    """Aggregated futures order book snapshot: {bids:[[p,s]...], asks:[[p,s]...]}."""
    g = to_gate_symbol(symbol)
    data = await _get(
        "/api/v4/futures/usdt/order_book",
        {"contract": g, "limit": limit, "with_agg": "true"},
    )
    bids = [[float(l["p"]), float(l["s"])] for l in data.get("bids") or []]
    asks = [[float(l["p"]), float(l["s"])] for l in data.get("asks") or []]
    if not bids or not asks:
        return None
    return {"bids": bids, "asks": asks, "ts": int(float(data.get("current") or 0) * 1000)}


async def contract_stats(symbol: str, interval: str = "1h", limit: int = 30) -> list:
    """Raw contract stats history (OI / funding / LSR / liquidation USD sizes)."""
    g = to_gate_symbol(symbol)
    return await _get(
        "/api/v4/futures/usdt/contract_stats",
        {"contract": g, "interval": interval, "limit": limit},
    )


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
    top_lsr = float(stats[-1]["top_lsr_size"]) if stats[-1].get("top_lsr_size") else None
    funding_hist = [
        {"time": int(s["time"]) * 1000, "rate": float(s["last_funding_rate"])}
        for s in stats if s.get("last_funding_rate") not in (None, "")
    ]

    return {
        "openInterest": last_oi * mult,
        "openInterestValue": oi_usd,
        "oiChangePct24h": oi_change,
        "oiHistory": oi_history,
        "fundingRate": funding,
        "fundingHistory": funding_hist or None,
        "longShortRatio": lsr_account,
        "longShortHistory": ls_hist,
        "takerBuySellRatio": lsr_taker,
        "topTraderRatio": top_lsr,
    }


async def options_snapshot(symbol: str) -> dict | None:
    """Options surface snapshot: ATM IV + 25-delta risk reversal per expiry,
    term structure, per-expiry put/call OI ratio and max pain (nearest
    expiry with real open interest)."""
    g = to_gate_symbol(symbol)
    tickers = await _get("/api/v4/options/tickers", {"underlying": g})
    if not tickers:
        return None
    valid = [t for t in tickers if t.get("mark_iv") is not None and t.get("expiration_time")]
    if not valid:
        return None

    def _f(t, k):
        return float(t[k]) if t.get(k) not in (None, "") else None

    def _abs_delta(t) -> float | None:
        d = _f(t, "delta")
        return abs(d) if d is not None else None

    # group by expiry
    by_exp: dict[int, list] = {}
    for t in valid:
        by_exp.setdefault(int(t["expiration_time"]), []).append(t)
    expiries = sorted(by_exp)

    def _pair_iv(group: list, target_delta: float):
        calls = [t for t in group if t["name"].endswith("-C") and _abs_delta(t) is not None]
        puts = [t for t in group if t["name"].endswith("-P") and _abs_delta(t) is not None]
        if not calls or not puts:
            return None
        c = min(calls, key=lambda t: abs(_abs_delta(t) - target_delta))
        p = min(puts, key=lambda t: abs(_abs_delta(t) - target_delta))
        return float(c["mark_iv"]), float(p["mark_iv"])

    term = []
    for exp in expiries:
        group = by_exp[exp]
        atm = _pair_iv(group, 0.5)
        rr = _pair_iv(group, 0.25)
        call_oi = sum(float(t.get("position_size") or 0.0) for t in group if t["name"].endswith("-C"))
        put_oi = sum(float(t.get("position_size") or 0.0) for t in group if t["name"].endswith("-P"))
        term.append({
            "expiry": exp,
            "atmIv": (atm[0] + atm[1]) / 2 if atm else None,
            "rr25": (rr[0] - rr[1]) if rr else None,  # call IV - put IV
            "putOi": put_oi,
            "callOi": call_oi,
            "pcr": (put_oi / call_oi) if call_oi > 0 else None,
        })

    # aggregate PCR across all expiries
    call_oi_all = sum(t["callOi"] for t in term)
    put_oi_all = sum(t["putOi"] for t in term)
    pcr = (put_oi_all / call_oi_all) if call_oi_all > 0 else None

    # max pain: strike minimizing total option payouts at expiry, nearest
    # expiry that has non-trivial OI
    max_pain = None
    for exp in expiries:
        group = by_exp[exp]
        if sum(float(t.get("position_size") or 0.0) for t in group) <= 0:
            continue
        strikes = set()
        calls, puts = [], []
        for t in group:
            strike = float(t["name"].split("-")[-2])
            oi = float(t.get("position_size") or 0.0)
            strikes.add(strike)
            if t["name"].endswith("-C"):
                calls.append((strike, oi))
            else:
                puts.append((strike, oi))
        if not calls or not puts:
            continue
        best_k, best_pay = None, None
        for k in sorted(strikes):
            pay = sum(oi * max(0.0, k - ks) for ks, oi in calls) + \
                sum(oi * max(0.0, ks - k) for ks, oi in puts)
            if best_pay is None or pay < best_pay:
                best_k, best_pay = k, pay
        if best_k is not None:
            max_pain = {"expiry": exp, "strike": best_k}
        break

    nearest = term[0] if term else {}
    return {
        "atmIv": nearest.get("atmIv"),
        "putCallRatio": pcr,
        "rr25": nearest.get("rr25"),
        "termStructure": term,
        "maxPain": max_pain,
        "contracts": len(valid),
        "expiry": expiries[0],
    }
