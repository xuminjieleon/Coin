"""Binance futures API async client.

Failover strategy: always try the official fapi.binance.com first; when it is
unreachable, retry the same host through the Windows system proxy when one
is configured (VPN clients — see services/sysproxy.py), then fall back to
the public market-data mirror (data-api.binance.vision) for
klines/exchangeInfo only. Futures-only endpoints (OI, funding, ratios) have
no mirror; the proxy link is what restores them on VPN'd networks.

Hosts that fail consecutively are marked down for a cooldown window so repeated
requests fail fast (short timeout) instead of stalling 10s every time.
"""
import time
from typing import Any

import httpx
from fastapi import HTTPException

from config import BINANCE_FAPI, BINANCE_SPOT_MIRROR
from services import sysproxy

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

# separate cooldown key for the fapi-via-system-proxy link (independent of
# the direct host: a down direct route must not block the proxy attempt and
# vice versa)
_FAPI_PROXY_KEY = f"{BINANCE_FAPI}|sysproxy"


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


# Shared keep-alive client: one TCP+TLS handshake per host instead of per
# request (the enterprise network's handshake costs 1-2s each, which made
# sequential derivative calls take 10s+ before pooling). Per-request
# timeouts still apply; connections are pooled across requests.
# keepalive_expiry drops idle sockets before the server does — without it a
# long-running process can wedge on server-closed keep-alive sockets
# (observed 2026-08-27: 1.5-day-old process failing on hosts a fresh
# process reached fine). Do NOT revert to per-request clients; the fresh
# client below is an error-path recovery only.
_POOL_KEEPALIVE = 60.0
_client: httpx.AsyncClient | None = None


def _new_pool() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=_PRIMARY_TIMEOUT,
        limits=httpx.Limits(keepalive_expiry=_POOL_KEEPALIVE),
    )


def _shared_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = _new_pool()
    return _client


async def _swap_client(fresh: httpx.AsyncClient) -> None:
    """Adopt `fresh` as the shared pool, closing the previous one."""
    global _client
    old, _client = _client, fresh
    if old is not None and not old.is_closed:
        try:
            await old.aclose()
        except Exception:  # noqa: BLE001
            pass


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def _fetch(url: str, params: dict | None, timeout: httpx.Timeout) -> Any:
    try:
        resp = await _shared_client().get(url, params=params, timeout=timeout)
    except httpx.ConnectTimeout:
        raise  # never connected — host genuinely unreachable, not a stale pool
    except httpx.TransportError:
        # A pooled socket may have been closed by the server while idle.
        # Retry once on a brand-new connection; if that works the old pool
        # was stale, so adopt the fresh client for subsequent requests.
        fresh = _new_pool()
        try:
            resp = await fresh.get(url, params=params, timeout=timeout)
        except Exception:  # noqa: BLE001
            await fresh.aclose()
            raise
        await _swap_client(fresh)
    resp.raise_for_status()
    return resp.json()


async def _fapi_via_proxy(path: str, params: dict | None) -> Any:
    """Official fapi endpoint through the Windows system proxy (VPN link).

    Sits between the direct official attempt and the mirror/other-source
    fallbacks: restores the futures-only endpoints (premiumIndex,
    futures/data/*) when only the proxied route reaches fapi. Any failure
    (incl. HTTP status — VPN exit may be geo-blocked) cools this link down
    for 300s; returns None so callers fall through to the next source.
    """
    if _host_down(_FAPI_PROXY_KEY):
        return None
    try:
        return await sysproxy.fetch_json(f"{BINANCE_FAPI}{path}", params=params, timeout=6.0)
    except Exception:  # noqa: BLE001 - proxy absent/broken/geo-blocked
        _mark_host_down(_FAPI_PROXY_KEY)
        return None


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

    # 1b) Same official host via the system proxy (VPN) — no-op when no
    #     proxy is configured (ProxyUnavailable -> None instantly).
    if data is None:
        data = await _fapi_via_proxy(path, params)

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
        except Exception:  # noqa: BLE001 - fall through to the proxy attempt
            _mark_host_down(BINANCE_FAPI)
    # official perp book via the system proxy (VPN) before other sources
    data = await _fapi_via_proxy("/fapi/v1/depth", params)
    if data is not None:
        return data, "binance_perp"
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
    for host in (BINANCE_FAPI, BINANCE_SPOT_MIRROR, _FAPI_PROXY_KEY):
        until = _host_down_until.get(host, 0.0)
        out[host] = {
            "down": until > now,
            "retryInS": round(max(0.0, until - now)) if until > now else 0,
        }
    return out
