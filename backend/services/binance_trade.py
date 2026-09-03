"""Binance USDT-M futures SIGNED REST client (executor only, no market data).

Two bases: production fapi.binance.com and the free testnet
(testnet.binancefuture.com — register there for API keys). Signed requests:
HMAC-SHA256 over the exact query string, X-MBX-APIKEY header. The query
string is built by us (never via httpx params=) so the signature always
matches what is sent.

Route chain mirrors services/binance.py, independently per base:
    direct -> same host via the Windows system proxy (VPN)
Transport failures cool the route for 300s. HTTP 451/403 from a route is a
route block (region), not an API error: the other link is tried before the
request fails. API-level errors ({"code": -xxxx}) raise TradeError.
-1021 (timestamp outside recvWindow) resyncs the clock and retries once.

All mutating order endpoints used by the executor are here; market data
stays in services/binance.py (public, unsigned chain).
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from config import BINANCE_FAPI, BINANCE_FAPI_TESTNET
from services import sysproxy

RECV_WINDOW = 5000
_ROUTE_COOLDOWN = 300.0
_POOL_KEEPALIVE = 60.0
_TIMEOUT = httpx.Timeout(8.0)


class TradeError(Exception):
    """Binance API-level error ({"code": -xxxx, "msg": ...})."""

    def __init__(self, code: int, msg: str):
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg


class RouteBlocked(Exception):
    """All routes (direct + proxy) unreachable or region-blocked."""


# ------------------------------------------------------------------ state

_base: str = BINANCE_FAPI_TESTNET
_key: str = ""
_secret: bytes = b""
_time_offset: int = 0  # server_time - local_time, ms

_clients: dict[str, httpx.AsyncClient] = {}
_proxy_url_now: str | None = None
_down_until: dict[str, float] = {}  # route key -> monotonic deadline
_closed = False  # set by close_client(); blocks every request afterwards


class AmbiguousRequest(Exception):
    """A MUTATING request (POST/DELETE order) hit an ambiguous transport
    error (e.g. ReadTimeout) after possibly being delivered. Callers must
    probe by clientOrderId before assuming the request never happened."""


def configure(base_url: str, api_key: str, api_secret: str) -> None:
    """Point the client at an environment (mainnet or testnet) + keys."""
    global _base, _key, _secret, _time_offset
    _base = base_url.rstrip("/")
    _key = api_key or ""
    _secret = (api_secret or "").encode()
    _time_offset = 0


def using_testnet() -> bool:
    return _base == BINANCE_FAPI_TESTNET


def mainnet_base() -> str:
    return BINANCE_FAPI


def testnet_base() -> str:
    return BINANCE_FAPI_TESTNET


def credentials_set() -> bool:
    return bool(_key and _secret)


async def close_client() -> None:
    global _proxy_url_now, _closed
    _closed = True
    for client in list(_clients.values()):
        if not client.is_closed:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass
    _clients.clear()
    _proxy_url_now = None


def _route_down(route: str) -> bool:
    return _down_until.get(route, 0.0) > time.monotonic()


def _mark_down(route: str) -> None:
    _down_until[route] = time.monotonic() + _ROUTE_COOLDOWN


def _direct() -> httpx.AsyncClient:
    if _closed:
        raise RouteBlocked("trade client closed (shutdown)")
    client = _clients.get("direct")
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=_TIMEOUT, limits=httpx.Limits(keepalive_expiry=_POOL_KEEPALIVE)
        )
        _clients["direct"] = client
    return client


async def _proxied() -> httpx.AsyncClient | None:
    """Client through the Windows system proxy, or None when unavailable."""
    global _proxy_url_now
    url = sysproxy.proxy_url()
    if url is None:
        return None
    if _proxy_url_now != url:
        old = _clients.get("proxy")
        if old is not None and not old.is_closed:
            try:
                await old.aclose()
            except Exception:  # noqa: BLE001
                pass
        _clients.pop("proxy", None)
        _proxy_url_now = url
    client = _clients.get("proxy")
    if client is None or client.is_closed:
        if _closed:
            raise RouteBlocked("trade client closed (shutdown)")
        client = httpx.AsyncClient(
            proxy=url, timeout=_TIMEOUT,
            limits=httpx.Limits(keepalive_expiry=_POOL_KEEPALIVE),
        )
        _clients["proxy"] = client
    return client


# ------------------------------------------------------------- transport

async def _request(method: str, path: str, query: str) -> httpx.Response:
    """Send one request trying direct then system-proxy routes.

    Transport failures / 451 / 403 mark the route down and try the next;
    other statuses return as-is (the caller interprets the API body)."""
    url = f"{_base}{path}"
    headers = {"X-MBX-APIKEY": _key} if _key else None
    last_exc: Exception | None = None

    for route in ("direct", "proxy"):
        if _route_down(route):
            continue
        try:
            if route == "direct":
                resp = await _direct().request(method, f"{url}?{query}", headers=headers)
            else:
                client = await _proxied()
                if client is None:
                    continue
                resp = await client.request(method, f"{url}?{query}", headers=headers)
        except httpx.TransportError as exc:
            # Never blind-retry a mutating request whose delivery is
            # ambiguous (Read/WriteTimeout after send): a duplicated order
            # POST is a real-money hazard. Connect-phase errors are
            # provably undelivered and safe to retry for any method.
            connect_phase = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
            if not connect_phase and method != "GET":
                raise AmbiguousRequest(
                    f"{route} {type(exc).__name__} after possible delivery") from exc
            # stale pooled socket defense (binance.py pattern): one retry on
            # a brand-new connection before declaring the route dead
            fresh = (
                httpx.AsyncClient(timeout=_TIMEOUT,
                                  limits=httpx.Limits(keepalive_expiry=_POOL_KEEPALIVE))
                if route == "direct"
                else httpx.AsyncClient(proxy=_proxy_url_now or "", timeout=_TIMEOUT,
                                       limits=httpx.Limits(keepalive_expiry=_POOL_KEEPALIVE))
            )
            try:
                resp = await fresh.request(method, f"{url}?{query}", headers=headers)
            except httpx.TransportError as exc2:
                await fresh.aclose()
                connect2 = isinstance(exc2, (httpx.ConnectError, httpx.ConnectTimeout))
                if not connect2 and method != "GET":
                    raise AmbiguousRequest(
                        f"{route} retry {type(exc2).__name__} after possible delivery"
                    ) from exc2
                _mark_down(route)
                last_exc = exc
                continue
            except Exception:  # noqa: BLE001
                await fresh.aclose()
                _mark_down(route)
                last_exc = exc
                continue
            # adopt fresh pool for this route
            old = _clients.get(route)
            if old is not None and not old.is_closed:
                try:
                    await old.aclose()
                except Exception:  # noqa: BLE001
                    pass
            _clients[route] = fresh
        if resp.status_code in (403, 451):
            _mark_down(route)  # region block on this route: try the other
            last_exc = RouteBlocked(f"HTTP {resp.status_code} on {route}")
            continue
        return resp

    raise RouteBlocked(str(last_exc) if last_exc else "no route available")


def _sign(query: str) -> str:
    return hmac.new(_secret, query.encode(), hashlib.sha256).hexdigest()


async def _signed(method: str, path: str, params: dict[str, Any]) -> Any:
    """Signed request: timestamp + recvWindow + HMAC over the query string."""
    if not credentials_set():
        raise TradeError(-1, "API key/secret not configured")
    for attempt in range(2):
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000) + _time_offset
        params["recvWindow"] = RECV_WINDOW
        query = urlencode(params)
        query = f"{query}&signature={_sign(query)}"
        resp = await _request(method, path, query)
        data = _json(resp)
        if isinstance(data, dict) and data.get("code") == -1021 and attempt == 0:
            await sync_time()
            continue
        _raise_api_error(resp, data)
        return data
    return None


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        raise TradeError(-1000, f"HTTP {resp.status_code}: {resp.text[:200]}") from None


def _raise_api_error(resp: httpx.Response, data: Any) -> None:
    if resp.is_success and not (isinstance(data, dict) and "code" in data and "msg" in data):
        return
    if isinstance(data, dict) and "code" in data and "msg" in data:
        code = data.get("code")
        if isinstance(code, int) and code < 0:
            raise TradeError(int(code), str(data["msg"]))
        return  # code 200/0 = success (e.g. marginType returns {"code":200,"msg":"success"})
    raise TradeError(-1000, f"HTTP {resp.status_code}: {str(data)[:200]}")


# ------------------------------------------------------------------ API

async def sync_time() -> int:
    """Fetch server time and store the clock offset (signed-call accuracy)."""
    global _time_offset
    resp = await _request("GET", "/fapi/v1/time", "")
    data = _json(resp)
    if isinstance(data, dict) and "serverTime" in data:
        _time_offset = int(data["serverTime"]) - int(time.time() * 1000)
    return _time_offset


async def ping_signed() -> dict:
    """Light signed call to verify key + route (GET /fapi/v2/balance)."""
    return await _signed("GET", "/fapi/v2/balance", {})


async def position_mode() -> bool | None:
    """True = hedge (dual-side) mode. Executor requires one-way (False)."""
    data = await _signed("GET", "/fapi/v1/positionSide/dual", {})
    if isinstance(data, dict):
        return bool(data.get("dualSidePosition"))
    return None


async def set_leverage(symbol: str, leverage: int) -> dict:
    return await _signed("POST", "/fapi/v1/leverage",
                         {"symbol": symbol, "leverage": leverage})


async def set_margin_type(symbol: str, isolated: bool = True) -> dict:
    params = {"symbol": symbol, "marginType": "ISOLATED" if isolated else "CROSSED"}
    try:
        return await _signed("POST", "/fapi/v1/marginType", params)
    except TradeError as exc:
        if exc.code == -4046:  # "No need to change margin type"
            return {"msg": exc.msg}
        raise


async def exchange_info() -> dict:
    resp = await _request("GET", "/fapi/v1/exchangeInfo", "")
    data = _json(resp)
    _raise_api_error(resp, data)
    return data


async def place_order(params: dict[str, Any]) -> dict:
    """POST /fapi/v1/order. Params: symbol, side, type, quantity, price,
    timeInForce (GTC/GTX), reduceOnly, newClientOrderId...
    NOTE: conditional types (STOP_MARKET/TAKE_PROFIT*/TRAILING_STOP) were
    migrated off this endpoint by Binance — testnet rejects them with -4120
    (实测 2026-09-03) and the Algo Order endpoints are live on mainnet too
    (endpoint existence probed same day); executor stops always use
    place_algo_order regardless of environment."""
    return await _signed("POST", "/fapi/v1/order", params)


# ------------------------------------------- algo orders (conditional)

async def place_algo_order(params: dict[str, Any]) -> dict:
    """POST /fapi/v1/algoOrder. Params: algoType ("CONDITIONAL"), symbol,
    side, type ("STOP_MARKET"...), quantity, triggerPrice, workingType,
    reduceOnly, clientAlgoId..."""
    return await _signed("POST", "/fapi/v1/algoOrder", params)


async def cancel_algo_order(client_algo_id: str) -> dict:
    try:
        return await _signed("DELETE", "/fapi/v1/algoOrder",
                             {"clientAlgoId": client_algo_id})
    except TradeError as exc:
        if exc.code in (-2011,):  # "Unknown order sent" = already gone
            return {"msg": exc.msg}
        raise


async def get_algo_order(client_algo_id: str) -> dict | None:
    """Algo order by client id; None when it no longer exists."""
    try:
        return await _signed("GET", "/fapi/v1/algoOrder",
                             {"clientAlgoId": client_algo_id})
    except TradeError as exc:
        if exc.code in (-2013,):  # "Order does not exist"
            return None
        raise


async def open_algo_orders() -> list:
    return await _signed("GET", "/fapi/v1/openAlgoOrders", {})


async def get_order_by_id(symbol: str, order_id: Any) -> dict | None:
    """Order by exchange orderId (the actual order an algo stop spawned)."""
    try:
        return await _signed("GET", "/fapi/v1/order",
                             {"symbol": symbol, "orderId": str(order_id)})
    except TradeError as exc:
        if exc.code in (-2013,):
            return None
        raise


async def cancel_order(symbol: str, coid: str) -> dict:
    try:
        return await _signed("DELETE", "/fapi/v1/order",
                             {"symbol": symbol, "origClientOrderId": coid})
    except TradeError as exc:
        if exc.code in (-2011,):  # "Unknown order sent" = already gone
            return {"msg": exc.msg}
        raise


async def cancel_all_orders(symbol: str) -> dict:
    return await _signed("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})


async def get_order(symbol: str, coid: str) -> dict | None:
    """Order by client id; None when it no longer exists (canceled/expired)."""
    try:
        return await _signed("GET", "/fapi/v1/order",
                              {"symbol": symbol, "origClientOrderId": coid})
    except TradeError as exc:
        if exc.code in (-2013,):  # "Order does not exist"
            return None
        raise


async def open_orders(symbol: str | None = None) -> list:
    params = {"symbol": symbol} if symbol else {}
    return await _signed("GET", "/fapi/v1/openOrders", params)


async def position_risk(symbol: str | None = None) -> list:
    params = {"symbol": symbol} if symbol else {}
    return await _signed("GET", "/fapi/v2/positionRisk", params)


async def usdt_equity() -> float | None:
    """Total USDT wallet balance of the futures account (sizing base)."""
    data = await _signed("GET", "/fapi/v2/balance", {})
    for item in data or []:
        if item.get("asset") == "USDT":
            return float(item.get("balance") or 0.0)
    return None


def route_status() -> dict:
    """Live cooldown state per route (for /api/executor diagnostics)."""
    now = time.monotonic()
    out = {}
    for route in ("direct", "proxy"):
        until = _down_until.get(route, 0.0)
        out[f"{_base}|{route}"] = {
            "down": until > now,
            "retryInS": round(max(0.0, until - now)) if until > now else 0,
        }
    return out
