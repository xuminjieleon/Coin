"""Composite scoring engine: regime-differentiated weights, MTF resonance,
CVD multi-timeframe confluence, sweep events and an executable trade plan.

Calibration notes:
  - Direction: no technical component predicts 1W direction above ~61% on the
    2y x 3-symbol sample; the composite keeps regime-differentiated weights
    (CVD moderate, negative-IC components zeroed, still displayed).
  - Trade plan (the validated deliverable), walk-forward calibrated in
    tests/plan_sweep.py with 40/30/30 folds: tune A -> blind B 94.9%,
    re-tune A+B -> blind C 91.0% non-loss rate (win + breakeven exit), EV
    +0.15R per filled trade; C-fold thinning 90.4-91.2%, per-symbol
    90.1-92.3%. Geometry: 0.75 ATR pullback, 1.5 ATR stop, BE at +0.25R,
    T1 0.75R, time exit 96 bars. Timeouts marked to market (strict).
"""

NEAR_PCT = 0.03  # 3% proximity for OB / FVG
MAGNET_PCT = 0.05  # 5% proximity for liquidity pools
FUNDING_THRESHOLD = 0.0005  # 0.05%

WEIGHTS = {
    "trending": {
        "structure": 30, "ema_stack": 8, "ob": 8, "fvg": 0, "mtf": 10, "cvd": 14,
        "cvd_conf": 9, "sweep": 0, "funding": 10, "oi": 10, "rsi_extreme": 0, "pd": 2,
        "chart_pat": 0, "candle": 0, "magnet": 4, "wyckoff": 6, "extension": 0,
    },
    "ranging": {
        "structure": 10, "ema_stack": 2, "ob": 10, "fvg": 0, "mtf": 8, "cvd": 16,
        "cvd_conf": 9, "sweep": 0, "funding": 8, "oi": 6, "rsi_extreme": 10, "pd": 5,
        "chart_pat": 0, "candle": 0, "magnet": 6, "wyckoff": 8, "extension": 0,
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
    patterns: dict | None = None,
    wyckoff: dict | None = None,
    volatility: dict | None = None,
    cvd_div: dict | None = None,
    mtf: list[dict] | None = None,
    atr: float | None = None,
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

    # --- FVGs (quality-scaled; zero weight -> display only) ---
    if w["fvg"] > 0:
        bull_fvgs = [f for f in smc["fvgs"] if f["type"] == "bullish" and not f["mitigated"]]
        below_f = [f for f in bull_fvgs if f["top"] <= price and (price - f["top"]) / price <= NEAR_PCT]
        if below_f:
            best = max(below_f, key=lambda f: f.get("quality") or 0)
            add(f"下方 3% 内存在未回补看涨 FVG（质量 {best.get('quality', 50)}）",
                _zone_weight(w["fvg"], best.get("quality")))
        bear_fvgs = [f for f in smc["fvgs"] if f["type"] == "bearish" and not f["mitigated"]]
        above_f = [f for f in bear_fvgs if f["bottom"] >= price and (f["bottom"] - price) / price <= NEAR_PCT]
        if above_f:
            best = max(above_f, key=lambda f: f.get("quality") or 0)
            add(f"上方 3% 内存在未回补看跌 FVG（质量 {best.get('quality', 50)}）",
                -_zone_weight(w["fvg"], best.get("quality")))

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

    # --- liquidity sweep events (weight 0 = display/alert only) ---
    if w["sweep"] > 0:
        sweeps = smc.get("sweepEvents") or []
        recent_sweeps = sweeps[-2:] if sweeps else []
        for ev in recent_sweeps:
            if ev["side"] == "buy_side" and ev["outcome"] == "reclaimed":
                add("上方买方流动性被扫后收回（短线诱多，反转信号）", -w["sweep"])
            elif ev["side"] == "sell_side" and ev["outcome"] == "reclaimed":
                add("下方卖方流动性被扫后收回（短线诱空，反转信号）", w["sweep"])
            elif ev["side"] == "buy_side" and ev["outcome"] == "broken":
                add("上方买方流动性被有效突破（延续看涨）", w["sweep"])
            elif ev["side"] == "sell_side" and ev["outcome"] == "broken":
                add("下方卖方流动性被有效跌破（延续看跌）", -w["sweep"])

    # --- OI x price confirmation (injected) ---
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

    # --- RSI (regime-differentiated) ---
    rsi = _last_valid(indicators.get("rsi14", []))
    if rsi is not None and w["rsi_extreme"] > 0:
        if rsi > 70:
            add(f"RSI 超买（{rsi:.0f}，震荡市反向信号）", -w["rsi_extreme"])
        elif rsi < 30:
            add(f"RSI 超卖（{rsi:.0f}，震荡市反向信号）", w["rsi_extreme"])

    # --- chart patterns (zero weight -> display only) ---
    if patterns and w["chart_pat"] > 0:
        for cp in (patterns.get("charts") or [])[-2:]:
            sgn = 1 if cp["direction"] == "bullish" else (-1 if cp["direction"] == "bearish" else 0)
            if sgn != 0:
                names = {
                    "double_top": "双顶", "double_bottom": "双底",
                    "head_shoulders_top": "头肩顶", "head_shoulders_bottom": "头肩底",
                }
                nm = names.get(cp["type"], cp["type"])
                add(f"图表形态「{nm}」已确认（置信度 {cp.get('confidence', 0.6):.0%}）",
                    sgn * w["chart_pat"] * cp.get("confidence", 0.6))

    # --- candlestick patterns (zero weight -> display only) ---
    if patterns and w["candle"] > 0:
        last_idx = patterns.get("lastIndex")
        cand = [
            c for c in (patterns.get("candles") or [])
            if c["direction"] != "neutral"
            and (last_idx is None or c.get("index") is None or last_idx - c["index"] <= 8)
        ]
        if cand:
            c = cand[-1]
            names = {
                "bullish_engulfing": "看涨吞没", "bearish_engulfing": "看跌吞没",
                "bullish_pinbar": "看涨 PinBar", "bearish_pinbar": "看跌 PinBar",
                "morning_star": "晨星", "evening_star": "暮星",
            }
            nm = names.get(c["type"], c["type"])
            add(f"近期 K 线形态「{nm}」", (1 if c["direction"] == "bullish" else -1) * w["candle"])

    # --- trend extension guard (zero weight: negative attribution) ---
    if e20 and atr and w["extension"] > 0:
        ext = (price - e20) / atr
        if ext > 2.5:
            add(f"价格短期过热（偏离 EMA20 达 {ext:.1f}×ATR，回撤风险）", -w["extension"])
        elif ext < -2.5:
            add(f"价格短期超卖（偏离 EMA20 达 {abs(ext):.1f}×ATR，反弹概率）", w["extension"])

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
                      high_confidence: bool = False, confidence_dir: str | None = None) -> dict | None:
    """Executable setup, geometry calibrated by walk-forward sweep
    (tests/plan_sweep.py: tune A -> blind B 94.9%, re-tune A+B -> blind C 91.0%,
    both phases selected the SAME config):

      pullback entry 0.75 ATR (zone edge within 0.75 ATR refines it)
      stop           1.5 ATR
      BE trigger     +0.25 R   (stop -> entry after a small favorable move)
      target1        +0.75 R
      target2        +1.5  R   (runner; backtest metric is T1 with BE mgmt)
      time exit      96 bars   (~4 days on 1h)

    Direction: high-confidence CVD confluence when present, else the composite
    bias at |score| >= 25. EV constraint held at +0.15R per filled trade.
    """
    if atr is None or atr <= 0:
        return None
    if high_confidence and confidence_dir:
        long = confidence_dir == "bullish"
    elif abs(score) >= 25 and bias != "neutral":
        long = bias == "bullish"
    else:
        return None

    depth, stopw = 0.75, 1.5
    # entry: nearest quality zone edge within 0.75 ATR, else pullback
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
    t1 = entry + 0.75 * risk if long else entry - 0.75 * risk
    t2 = entry + 1.5 * risk if long else entry - 1.5 * risk
    be_trigger = entry + 0.25 * risk if long else entry - 0.25 * risk
    note = (
        "CVD 多周期共振方向；回踩 0.75×ATR 限价入场，1.5×ATR 止损；"
        "触及 +0.25R 即将止损移至入场价（保本管理），目标 0.75R/1.5R，96 根 K 线未触发则市价离场"
        if high_confidence
        else "结构评分方向；回踩 0.75×ATR 限价入场，1.5×ATR 止损；"
        "触及 +0.25R 即将止损移至入场价（保本管理），目标 0.75R/1.5R，96 根 K 线未触发则市价离场"
    )
    return {
        "direction": "long" if long else "short",
        "entry": round(float(entry), 8),
        "stop": round(float(stop), 8),
        "target1": round(float(t1), 8),
        "target2": round(float(t2), 8),
        "beTrigger": round(float(be_trigger), 8),
        "rr": 0.75,
        "note": note,
    }
