"""Binance futures API async client.

Failover strategy: always try the official fapi.binance.com first; when it is
unreachable, fall back to the public market-data mirror (data-api.binance.vision)
for klines/exchangeInfo only. Futures-only endpoints (OI, funding, ratios) have
no mirror and raise 502, which routers convert to null fields.

Hosts that fail consecutively are marked down for a cooldown window so repeated
requests fail fast (short timeout) instead of stalling 10s every time.
"""
import time
from typing import Any

import httpx
from fastapi import HTTPException

from config import BINANCE_FAPI, BINANCE_SPOT_MIRROR

# Short timeout for the primary attempt: fast failover when the official host
# is blocked (connection RST/timeout). The mirror gets a more generous budget.
_PRIMARY_TIMEOUT = httpx.Timeout(4.0)
_FALLBACK_TIMEOUT = httpx.Timeout(12.0)

# mirror fallback paths for market-data endpoints (official host first,
# mirror only when the official host is unreachable — adapts to any network)
_FALLBACK_PATHS = {
    "/fapi/v1/klines": "/api/v3/klines",
    "/fapi/v1/exchangeInfo": "/api/v3/exchangeInfo",
    "/fapi/v1/ticker/24hr": "/api/v3/ticker/24hr",
}

# mirror-only market-data endpoints (no fapi equivalent reachable)
_MIRROR_ONLY = {
    "/api/v3/depth": True,
    "/api/v3/ticker/24hr": True,
}


async def get_mirror_json(path: str, params: dict | None = None) -> Any:
    """Fetch a mirror-only market-data endpoint (spot depth / 24h tickers)."""
    if path not in _MIRROR_ONLY:
        raise ValueError(f"path not allowed on mirror: {path}")
    url = f"{BINANCE_SPOT_MIRROR}{path}"
    if _host_down(BINANCE_SPOT_MIRROR):
        raise HTTPException(status_code=502, detail="Binance mirror in cooldown")
    try:
        data = await _fetch(url, params, _FALLBACK_TIMEOUT)
        _mark_host_ok(BINANCE_SPOT_MIRROR)
        return data
    except Exception as exc:  # noqa: BLE001
        _mark_host_down(BINANCE_SPOT_MIRROR)
        raise HTTPException(status_code=502, detail=f"Binance mirror failed: {exc}") from exc

# host -> monotonic timestamp after which it may be retried
_host_down_until: dict[str, float] = {}
_HOST_COOLDOWN = 300.0  # seconds


def _host_down(host: str) -> bool:
    return _host_down_until.get(host, 0.0) > time.monotonic()


def _mark_host_down(host: str) -> None:
    _host_down_until[host] = time.monotonic() + _HOST_COOLDOWN


def _mark_host_ok(host: str) -> None:
    _host_down_until.pop(host, None)


# cache: key -> (expires_at_epoch, data)
_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry and entry[0] > time.time():
        return entry[1]
    return None


def _cache_set(key: str, data: Any, ttl: float) -> None:
    _cache[key] = (time.time() + ttl, data)


async def _fetch(url: str, params: dict | None, timeout: httpx.Timeout) -> Any:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


async def _get(path: str, params: dict | None = None, cache_ttl: float = 0) -> Any:
    url = f"{BINANCE_FAPI}{path}"
    cache_key = f"{url}?{params}" if params else url
    if cache_ttl > 0:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    data: Any = None
    primary_err: Exception | None = None

    # 1) Official host first (skipped entirely while marked down).
    if not _host_down(BINANCE_FAPI):
        try:
            data = await _fetch(url, params, _PRIMARY_TIMEOUT)
            _mark_host_ok(BINANCE_FAPI)
        except Exception as exc:  # noqa: BLE001 - failover needs broad catch
            primary_err = exc
            _mark_host_down(BINANCE_FAPI)

    # 2) Mirror fallback for market-data paths only.
    if data is None:
        fallback = _FALLBACK_PATHS.get(path)
        if fallback is not None and not _host_down(BINANCE_SPOT_MIRROR):
            try:
                data = await _fetch(f"{BINANCE_SPOT_MIRROR}{fallback}", params, _FALLBACK_TIMEOUT)
                _mark_host_ok(BINANCE_SPOT_MIRROR)
            except Exception as exc:  # noqa: BLE001
                _mark_host_down(BINANCE_SPOT_MIRROR)
                if primary_err is not None:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Binance request failed: {primary_err}; mirror failed: {exc}",
                    ) from exc
                raise HTTPException(status_code=502, detail=f"Binance mirror failed: {exc}") from exc

    if data is None:
        if primary_err is not None:
            raise HTTPException(status_code=502, detail=f"Binance request failed: {primary_err}")
        raise HTTPException(status_code=502, detail="Binance hosts unreachable (cooldown)")

    if cache_ttl > 0:
        _cache_set(cache_key, data, cache_ttl)
    return data


async def get_klines(
    symbol: str,
    interval: str,
    limit: int = 500,
    cache_ttl: float = 0,
    end_time: int | None = None,
) -> list:
    params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
    if end_time is not None:
        params["endTime"] = end_time
    return await _get("/fapi/v1/klines", params, cache_ttl=cache_ttl)


async def get_exchange_info() -> dict:
    return await _get("/fapi/v1/exchangeInfo", cache_ttl=3600)


async def get_open_interest_hist(symbol: str, period: str = "1h", limit: int = 30) -> list:
    return await _get("/futures/data/openInterestHist", {"symbol": symbol, "period": period, "limit": limit})


async def get_premium_index(symbol: str) -> dict:
    return await _get("/fapi/v1/premiumIndex", {"symbol": symbol})


async def get_funding_rate_hist(symbol: str, limit: int = 30) -> list:
    return await _get("/fapi/v1/fundingRate", {"symbol": symbol, "limit": limit})


async def get_long_short_ratio(symbol: str, period: str = "1h", limit: int = 30) -> list:
    return await _get("/futures/data/globalLongShortAccountRatio", {"symbol": symbol, "period": period, "limit": limit})


async def get_taker_ratio(symbol: str, period: str = "1h", limit: int = 30) -> list:
    return await _get("/futures/data/takerlongshortRatio", {"symbol": symbol, "period": period, "limit": limit})


async def get_ticker24h() -> list:
    """24h tickers for all symbols: official futures first, spot mirror fallback."""
    return await _get("/fapi/v1/ticker/24hr", cache_ttl=120)


async def get_depth(symbol: str, limit: int = 100, allow_mirror: bool = True) -> tuple[dict, str]:
    """Order book snapshot with origin: official USDT-M futures depth first,
    then (optionally) the spot mirror. Returns (book, source) where source is
    'binance_perp' | 'binance_spot'; sizes are in base-coin units for both.
    Callers that rank another perp source above the spot mirror pass
    allow_mirror=False and handle the fallback themselves."""
    params = {"symbol": symbol.upper(), "limit": limit}
    if not _host_down(BINANCE_FAPI):
        try:
            data = await _fetch(f"{BINANCE_FAPI}/fapi/v1/depth", params, _PRIMARY_TIMEOUT)
            _mark_host_ok(BINANCE_FAPI)
            return data, "binance_perp"
        except Exception:  # noqa: BLE001 - fall through to the mirror
            _mark_host_down(BINANCE_FAPI)
    if allow_mirror and not _host_down(BINANCE_SPOT_MIRROR):
        try:
            data = await _fetch(f"{BINANCE_SPOT_MIRROR}/api/v3/depth", params, _FALLBACK_TIMEOUT)
            _mark_host_ok(BINANCE_SPOT_MIRROR)
            return data, "binance_spot"
        except Exception as exc:  # noqa: BLE001
            _mark_host_down(BINANCE_SPOT_MIRROR)
            raise HTTPException(status_code=502, detail=f"Binance depth sources failed: {exc}") from exc
    raise HTTPException(status_code=502, detail="Binance depth sources unreachable (cooldown)")


def host_status() -> dict:
    """Live cooldown state per host (for /api/sources diagnostics)."""
    out = {}
    now = time.monotonic()
    for host in (BINANCE_FAPI, BINANCE_SPOT_MIRROR):
        until = _host_down_until.get(host, 0.0)
        out[host] = {
            "down": until > now,
            "retryInS": round(max(0.0, until - now)) if until > now else 0,
        }
    return out
