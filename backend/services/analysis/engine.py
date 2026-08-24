"""Full analysis pipeline over one kline dataframe. Shared by the analysis
router (current TF) and the MTF context (higher TFs)."""
from services.analysis import indicators, smc, swings, volume, wyckoff


def _last_valid(series: list) -> float | None:
    for v in reversed(series):
        if v is not None:
            return v
    return None


def full_analysis(df, prev_day: dict | None = None) -> dict:
    swing_list = swings.detect_swings(df)

    ind = {
        "ema20": indicators.ema(df, 20),
        "ema50": indicators.ema(df, 50),
        "ema200": indicators.ema(df, 200),
        "rsi14": indicators.rsi(df, 14),
        "atr14": indicators.atr(df, 14),
        "adx14": indicators.adx(df, 14),
    }
    atr_last = _last_valid(ind["atr14"])

    smc_result = smc.analyze(df, swing_list, prev_day=prev_day, atr_series=ind["atr14"])

    vp = volume.volume_profile(df)
    dev_series = volume.developing_poc_series(df)
    vp["pocSeries"] = dev_series
    vp["developingPoc"] = dev_series[-1]["poc"] if dev_series else vp["poc"]

    cvd_series = indicators.cvd(df)
    ind["cvd"] = cvd_series
    cvd_div = indicators.cvd_divergence(df, cvd_series)

    vol_state = indicators.volatility_state(df, ind["atr14"])

    # patterns (chart / candlestick) removed in round 12b: zero-weight with
    # consistently negative attribution — see decision.py docstring.
    wy = wyckoff.analyze(df, swing_list, smc_result, atr_last)

    return {
        "smc": smc_result,
        "indicators": ind,
        "volumeProfile": vp,
        "wyckoff": wy,
        "volatility": vol_state,
        "cvdDivergence": cvd_div,
    }
