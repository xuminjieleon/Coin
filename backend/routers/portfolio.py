"""Portfolio-level risk aggregation.

Position-level advice (routers/position.py) checks one trade against the
current chart; this endpoint aggregates ALL open positions the user holds:
net/gross exposure, margin, stop-risk budget, concentration, pairwise
correlation (from local 1d klines) and beta-to-BTC, with rule-based
warnings. The institutional point: every single position can be "correct"
while the book as a whole is one correlated bet.

Each position also gets an `attention` triage level (danger > warn > info >
ok): missing stop / stop beyond liquidation / past the time-exit window /
deep loss / score against the position. The score runs the same lightweight
engine as the market scanner (200 bars, no MTF/derivs context) — triage
caliber; the precise per-trade action lives in /api/position/advise.
"""
import asyncio
import time

import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services import kline_cache
from services.analysis import decision, engine, indicators
from services.analysis.decision import PLAN_GEOMETRY, PLAN_DEFAULT_INTERVAL

router = APIRouter(prefix="/api")

STEP_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}


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


async def _position_context(symbol: str, interval: str) -> dict:
    """Price/ATR plus a lightweight composite score (scanner caliber:
    200 bars, no MTF/derivs context). Empty dict when unavailable."""
    try:
        rows = await kline_cache.get_klines(symbol, interval, 200)
        if len(rows) < 60:
            return {}
        df = kline_cache.rows_to_df(rows)
        atr = indicators.atr(df, 14)
        atr_last = next((v for v in reversed(atr) if v is not None), None)
        full = engine.full_analysis(df)
        closes = df["close"]
        lookback = min(24, len(closes) - 1)
        pcp = None
        if lookback > 0 and float(closes.iloc[-1 - lookback]) > 0:
            pcp = (float(closes.iloc[-1]) - float(closes.iloc[-1 - lookback])) \
                / float(closes.iloc[-1 - lookback]) * 100.0
        summary = decision.build_summary(
            last_close=float(closes.iloc[-1]),
            smc=full["smc"],
            indicators=full["indicators"],
            volume_profile=full["volumeProfile"],
            price_change_pct=pcp,
            wyckoff=full["wyckoff"],
            volatility=full["volatility"],
            cvd_div=full["cvdDivergence"],
            atr=atr_last,
            interval=interval,
        )
        return {"price": float(closes.iloc[-1]), "atr": atr_last,
                "score": summary["score"], "bias": summary["bias"]}
    except Exception:
        return {}


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
    contexts = await asyncio.gather(*[_position_context(p.symbol.upper(), p.interval) for p in body.positions])
    returns = await asyncio.gather(*[_daily_returns(s) for s in symbols])
    ret_by_symbol = {s: r for s, r in zip(symbols, returns) if r is not None}

    rows = []
    net_usd = 0.0
    gross_usd = 0.0
    total_margin = 0.0
    total_risk = 0.0
    notional_list = []
    now_ms = int(time.time() * 1000)
    danger_syms: list[str] = []
    warn_syms: list[str] = []

    for p, ctx in zip(body.positions, contexts):
        long = p.direction == "long"
        lev = p.leverage or 1.0
        qty = p.qty
        sym = p.symbol.upper()
        price = ctx.get("price")
        atr = ctx.get("atr")
        score = ctx.get("score")
        geo = PLAN_GEOMETRY.get(p.interval, PLAN_GEOMETRY[PLAN_DEFAULT_INTERVAL])
        stopw, texit = geo[1], geo[4]
        if price is None:
            rows.append({"symbol": sym, "price": None, "notionalUsd": None,
                         "riskUsd": None, "liqPrice": None, "unrealizedPct": None,
                         "unrealizedR": None, "barsHeld": None,
                         "attention": None,
                         "interval": p.interval, "direction": p.direction})
            continue
        move = (price - p.entry) if long else (p.entry - price)
        pnl_pct = move / p.entry * 100.0

        suggested_stop = (p.entry - stopw * atr if long else p.entry + stopw * atr) if atr else None
        if p.stop is not None:
            risk_per = abs(p.entry - p.stop)
        else:
            risk_per = stopw * atr if atr else p.entry * 0.02
        if lev > 1:
            liq_price = p.entry * (1 - 1.0 / lev) if long else p.entry * (1 + 1.0 / lev)
        else:
            liq_price = None
        unrealized_r = move / risk_per if risk_per > 0 else None

        bars_held = None
        if p.openedAt:
            bars_held = (now_ms - p.openedAt) // STEP_MS.get(p.interval, STEP_MS["1h"]) + 1

        # --- attention triage (danger > warn > info > ok) ---
        att_level, att_text = "ok", None
        if p.stop is None:
            att_level = "danger"
            att_text = (f"未设止损" + (f"——几何建议 {_fmt(suggested_stop)}（{stopw}×ATR）" if suggested_stop else ""))
        elif liq_price is not None and ((long and p.stop < liq_price) or ((not long) and p.stop > liq_price)):
            att_level = "danger"
            att_text = f"止损 {_fmt(p.stop)} 越过估算强平价 {_fmt(liq_price)}——先降杠杆或收紧止损"
        elif bars_held is not None and bars_held >= texit:
            att_level = "danger"
            att_text = f"已持仓 {bars_held}/{texit} 根——超时间退出窗口，按纪律应离场"
        elif unrealized_r is not None and unrealized_r <= -1:
            att_level = "warn"
            att_text = f"浮亏 {unrealized_r:.1f}R——接近/超过单笔计划风险"
        elif score is not None and ((score > 0) != long):
            if abs(score) >= 25:
                att_level = "warn"
                att_text = f"逆势（当前评分 {score:+d}）"
            else:
                att_level = "info"
                att_text = f"评分与持仓相反（{score:+d}）"
        elif score is not None:
            att_text = f"顺势（当前评分 {score:+d}）"
        if att_level == "danger":
            danger_syms.append(sym)
        elif att_level == "warn":
            warn_syms.append(sym)

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
            "unrealizedR": round(unrealized_r, 2) if unrealized_r is not None else None,
            "barsHeld": bars_held,
            "attention": {"level": att_level, "text": att_text},
            "interval": p.interval, "direction": p.direction,
        })

    items: list[dict] = []

    # per-position attention summary (act-now list first)
    if danger_syms:
        items.append({
            "level": "danger",
            "text": f"{len(danger_syms)} 个仓位需立即处理：{('、'.join(danger_syms))}"
                    f"（无止损/超时间退出/止损越过强平价）",
        })
    if warn_syms:
        items.append({
            "level": "warn",
            "text": f"{len(warn_syms)} 个仓位逆势或深度浮亏：{('、'.join(warn_syms))}——注意防守/减仓",
        })

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
        "note": "组合层面规则化检查：每仓位紧急度（attention）/集中度/相关性/风险预算。"
                "attention 评分为扫描器口径（200 根，不含 MTF/衍生品上下文，与决策卡可能有差异），"
                "精确动作请看「我的仓位」。相关性来自本地 1d K 线（90 日）。",
    }
