"""Windows system-proxy detection + a shared proxied httpx client.

VPN/proxy clients (Clash, v2rayN, ...) commonly run in system-proxy mode:
they set ProxyEnable/ProxyServer in the HKCU Internet Settings registry key
and only WinINET-aware apps (browsers) honor it. httpx reads environment
variables only, so the backend would bypass the proxy entirely. This module
detects the registry proxy at runtime (60s TTL) and exposes an opt-in
proxied client so data-source chains can try "same host via system proxy"
as one more link:

    direct official host -> host via system proxy -> mirror / other source

Failure model (mirrors binance.py host cooldowns):
- transport-level failure (proxy unreachable / stale pooled socket) marks
  the proxy down for 300s so callers fail fast to their next link;
- HTTP status errors do NOT mark the proxy down (the proxy itself works —
  e.g. a 451 from Binance means the VPN exit region is blocked, not that
  the proxy is broken).
"""
from __future__ import annotations

import threading
import time
from typing import Any

import httpx

_TTL = 60.0        # seconds to cache the registry lookup
_COOLDOWN = 300.0  # proxy-unreachable cooldown (matches host cooldowns)
_KEEPALIVE = 60.0  # idle-socket expiry (stale-pool defense, see binance.py)

_lock = threading.Lock()
_cached_url: str | None = None
_checked_at = 0.0
_down_until = 0.0

_client: httpx.AsyncClient | None = None
_client_url: str | None = None


class ProxyUnavailable(Exception):
    """No system proxy configured, or the proxy is unreachable/in cooldown."""


def _read_registry() -> str | None:
    try:
        import winreg  # Windows only; import kept lazy for other platforms
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return None
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except Exception:  # noqa: BLE001 - non-Windows / registry unavailable
        return None
    return _normalize(str(server))


def _normalize(server: str) -> str | None:
    """Accept "host:port", "http://host:port" and per-protocol forms."""
    server = server.strip()
    if not server:
        return None
    if "=" in server:  # per-protocol form: "http=h:p;https=h:p;socks=h:p"
        picked: str | None = None
        for part in server.split(";"):
            proto, _, addr = part.partition("=")
            proto = proto.strip().lower()
            if proto == "https":
                picked = addr.strip()
                break
            if proto == "http" and picked is None:
                picked = addr.strip()
        server = picked or ""
        if not server:
            return None  # socks-only entries need socksio; not supported
    if not server.startswith(("http://", "https://")):
        server = "http://" + server
    return server


def proxy_url() -> str | None:
    """Current system-proxy URL, or None when disabled/unset/cooling down."""
    global _cached_url, _checked_at
    with _lock:
        now = time.monotonic()
        if now < _down_until:
            return None
        if now - _checked_at > _TTL:
            _cached_url = _read_registry()
            _checked_at = now
        return _cached_url


def mark_down() -> None:
    """Proxy transport failed — fast-fail callers for a cooldown window."""
    global _down_until
    with _lock:
        _down_until = time.monotonic() + _COOLDOWN


def status() -> dict:
    """Live proxy state for /api/sources diagnostics."""
    global _cached_url, _checked_at
    with _lock:
        now = time.monotonic()
        if now >= _down_until and now - _checked_at > _TTL:
            _cached_url = _read_registry()
            _checked_at = now
        down = now < _down_until
        return {
            "url": None if down else _cached_url,
            "down": down,
            "retryInS": round(_down_until - now) if down else 0,
        }


def _build(url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        proxy=url,
        timeout=httpx.Timeout(8.0),
        limits=httpx.Limits(keepalive_expiry=_KEEPALIVE),
    )


async def close_client() -> None:
    global _client, _client_url
    if _client is not None and not _client.is_closed:
        try:
            await _client.aclose()
        except Exception:  # noqa: BLE001
            pass
    _client, _client_url = None, None


async def _adopt(fresh: httpx.AsyncClient, url: str) -> None:
    global _client, _client_url
    old, _client, _client_url = _client, fresh, url
    if old is not None and not old.is_closed:
        try:
            await old.aclose()
        except Exception:  # noqa: BLE001
            pass


async def request(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float | httpx.Timeout = 8.0,
) -> httpx.Response:
    """GET through the system proxy. Raises ProxyUnavailable when no proxy
    is configured or its transport fails; HTTP status errors propagate."""
    url_now = proxy_url()
    if url_now is None:
        raise ProxyUnavailable("no system proxy available")
    global _client, _client_url
    if _client is not None and (_client.is_closed or _client_url != url_now):
        await close_client()
    if _client is None:
        _client, _client_url = _build(url_now), url_now
    try:
        resp = await _client.get(url, params=params, headers=headers, timeout=timeout)
    except httpx.TransportError:
        # possibly a stale pooled socket (see binance._fetch): retry once on
        # a brand-new proxied connection; if that also fails the proxy is
        # genuinely unreachable -> cooldown + ProxyUnavailable
        fresh = _build(url_now)
        try:
            resp = await fresh.get(url, params=params, headers=headers, timeout=timeout)
        except httpx.TransportError as exc:
            await fresh.aclose()
            mark_down()
            raise ProxyUnavailable(f"proxy transport failed: {exc}") from exc
        except Exception:  # noqa: BLE001
            await fresh.aclose()
            raise
        await _adopt(fresh, url_now)
    return resp


async def fetch_json(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float | httpx.Timeout = 8.0,
) -> Any:
    """request() + raise_for_status + json()."""
    resp = await request(url, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()
