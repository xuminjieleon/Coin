"""Hand-crafted candle test for the SMC engine.

Scenario: rally forms a swing high, pullback forms a swing low,
then price breaks above the swing high -> expect:
  - bullish BOS structure event
  - a bullish order block
  - at least one bullish FVG

Run: python tests\\test_smc.py   (plain asserts, no pytest required)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from services.analysis import smc, swings

# (open, high, low, close)
RAW = [
    (10.0, 10.5, 9.8, 10.2),   # 0
    (10.2, 11.0, 10.1, 10.9),  # 1
    (10.9, 11.5, 10.8, 11.4),  # 2
    (11.4, 12.0, 11.3, 11.9),  # 3
    (11.9, 12.5, 11.8, 12.4),  # 4
    (12.4, 13.0, 12.3, 12.9),  # 5
    (12.9, 13.5, 12.8, 13.4),  # 6  <- swing high 13.5
    (13.4, 13.2, 12.9, 13.0),  # 7
    (13.0, 13.1, 12.6, 12.8),  # 8
    (12.8, 12.9, 12.3, 12.5),  # 9
    (12.5, 12.6, 12.0, 12.2),  # 10 <- swing low 12.0
    (12.2, 12.7, 12.1, 12.6),  # 11
    (12.6, 12.9, 12.4, 12.8),  # 12
    (12.8, 13.2, 12.7, 13.1),  # 13
    (13.1, 13.8, 13.3, 13.7),  # 14 <- close 13.7 breaks 13.5 -> BOS
    (13.7, 14.2, 13.6, 14.1),  # 15 (low 13.6 > high[13] 13.2 -> FVG at 14)
    (14.1, 14.5, 14.0, 14.4),  # 16
]


def make_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "time": i * 3_600_000,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": 100.0,
            }
            for i, (o, h, lo, c) in enumerate(RAW)
        ]
    )


def test_swings_detected():
    df = make_df()
    sw = swings.detect_swings(df)
    highs = [s for s in sw if s["kind"] == "high"]
    lows = [s for s in sw if s["kind"] == "low"]
    assert any(s["index"] == 6 and abs(s["price"] - 13.5) < 1e-9 for s in highs), \
        f"swing high at idx6 not found: {highs}"
    assert any(s["index"] == 10 and abs(s["price"] - 12.0) < 1e-9 for s in lows), \
        f"swing low at idx10 not found: {lows}"
    # last 2 candles never produce swings
    assert all(s["index"] < len(df) - 2 for s in sw)


def test_bullish_bos_orderblock_fvg():
    df = make_df()
    sw = swings.detect_swings(df)
    result = smc.analyze(df, sw)

    # 1. bullish BOS
    bos = [e for e in result["structureEvents"]
           if e["kind"] == "BOS" and e["direction"] == "bullish"]
    assert bos, f"no bullish BOS found: {result['structureEvents']}"
    assert abs(bos[0]["price"] - 13.5) < 1e-9, f"BOS price should be broken swing 13.5: {bos}"

    # 2. bullish order block (last bearish candle before breakout: index 10)
    bull_obs = [ob for ob in result["orderBlocks"] if ob["type"] == "bullish"]
    assert bull_obs, "no bullish order block found"
    ob = bull_obs[-1]
    assert abs(ob["top"] - 12.6) < 1e-9 and abs(ob["bottom"] - 12.0) < 1e-9, \
        f"unexpected OB range: {ob}"
    assert ob["mitigated"] is False

    # 3. bullish FVG (at candle 14: low[15]=13.6 > high[13]=13.2)
    bull_fvgs = [f for f in result["fvgs"] if f["type"] == "bullish"]
    assert bull_fvgs, "no bullish FVG found"
    target = [f for f in bull_fvgs if abs(f["bottom"] - 13.2) < 1e-9 and abs(f["top"] - 13.6) < 1e-9]
    assert target, f"expected FVG [13.2, 13.6] not found: {bull_fvgs}"
    assert target[0]["startTime"] == 14 * 3_600_000
    assert target[0]["mitigated"] is False

    # 4. premium/discount present and coherent
    pdz = result["premiumDiscount"]
    assert pdz["rangeHigh"] > pdz["rangeLow"]
    assert 0.0 <= pdz["pct"] <= 1.0


if __name__ == "__main__":
    test_swings_detected()
    print("PASS test_swings_detected")
    test_bullish_bos_orderblock_fvg()
    print("PASS test_bullish_bos_orderblock_fvg")
    print("ALL TESTS PASSED")
