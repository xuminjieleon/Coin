"""Position advisor: evaluate the user's live position against the latest
analysis and the calibrated plan geometry (PLAN_GEOMETRY / PLAN_THRESHOLD).

The composite score quoted for the 顺势/逆势 check runs through the SAME
pipeline as /api/analysis (500-bar window, prev-day levels, MTF resonance,
funding/OI weighted components), so it matches the decision card.

Advice is deterministic rule application (no prediction):
  - alignment of position direction vs current score/bias; thesis state
    (score / structure / MTF / CVD evidence consolidated) with a single
    top-priority `action` so the user always knows the next discipline step
  - score drift: decision replay at the entry moment (only bars fully closed
    before entry — no lookahead) + entry-quality check vs PLAN_THRESHOLD
  - structure (BOS/CHoCH), liquidity-sweep and Wyckoff events since the
    entry bar (a longer covering window is analysed when the position
    predates the 500-bar score window); higher-timeframe background vs the
    position; current-TF CVD divergence vs the position
  - MFE/MAE excursion tracking since open + profit give-back warning
  - R-multiple management ladder: +beR scale-out & stop->entry, runner trail
    ratcheted from MFE (+ tighten-by-half when the thesis weakens);
    structural stop reference (last confirmed swing on the profit side);
    stop proximity to unswept liquidity pools (sweep risk); stop width vs ATR
  - take-profit ladder: unswept pools / VAH-VAL / POC / range extremes
    ahead of the position with distance and +R at each level
  - funding carry cost (8h settlement, percentile context), high-impact
    event risk within 48h, time exit window, notional/margin risk and
    liquidation-distance checks
"""
import asyncio
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services.analysis.context import (
    ALLOWED_INTERVALS,
    derivatives_context,
    mtf_context,
    prev_day_levels,
)
from routers.calendar import upcoming_events
from services import derivs_store, kline_cache
from services.analysis import decision, engine

router = APIRouter(prefix="/api")

STEP_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}

WINDOW = 500  # same bar count as the decision card -> identical composite score


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

    step = STEP_MS[pos.interval]
    bar_start = None  # open time of the bar the position was opened in
    if pos.openedAt is not None:
        bar_start = pos.openedAt - (pos.openedAt % step)

    # positions older than the 500-bar score window need a longer window for
    # MFE/MAE and events-since-open (score parity always uses 500 bars)
    async def _long_window() -> list:
        if bar_start is None:
            return []
        now_ms = int(time.time() * 1000)
        needed = (now_ms - bar_start) // step + 1
        if needed <= WINDOW:
            return []
        try:
            return await kline_cache.get_klines(symbol, pos.interval, min(needed, 3000))
        except Exception:
            return []

    async def _score_at_open() -> int | None:
        """Decision replay at the entry moment: only bars fully closed before
        the entry (end_bar = bar before the entry bar; MTF bars fully closed
        by that bar's close; derivs daily rows closed before entry)."""
        if bar_start is None:
            return None
        try:
            end_bar = bar_start - step
            rows, prev, mtf, (oi, funding) = await asyncio.gather(
                kline_cache.get_klines(symbol, pos.interval, WINDOW, end_time=end_bar),
                prev_day_levels(symbol, pos.interval, as_of_ms=pos.openedAt),
                mtf_context(symbol, pos.interval, as_of_ms=end_bar),
                derivatives_context(symbol, as_of_ms=pos.openedAt),
            )
            df = kline_cache.rows_to_df(rows)
            if len(df) < 60:
                return None
            full = engine.full_analysis(df, prev)
            closes = df["close"]
            lookback = min(24, len(closes) - 1)
            pcp = None
            if lookback > 0:
                base = float(closes.iloc[-1 - lookback])
                if base > 0:
                    pcp = (float(closes.iloc[-1]) - base) / base * 100.0
            summary = decision.build_summary(
                last_close=float(closes.iloc[-1]),
                smc=full["smc"],
                indicators=full["indicators"],
                volume_profile=full["volumeProfile"],
                oi_change_pct=oi,
                price_change_pct=pcp,
                funding_rate=funding,
                wyckoff=full["wyckoff"],
                volatility=full["volatility"],
                cvd_div=full["cvdDivergence"],
                mtf=mtf["list"],
                atr=next((v for v in reversed(full["indicators"]["atr14"]) if v is not None), None),
                interval=pos.interval,
            )
            return summary["score"]
        except Exception:
            return None

    rows, prev_day, mtf, (oi_change, funding), score_at_open, long_rows = await asyncio.gather(
        kline_cache.get_klines(symbol, pos.interval, WINDOW),
        prev_day_levels(symbol, pos.interval),
        mtf_context(symbol, pos.interval),
        derivatives_context(symbol),
        _score_at_open(),
        _long_window(),
    )
    df = kline_cache.rows_to_df(rows)
    if len(df) < 60:
        raise HTTPException(status_code=404, detail=f"no klines for {symbol}")

    full = engine.full_analysis(df, prev_day)
    covering_df = df
    events_full = full
    if long_rows:
        long_df = kline_cache.rows_to_df(long_rows)
        if len(long_df) and int(long_df["time"].iloc[0]) <= bar_start:
            covering_df = long_df
            events_full = engine.full_analysis(long_df)
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
        oi_change_pct=oi_change,
        price_change_pct=price_change_pct,
        funding_rate=funding,
        wyckoff=full["wyckoff"],
        volatility=full["volatility"],
        cvd_div=full["cvdDivergence"],
        mtf=mtf["list"],
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
    events_since_open: list[dict] = []

    # --- direction alignment (decision-card-parity composite score) ---
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

    # --- score drift: entry-moment replay vs now ---
    if score_at_open is not None:
        rel_drift = (score - score_at_open) * (1 if long else -1)
        if (long and score_at_open <= -15) or ((not long) and score_at_open >= 15):
            items.append({
                "level": "info",
                "text": f"开仓时即为逆势入场（当时评分 {score_at_open:+d}）——逆势仓更应严格执行保本纪律",
            })
        if rel_drift <= -25:
            items.append({
                "level": "warn",
                "text": f"评分漂移：开仓时 {score_at_open:+d} → 当前 {score:+d}，持仓论据明显减弱",
            })
        elif rel_drift >= 25:
            items.append({
                "level": "ok",
                "text": f"评分漂移：开仓时 {score_at_open:+d} → 当前 {score:+d}，持仓论据增强",
            })
        elif abs(rel_drift) >= 10:
            items.append({
                "level": "info",
                "text": f"开仓时评分 {score_at_open:+d} → 当前 {score:+d}（变化不大）",
            })
        # entry quality: was the entry inside the system's signal band?
        th = decision.PLAN_THRESHOLD.get(
            pos.interval, decision.PLAN_THRESHOLD[decision.PLAN_DEFAULT_INTERVAL])
        if abs(score_at_open) < th:
            items.append({
                "level": "info",
                "text": f"开仓时点评分 {score_at_open:+d} 低于本周期计划阈值 |评分|≥{th}——入场不在系统信号内，"
                        f"建议按防守型管理（尽早保本、降低盈利预期）",
            })
        elif (score_at_open > 0) == long:
            items.append({
                "level": "ok",
                "text": f"开仓时点为顺势系统信号（评分 {score_at_open:+d} ≥ 阈值 {th}）——入场质量良好",
            })

    # --- structure / sweep / wyckoff events since the entry bar ---
    if bar_start is not None:
        side = "bullish" if long else "bearish"
        against = "bearish" if long else "bullish"
        struct_evs = [e for e in events_full["smc"]["structureEvents"] if e["time"] >= bar_start]
        choch_against = [e for e in struct_evs if e["kind"] == "CHoCH" and e["direction"] == against]
        bos_with = [e for e in struct_evs if e["kind"] == "BOS" and e["direction"] == side]
        bos_against = [e for e in struct_evs if e["kind"] == "BOS" and e["direction"] == against]
        n_choch = sum(1 for e in struct_evs if e["kind"] == "CHoCH")
        for e in struct_evs:
            events_since_open.append({
                "time": e["time"], "kind": "structure", "direction": e["direction"],
                "text": f"{'结构反转' if e['kind'] == 'CHoCH' else '结构延续'} {e['kind']}"
                        f"{'看涨' if e['direction'] == 'bullish' else '看跌'} @ {_fmt(e['price'])}",
            })
        if struct_evs:
            last_ev = struct_evs[-1]
            if last_ev["direction"] == side:
                if n_choch:
                    items.append({
                        "level": "ok",
                        "text": f"结构经历 {n_choch} 次 CHoCH 反转后已回到持仓方向"
                                f"（最新事件 {last_ev['kind']} 顺势 @ {_fmt(last_ev['price'])}）",
                    })
                else:
                    items.append({"level": "ok", "text": f"持仓期间 {len(bos_with)} 次顺势 BOS——结构延续支持持仓"})
            elif last_ev["kind"] == "CHoCH":
                items.append({
                    "level": "warn",
                    "text": f"最新结构事件为反向 CHoCH @ {_fmt(last_ev['price'])}"
                            f"（结构转向{'看跌' if last_ev['direction'] == 'bearish' else '看涨'}）——"
                            f"原趋势结构被破坏，建议收紧止损或减仓",
                })
            elif choch_against:
                items.append({
                    "level": "warn",
                    "text": f"结构当前与持仓相反：持仓期间出现 {len(choch_against)} 次反向 CHoCH，"
                            f"最新事件为反向 BOS @ {_fmt(last_ev['price'])}——建议防守",
                })
            else:
                items.append({"level": "info", "text": f"持仓期间出现 {len(bos_against)} 次反向 BOS（未构成 CHoCH）——注意结构走弱"})
        for e in events_full["smc"]["sweepEvents"]:
            if e["time"] < bar_start:
                continue
            if e["side"] == "buy_side":
                sw_dir = "bullish" if e["outcome"] == "broken" else "bearish"
                where = "上方"
            else:
                sw_dir = "bearish" if e["outcome"] == "broken" else "bullish"
                where = "下方"
            events_since_open.append({
                "time": e["time"], "kind": "sweep", "direction": sw_dir,
                "text": f"扫{where}流动性 @ {_fmt(e['price'])}（{'突破' if e['outcome'] == 'broken' else '收回'}）",
            })
        for e in (events_full["wyckoff"].get("events") or []):
            if e["time"] < bar_start:
                continue
            if e["type"] == "spring":
                events_since_open.append({"time": e["time"], "kind": "wyckoff", "direction": "bullish",
                                          "text": "Wyckoff Spring：区间下沿刺破后收回"})
            elif e["type"] == "utad":
                events_since_open.append({"time": e["time"], "kind": "wyckoff", "direction": "bearish",
                                          "text": "Wyckoff UTAD：区间上沿上冲后回落"})
            elif e["type"] == "sos":
                events_since_open.append({"time": e["time"], "kind": "wyckoff", "direction": "bullish",
                                          "text": "Wyckoff SOS：放量突破"})
        events_since_open.sort(key=lambda e: e["time"])

    # --- higher-timeframe background vs the position ---
    mtf_list = mtf.get("list") or []
    if mtf_list:
        parts: list[str] = []
        friends = foes = 0
        for tf in mtf_list:
            b = tf.get("bias")
            if b == "bullish":
                parts.append(f"{tf['interval']}看多")
                friends += 1 if long else 0
                foes += 0 if long else 1
            elif b == "bearish":
                parts.append(f"{tf['interval']}看空")
                foes += 1 if long else 0
                friends += 0 if long else 1
            else:
                parts.append(f"{tf['interval']}中性")
        joined = "、".join(parts)
        if foes and not friends:
            items.append({"level": "warn", "text": f"高周期背景与持仓相反（{joined}）——逆高周期持仓，反弹/回调注意减仓"})
        elif foes and friends:
            items.append({"level": "info", "text": f"高周期分歧（{joined}）"})
        elif friends:
            items.append({"level": "ok", "text": f"高周期背景支持持仓（{joined}）"})

    # --- current-TF CVD divergence vs the position ---
    cvd_div = full["cvdDivergence"]
    if cvd_div:
        cvd_with = (cvd_div["type"] == "bullish") == long
        div_txt = "看涨" if cvd_div["type"] == "bullish" else "看跌"
        if cvd_with:
            items.append({"level": "ok", "text": f"当前周期 CVD {div_txt}背离与持仓同向——主动买卖盘动能支持"})
        else:
            items.append({"level": "warn", "text": f"当前周期 CVD {div_txt}背离与持仓相反——动能衰竭信号，注意防守"})

    # --- thesis state: consolidated current evidence for/against the position ---
    # evidence sources: composite score (x2), latest structure event, MTF
    # majority, current-TF CVD divergence. Descriptive only — NOT a predictor.
    evidence = 0
    if score != 0:
        evidence += 2 if ((score > 0) == long) else -2
    all_evs = full["smc"]["structureEvents"]
    if all_evs:
        evidence += 1 if ((all_evs[-1]["direction"] == "bullish") == long) else -1
    if mtf_list:
        mtf_bulls = sum(1 for tf in mtf_list if tf.get("bias") == "bullish")
        mtf_bears = sum(1 for tf in mtf_list if tf.get("bias") == "bearish")
        if mtf_bulls != mtf_bears:
            evidence += 1 if ((mtf_bulls > mtf_bears) == long) else -1
    if cvd_div:
        evidence += 1 if ((cvd_div["type"] == "bullish") == long) else -1
    if evidence >= 3:
        thesis_state = "strong"
    elif evidence >= 1:
        thesis_state = "intact"
    elif evidence >= -1:
        thesis_state = "weakened"
    else:
        thesis_state = "broken"

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

    # --- stop width vs volatility: avoid noise stop-outs and over-wide risk ---
    if pos.stop is not None:
        stop_atrs = risk / atr
        if stop_atrs < 0.8:
            wider = pos.entry - 0.8 * atr if long else pos.entry + 0.8 * atr
            items.append({
                "level": "warn",
                "text": f"止损距离仅 {stop_atrs:.2f}×ATR，偏紧——正常波动即可扫损，"
                        f"建议 ≥0.8×ATR（≈{_fmt(wider)}）",
            })
        elif stop_atrs > 3.0:
            items.append({
                "level": "info",
                "text": f"止损距离 {stop_atrs:.2f}×ATR 偏宽——单笔全额风险大且 R 效率低，可考虑收紧或减仓",
            })

    # --- PnL / R ladder ---
    move = (price - pos.entry) if long else (pos.entry - price)
    pnl_pct = move / pos.entry * 100.0
    unrealized_r = move / risk
    be_trigger = pos.entry + be_frac * risk if long else pos.entry - be_frac * risk

    # MFE / MAE since open (covering window; for trail ratchet + excursion)
    mfe_r = None
    mae_r = None
    bars_held = None
    trail_stop = None
    if bar_start is not None:
        after = covering_df[covering_df["time"] >= bar_start]
        if len(after) > 0:
            last_ts = int(covering_df["time"].iloc[-1])
            bars_held = (last_ts - bar_start) // step + 1
            if long:
                mfe_move = float(after["high"].max()) - pos.entry
                mae_move = float(after["low"].min()) - pos.entry
            else:
                mfe_move = pos.entry - float(after["low"].min())
                mae_move = pos.entry - float(after["high"].max())
            mfe_r = mfe_move / risk
            mae_r = mae_move / risk

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

    # --- excursion tracking (MFE/MAE + give-back) ---
    if mfe_r is not None:
        if mae_r is not None and mae_r >= 0:
            excursion_txt = f"持仓期最大浮盈 {mfe_r:+.2f}R，全程未出现浮亏（最差时点仍 +{mae_r:.2f}R）"
        else:
            excursion_txt = f"持仓期最大浮盈 {mfe_r:+.2f}R"
            if mae_r is not None:
                excursion_txt += f" / 最大浮亏 {mae_r:+.2f}R"
        items.append({"level": "info", "text": excursion_txt})
        if mfe_r >= 0.5 and (mfe_r - unrealized_r) >= 0.5:
            items.append({
                "level": "warn",
                "text": f"浮盈自最高 {mfe_r:+.2f}R 回吐至 {unrealized_r:+.2f}R——按纪律收紧止损（跟踪止盈）",
            })
        elif mae_r is not None and mae_r <= -0.7 and unrealized_r > -0.5:
            items.append({
                "level": "info",
                "text": f"曾承受 {mae_r:+.2f}R 浮亏后回升——止损宽度经受住了插针考验",
            })

    # --- early-cut heuristic: thesis broken while losing ---
    if unrealized_r < 0 and thesis_state == "broken":
        items.append({
            "level": "warn",
            "text": f"浮亏 {unrealized_r:+.2f}R 且持仓证据已转空（评分/结构/高周期/CVD 多数反向）——"
                    f"可考虑主动减仓或离场，不必等止损全额兑现",
        })

    # --- tighten the runner trail when the thesis weakens ---
    if (trail is not None and mfe_r is not None and mfe_r > be_frac
            and thesis_state in ("weakened", "broken")):
        tighter_r = mfe_r - trail / 2.0
        if tighter_r > 0:
            tighter_stop = pos.entry + tighter_r * risk if long else pos.entry - tighter_r * risk
            items.append({
                "level": "info",
                "text": f"证据转弱（{thesis_state}）：可将跟踪止盈回撤容忍收紧一半——止盈位 {_fmt(tighter_stop)}（+{tighter_r:.2f}R）",
            })

    # --- structural stop reference (last confirmed swing on the profit side) ---
    structure_stop = None
    if unrealized_r > 0:
        n = len(df)
        confirmed = [s for s in full["smc"]["swings"] if s["index"] <= n - 3]
        if long:
            cands = [s for s in confirmed if s["kind"] == "low"
                     and pos.entry < s["price"] < price - 0.1 * atr]
        else:
            cands = [s for s in confirmed if s["kind"] == "high"
                     and price + 0.1 * atr < s["price"] < pos.entry]
        if cands:
            structure_stop = float(cands[-1]["price"])
    if structure_stop is not None:
        dist_pct = abs(price - structure_stop) / price * 100
        items.append({
            "level": "info",
            "text": f"结构止损参考：最近确认摆动{'低' if long else '高'}点 {_fmt(structure_stop)}"
                    f"（距现价 {dist_pct:.2f}%），可作为 R 跟踪之外的结构性防守位",
        })

    # --- stop sitting on an unswept liquidity pool (sweep risk) ---
    if pos.stop is not None:
        pools = full["smc"]["liquidityPools"]
        if long:
            cand = [p for p in pools if p["type"] == "sell_side" and not p["swept"] and p["price"] < price]
        else:
            cand = [p for p in pools if p["type"] == "buy_side" and not p["swept"] and p["price"] > price]
        if cand:
            nearest = max(cand, key=lambda p: p["price"]) if long else min(cand, key=lambda p: p["price"])
            if abs(pos.stop - nearest["price"]) <= 0.5 * atr:
                beyond = nearest["price"] - 0.3 * atr if long else nearest["price"] + 0.3 * atr
                items.append({
                    "level": "warn",
                    "text": f"止损 {_fmt(pos.stop)} 紧贴{('卖方' if long else '买方')}流动性池 {_fmt(nearest['price'])}"
                            f"（{nearest['touches']} 次触碰未扫）——挂单密集处易被插针扫损后反弹，"
                            f"可考虑移至池{'下' if long else '上'}方（≈{_fmt(beyond)}）",
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
    stop_beyond_liq = False
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

    # --- funding carry: leveraged positions pay/receive funding every 8h ---
    if funding is not None and funding != 0 and pos.qty is not None:
        notional_now = pos.qty * price
        pay_8h = notional_now * funding * (1 if long else -1)  # >0 = position pays
        try:
            stats = derivs_store.history_stats(symbol) or {}
        except Exception:
            stats = {}
        fp = stats.get("fundingPctl")
        pctl_txt = f"，处于本地历史 {fp:.0f}% 分位" if fp is not None else ""
        slot = 8 * 3600
        hrs_to_funding = ((int(time.time()) // slot + 1) * slot - time.time()) / 3600.0
        if pay_8h > 0:
            items.append({
                "level": "warn" if abs(funding) >= 3 * decision.FUNDING_THRESHOLD else "info",
                "text": f"资金费率 {funding * 100:.4f}%{pctl_txt}，对本仓为支出：每 8h ≈ {_fmt(pay_8h)} USDT"
                        f"（下次结算约 {hrs_to_funding:.1f}h 后）——负 carry 持续侵蚀浮盈，长持需计入成本",
            })
        else:
            items.append({
                "level": "info",
                "text": f"资金费率 {funding * 100:.4f}%{pctl_txt}，对本仓为收入：每 8h ≈ {_fmt(-pay_8h)} USDT"
                        f"（下次结算约 {hrs_to_funding:.1f}h 后）",
            })

    # --- high-impact event risk within 48h ---
    try:
        evs_ahead = upcoming_events(int(time.time() * 1000), 48 * 3600 * 1000)
    except Exception:
        evs_ahead = []
    for ev in evs_ahead[:2]:
        if ev["impact"] != "high":
            continue
        hrs_to_ev = (ev["ts"] - int(time.time() * 1000)) / 3600000.0
        items.append({
            "level": "warn",
            "text": f"约 {hrs_to_ev:.0f}h 后有高影响事件：{ev['title']}——事件前后波动率骤增，"
                    f"建议提前降杠杆/收紧止损或减仓，避免事件缺口打穿防线",
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

    # --- take-profit ladder: reference levels on the PROFIT side ---
    # unswept liquidity pools (the strongest magnets), value-area edges, POC,
    # range extremes and the decision card's key levels — each with distance
    # and the +R it locks in. Only levels BEYOND the entry qualify: a level
    # between entry and price is on the adverse side, not a take-profit.
    # Informational runner-management aid.
    ladder_cands: list[tuple[float, str]] = []

    def _ladder_add(p: float | None, label: str):
        if p is None or p <= 0:
            return
        if long and not (p > price * 1.001 and p > pos.entry):
            return
        if not long and not (p < price * 0.999 and p < pos.entry):
            return
        for existing, _ in ladder_cands:
            if abs(existing - p) / p < 0.003:
                return
        ladder_cands.append((float(p), label))

    for pool in full["smc"]["liquidityPools"]:
        if pool["swept"]:
            continue
        if long and pool["type"] == "buy_side":
            _ladder_add(pool["price"], f"买方流动性池（{pool['touches']} 次触碰）")
        if (not long) and pool["type"] == "sell_side":
            _ladder_add(pool["price"], f"卖方流动性池（{pool['touches']} 次触碰）")
    vp = full["volumeProfile"]
    if long:
        _ladder_add(vp.get("vah"), "VAH 价值区上沿")
    else:
        _ladder_add(vp.get("val"), "VAL 价值区下沿")
    _ladder_add(vp.get("poc"), "POC 控制点")
    pd_zone = full["smc"]["premiumDiscount"]
    if long:
        _ladder_add(pd_zone.get("rangeHigh"), "区间高点（溢价区）")
    else:
        _ladder_add(pd_zone.get("rangeLow"), "区间低点（折价区）")
    for k in summary.get("keyLevels") or []:
        _ladder_add(k["price"], k["label"])
    ladder_cands.sort(key=lambda t: abs(t[0] - price))
    take_profit_ladder: list[dict] = []
    for p, label in ladder_cands[:4]:
        r_mult = (p - pos.entry) / risk if long else (pos.entry - p) / risk
        take_profit_ladder.append({
            "price": round(p, 8),
            "label": label,
            "distPct": round(abs(p - price) / price * 100, 2),
            "rMultiple": round(r_mult, 2),
        })
    if take_profit_ladder:
        first = take_profit_ladder[0]
        items.append({
            "level": "info",
            "text": f"顺方向最近目标位：{first['label']} {_fmt(first['price'])}"
                    f"（{first['distPct']:.2f}%，+{first['rMultiple']:.2f}R）——完整止盈参考见阶梯",
        })
    else:
        if unrealized_r > 0:
            hint = "——突破真空区，剩余仓位可让利润奔跑，防守交给跟踪止盈"
        else:
            hint = "——防守交给止损与保本纪律"
        items.append({
            "level": "info",
            "text": f"顺方向盈利侧无流动性池/关键位参考{hint}",
        })

    # --- volatility squeeze notice ---
    vol_state = full["volatility"]
    if vol_state and vol_state.get("squeeze"):
        items.append({
            "level": "info",
            "text": "波动率压缩（布林挤压 + ATR 低百分位）——突破临近，持仓注意假突破与止损空间",
        })

    # --- top-priority action: the one discipline step that matters now ---
    if pos.stop is None:
        action = {"level": "danger", "text": f"立即设置止损：建议 {_fmt(suggested_stop)}（{stopw}×ATR）"}
    elif stop_beyond_liq:
        action = {"level": "danger", "text": "止损已越过估算强平价：先降杠杆或收紧止损，否则大概率直接被强平"}
    elif bars_held is not None and bars_held >= texit:
        action = {"level": "warn", "text": f"时间退出窗口已过（{bars_held}/{texit} 根）：按纪律市价离场"}
    elif unrealized_r < 0 and thesis_state == "broken":
        action = {"level": "warn", "text": "持仓证据已转空且浮亏：考虑主动减仓/离场，不等止损全额兑现"}
    elif unrealized_r >= be_frac:
        if trail_stop is not None:
            action = {"level": "ok", "text": f"保本管理进行中：跟踪止盈位 {_fmt(trail_stop)}（若减半+保本尚未执行请立即执行）"}
        else:
            action = {"level": "ok", "text": f"已达 +{be_frac}R：出半仓 + 止损移至入场价 {_fmt(pos.entry)}（若尚未执行）"}
    else:
        defend = pos.stop if pos.stop is not None else suggested_stop
        action = {"level": "info", "text": f"按计划持有：防守位 {_fmt(defend)}，触发即执行不犹豫"}

    return {
        "symbol": symbol,
        "interval": pos.interval,
        "price": price,
        "pnlPct": pnl_pct,
        "unrealizedR": unrealized_r,
        "mfeR": mfe_r,
        "maeR": mae_r,
        "barsHeld": bars_held,
        "scoreNow": score,
        "scoreAtOpen": score_at_open,
        "thesisState": thesis_state,
        "eventsSinceOpen": events_since_open,
        "takeProfitLadder": take_profit_ladder,
        "action": action,
        "levels": {
            "suggestedStop": suggested_stop,
            "beTrigger": be_trigger,
            "trailStop": trail_stop,
            "structureStop": structure_stop,
            "liqPrice": liq_price,
        },
        "items": items,
        "note": "建议为规则化提示（基于校准几何、开仓时点决策回放、持仓证据状态与当前盘面），非预测；请自行评估风险。",
    }
