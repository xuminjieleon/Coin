"""Round 12b: factor-as-score-component simulation (no record recompute).

Factors are already attached to the cached records (profit3_factors). The
score delta from new components is computed directly from those fields, plan
direction re-derived (cvd_conf override preserved), then capacity-constrained
evaluation on the round-11 geometry — same folds, A+B selection, blind B+C.

Pre-declared component definitions (signs from A+B diagnostics + prior
knowledge; percentile thresholds coarse):
  topLsr (大户多空比, 同向):  trailing pctl >= 80 -> +w_top, <= 20 -> -w_top
  oiPctl  (OI 百分位, 反向):  <= 20 -> +w_oi,   >= 80 -> -w_oi
  vix     (VIX, 反向/买恐慌): >= 25 -> +w_vix,  <= 14 -> -w_vix
  fundPctl(资金费率, 反向):    <= 10 -> +w_f,    >= 90 -> -w_f
  lsrPctl (散户多空比, 反向):  <= 15 -> +w_l,    >= 85 -> -w_l

Weight grid (coarse, 6 cells): inc / full / half / top-only / crowd-only /
macro-only. Acceptance: blind B+C totalR > incumbent by >10% and per-symbol
blind EV all positive; else factors integrate display-only (weight 0).

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/profit3_weights.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import profit_sweep2 as ps
from profit2_cap import capacity_eval, fmt as cap_fmt
from profit3_factors import attach_factors, load_derivs_daily, load_macro

GEOS = {
    "4h": ((0.75, 1.2, 0.5, None, 48, 0.5), 1.5),
    "1d": ((0.75, 1.5, 0.5, None, 24, 0.5), 1.5),
    "1w": ((0.75, 1.5, 0.5, None, 24, 0.75), 2.0),
}
TH = 10

CONFIGS = {
    "incumbent": (0, 0, 0, 0, 0),
    "full":      (8, 6, 4, 4, 4),
    "half":      (4, 3, 2, 2, 2),
    "top-only":  (8, 0, 0, 0, 0),
    "crowd-only": (0, 6, 0, 4, 4),
    "macro-only": (0, 0, 4, 0, 0),
}


def _pctl_of_trailing(vals, i, window=365):
    lo = max(0, i - window + 1)
    seg = [v for v in vals[lo:i + 1] if v is not None]
    if not seg or len(seg) < 30:
        return None
    x = seg[-1]
    return sum(1 for v in seg if v <= x) / len(seg) * 100.0


def attach_top_pctl(records: list, derivs: dict) -> None:
    """Trailing percentile of top trader LSR per record (needs derivs rows)."""
    for r in records:
        rows = derivs.get(r["symbol"]) or []
        ts_list = [row["ts"] for row in rows]
        close_s = (r["time"] + r["_step_ms"]) / 1000.0
        import bisect
        i = bisect.bisect_right(ts_list, close_s - 86400.0) - 1 if ts_list else -1
        if i >= 0 and ts_list[i] <= close_s - 86400.0:
            tops = [x["top"] for x in rows]
            r["top_pctl"] = _pctl_of_trailing(tops, i)


def score_delta(r: dict, w: tuple) -> float:
    w_top, w_oi, w_vix, w_f, w_l = w
    d = 0.0
    tp = r.get("top_pctl")
    if w_top and tp is not None:
        if tp >= 80:
            d += w_top
        elif tp <= 20:
            d -= w_top
    op = r.get("oi_pctl")
    if w_oi and op is not None:
        if op <= 20:
            d += w_oi
        elif op >= 80:
            d -= w_oi
    v = r.get("vix")
    if w_vix and v is not None:
        if v >= 25:
            d += w_vix
        elif v <= 14:
            d -= w_vix
    fp = r.get("funding_pctl")
    if w_f and fp is not None:
        if fp <= 10:
            d += w_f
        elif fp >= 90:
            d -= w_f
    lp = r.get("lsr_pctl")
    if w_l and lp is not None:
        if lp <= 15:
            d += w_l
        elif lp >= 85:
            d -= w_l
    return d


def rederive_plans(records: list, w: tuple) -> list:
    """New plan directions from score+delta (cvd_conf override preserved)."""
    out = []
    for r in records:
        r2 = dict(r)
        new_score = r["score"] + score_delta(r, w)
        conf = r.get("cvd_conf")
        if conf:
            r2["plan"] = conf
        elif abs(new_score) >= TH:
            r2["plan"] = "long" if new_score > 0 else "short"
        else:
            r2["plan"] = None
        r2["dir"] = r2["plan"]
        out.append(r2)
    return out


def main():
    derivs = load_derivs_daily()
    macro = load_macro()
    for tf in ("4h", "1d", "1w"):
        geo, fill = GEOS[tf]
        dfs = ps.load_dfs(tf, mtf1w=False)
        tidx = ps.make_tidx(dfs, tf)
        records = ps.load_records(tf, dfs, mtf1w=False, refresh=False)
        step_ms = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}[tf]
        for r in records:
            r["_step_ms"] = step_ms
        records = attach_factors(records, derivs, macro)
        attach_top_pctl(records, derivs)
        FA, FB, FC = ps.folds(records)
        print(f"\n{'='*72}\n===== {tf} 因子权重模拟（容量约束，th={TH}，A+B 选择）=====\n{'='*72}")
        results = {}
        for name, w in CONFIGS.items():
            sel = rederive_plans(FA + FB, w)
            res = capacity_eval(sel, geo, dfs, tidx, tf, fill)
            results[name] = res
            print(f"  {name:<11} A+B[{cap_fmt(res)}]")
        ranked = sorted([(r, n) for n, r in results.items() if r["filled"] >= 40],
                        key=lambda x: -x[0]["totalR"])
        best = ranked[0][1] if ranked else "incumbent"
        print(f"  [A+B 选定] {best}")
        # blind for the best non-incumbent config + incumbent reference
        for name in dict.fromkeys([best, "incumbent"]):
            if name == "incumbent" and best != "incumbent":
                blind = rederive_plans(FB + FC, CONFIGS["incumbent"])
            else:
                blind = rederive_plans(FB + FC, CONFIGS[name])
            res = capacity_eval(blind, geo, dfs, tidx, tf, fill)
            print(f"  -- blind [{name}]: {cap_fmt(res)}")
            for sym in ps.SYMBOLS:
                sub = [r for r in blind if r["symbol"] == sym]
                rr = capacity_eval(sub, geo, dfs, tidx, tf, fill)
                print(f"     {sym}: {cap_fmt(rr)}")


if __name__ == "__main__":
    main()
