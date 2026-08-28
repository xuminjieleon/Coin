"""AUDIT-followup (2026-08-28): compound (fixed-fractional) results for the 1h
LOWER-BOUND convention (journal/plan semantics: BE resting order fills before
target on the same bar).

User directive: 复利节里的 1h 也用下界口径计算。

复用 compound_backtest 的全部口径（事件账户、费率 feeR=双边×entry/risk、
容量串行、共享预算 scale、期键），仅把 1h 的 sim 从 sim_outcome_fast 换成
audit_order_and_entry.sim_journal_order。记录缓存复用 _5y_cache_*，不重算决策。

产出（全部机器输出，供 BACKTEST §5 修订抄写）：
  A) 仅1h f=1% / f=0.5%（净@双边0.10% + 毛）复利期末、年化、DD、非复利对照
  B) 1h+4h 共享预算 f=1%（1h 用下界、4h 原样[两口径等价]）净/毛
  C) 仅1h 下界分年复利收益率（f=1% 净）
  D) 1h+4h共享预算(下界) 月度/季度分布
  E) 权益轨迹锚点（1万起步，下界 1h f=1% 净）供容量段参考

Usage: PYTHONIOENCODING=utf-8 ..\\.venv\\Scripts\\python.exe tests\\audit_compound_1h_lb.py
"""
import asyncio
import multiprocessing as mp
import os
import sys
import time

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import profit_sweep2 as ps
from backtest_5y import SYMBOLS, W5, CONF5, sim_outcome_fast
from backtest_ltc import CONF
from profit2_r5 import with_loose_plans
from audit_order_and_entry import sim_journal_order

import compound_backtest as cb
from compound_backtest import (compound, shared_budget_portfolio, year_of,
                               FEE_NET, YEAR_MS)

TFS = ("1h", "4h", "1d")
NEED_ITVS = ("1h", "4h", "1d")
_FAR = 4_000_000_000_000


def capacity_trades_lb(recs, cfg, df, sim):
    """capacity_trades 但 sim 可替换（下界口径）。"""
    depth, stopw, be_frac, tgt, texit, trail = tuple(cfg["geo"])
    fill_bars = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    n = len(df)
    times = df["time"].to_numpy()
    tidx = {int(t): i for i, t in enumerate(times)}
    trades = []
    busy = -1
    for r in recs:
        if r.get("plan") is None:
            continue
        i = tidx.get(r["time"])
        if i is None or i <= busy:
            continue
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


def run_symbol(sym):
    dfs = {}

    async def _fetch():
        for itv in NEED_ITVS:
            rows = await ps.kline_cache.get_klines(sym, itv, W5[itv], end_time=_FAR)
            dfs[itv] = ps.kline_cache.rows_to_df(rows)

    asyncio.run(_fetch())
    out = {}
    for tf in TFS:
        cfg = CONF5[tf]
        cache_file = os.path.join(ps.CACHE_DIR, f"_5y_cache_{sym}_{tf}.pkl")
        import pickle
        with open(cache_file, "rb") as f:
            records = pickle.load(f)["records"]
        recs = records if cfg["th"] == 25 else with_loose_plans(records, cfg["th"])
        # 1h 用下界口径；4h/1d 两口径等价，用原 sim（数值相同）
        sim = sim_journal_order if tf == "1h" else sim_outcome_fast
        out[tf] = capacity_trades_lb(recs, cfg, dfs[tf], sim)
        print(f"[sim-lb] {sym} {tf}: {len(out[tf])} trades", flush=True)
    return {sym: out}


def month_key(ms):
    d = __import__("datetime", fromlist=["datetime", "timezone"]).datetime.fromtimestamp(
        ms / 1000, tz=__import__("datetime", fromlist=["timezone"]).timezone.utc)
    return (d.year, d.month)


def quarter_key(ms):
    from datetime import datetime, timezone
    d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return (d.year, (d.month - 1) // 3 + 1)


def fmt_line(tag, res, f):
    if res is None:
        print(f"  {tag:<28} f={f*100:.1f}%  (无交易)")
        return
    print(f"  {tag:<28} f={f*100:.1f}%  复利期末={res['multiple']:,.4g}×  "
          f"年化={res['cagr']:.4g}×/年  DD={res['maxdd']*100:.1f}%  "
          f"非复利对照={res['flat']:.3g}×  (n={res['n']})", flush=True)


def main():
    t0 = time.time()
    with mp.Pool(min(4, len(SYMBOLS))) as pool:
        results = pool.map(run_symbol, SYMBOLS)
    data = {}
    for r in results:
        data.update(r)

    # 仅1h 下界（净 + 毛）
    h1 = [t for sym in SYMBOLS for t in data[sym]["1h"]]
    h1.sort(key=lambda t: t["entry_t"])
    print("\n===== A) 仅1h【下界口径】复利 =====")
    for f in (0.005, 0.01):
        fmt_line("仅1h 净@双边0.10%", compound(h1, f, FEE_NET), f)
    fmt_line("仅1h 毛(无费用)", compound(h1, 0.01, 0.0), 0.01)

    # 1h+4h 共享预算（1h 下界）
    print("\n===== B) 1h+4h 共享预算（1h 下界，4h 原样）=====")
    shared = shared_budget_portfolio(data)
    fmt_line("共享预算 净@双边0.10%", compound(shared, 0.01, FEE_NET), 0.01)
    fmt_line("共享预算 毛(无费用)", compound(shared, 0.01, 0.0), 0.01)

    # 分年复利（仅1h 下界 + 共享预算下界，f=1% 净）
    print("\n===== C) 分年复利收益率（f=1% 净@0.10%）=====")
    for tag, trades in (("仅1h【下界】", h1), ("1h+4h共享预算【1h下界】", shared_budget_portfolio(data))):
        res = compound(trades, 0.01, FEE_NET, period_fn=year_of)
        parts = "  ".join(
            f"{y}:{'×%.4g' % m if m >= 2 else '%+.0f%%' % ((m-1)*100)}"
            for y, m in sorted(res["yearly"].items()))
        print(f"  {tag:<22} {parts}", flush=True)

    # 月度/季度分布（共享预算 下界 f=1% 净）
    print("\n===== D) 1h+4h共享预算【1h下界】月度/季度分布（f=1% 净@0.10%）=====")
    for name, pfn in (("月", month_key), ("季", quarter_key)):
        r = compound(shared, 0.01, FEE_NET, period_fn=pfn)
        rets = [v - 1.0 for v in r["yearly"].values()]
        rets = np.array(sorted(rets))
        pos = float((rets > 0).mean() * 100)
        med = np.median(rets) * 100
        print(f"  {name}: 期数={len(rets)} 正占比={pos:.0f}% 中位={med:+.0f}% "
              f"最差={rets[0]*100:+.0f}% 最好={rets[-1]*100:+.0f}%", flush=True)

    # 权益轨迹锚点（1万起步，仅1h 下界 f=1% 净）
    print("\n===== E) 权益轨迹（仅1h 下界 f=1% 净，1万 USDT 起步；容量参考）=====")
    events = []
    for idx, t in enumerate(h1):
        events.append((t["entry_t"], 1, idx))
        events.append((t["exit_t"], 0, idx))
    events.sort(key=lambda e: (e[0], e[1]))
    E = 10000.0
    e_entry = {}
    marks = {}
    for ts, kind, idx in events:
        t = h1[idx]
        if kind == 1:
            e_entry[idx] = E
            continue
        rr_net = t["rr"] - FEE_NET * t["entry_px"] / t["risk_px"]
        e0 = e_entry.pop(idx, E)
        E += 0.01 * e0 * t["scale"] * rr_net
        marks[year_of(ts)] = E
    for y in sorted(marks):
        # 单仓名义 ≈ f×E / 止损距离中位1.84%
        notional = 0.01 * marks[y] / 0.0184
        print(f"  {y} 年末: 权益≈{marks[y]:,.0f} USDT（单仓名义≈{notional:,.0f}）", flush=True)

    print(f"\n[done] 总耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
