"""On-chain data adapter (BTC-focused, multi-source with graceful degradation).

Reachable free sources in this network (probed 2026-08-24):
  - mempool.space       : fees, mempool backlog, hashrate, difficulty epoch
  - api.blockchain.info : 30d hashrate / active-addresses / tx-count charts

Not available (honestly reported as null): exchange netflow / stablecoin
flows / entity-labelled metrics — those need proprietary sources
(Glassnode/Nansen/CryptoQuant) which are blocked or key-gated here.

All endpoints are cached 10 minutes in memory; any failed source degrades
to null fields instead of failing the whole snapshot.
"""
from __future__ import annotations

import asyncio
import time

import httpx

_TIMEOUT = httpx.Timeout(8.0)
_CACHE_TTL = 600.0
_cached: dict | None = None
_cached_at = 0.0


async def _get_json(client: httpx.AsyncClient, url: str):
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.json()


def _chg_pct(series: list[float]) -> float | None:
    if len(series) < 2 or series[0]:
        try:
            return (series[-1] - series[0]) / series[0] * 100.0
        except ZeroDivisionError:
            return None
    return None


async def _mempool_fee(client) -> dict | None:
    try:
        return await _get_json(client, "https://mempool.space/api/v1/fees/recommended")
    except Exception:
        return None


async def _mempool_pool(client) -> dict | None:
    try:
        return await _get_json(client, "https://mempool.space/api/mempool")
    except Exception:
        return None


async def _mempool_hashrate(client) -> list | None:
    try:
        data = await _get_json(client, "https://mempool.space/api/v1/mining/hashrate/3d")
        return data.get("hashrates") or None
    except Exception:
        return None


async def _mempool_difficulty(client) -> dict | None:
    try:
        return await _get_json(client, "https://mempool.space/api/v1/difficulty-adjustment")
    except Exception:
        return None


async def _bc_chart(client, chart: str) -> dict | None:
    try:
        return await _get_json(
            client,
            f"https://api.blockchain.info/charts/{chart}?timespan=30d&format=json&sampled=true",
        )
    except Exception:
        return None


def _chart_values(chart: dict | None) -> list[float]:
    """blockchain.info chart values: [{x: ts, y: v}...] (sampled) or plain list."""
    if not chart or not chart.get("values"):
        return []
    out = []
    for v in chart["values"]:
        if isinstance(v, dict):
            v = v.get("y")
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


async def onchain_snapshot() -> dict:
    """Aggregate on-chain metrics; per-source failures degrade to null."""
    global _cached, _cached_at
    if _cached is not None and time.time() - _cached_at < _CACHE_TTL:
        return _cached

    btc: dict = {"hashrate": None, "hashrateChg30d": None, "mempoolTxs": None,
                 "mempoolVsize": None, "fees": None, "difficulty": None,
                 "activeAddresses": None, "activeAddrAvg30d": None,
                 "txCount24h": None}
    sources: list[str] = []

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        fee, pool, hrs, diff, hr_chart, addr_chart, tx_chart = await asyncio.gather(
            _mempool_fee(client), _mempool_pool(client), _mempool_hashrate(client),
            _mempool_difficulty(client), _bc_chart(client, "hash-rate"),
            _bc_chart(client, "n-unique-addresses"), _bc_chart(client, "n-transactions"),
        )

    if fee:
        btc["fees"] = {
            "fastest": fee.get("fastestFee"), "halfHour": fee.get("halfHourFee"),
            "hour": fee.get("hourFee"), "economy": fee.get("economyFee"),
        }
        sources.append("mempool.space")
    if pool:
        btc["mempoolTxs"] = pool.get("count")
        btc["mempoolVsize"] = pool.get("vsize")  # vbytes of backlog
        sources.append("mempool.space")
    if hrs:
        values = [float(h.get("avgHashrate") or 0) for h in hrs]
        if values:
            btc["hashrate"] = values[-1]
        sources.append("mempool.space")
    if diff:
        btc["difficulty"] = {
            "progressPct": diff.get("progressPercent"),
            "difficultyChangePct": diff.get("difficultyChange"),
            "remainingBlocks": diff.get("remainingBlocks"),
            "estimatedRetargetDate": diff.get("estimatedRetargetDate"),
        }
    if hr_chart and hr_chart.get("values"):
        vals = _chart_values(hr_chart)
        if btc["hashrate"] is None and vals:
            btc["hashrate"] = vals[-1]
        btc["hashrateChg30d"] = _chg_pct(vals)
        sources.append("blockchain.info")
    if addr_chart and addr_chart.get("values"):
        vals = _chart_values(addr_chart)
        btc["activeAddresses"] = int(vals[-1]) if vals else None
        btc["activeAddrAvg30d"] = int(sum(vals) / len(vals)) if vals else None
        sources.append("blockchain.info")
    if tx_chart and tx_chart.get("values"):
        vals = _chart_values(tx_chart)
        btc["txCount24h"] = int(vals[-1]) if vals else None

    _cached = {
        "btc": btc,
        "sources": sorted(set(sources)),
        "unavailable": "交易所净流入/稳定币流向等实体标签数据需要付费源（Glassnode/Nansen 等），本网络不可达，如实置空",
        "updatedAt": int(time.time() * 1000),
    }
    _cached_at = time.time()
    return _cached
