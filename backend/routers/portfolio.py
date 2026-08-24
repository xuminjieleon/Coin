"""Portfolio-level risk aggregation.

Position-level advice (routers/position.py) checks one trade against the
current chart; this endpoint aggregates ALL open positions the user holds:
net/gross exposure, margin, stop-risk budget, concentration, pairwise
correlation (from local 1d klines) and beta-to-BTC, with rule-based
warnings. The institutional point: every single position can be "correct"
while the book as a whole is one correlated bet.
"""
import asyncio

import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services import kline_cache
from services.analysis import indicators
from services.analysis.decision import PLAN_GEOMETRY, PLAN_DEFAULT_INTERVAL

router = APIRouter(prefix="/api")


class PortfolioPosition(BaseModel):
    symbol: str
    interval: str = "1h"
    direction: str
    entry: float = Field(gt=0)
    stop: float | None = None
    qty: float | None = Field(default=None, gt=0)
    leverage: float | None = Field(default=None, ge=1, le=200)
    openedAt: int | None = None


class PortfolioInput(BaseModel):
    positions: list[PortfolioPosition]
    accountEquity: float | None = Field(default=None, gt=0)


def _fmt(x: float) -> str:
    if x >= 1000:
        return f"{x:,.0f}"
    if x >= 1:
        return f"{x:,.2f}"
    return f"{x:.6f}".rstrip("0").rstrip(".")


async def _price_atr(symbol: str, interval: str) -> tuple[float | None, float | None]:
    try:
        rows = await kline_cache.get_klines(symbol, interval, 120)
        if len(rows) < 20:
            return None, None
        df = kline_cache.rows_to_df(rows)
        atr = indicators.atr(df, 14)
        atr_last = next((v for v in reversed(atr) if v is not None), None)
        return float(df["close"].iloc[-1]), atr_last
    except Exception:
        return None, None


async def _daily_returns(symbol: str, days: int = 90) -> pd.Series | None:
    try:
        rows = await kline_cache.get_klines(symbol, "1d", days)
        if len(rows) < 30:
            return None
        df = kline_cache.rows_to_df(rows)
        return df["close"].pct_change().dropna()
    except Exception:
        return None


@router.post("/portfolio/advise")
async def advise_portfolio(body: PortfolioInput):
    if len(body.positions) == 0:
        raise HTTPException(status_code=400, detail="positions 不能为空")
    if len(body.positions) > 20:
        raise HTTPException(status_code=400, detail="positions 最多 20 个")
    for p in body.positions:
        if p.direction not in ("long", "short"):
            raise HTTPException(status_code=400, detail="direction must be long or short")

    symbols = sorted({p.symbol.upper() for p in body.positions})
    price_atr = await asyncio.gather(*[_price_atr(p.symbol.upper(), p.interval) for p in body.positions])
    returns = await asyncio.gather(*[_daily_returns(s) for s in symbols])
    ret_by_symbol = {s: r for s, r in zip(symbols, returns) if r is not None}

    rows = []
    net_usd = 0.0
    gross_usd = 0.0
    total_margin = 0.0
    total_risk = 0.0
    notional_list = []

    for p, (price, atr) in zip(body.positions, price_atr):
        long = p.direction == "long"
        lev = p.leverage or 1.0
        qty = p.qty
        sym = p.symbol.upper()
        if price is None:
            rows.append({"symbol": sym, "price": None, "notionalUsd": None,
                         "riskUsd": None, "liqPrice": None, "unrealizedPct": None,
                         "interval": p.interval, "direction": p.direction})
            continue
        move = (price - p.entry) if long else (p.entry - price)
        pnl_pct = move / p.entry * 100.0

        if p.stop is not None:
            risk_per = abs(p.entry - p.stop)
        else:
            _, stopw, *_ = PLAN_GEOMETRY.get(p.interval, PLAN_GEOMETRY[PLAN_DEFAULT_INTERVAL])
            risk_per = stopw * atr if atr else p.entry * 0.02
        if lev > 1:
            liq_price = p.entry * (1 - 1.0 / lev) if long else p.entry * (1 + 1.0 / lev)
        else:
            liq_price = None

        notional = qty * price if qty else None
        risk_usd = qty * risk_per if qty else None
        margin = (qty * p.entry / lev) if (qty and lev > 1) else notional
        if notional:
            signed = notional if long else -notional
            net_usd += signed
            gross_usd += notional
            notional_list.append((sym, signed, long))
        if margin:
            total_margin += margin
        if risk_usd:
            total_risk += risk_usd

        rows.append({
            "symbol": sym, "price": price,
            "notionalUsd": notional, "riskUsd": risk_usd,
            "liqPrice": liq_price, "unrealizedPct": round(pnl_pct, 2),
            "interval": p.interval, "direction": p.direction,
        })

    items: list[dict] = []

    # concentration
    if gross_usd > 0 and notional_list:
        top_sym, top_signed, _ = max(notional_list, key=lambda x: abs(x[1]))
        top_share = abs(top_signed) / gross_usd * 100
        if top_share > 50 and len(notional_list) > 1:
            items.append({
                "level": "warn",
                "text": f"集中度：{top_sym} 占总敞口 {top_share:.0f}%——单一标的风险集中",
            })

    # net exposure direction
    if gross_usd > 0:
        net_ratio = net_usd / gross_usd
        items.append({
            "level": "info",
            "text": f"净敞口 {_fmt(net_usd)} USDT（占总敞口 {net_ratio * 100:+.0f}%）；"
                    f"总敞口 {_fmt(gross_usd)} USDT"
                    + (f"，保证金占用 ≈ {_fmt(total_margin)} USDT" if total_margin else ""),
        })

    # pairwise correlation of held symbols (same-direction correlated risk)
    corr_pairs = []
    held = [s for s in symbols if s in ret_by_symbol]
    for i in range(len(held)):
        for j in range(i + 1, len(held)):
            a, b = held[i], held[j]
            joined = pd.DataFrame({"a": ret_by_symbol[a], "b": ret_by_symbol[b]}).dropna()
            if len(joined) >= 30:
                corr = float(joined["a"].corr(joined["b"]))
                if abs(corr) >= 0.7:
                    corr_pairs.append((a, b, corr))
    for a, b, corr in corr_pairs:
        items.append({
            "level": "warn",
            "text": f"{a} 与 {b} 90 日相关性 {corr:+.2f}——同向持仓时实际风险叠加，等效于加大单笔仓位",
        })

    # beta to BTC
    betas = {}
    btc_ret = ret_by_symbol.get("BTCUSDT")
    if btc_ret is not None:
        for s in held:
            if s == "BTCUSDT":
                betas[s] = 1.0
                continue
            joined = pd.DataFrame({"s": ret_by_symbol[s], "btc": btc_ret}).dropna()
            if len(joined) >= 30 and float(joined["btc"].var()) > 0:
                betas[s] = round(float(joined["s"].cov(joined["btc"]) / joined["btc"].var()), 2)
    if betas and len(betas) > 1:
        txt = "、".join(f"{s} β={b:+.2f}" for s, b in betas.items() if s != "BTCUSDT")
        if txt:
            items.append({"level": "info", "text": f"对 BTC 的 beta（60-90 日）：{txt}"})

    # risk budget vs equity
    if body.accountEquity and total_risk > 0:
        risk_pct = total_risk / body.accountEquity * 100
        lvl = "danger" if risk_pct > 6 else ("warn" if risk_pct > 3 else "ok")
        items.append({
            "level": lvl,
            "text": f"全部止损触发合计亏损 ≈ {_fmt(total_risk)} USDT，占账户权益 {risk_pct:.1f}%"
                    + ("（超过 6% 高风险）" if risk_pct > 6 else "（3-6% 偏高）" if risk_pct > 3 else "（≤3% 合理）"),
        })
    elif total_risk > 0:
        items.append({
            "level": "info",
            "text": f"全部止损触发合计亏损 ≈ {_fmt(total_risk)} USDT（填写账户权益可计算占比）",
        })

    return {
        "positions": rows,
        "netUsd": round(net_usd, 2),
        "grossUsd": round(gross_usd, 2),
        "marginUsd": round(total_margin, 2) if total_margin else None,
        "totalRiskUsd": round(total_risk, 2) if total_risk else None,
        "riskPctOfEquity": round(total_risk / body.accountEquity * 100, 2)
            if (body.accountEquity and total_risk) else None,
        "correlatedPairs": [{"a": a, "b": b, "corr": round(c, 2)} for a, b, c in corr_pairs],
        "betas": betas,
        "items": items,
        "note": "组合层面规则化检查：集中度/相关性/风险预算。相关性来自本地 1d K 线（90 日）。",
    }
