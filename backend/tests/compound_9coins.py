"""Round-50 §4 refresh: 9-coin compound tables (monthly/quarterly/yearly/trail).

BACKTEST.md 复利节原为四币口径（第二十九轮，2026-08-28）。第五十轮推送列表扩为
9 币（+LTC），复利节按九币刷新（BACKTEST §4）；四币口径保留一句保守参照。
本脚本机器输出 §4.1/§4.2/§4.3/§4.4 所需的全部叙述数字
（§7.9：叙述数字只抄机器输出）。
**追加（同日，用户要求）**：§4 加睡眠折扣臂——休息 8h/天（北京 00:00~08:00）
内产生信号的单子错过（与 tests/sleep_discount.py 同口径），两臂（基线/休息8h）
同跑复利表，§4.1/§4.2/§4.3 均出两口径。

口径：9 币（BTC/ETH/SOL/BNB/XRP/ZEC/DOGE/SUI/LTC）、R13 几何、容量约束串行、
1h=下界口径（sim_journal_order）、4h/1d=sim_outcome_fast（两顺序等价）、
复利 harness=compound_backtest.compound（第二十九轮单源）、净@双边0.10%。
记录缓存 _5y_cache_*（窗口尾部 2026-09-01，与 §2 同源）。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/compound_9coins.py
"""
import multiprocessing as mp
import os
import pickle
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import profit_sweep2 as ps
from backtest_5y import W5, CONF5, compute_records, sim_outcome_fast
from audit_order_and_entry import sim_journal_order
from profit2_r5 import with_loose_plans
import compound_backtest as cb

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
           "XRPUSDT", "ZECUSDT", "DOGEUSDT", "SUIUSDT", "LTCUSDT"]
TFS = ("1h", "4h", "1d")
FEE_NET = 0.0010
NOW_MS = 1788271200000  # 2026-09-01 14:00 UTC（与 §2 同锚点）
YEAR_MS = 365.25 * 86400 * 1000
BEIJING = timezone(timedelta(hours=8))
TF_STEP_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
SLEEP_HOURS = (0, 8)  # 北京 00:00~08:00 休息窗口


def load_df(sym, itv):
    rows = ps.kline_cache._read_rows(sym, itv, NOW_MS, W5[itv])
    return ps.kline_cache.rows_to_df(rows)


def load_cached_records(sym, tf, dfs):
    cache_file = os.path.join(ps.CACHE_DIR, f"_5y_cache_{sym}_{tf}.pkl")
    key = {"ver": 1, "tf": tf, "symbol": sym, "window": W5[tf], "src": ps.source_hash()}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                entry = pickle.load(f)
            if entry.get("key") == key:
                return entry["records"]
        except Exception:
            pass
    records = compute_records(sym, tf, dfs)
    tmp = cache_file + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump({"key": key, "records": records}, f)
    os.replace(tmp, cache_file)
    return records


def capacity_trades(recs, cfg, df, sim, sleep_hours=None):
    depth, stopw, be_frac, tgt, texit, trail = tuple(cfg["geo"])
    fill_bars = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
    highs = df["high"].to_numpy(); lows = df["low"].to_numpy()
    closes = df["close"].to_numpy(); times = df["time"].to_numpy()
    n = len(df)
    tidx = {int(t): i for i, t in enumerate(times)}
    trades = []
    busy = -1
    for r in recs:
        if r.get("plan") is None:
            continue
        i = tidx.get(r["time"])
        if i is None or i <= busy:
            continue
        if sleep_hours is not None:
            sig_ms = int(r["time"]) + TF_STEP_MS[cfg["_tf"]]
            hour_bj = datetime.fromtimestamp(sig_ms / 1000, tz=BEIJING).hour
            if sleep_hours[0] <= hour_bj < sleep_hours[1]:
                continue  # 休息窗口内产生信号 → 错过
        built = ps.build_plan(r, depth, stopw)
        if built is None:
            continue
        direction, entry, stop = built
        out = sim(highs, lows, closes, n, i, direction, entry, stop,
                  be_frac, tgt, texit, fill_bars, trail)
        if out is None:
            continue
        rr, fill, exit_bar = out
        busy = exit_bar
        trades.append({"entry_t": int(times[fill]), "exit_t": int(times[exit_bar]),
                       "dir": direction, "rr": float(rr),
                       "entry_px": float(entry), "risk_px": float(abs(entry - stop)),
                       "scale": 1.0})
    return trades


def worker(args):
    sym, sleep_hours = args
    try:
        dfs = {itv: load_df(sym, itv) for itv in TFS}
        out = {}
        for tf in TFS:
            cfg = dict(CONF5[tf])
            cfg["_tf"] = tf
            records = load_cached_records(sym, tf, dfs)
            recs = records if cfg["th"] == 25 else with_loose_plans(records, cfg["th"])
            sim = sim_journal_order if tf == "1h" else sim_outcome_fast
            out[tf] = capacity_trades(recs, cfg, dfs[tf], sim, sleep_hours)
        return {"sym": sym, "data": out}
    except Exception:
        import traceback
        return {"sym": sym, "error": traceback.format_exc()}


def shared_budget_portfolio(data, symbols):
    out = []
    for sym in symbols:
        merged = sorted(data[sym]["1h"] + data[sym]["4h"], key=lambda t: t["entry_t"])
        ent = np.array([t["entry_t"] for t in merged], dtype=np.int64)
        ext = np.array([t["exit_t"] for t in merged], dtype=np.int64)
        for t in merged:
            span = t["exit_t"] - t["entry_t"]
            sc = 1.0
            if span > 0:
                lo = np.maximum(t["entry_t"], ent)
                hi = np.minimum(t["exit_t"], ext)
                ov = float(np.maximum(0, hi - lo).sum()) - span
                sc = 1.0 - 0.5 * min(1.0, ov / span)
            c = dict(t)
            c["scale"] = sc
            out.append(c)
    return out


def fmt_x(x):
    if x != x or x == float("inf"):
        return "n/a"
    if x >= 1000:
        return f"{x/1000:.1f}千×" if x < 1e6 else f"{x:.2e}×"
    return f"{x:.2f}×"


def main():
    t0 = time.time()
    ctx = mp.get_context("spawn")
    arms = {"base": None, "sleep8h": SLEEP_HOURS}
    results = {}
    for label, sh in arms.items():
        with ctx.Pool(len(SYMBOLS)) as pool:
            res = pool.map(worker, [(s, sh) for s in SYMBOLS])
        data = {}
        for r in res:
            if "error" in r:
                print(f"[worker-error]\n{r['error']}", flush=True)
            else:
                data[r["sym"]] = r["data"]
        if len(data) != len(SYMBOLS):
            raise SystemExit("worker failed")
        results[label] = data
        print(f"[arm {label}] {time.time()-t0:.0f}s", flush=True)

    def ports_of(data):
        return {
            "仅1h(下界)": [t for s in SYMBOLS for t in data[s]["1h"]],
            "仅4h": [t for s in SYMBOLS for t in data[s]["4h"]],
            "仅1d": [t for s in SYMBOLS for t in data[s]["1d"]],
            "1h+4h共享": shared_budget_portfolio(data, SYMBOLS),
        }

    ports = {label: ports_of(results[label]) for label in arms}

    # ---- §4.1 总表（f=0.5%/1.0%，基线 vs 休息8h）----
    print("== §4.1 复利 vs 非复利（9 币，净@双边0.10%；基线 / 休息8h 两臂）==")
    print(f"{'组合':<16}{'f':>6}{'口径':<9}{'复利期末':>12}{'年化':>10}{'最大回撤':>9}{'非复利对照':>11}{'笔数':>7}")
    for name in ports["base"]:
        for f in ((0.005, 0.01) if name == "仅1h(下界)" else (0.01,)):
            for label, tag in (("base", "基线"), ("sleep8h", "休息8h")):
                tr = ports[label][name]
                st = cb.compound(tr, f, FEE_NET)
                ann = (st["cagr"] - 1.0) * 100 if st["cagr"] == st["cagr"] else float("nan")
                print(f"{name:<16}{f*100:>5.1f}%{tag:<9}{fmt_x(st['multiple']):>12}{ann:>+9.1f}%"
                      f"{st['maxdd']*100:>8.1f}%{fmt_x(st['flat']):>11}{st['n']:>7}")

    # ---- §4.2 分年复利（f=1%）----
    print("\n== §4.2 分年复利收益率（f=1%，净@0.10%；基线 / 休息8h）==")
    for name in ports["base"]:
        for label, tag in (("base", "基线"), ("sleep8h", "休息8h")):
            st = cb.compound(ports[label][name], 0.01, FEE_NET)
            parts = "  ".join(f"{y}:{(v-1)*100:+.0f}%" for y, v in sorted(st["yearly"].items()))
            print(f"  {name:<12}{tag:<6} {parts}")

    # ---- §4.3 月度/季度分布（f=1%，平仓归期）----
    print("\n== §4.3 月度/季度分布（f=1%，净@0.10%；基线 / 休息8h）==")
    month_fn = lambda ts: (lambda d: (d.year, d.month))(datetime.fromtimestamp(ts / 1000, tz=timezone.utc))
    quarter_fn = lambda ts: (lambda d: (d.year, (d.month - 1) // 3 + 1))(datetime.fromtimestamp(ts / 1000, tz=timezone.utc))
    for name in ports["base"]:
        for pf_label, pfn in (("月", month_fn), ("季", quarter_fn)):
            for label, tag in (("base", "基线"), ("sleep8h", "休息8h")):
                st = cb.compound(ports[label][name], 0.01, FEE_NET, period_fn=pfn)
                vals = np.array(list(st["yearly"].values()), dtype=float)
                pos = float(np.mean(vals > 1.0 + 1e-12))
                med = (float(np.median(vals)) - 1.0) * 100
                worst = (float(vals.min()) - 1.0) * 100
                best = (float(vals.max()) - 1.0) * 100
                smooth = float(np.prod(vals) ** (1.0 / len(vals)))
                print(f"  {name:<12}{pf_label}{tag:<5} 期数={len(vals):>3} 正占比={pos*100:5.1f}% "
                      f"中位={med:+6.1f}% 最差={worst:+6.1f}% 最好={best:+6.1f}% 平滑=×{smooth:.2f}")

    # ---- §4.4 容量轨迹（仅1h 下界 f=1% 净，1 万起步；基线 vs 休息8h）----
    print("\n== §4.4 容量轨迹（仅1h下界 f=1% 净，1万 USDT 起步；单仓名义≈0.54×权益）==")
    for label, tag in (("base", "基线"), ("sleep8h", "休息8h")):
        tr = ports[label]["仅1h(下界)"]
        eq = 10000.0
        year_end = {}
        for t in sorted(tr, key=lambda x: x["exit_t"]):
            y = datetime.fromtimestamp(t["exit_t"] / 1000, tz=timezone.utc).year
            net = t["rr"] - FEE_NET * t["entry_px"] / t["risk_px"]
            eq *= (1.0 + 0.01 * net * t.get("scale", 1.0))
            year_end[y] = eq
        print(f"  [{tag}]")
        for y, e in sorted(year_end.items()):
            print(f"    {y} 年末: 权益≈{e:,.0f} USDT / 单仓名义≈{e*0.54:,.0f}")

    # ---- 固定注额对照（年化 R/年）----
    print("\n== 固定注额对照（9 币池化毛 R/年，span=决策起点..末尾；基线 / 休息8h）==")
    for name in ports["base"]:
        for label, tag in (("base", "基线"), ("sleep8h", "休息8h")):
            tr = ports[label][name]
            if not tr:
                continue
            t_lo = min(t["entry_t"] for t in tr)
            t_hi = max(t["exit_t"] for t in tr)
            span_y = (t_hi - t_lo) / YEAR_MS
            tot = sum(t["rr"] * t.get("scale", 1.0) for t in tr)
            net = sum((t["rr"] - FEE_NET * t["entry_px"] / t["risk_px"]) * t.get("scale", 1.0) for t in tr)
            print(f"  {name:<12}{tag:<6} 毛 {tot:>+9.1f}R ({tot/span_y:>+7.1f}/年)  "
                  f"净@0.10% {net:>+9.1f}R ({net/span_y:>+7.1f}/年)  span={span_y:.2f}年")

    print(f"\n[total] {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
