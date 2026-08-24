"""Whole-market scanner: rank every liquid USDT pair by the analysis engine.

Universe: 24h tickers via binance.get_ticker24h (official futures host
first, spot mirror fallback — adapts to the network), minus stables and
leveraged tokens. Each symbol runs the same SMC/indicator pipeline (200
bars via kline_cache) and gets a composite score; results are cached 5
minutes per interval. This is the "screen first, dig later" flow
institutional desks start their day with.
"""
from __future__ import annotations

import asyncio
import time

from services import binance, kline_cache
from services.analysis import decision, engine

STABLE_BASES = {
    "USDC", "TUSD", "FDUSD", "USDP", "DAI", "AEUR", "EURI", "EUR", "XUSD",
    "USD1", "PAXG", "WBTC", "WBETH", "USDE", "USDS", "SUSD", "PYUSD", "BUSD",
}
_CACHE_TTL = 300.0
_results_cache: dict[tuple[str, int], tuple[float, dict]] = {}
_ticker_cache: tuple[float, list] = (0.0, [])


async def _top_usdt_pairs(top_n: int) -> list[dict]:
    """[{symbol, last, chg24h, quoteVolume}] sorted by 24h quote volume."""
    global _ticker_cache
    now = time.monotonic()
    if _ticker_cache[0] > now and _ticker_cache[1]:
        tickers = _ticker_cache[1]
    else:
        tickers = await binance.get_ticker24h()  # official futures -> mirror
        _ticker_cache = (now + 120.0, tickers)

    out = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or len(sym) <= 4:
            continue
        base = sym[:-4]
        if base in STABLE_BASES:
            continue
        # leveraged tokens: *UP/*DOWN/*BULL/*BEAR or 1L/2L/3L/1S/2S/3S suffixes
        if base.endswith(("UP", "DOWN", "BULL", "BEAR")) or base[-2:] in ("1L", "2L", "3L", "1S", "2S", "3S"):
            continue
        try:
            vol = float(t.get("quoteVolume") or 0)
            last = float(t.get("lastPrice") or 0)
            chg = float(t.get("priceChangePercent") or 0)
        except (TypeError, ValueError):
            continue
        if vol <= 0 or last <= 0:
            continue
        out.append({"symbol": sym, "last": last, "chg24h": chg, "quoteVolume": vol})
    out.sort(key=lambda x: -x["quoteVolume"])
    return out[:top_n]


async def _score_one(interval: str, meta: dict, semaphore: asyncio.Semaphore) -> dict | None:
    sym = meta["symbol"]
    async with semaphore:
        try:
            rows = await kline_cache.get_klines(sym, interval, 200)
            if len(rows) < 120:
                return None
            df = kline_cache.rows_to_df(rows)
            full = engine.full_analysis(df)
            closes = df["close"]
            lookback = min(24, len(closes) - 1)
            price_change_pct = None
            if lookback > 0 and float(closes.iloc[-1 - lookback]) > 0:
                price_change_pct = (float(closes.iloc[-1]) - float(closes.iloc[-1 - lookback])) \
                    / float(closes.iloc[-1 - lookback]) * 100.0
            summary = decision.build_summary(
                last_close=float(closes.iloc[-1]),
                smc=full["smc"],
                indicators=full["indicators"],
                volume_profile=full["volumeProfile"],
                price_change_pct=price_change_pct,
                wyckoff=full["wyckoff"],
                volatility=full["volatility"],
                cvd_div=full["cvdDivergence"],
                atr=next((v for v in reversed(full["indicators"]["atr14"]) if v is not None), None),
                interval=interval,
            )
            reasons = summary.get("reasons") or []
            top = next((r for r in reasons if abs(r.get("weight") or 0) > 0), None)
            return {
                "symbol": sym,
                "last": meta["last"],
                "chg24h": meta["chg24h"],
                "quoteVolume": meta["quoteVolume"],
                "score": summary["score"],
                "bias": summary["bias"],
                "regime": summary["regime"],
                "cvdDiv": (full["cvdDivergence"] or {}).get("type"),
                "hasPlan": summary.get("tradePlan") is not None,
                "topReason": top["text"] if top else None,
            }
        except Exception:
            return None


async def scan_market(interval: str, top_n: int = 40) -> dict:
    key = (interval, top_n)
    cached = _results_cache.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]

    started = time.monotonic()
    universe = await _top_usdt_pairs(top_n)
    semaphore = asyncio.Semaphore(6)
    tasks = [_score_one(interval, meta, semaphore) for meta in universe]
    results = await asyncio.gather(*tasks)
    rows = [r for r in results if r]
    rows.sort(key=lambda r: -abs(r["score"]))
    out = {
        "interval": interval,
        "scanned": len(universe),
        "rows": rows,
        "updatedAt": int(time.time() * 1000),
        "durationMs": int((time.monotonic() - started) * 1000),
        "note": "按 24h 成交额取前 N 名 USDT 交易对，跑同一套 SMC/指标引擎（200 根 K 线）评分排序；"
                "点击行可切换标的。评分口径与单标的分析一致（不含 MTF/衍生品上下文）。",
    }
    _results_cache[key] = (time.monotonic() + _CACHE_TTL, out)
    return out
