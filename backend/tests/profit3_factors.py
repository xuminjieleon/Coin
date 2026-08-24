"""Round 12: new-factor backtest (derivatives history + macro), profit-first.

User spec (2026-08-24): the new data services (derivs percentiles, macro
linkage) must be considered by the decision engine, backtested with the same
protocol as before — total profit first. Two deliverables:
  a) factor diagnostics + gate sweep on the round-11 production geometry;
  b) if factors survive, integrate into decision.py (weights / plan gate).

Data (local, immutable):
  derivs.db gateio_stats 1d x1000 per symbol (2023-11..2026-08): funding,
  oi_usd, lsr, top_lsr, long/short liq USD. A daily row is usable only when
  FULLY CLOSED: row_ts + 86400 <= decision time (seconds vs ms!). Rolling
  percentiles use trailing 365 rows only (no lookahead).
  macro.db (5y daily): vix / dxy / ndx / gold / tnx. Last close strictly
  before the decision's UTC date.

Protocol: same as round 11 — records cached from profit_sweep2 (engine
unchanged), folds A 40% / B 30% / C 30% by time, capacity-constrained serial
execution (one position per symbol), A+B selection, blind B+C reported once.
Acceptance for integrating a gate (pre-registered): blind B+C totalR beats
incumbent by >10% AND every per-symbol blind EV stays positive. Honest note:
blind folds were already spent in round 11 — this is a NEW data dimension,
not parameter re-tuning, but confidence is still discounted; anything below
the bar integrates as display-only (weight 0).

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/profit3_factors.py
"""
import asyncio
import bisect
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import profit_sweep2 as ps
from profit2_cap import capacity_eval, fmt as cap_fmt
from profit2_r5 import with_loose_plans

GEOS = {  # round-11 production geometry, fill multiplier
    "4h": ((0.75, 1.2, 0.5, None, 48, 0.5), 1.5),
    "1d": ((0.75, 1.5, 0.5, None, 24, 0.5), 1.5),
    "1w": ((0.75, 1.5, 0.5, None, 24, 0.75), 2.0),
}
TH = 10
DERIVS_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cache", "derivs.db")
MACRO_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cache", "macro.db")

GATES = {
    # derivs positioning (contrarian: skip the crowded side)
    "fund_hi_long":  lambda r: not (r.get("dir") == "long" and (r.get("funding_pctl") or 50) >= 90),
    "fund_lo_short": lambda r: not (r.get("dir") == "short" and (r.get("funding_pctl") or 50) <= 10),
    "lsr_hi_long":   lambda r: not (r.get("dir") == "long" and (r.get("lsr_pctl") or 50) >= 85),
    "lsr_lo_short":  lambda r: not (r.get("dir") == "short" and (r.get("lsr_pctl") or 50) <= 15),
    "liq_hi":        lambda r: (r.get("liq_pctl") or 0) < 95,
    # macro risk-off
    "vix_hi":        lambda r: (r.get("vix") or 15) < 28,
    "dxy_up_long":   lambda r: not (r.get("dir") == "long" and (r.get("dxy_5d") or 0) > 1.0),
    "ndx_dn_long":   lambda r: not (r.get("dir") == "long" and (r.get("ndx_5d") or 0) < -4.0),
    "ndx_dn_all":    lambda r: (r.get("ndx_5d") or 0) > -5.0,
}


def load_derivs_daily() -> dict:
    """symbol -> list of rows sorted by ts (seconds)."""
    conn = sqlite3.connect(DERIVS_DB)
    out: dict = {}
    for sym in ps.SYMBOLS:
        cur = conn.execute(
            "SELECT ts, funding_rate, oi_usd, lsr_account, top_lsr, long_liq_usd, short_liq_usd "
            "FROM gateio_stats WHERE symbol=? AND interval='1d' ORDER BY ts", (sym,))
        rows = []
        for ts, fr, oi, lsr, top, llq, slq in cur.fetchall():
            rows.append({
                "ts": int(ts), "funding": fr, "oi": oi, "lsr": lsr, "top": top,
                "liq": (llq or 0) + (slq or 0) if (llq is not None or slq is not None) else None,
            })
        out[sym] = rows
    conn.close()
    return out


def load_macro() -> dict:
    """key -> (dates list sorted, closes list)."""
    conn = sqlite3.connect(MACRO_DB)
    out: dict = {}
    for key in ("vix", "dxy", "ndx", "gold", "tnx"):
        cur = conn.execute("SELECT date, close FROM macro_series WHERE key=? ORDER BY date", (key,))
        pairs = cur.fetchall()
        if pairs:
            out[key] = ([p[0] for p in pairs], [float(p[1]) for p in pairs])
    conn.close()
    return out


def _pctl_trailing(vals: list, i: int, window: int = 365) -> float | None:
    lo = max(0, i - window + 1)
    seg = [v for v in vals[lo:i + 1] if v is not None]
    if not seg or seg[-1] is None or len(seg) < 30:
        return None
    x = seg[-1]
    return sum(1 for v in seg if v <= x) / len(seg) * 100.0


def attach_factors(records: list, derivs: dict, macro: dict) -> list:
    for r in records:
        sym = r["symbol"]
        t = r["time"]  # ms, candle open time
        # --- derivs: last FULLY CLOSED daily row (row end <= candle close) ---
        rows = derivs.get(sym) or []
        ts_list = [row["ts"] for row in rows]
        # candle close = t + step; use step from record's tf via lookup on caller? approximate:
        # closed means row end (ts + 86400) <= decision moment. Decision moment = candle close.
        # The caller passes records per tf; we get step from ps.CFG via records' tf tag set by caller.
        close_s = (t + r["_step_ms"]) / 1000.0
        i = bisect.bisect_right(ts_list, close_s - 86400.0 + 1e-9) - 1 if ts_list else -1
        # ensure row end <= close: row ts <= close - 86400
        if i >= 0 and ts_list[i] <= close_s - 86400.0:
            row = rows[i]
            fundings = [x["funding"] for x in rows]
            ois = [x["oi"] for x in rows]
            lsrs = [x["lsr"] for x in rows]
            liqs = [x["liq"] for x in rows]
            r["funding"] = row["funding"]
            r["funding_pctl"] = _pctl_trailing(fundings, i)
            r["oi_pctl"] = _pctl_trailing(ois, i)
            r["lsr_pctl"] = _pctl_trailing(lsrs, i)
            r["liq_pctl"] = _pctl_trailing(liqs, i, 90)
            r["liq_usd"] = row["liq"]
            r["top_lsr"] = row["top"]
            # OI 1d change from previous row
            if i > 0 and rows[i - 1]["oi"] and row["oi"]:
                r["oi_chg_1d"] = (row["oi"] - rows[i - 1]["oi"]) / rows[i - 1]["oi"] * 100.0
        # --- macro: last close strictly before the decision's UTC date ---
        d = datetime.fromtimestamp(t / 1000.0, tz=timezone.utc)
        dstr = d.strftime("%Y-%m-%d")
        for key in ("vix", "dxy", "ndx", "gold", "tnx"):
            dates, closes = macro.get(key, ([], []))
            j = bisect.bisect_left(dates, dstr) - 1
            if j >= 0:
                r[key] = closes[j]
                k5 = j - 5
                if k5 >= 0 and closes[k5]:
                    r[f"{key}_5d"] = (closes[j] - closes[k5]) / closes[k5] * 100.0
    return records


def coverage_report(records: list, tag: str) -> None:
    n = len(records)
    have_f = sum(1 for r in records if r.get("funding_pctl") is not None)
    have_v = sum(1 for r in records if r.get("vix") is not None)
    have_t = sum(1 for r in records if r.get("top_lsr") is not None)
    have_l = sum(1 for r in records if r.get("liq_pctl") is not None)
    print(f"  [{tag}] records={n} derivs覆盖={have_f}({have_f/max(n,1)*100:.0f}%) "
          f"liq覆盖={have_l} macro覆盖={have_v} topLsr覆盖={have_t}")


def factor_diagnostics(records: list, tag: str) -> None:
    """Forward 24-bar return IC + directional hit of each factor (A+B only)."""
    print(f"\n  -- 因子诊断 [{tag}]（前瞻 24 根收益的秩相关 IC 与分桶方向命中率）--")
    factors = [
        ("funding_pctl", +1, "资金费率百分位(反向)"),
        ("oi_pctl", +1, "OI 百分位"),
        ("lsr_pctl", -1, "散户多空比百分位(反向)"),
        ("top_lsr", +1, "大户多空比(同向)"),
        ("liq_pctl", +1, "清算烈度百分位"),
        ("vix", -1, "VIX(反向)"),
        ("dxy_5d", -1, "美元 5 日变动(反向)"),
        ("ndx_5d", +1, "纳指 5 日变动(同向)"),
        ("gold_5d", +1, "黄金 5 日变动"),
        ("tnx_5d", -1, "美债收益率 5 日变动(反向)"),
    ]
    rets = np.array([r.get("ret_24") if r.get("ret_24") is not None else np.nan for r in records])
    for key, prior, label in factors:
        vals = np.array([r.get(key) if r.get(key) is not None else np.nan for r in records])
        m = ~(np.isnan(vals) | np.isnan(rets))
        if m.sum() < 200:
            print(f"  {label:<22} n={int(m.sum()):<5} 样本不足")
            continue
        v, rr = vals[m], rets[m]
        if np.std(v) == 0:
            continue
        rank_v = np.argsort(np.argsort(v)).astype(float)
        rank_r = np.argsort(np.argsort(rr)).astype(float)
        ic = float(np.corrcoef(rank_v, rank_r)[0, 1])
        # top/bottom tercile direction hit vs prior sign
        q1, q2 = np.quantile(v, 1 / 3), np.quantile(v, 2 / 3)
        hi = rr[v >= q2].mean()
        lo = rr[v <= q1].mean()
        print(f"  {label:<22} n={int(m.sum()):<5} IC={ic:+.3f}  低1/3均收益={lo*100:+.2f}%  高1/3均收益={hi*100:+.2f}%")


def gate_sweep(tf: str, records: list, dfs, tidx) -> None:
    geo, fill = GEOS[tf]
    FA, FB, FC = ps.folds(records)
    recs10 = with_loose_plans(records, TH)
    for r in recs10:
        r["dir"] = r.get("plan")
    FA10 = with_loose_plans(FA, TH)
    FB10 = with_loose_plans(FB, TH)
    FC10 = with_loose_plans(FC, TH)
    for lst in (FA10, FB10, FC10, recs10):
        for r in lst:
            r["dir"] = r.get("plan")

    print(f"\n===== {tf} 门控扫描（容量约束，th={TH}，A+B 选择）=====")
    inc_a = capacity_eval(FA10, geo, dfs, tidx, tf, fill)
    inc_b = capacity_eval(FB10 + FC10, geo, dfs, tidx, tf, fill)
    print(f"  incumbent(无门控) A+B[{cap_fmt(inc_a)}]")
    print(f"                     blind[{cap_fmt(inc_b)}]")

    results = {}
    for gname, gfn in GATES.items():
        sel = [r for r in FA10 if gfn(r)]
        res = capacity_eval(sel, geo, dfs, tidx, tf, fill)
        results[gname] = res
        print(f"  {gname:<14} A+B[{cap_fmt(res)}]")

    # top-2 by A+B totalR (with min sample) -> blind report (once)
    ranked = sorted([(r, g) for g, r in results.items() if r["filled"] >= 40],
                    key=lambda x: -x[0]["totalR"])[:2]
    for _, gname in ranked:
        gfn = GATES[gname]
        blind = [r for r in FB10 + FC10 if gfn(r)]
        res = capacity_eval(blind, geo, dfs, tidx, tf, fill)
        print(f"  -- blind [{gname}]: {cap_fmt(res)}")
        for sym in ps.SYMBOLS:
            sub = [r for r in blind if r["symbol"] == sym]
            rr = capacity_eval(sub, geo, dfs, tidx, tf, fill)
            print(f"     {sym}: {cap_fmt(rr)}")


def main():
    derivs = load_derivs_daily()
    macro = load_macro()
    for tf in ("4h", "1d", "1w"):
        dfs = ps.load_dfs(tf, mtf1w=False)
        tidx = ps.make_tidx(dfs, tf)
        records = ps.load_records(tf, dfs, mtf1w=False, refresh=False)
        step_ms = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}[tf]
        for r in records:
            r["_step_ms"] = step_ms
        records = attach_factors(records, derivs, macro)
        FA, FB, FC = ps.folds(records)
        print(f"\n{'='*72}\n===== {tf} 新因子回测（第 12 轮）=====\n{'='*72}")
        coverage_report(FA + FB, f"{tf} A+B")
        coverage_report(FC, f"{tf} C")
        factor_diagnostics(FA + FB, f"{tf} A+B")
        gate_sweep(tf, records, dfs, tidx)


if __name__ == "__main__":
    main()
