"""Position advisor: evaluate the user's live position against the latest
analysis and the calibrated plan geometry (PLAN_GEOMETRY / PLAN_THRESHOLD,
round-11 profit-first calibration; 1h round-9 non-loss geometry).

Advice is deterministic rule application (no prediction):
  - alignment of position direction vs current score/bias
  - stop suggestion from calibrated stop-ATR multiple when missing
  - R-multiple management ladder: +beR scale-out & stop->entry, then runner
    trail (ratcheted from MFE since the user's open time when provided)
  - time exit window (texit bars)
  - notional risk check when qty provided
"""
import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from routers.analysis import ALLOWED_INTERVALS
from services import kline_cache
from services.analysis import decision, engine

router = APIRouter(prefix="/api")

STEP_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}


class PositionInput(BaseModel):
    symbol: str
    interval: str
    direction: str  # 'long' | 'short'
    entry: float = Field(gt=0)
    stop: float | None = None
    qty: float | None = Field(default=None, gt=0)
    leverage: float | None = Field(default=None, ge=1, le=200)
    openedAt: int | None = None  # ms epoch


def _fmt(x: float) -> str:
    if x >= 1000:
        return f"{x:,.0f}"
    if x >= 1:
        return f"{x:,.2f}"
    return f"{x:.6f}".rstrip("0").rstrip(".")


@router.post("/position/advise")
async def advise_position(pos: PositionInput):
    if pos.interval not in ALLOWED_INTERVALS:
        raise HTTPException(status_code=400, detail=f"interval must be one of {sorted(ALLOWED_INTERVALS)}")
    if pos.direction not in ("long", "short"):
        raise HTTPException(status_code=400, detail="direction must be long or short")
    symbol = pos.symbol.upper()
    long = pos.direction == "long"
    if pos.stop is not None:
        if long and pos.stop >= pos.entry:
            raise HTTPException(status_code=400, detail="做多止损必须低于入场价")
        if not long and pos.stop <= pos.entry:
            raise HTTPException(status_code=400, detail="做空止损必须高于入场价")

    rows = await kline_cache.get_klines(symbol, pos.interval, 400)
    df = kline_cache.rows_to_df(rows)
    if len(df) < 60:
        raise HTTPException(status_code=404, detail=f"no klines for {symbol}")

    full = engine.full_analysis(df)
    closes = df["close"]
    lookback = min(24, len(closes) - 1)
    price_change_pct = None
    if lookback > 0:
        base = float(closes.iloc[-1 - lookback])
        if base > 0:
            price_change_pct = (float(closes.iloc[-1]) - base) / base * 100.0
    summary = decision.build_summary(
        last_close=float(closes.iloc[-1]),
        smc=full["smc"],
        indicators=full["indicators"],
        volume_profile=full["volumeProfile"],
        price_change_pct=price_change_pct,
        patterns=full["patterns"],
        wyckoff=full["wyckoff"],
        volatility=full["volatility"],
        cvd_div=full["cvdDivergence"],
        atr=next((v for v in reversed(full["indicators"]["atr14"]) if v is not None), None),
        interval=pos.interval,
    )

    price = float(closes.iloc[-1])
    atr = next((v for v in reversed(full["indicators"]["atr14"]) if v is not None), None)
    if atr is None or atr <= 0:
        raise HTTPException(status_code=503, detail="ATR unavailable")

    depth, stopw, be_frac, tgt_r, texit, trail, fill_bars = decision.PLAN_GEOMETRY.get(
        pos.interval, decision.PLAN_GEOMETRY[decision.PLAN_DEFAULT_INTERVAL])

    items: list[dict] = []

    # --- direction alignment ---
    score = summary["score"]
    bias = summary["bias"]
    aligned = (long and score > 0) or (not long and score < 0)
    if aligned:
        items.append({"level": "ok", "text": f"顺势持仓：当前 {pos.interval} 评分 {score:+d}（{bias}），与持仓方向一致"})
    else:
        items.append({
            "level": "warn" if abs(score) < 25 else "danger",
            "text": f"逆势持仓：当前 {pos.interval} 评分 {score:+d}（{bias}）与持仓方向相反"
                    + ("，建议减仓或至少将止损移至保本" if abs(score) >= 25 else ""),
        })

    # --- risk / stop ---
    suggested_stop = pos.entry - stopw * atr if long else pos.entry + stopw * atr
    if pos.stop is None:
        items.append({
            "level": "danger",
            "text": f"未设止损。按 {pos.interval} 校准几何建议 {_fmt(suggested_stop)}（{stopw}×ATR）",
        })
        risk = stopw * atr
    else:
        risk = abs(pos.entry - pos.stop)
    if risk <= 0:
        raise HTTPException(status_code=400, detail="invalid stop")

    # --- PnL / R ladder ---
    move = (price - pos.entry) if long else (pos.entry - price)
    pnl_pct = move / pos.entry * 100.0
    unrealized_r = move / risk
    be_trigger = pos.entry + be_frac * risk if long else pos.entry - be_frac * risk

    # MFE since open (for trail ratchet)
    mfe_r = None
    bars_held = None
    trail_stop = None
    if pos.openedAt is not None:
        step = STEP_MS[pos.interval]
        after = df[df["time"] >= pos.openedAt - step]
        if len(after) > 0:
            bars_held = len(after)
            if long:
                mfe_price = float(after["high"].max())
            else:
                mfe_price = float(after["low"].min())
            mfe_move = (mfe_price - pos.entry) if long else (pos.entry - mfe_price)
            mfe_r = mfe_move / risk

    if unrealized_r < be_frac:
        gap = (be_trigger - price) if long else (price - be_trigger)
        items.append({
            "level": "info",
            "text": f"浮盈 {pnl_pct:+.2f}%（{unrealized_r:+.2f}R）。距 +{be_frac}R 减仓位还差 {_fmt(abs(gap))}"
                    f"（{_fmt(be_trigger)}）；触及后：出半仓 + 止损移至入场价",
        })
    else:
        items.append({
            "level": "ok",
            "text": f"已达 +{be_frac}R 减仓位（当前 {unrealized_r:+.2f}R）。若尚未执行：建议立即出半仓锁定利润，"
                    f"止损移至入场价 {_fmt(pos.entry)}（保本管理）",
        })
        if trail is not None and mfe_r is not None and mfe_r > be_frac:
            trail_r_now = mfe_r - trail
            if trail_r_now > 0:
                trail_stop = pos.entry + trail_r_now * risk if long else pos.entry - trail_r_now * risk
                items.append({
                    "level": "ok",
                    "text": f"剩余半仓跟踪止盈：持仓期最高浮盈 {mfe_r:+.2f}R，"
                            f"当前建议止损跟随至 {_fmt(trail_stop)}（自最高点回撤 {trail}R 离场）",
                })

    # --- time exit ---
    if bars_held is not None and bars_held >= texit:
        items.append({
            "level": "warn",
            "text": f"已持仓 {bars_held} 根 K 线，超过 {texit} 根时间退出窗口——按纪律应市价离场",
        })
    elif bars_held is not None:
        items.append({
            "level": "info",
            "text": f"已持仓 {bars_held}/{texit} 根 K 线（时间退出窗口）",
        })

    # --- leverage / liquidation (isolated-margin approximation) ---
    liq_price = None
    if pos.leverage is not None and pos.leverage > 1:
        lev = pos.leverage
        liq_price = pos.entry * (1 - 1.0 / lev) if long else pos.entry * (1 + 1.0 / lev)
        liq_dist = abs(pos.entry - liq_price)
        if pos.stop is not None:
            stop_dist = abs(pos.entry - pos.stop)
            stop_beyond_liq = (long and pos.stop < liq_price) or ((not long) and pos.stop > liq_price)
            if stop_beyond_liq:
                items.append({
                    "level": "danger",
                    "text": f"{lev:g}× 杠杆下估算强平价 ≈ {_fmt(liq_price)}（隔离保证金近似，未计维持保证金），"
                            f"止损价 {_fmt(pos.stop)} 已越过强平价——大概率先被强平而非止损离场。"
                            f"建议：降低杠杆或收紧止损",
                })
            elif liq_dist > 0 and stop_dist / liq_dist >= 0.8:
                items.append({
                    "level": "warn",
                    "text": f"{lev:g}× 杠杆下估算强平价 ≈ {_fmt(liq_price)}，止损距离已占强平距离的 "
                            f"{stop_dist / liq_dist * 100:.0f}%——缓冲很薄，插针行情可能在止损前先触发强平",
                })

    # --- qty risk ---
    if pos.qty is not None:
        notional = pos.qty * price
        risk_usd = pos.qty * risk
        loss_now = pos.qty * move
        if pos.leverage is not None and pos.leverage > 1:
            margin = pos.qty * pos.entry / pos.leverage
            risk_margin_pct = risk_usd / margin * 100 if margin > 0 else None
            items.append({
                "level": "info",
                "text": f"名义价值 ≈ {_fmt(notional)} USDT（{pos.leverage:g}× 杠杆，保证金 ≈ {_fmt(margin)} USDT）；"
                        f"止损触发亏损 ≈ {_fmt(risk_usd)} USDT"
                        + (f"（占保证金 {risk_margin_pct:.0f}%）" if risk_margin_pct is not None else "")
                        + f"；当前浮动盈亏 ≈ {_fmt(loss_now)} USDT",
            })
        else:
            risk_pct = risk_usd / notional * 100 if notional > 0 else None
            items.append({
                "level": "info",
                "text": f"名义价值 ≈ {_fmt(notional)} USDT；止损触发亏损 ≈ {_fmt(risk_usd)} USDT"
                        + (f"（占名义 {risk_pct:.1f}%）" if risk_pct is not None else "")
                        + f"；当前浮动盈亏 ≈ {_fmt(loss_now)} USDT",
            })

    # --- nearest opposing level as target hint ---
    kl = summary.get("keyLevels") or []
    if long:
        above = [k for k in kl if k["price"] > price]
        tgt_lvl = min(above, key=lambda k: k["price"]) if above else None
    else:
        below = [k for k in kl if k["price"] < price]
        tgt_lvl = max(below, key=lambda k: k["price"]) if below else None
    if tgt_lvl is not None:
        dist_pct = abs(tgt_lvl["price"] - price) / price * 100
        items.append({
            "level": "info",
            "text": f"顺方向最近关键位：{tgt_lvl['label']} {_fmt(tgt_lvl['price'])}（{dist_pct:.2f}%），"
                    f"可作为分批止盈参考",
        })

    return {
        "symbol": symbol,
        "interval": pos.interval,
        "price": price,
        "pnlPct": pnl_pct,
        "unrealizedR": unrealized_r,
        "mfeR": mfe_r,
        "barsHeld": bars_held,
        "levels": {
            "suggestedStop": suggested_stop,
            "beTrigger": be_trigger,
            "trailStop": trail_stop,
            "liqPrice": liq_price,
        },
        "items": items,
        "note": "建议为规则化提示（基于校准几何与当前盘面），非预测；请自行评估风险。",
    }
