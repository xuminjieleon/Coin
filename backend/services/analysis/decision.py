"""Composite scoring engine: regime-differentiated weights, MTF resonance,
CVD multi-timeframe confluence, sweep events and an executable trade plan.

Calibration notes:
  - Direction: no technical component predicts 1W direction above ~61% on the
    2y x 3-symbol sample; the composite keeps regime-differentiated weights
    (CVD moderate, negative-IC components zeroed, still displayed).
  - Trade plan geometry per interval. Round 11 (2026-08-22, profit-first
    multi-round loop on extended windows, tests/profit_sweep2.py):
    4h (3y x 3 symbols, 4506 decisions): 0.75 ATR pullback, 1.2 ATR stop,
        +0.5R scale-out+BE, runner trail 0.5R (no fixed target), texit 48,
        order valid 18 bars. Blind B+C +317.4R (incumbent +115.4R), EV
        +0.276R, win 84.8%, per-symbol BTC/ETH/SOL +170/+171/+177.
    1d (4y, 1575 decisions): 0.75/1.5/0.5R, trail 0.5R, texit 24, order
        valid 9 bars. Blind B+C +85.5R (incumbent +64.0R), EV +0.238R,
        win 86.4%, per-symbol +55/+64/+41.
    1w (10y-capped, 651 decisions): family inherited from 4h/1d (NOT tuned
        on 1w - sample too thin: ~67 blind fills; BTC marginal +2.9R), fill
        window 8 bars. Blind B+C +13.6R, EV +0.204R, win 86.6%.
    1h unchanged: non-loss-first geometry (blind ~98% non-loss, EV +0.09R).
    Gates tested twice (trend/range/vol/score-thresholds/conf/align/zone):
    all REDUCE total profit under profit-first objective; base gate kept.
    Note: 1w backtest records used warmup 170 (no EMA200) while production
    1w analysis may include it - second-order score-composition mismatch,
    documented. Fees/slippage NOT modeled (~maker 0.02%).
  - Round 12 (2026-08-24, tests/profit3_factors.py + profit3_weights.py):
    NEW data dimensions (derivs percentiles from Gate.io history, macro
    linkage) tested for profit-first integration — 9 gates (funding/OI/LSR/
    liquidation crowding, VIX/DXY/NDX risk-off) ALL reduced blind profit;
    score-component weight grid hurt 1d (incumbent best on A+B), improved
    4h by only ~2% blind (below the pre-registered 10% bar), 1w +19%
    relative but on a 36-fill sample. Round 12b (same day, user request):
    backtested-useless factors REMOVED from code and UI —
      * derivs/macro factorContext chips (round-12 verdict: no profit lift)
      * chart patterns / candlestick patterns (zero weight since round 6,
        negative attribution in every round; detection module deleted)
      * FVG / sweep / extension decision branches (dead code at weight 0)
    Kept because they are NOT decision factors or carry real weight:
      * FVG detection — anchors trade-plan entry zones (part of the
        round-11 validated geometry: zones = OB + FVG)
      * sweep events — feed the alert engine and chart 扫↑/扫↓ markers
      * funding/OI weighted components (10/8 and 10/6) — pre-existing
        weights from early calibration, now with a Gate.io daily fallback
        via derivs_store.daily_rates() when Binance is unreachable
    Score is unchanged by this cleanup (everything removed had weight 0
    or was display-only); regression-checked on live endpoints.
"""

NEAR_PCT = 0.03  # 3% proximity for OB / FVG
MAGNET_PCT = 0.05  # 5% proximity for liquidity pools
FUNDING_THRESHOLD = 0.0005  # 0.05%

# interval -> (depth ATR, stop ATR, BE trigger R, runner target R (None=trail
# managed), texit bars, runner trail R (None=off), order validity bars)
# Round 11 calibration (profit-first), see module docstring.
PLAN_GEOMETRY = {
    "1h": (0.75, 2.5, 0.10, 0.75, 96, None, 24),
    "4h": (0.75, 1.2, 0.50, None, 48, 0.50, 18),
    "1d": (0.75, 1.5, 0.50, None, 24, 0.50, 9),
    "1w": (0.75, 1.5, 0.50, None, 24, 0.75, 8),
}
PLAN_DEFAULT_INTERVAL = "1h"

# |score| threshold for generating a plan (per interval). Round 11: the edge
# lives in the entry+management layer, not signal strength — looser thresholds
# add profitable trades under capacity-constrained (serial single-position)
# execution on 4h/1d/1w. 1h keeps the conservative high-conviction gate.
PLAN_THRESHOLD = {
    "1h": 25,
    "4h": 10,
    "1d": 10,
    "1w": 10,
}

# Weighted components only. Removed at weight 0 after consistent negative
# attribution across calibration rounds (round 12b cleanup): fvg, sweep,
# chart_pat, candle, extension — see module docstring.
WEIGHTS = {
    "trending": {
        "structure": 30, "ema_stack": 8, "ob": 8, "mtf": 10, "cvd": 14,
        "cvd_conf": 9, "funding": 10, "oi": 10, "rsi_extreme": 0, "pd": 2,
        "magnet": 4, "wyckoff": 6,
    },
    "ranging": {
        "structure": 10, "ema_stack": 2, "ob": 10, "mtf": 8, "cvd": 16,
        "cvd_conf": 9, "funding": 8, "oi": 6, "rsi_extreme": 10, "pd": 5,
        "magnet": 6, "wyckoff": 8,
    },
}


def _last_valid(series: list) -> float | None:
    for v in reversed(series):
        if v is not None:
            return v
    return None


def _zone_weight(base: float, quality: float | None) -> float:
    """Scale zone contribution by its quality score (0-100)."""
    if quality is None:
        return base
    return base * (0.5 + quality / 200.0)


def build_summary(
    *,
    last_close: float,
    smc: dict,
    indicators: dict,
    volume_profile: dict,
    oi_change_pct: float | None = None,
    price_change_pct: float | None = None,
    funding_rate: float | None = None,
    wyckoff: dict | None = None,
    volatility: dict | None = None,
    cvd_div: dict | None = None,
    mtf: list[dict] | None = None,
    atr: float | None = None,
    interval: str | None = None,
) -> dict:
    components: list[dict] = []  # {text, direction, weight}

    def add(text: str, weight: float):
        if weight == 0:
            components.append({"text": text, "direction": "neutral", "weight": 0})
            return
        components.append({
            "text": text,
            "direction": "bullish" if weight > 0 else "bearish",
            "weight": weight,
        })

    price = last_close
    adx = _last_valid(indicators.get("adx14", []))
    regime = "trending" if (adx is not None and adx >= 25) else "ranging"
    w = WEIGHTS[regime]
    regime_note = "趋势市" if regime == "trending" else "震荡市"

    # --- structure trend (with age decay: stale breakouts carry less weight) ---
    events = smc["structureEvents"]
    structure_dir: str | None = None
    if events:
        last_ev = events[-1]
        structure_dir = last_ev["direction"]
        sgn = 1 if structure_dir == "bullish" else -1
        age = smc.get("trendAge")
        stale = age is not None and age > 60
        weight = w["structure"] * (0.5 if stale else 1.0)
        stale_note = "，趋势老化衰减" if stale else ""
        add(f"结构趋势{'看涨' if sgn > 0 else '看跌'}（最近事件 {last_ev['kind']}，{regime_note}权重{stale_note}）",
            sgn * weight)

    # --- EMA stack ---
    e20 = _last_valid(indicators.get("ema20", []))
    e50 = _last_valid(indicators.get("ema50", []))
    e200 = _last_valid(indicators.get("ema200", []))
    if e20 and e50 and e200:
        if e20 > e50 > e200:
            add("EMA 多头排列（20>50>200）", w["ema_stack"])
        elif e20 < e50 < e200:
            add("EMA 空头排列（20<50<200）", -w["ema_stack"])
        elif e20 > e50:
            add("短期 EMA 转强（20 上穿 50 附近）", w["ema_stack"] * 0.5)
        elif e20 < e50:
            add("短期 EMA 转弱（20 下穿 50 附近）", -w["ema_stack"] * 0.5)

    # --- premium / discount ---
    pd_zone = smc["premiumDiscount"]
    if w["pd"] > 0:
        if pd_zone["position"] == "discount":
            add(f"价格处于折价区（区间下半部，{regime_note}权重）", w["pd"])
        elif pd_zone["position"] == "premium":
            add(f"价格处于溢价区（区间上半部，{regime_note}权重）", -w["pd"])

    # --- order blocks (quality-scaled) ---
    if w["ob"] > 0:
        bull_obs = [ob for ob in smc["orderBlocks"] if ob["type"] == "bullish" and not ob["mitigated"]]
        below = [ob for ob in bull_obs if ob["top"] <= price and (price - ob["top"]) / price <= NEAR_PCT]
        if below:
            best = max(below, key=lambda ob: ob.get("quality") or 0)
            add(f"下方 3% 内存在未缓解看涨订单块（质量 {best.get('quality', 50)}）",
                _zone_weight(w["ob"], best.get("quality")))
        bear_obs = [ob for ob in smc["orderBlocks"] if ob["type"] == "bearish" and not ob["mitigated"]]
        above = [ob for ob in bear_obs if ob["bottom"] >= price and (ob["bottom"] - price) / price <= NEAR_PCT]
        if above:
            best = max(above, key=lambda ob: ob.get("quality") or 0)
            add(f"上方 3% 内存在未缓解看跌订单块（质量 {best.get('quality', 50)}）",
                -_zone_weight(w["ob"], best.get("quality")))

    # --- FVG as a DECISION FACTOR removed (round 12b): weight 0 with negative
    # attribution in every calibration round. FVG zones are still detected in
    # the SMC engine because the validated trade-plan entry logic anchors on
    # OB + FVG zones (round-11 geometry). ---

    # --- MTF resonance (higher timeframes) ---
    if mtf and structure_dir is not None:
        sgn = 1 if structure_dir == "bullish" else -1
        total = 0
        parts: list[str] = []
        for tf in mtf:
            tf_bias = tf.get("bias")
            if tf_bias == "bullish":
                total += 8
                parts.append(f"{tf['interval']}看多")
            elif tf_bias == "bearish":
                total -= 8
                parts.append(f"{tf['interval']}看空")
            else:
                parts.append(f"{tf['interval']}中性")
        total = max(-w["mtf"], min(w["mtf"], total))
        if total != 0:
            direction_txt = "共振偏多" if total > 0 else "共振偏空"
            add(f"多周期{'，'.join(parts)}（{direction_txt}）", total)

    # --- CVD divergence (current TF) ---
    if cvd_div:
        sgn = 1 if cvd_div["type"] == "bullish" else -1
        txt = "价格上涨但主动买盘走弱（疑似虚假突破）" if sgn < 0 else "价格下跌但主动买盘吸筹（下跌动能衰竭）"
        add(f"CVD{'看跌' if sgn < 0 else '看涨'}背离（强度 {cvd_div.get('strength', 50)}）：{txt}",
            sgn * w["cvd"])

    # --- CVD multi-timeframe confluence (the strongest attributed signal) ---
    cvd_dirs: list[str] = []
    if cvd_div:
        cvd_dirs.append(cvd_div["type"])
    for tf in mtf or []:
        d = tf.get("cvdDiv")
        if d:
            cvd_dirs.append(d)
    bulls = sum(1 for d in cvd_dirs if d == "bullish")
    bears = sum(1 for d in cvd_dirs if d == "bearish")
    conf_dir: str | None = None
    conf_count = 0
    if bulls >= 2 and bulls > bears:
        conf_dir, conf_count = "bullish", bulls
    elif bears >= 2 and bears > bulls:
        conf_dir, conf_count = "bearish", bears
    if conf_dir and w["cvd_conf"] > 0:
        sgn = 1 if conf_dir == "bullish" else -1
        add(f"CVD 多周期共振（{conf_count} 个周期背离一致）", sgn * w["cvd_conf"] * min(conf_count - 1, 2))

    # --- liquidity sweep events as a DECISION FACTOR removed (round 12b):
    # weight 0, negative attribution since round 2. Sweep events are still
    # detected for the alert engine and the chart 扫↑/扫↓ markers. ---

    # --- OI x price confirmation (injected; router applies the
    # derivs_store.daily_rates fallback when Binance live data is missing) ---
    if oi_change_pct is not None and price_change_pct is not None:
        if price_change_pct > 0 and oi_change_pct > 0:
            add("价格上涨且持仓量增加（趋势确认）", w["oi"])
        elif price_change_pct < 0 and oi_change_pct > 0:
            add("价格下跌且持仓量增加（空头确认）", -w["oi"])
        elif price_change_pct > 0 and oi_change_pct < 0:
            add("价格上涨但持仓量下降（疑似空头回补）", -w["oi"] * 0.5)
        elif price_change_pct < 0 and oi_change_pct < 0:
            add("价格下跌但持仓量下降（疑似多头回补）", w["oi"] * 0.5)

    # --- funding rate (injected) ---
    if funding_rate is not None:
        if funding_rate > FUNDING_THRESHOLD:
            add("资金费率显著为正（多头过热，反向信号）", -w["funding"])
        elif funding_rate < -FUNDING_THRESHOLD:
            add("资金费率显著为负（空头过热，反向信号）", w["funding"])

    # --- derivs / macro factor chips REMOVED (round 12b): gates and score
    # weights both failed the pre-registered profit-first acceptance bar
    # (tests/profit3_factors.py, tests/profit3_weights.py). ---

    # --- RSI (regime-differentiated) ---
    rsi = _last_valid(indicators.get("rsi14", []))
    if rsi is not None and w["rsi_extreme"] > 0:
        if rsi > 70:
            add(f"RSI 超买（{rsi:.0f}，震荡市反向信号）", -w["rsi_extreme"])
        elif rsi < 30:
            add(f"RSI 超卖（{rsi:.0f}，震荡市反向信号）", w["rsi_extreme"])

    # --- chart patterns / candlestick patterns / extension guard REMOVED
    # (round 12b): all three sat at weight 0 with consistently negative
    # attribution across calibration rounds — see module docstring. ---

    # --- liquidity magnet ---
    buy_pools = [p for p in smc["liquidityPools"] if p["type"] == "buy_side" and p["price"] >= price]
    near_buy = [p for p in buy_pools if (p["price"] - price) / price <= MAGNET_PCT]
    if near_buy:
        add("上方 5% 内存在买方流动性池（磁吸效应）", w["magnet"])
    sell_pools = [p for p in smc["liquidityPools"] if p["type"] == "sell_side" and p["price"] <= price]
    near_sell = [p for p in sell_pools if (price - p["price"]) / price <= MAGNET_PCT]
    if near_sell:
        add("下方 5% 内存在卖方流动性池（磁吸效应）", -w["magnet"])

    # --- Wyckoff ---
    if wyckoff and wyckoff.get("phase") not in (None, "none"):
        phase_txt = {
            "accumulation": "吸筹区间（Wyckoff）", "distribution": "派发区间（Wyckoff）",
            "markup": "拉升阶段（Wyckoff）", "markdown": "下跌阶段（Wyckoff）",
        }
        sgn = 1 if wyckoff["phase"] in ("accumulation", "markup") else -1
        add(f"当前处于{phase_txt.get(wyckoff['phase'], wyckoff['phase'])}", sgn * w["wyckoff"])
    if wyckoff:
        for ev in wyckoff.get("events") or []:
            if ev["type"] == "spring":
                add("Wyckoff Spring：区间下沿被刺破后收回", w["wyckoff"])
            elif ev["type"] == "utad":
                add("Wyckoff UTAD：区间上沿上冲后回落", -w["wyckoff"])

    # --- volatility state hint ---
    if volatility:
        if volatility.get("squeeze"):
            add("波动率压缩（布林挤压 + ATR 低百分位），注意突破方向选择", 0)
        elif volatility.get("state") == "expanded":
            add("波动率放大（ATR 高百分位），注意止损空间与假突破", 0)

    # --- aggregate ---
    score = sum(c["weight"] for c in components)
    score = max(-100, min(100, round(score)))
    if score >= 15:
        bias = "bullish"
    elif score <= -15:
        bias = "bearish"
    else:
        bias = "neutral"

    # --- key levels ---
    key_levels: list[dict] = []
    if buy_pools:
        nearest_buy = min(buy_pools, key=lambda p: p["price"])
        key_levels.append({"price": nearest_buy["price"], "label": "买方流动性池"})
    if sell_pools:
        nearest_sell = max(sell_pools, key=lambda p: p["price"])
        key_levels.append({"price": nearest_sell["price"], "label": "卖方流动性池"})
    key_levels.append({"price": volume_profile["poc"], "label": "POC 控制点"})
    if volume_profile.get("developingPoc") and volume_profile["developingPoc"] != volume_profile["poc"]:
        key_levels.append({"price": volume_profile["developingPoc"], "label": "动态 POC"})
    key_levels.append({"price": pd_zone["rangeHigh"], "label": "区间高点"})
    key_levels.append({"price": pd_zone["rangeLow"], "label": "区间低点"})
    key_levels = key_levels[:8]

    reasons = sorted(components, key=lambda c: -abs(c["weight"]))[:10]

    # --- confidence: multi-TF CVD confluence is the validated high-confidence signal ---
    high_confidence = conf_dir is not None and conf_count >= 2
    confidence_dir = conf_dir

    # --- trade plan ---
    trade_plan = _build_trade_plan(
        bias=bias, score=score, price=price, smc=smc, atr=atr,
        buy_pools=buy_pools, sell_pools=sell_pools, pd_zone=pd_zone,
        high_confidence=high_confidence, confidence_dir=confidence_dir,
        interval=interval,
    )

    return {
        "score": score,
        "bias": bias,
        "regime": regime,
        "keyLevels": key_levels,
        "reasons": reasons,
        "tradePlan": trade_plan,
        "highConfidence": high_confidence,
        "cvdConfluence": {"direction": conf_dir, "count": conf_count} if conf_dir else None,
    }


def _build_trade_plan(*, bias: str, score: int, price: float, smc: dict, atr: float | None,
                      buy_pools: list, sell_pools: list, pd_zone: dict,
                      high_confidence: bool = False, confidence_dir: str | None = None,
                      interval: str | None = None) -> dict | None:
    """Executable setup; geometry per interval, see PLAN_GEOMETRY and the
    module docstring for the walk-forward validation behind each."""
    if atr is None or atr <= 0:
        return None
    threshold = PLAN_THRESHOLD.get(interval or PLAN_DEFAULT_INTERVAL,
                                   PLAN_THRESHOLD[PLAN_DEFAULT_INTERVAL])
    if high_confidence and confidence_dir:
        long = confidence_dir == "bullish"
    elif abs(score) >= threshold:
        long = score > 0
    else:
        return None

    depth, stopw, be_frac, tgt_r, texit, trail, fill_bars = PLAN_GEOMETRY.get(
        interval or PLAN_DEFAULT_INTERVAL, PLAN_GEOMETRY[PLAN_DEFAULT_INTERVAL])
    # entry: nearest quality zone edge within `depth` ATR, else pullback
    if long:
        zones = [z for z in smc["orderBlocks"] + smc["fvgs"]
                 if z["type"] == "bullish" and not z["mitigated"] and z["top"] <= price]
        near = [z for z in zones if price - z["top"] <= depth * atr]
        if near:
            zone = max(near, key=lambda z: z.get("quality") or 0)
            entry = min(price, zone["top"])
        else:
            entry = price - depth * atr
    else:
        zones = [z for z in smc["orderBlocks"] + smc["fvgs"]
                 if z["type"] == "bearish" and not z["mitigated"] and z["bottom"] >= price]
        near = [z for z in zones if z["bottom"] - price <= depth * atr]
        if near:
            zone = max(near, key=lambda z: z.get("quality") or 0)
            entry = max(price, zone["bottom"])
        else:
            entry = price + depth * atr

    stop = entry - stopw * atr if long else entry + stopw * atr
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    t1 = None
    be_trigger = entry + be_frac * risk if long else entry - be_frac * risk
    src = "CVD 多周期共振方向" if high_confidence else "结构评分方向"
    if trail is not None and tgt_r is None:
        # trail-managed runner (4h/1d/1w family)
        note = (
            f"{src}（|评分|≥{threshold}）；回踩 {depth}×ATR 限价入场（区域边缘更优），{stopw}×ATR 止损；"
            f"触及 +{be_frac}R 先出半仓锁定利润并将止损移至入场价；剩余半仓不设固定目标，"
            f"以 {trail}R 跟踪止盈（自最高盈利回撤 {trail}R 即离场），{texit} 根 K 线未离场则市价离场；"
            f"限价单 {fill_bars} 根未成交自动撤单；建议单仓位串行执行（持仓期间忽略新信号，"
            f"未成交挂单被新信号替换）"
        )
        rr = None
    elif tgt_r is not None:
        t1 = entry + tgt_r * risk if long else entry - tgt_r * risk
        note = (
            f"{src}；回踩 {depth}×ATR 限价入场，{stopw}×ATR 止损；触及 +{be_frac}R 先出半仓锁定利润并将止损移至入场价"
            f"（保本管理），剩余半仓目标 +{tgt_r}R，{texit} 根 K 线未触发则市价离场；"
            f"限价单 {fill_bars} 根未成交自动撤单"
        )
        rr = round(0.5 * be_frac + 0.5 * tgt_r, 2)
    else:
        return None
    return {
        "direction": "long" if long else "short",
        "entry": round(float(entry), 8),
        "stop": round(float(stop), 8),
        "target1": round(float(t1), 8) if t1 is not None else None,
        "target2": None,
        "beTrigger": round(float(be_trigger), 8),
        "beR": be_frac,
        "targetR": tgt_r,
        "scaleOut": True,
        "trailR": trail,
        "stopAtr": stopw,
        "depthAtr": depth,
        "texitBars": texit,
        "fillBars": fill_bars,
        "rr": rr,
        "note": note,
    }
