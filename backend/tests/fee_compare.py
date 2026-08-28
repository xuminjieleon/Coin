"""Fee/slippage sensitivity: 1h vs 4h production strategy (2026-08-28).

User question: "如果是 0.05~0.06% 的手续费+滑点，1h 和 4h 的收益比较".

口径与 tests/backtest_5y.py 完全一致（四币 BTC/ETH/BNB/SOL × 5 年窗口、
第 13 轮生产几何、容量约束串行执行、1h th=25 原生 / 4h th=10 放宽），
唯一新增：每笔交易扣双边费用，R 换算 feeR = fee_rt × entry / risk
（与 backtest_ashare.py FEE_RT / backtest_pine.py 2*FEE 同口径——
仓位可能分两腿出场，但总名义额恒为 1 进 + 1 出，用 entry 近似出场价）。

费用场景（"0.05~0.06% 手续费+滑点"两种读法都算，主读法=单边）：
  双边合计 0.05% / 0.06%（一进一出合计）
  单边 0.05% / 0.06%（=双边 0.10% / 0.12%；吃单费+滑点全按单边计）

§7.8：每 symbol 一个多进程 worker；worker 内单次 asyncio.run 拉全部周期
（binance 共享 client 绑定首个 event loop 的坑，见 DEVLOG 第二十二轮）；
worker 抛异常会挂死 Pool——捕获后返回 error 字段。
_5y_cache_* 与当前源码哈希不匹配时重算并立即落盘（可断点续跑）。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/fee_compare.py
"""
import asyncio
import multiprocessing as mp
import os
import pickle
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import profit_sweep2 as ps
from backtest_5y import SYMBOLS, W5, CONF5, compute_records, sim_outcome_fast
from backtest_ltc import trade_stats
from profit2_r5 import with_loose_plans

TFS = ("1h", "4h")
NEED_ITVS = ("1h", "4h", "1d")
SCENARIOS = [
    ("无费用（基线）", 0.0),
    ("双边合计0.05%", 0.0005),
    ("双边合计0.06%", 0.0006),
    ("maker入+taker出(双边0.07%)", 0.0007),
    ("单边0.05%（双边0.10%）", 0.0010),
    ("单边0.06%（双边0.12%）", 0.0012),
]


def load_cached_records(sym: str, tf: str, dfs: dict) -> list[dict]:
    """Same cache file/key convention as backtest_5y.load_records."""
    cache_file = os.path.join(ps.CACHE_DIR, f"_5y_cache_{sym}_{tf}.pkl")
    key = {"ver": 1, "tf": tf, "symbol": sym, "window": W5[tf], "src": ps.source_hash()}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                entry = pickle.load(f)
            if entry.get("key") == key:
                print(f"[cache] {sym} {tf}: {len(entry['records'])} records", flush=True)
                return entry["records"]
        except Exception:
            pass
    records = compute_records(sym, tf, dfs)
    tmp = cache_file + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump({"key": key, "records": records}, f)
    os.replace(tmp, cache_file)
    print(f"[cache] {sym} {tf}: saved {len(records)} records", flush=True)
    return records


def capacity_trades(recs: list[dict], cfg: dict, df) -> list[tuple]:
    """backtest_5y.capacity_run_fast, but keep (time, rr, entry, risk) per trade."""
    depth, stopw, be_frac, tgt, texit, trail = tuple(cfg["geo"])
    fill_bars = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    n = len(df)
    tidx = {int(t): i for i, t in enumerate(df["time"].to_numpy())}
    trades: list[tuple] = []
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
        out = sim_outcome_fast(highs, lows, closes, n, i, direction, entry, stop,
                               be_frac, tgt, texit, fill_bars, trail)
        if out is None:
            continue
        rr, fill, exit_bar = out
        busy = exit_bar
        trades.append((r["time"], float(rr), float(entry), float(abs(entry - stop))))
    return trades


def run_symbol(sym: str):
    """One worker per symbol: single asyncio.run, then both TFs end-to-end."""
    try:
        dfs: dict = {}

        async def _fetch_all():
            for itv in NEED_ITVS:
                rows = await ps.kline_cache.get_klines(sym, itv, W5[itv])
                dfs[itv] = ps.kline_cache.rows_to_df(rows)
                print(f"[data] {sym} {itv}: {len(dfs[itv])} bars", flush=True)

        asyncio.run(_fetch_all())
        out: dict = {}
        for tf in TFS:
            cfg = CONF5[tf]
            records = load_cached_records(sym, tf, dfs)
            recs = records if cfg["th"] == 25 else with_loose_plans(records, cfg["th"])
            out[tf] = capacity_trades(recs, cfg, dfs[tf])
            print(f"[sim] {sym} {tf}: {len(out[tf])} trades", flush=True)
        return {sym: out}
    except SystemExit as exc:
        return {"error": f"{sym}: SystemExit {exc}"}
    except Exception as exc:
        import traceback
        return {"error": f"{sym}: {exc}\n{traceback.format_exc()}"}


def year_of(ms: int) -> int:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year


def net_trades(trades: list[tuple], fee_rt: float) -> list[tuple[int, float]]:
    return [(t, rr - fee_rt * entry / risk) for t, rr, entry, risk in trades]


def fmt_row(name: str, st: dict) -> str:
    pf = f"{st['pf']:.2f}" if st["pf"] == st["pf"] and st["pf"] != float("inf") else "inf"
    dd = f"{st['maxdd']:.1f}" if st["maxdd"] == st["maxdd"] else "-"
    mar = f"{st['totalR'] / st['maxdd']:.0f}" if st["maxdd"] == st["maxdd"] and st["maxdd"] > 0 else "-"
    return (f"{name:<22} {st['filled']:>6} {st['ev']:+.3f}R {st['nonloss']*100:>5.1f}% "
            f"{st['totalR']:+9.1f}R {dd:>6}R {mar:>6} PF={pf}")


def main():
    t0 = time.time()
    print(f"fee_compare: symbols={SYMBOLS} tfs={TFS} "
          f"(geometry: 1h {ps.geo_str(tuple(CONF5['1h']['geo']))} / "
          f"4h {ps.geo_str(tuple(CONF5['4h']['geo']))})", flush=True)
    ctx = mp.get_context("spawn")
    with ctx.Pool(len(SYMBOLS)) as pool:
        results = pool.map(run_symbol, SYMBOLS)
    data: dict = {}
    for res in results:
        if "error" in res:
            print(f"[worker-error]\n{res['error']}", flush=True)
        else:
            data.update(res)
    if len(data) != len(SYMBOLS):
        raise SystemExit("有 worker 失败，中止（部分结果不可汇总）")
    print(f"[pool] done in {time.time()-t0:.0f}s", flush=True)

    by_tf: dict = {}
    for tf in TFS:
        trades = sorted((tr for sym in SYMBOLS for tr in data[sym][tf]), key=lambda x: x[0])
        by_tf[tf] = trades

    first_time = by_tf["1h"][0][0]
    last_time = by_tf["1h"][-1][0]
    print(f"\n数据窗口（成交时间范围）: {ps.fmt_ts(first_time)} .. {ps.fmt_ts(last_time)}")

    print(f"\n{'='*104}\n===== 主表：四币合计 5 年，容量约束串行，费用=双边名义额×费率 =====\n{'='*104}")
    for tf in TFS:
        trades = by_tf[tf]
        stops_pct = np.array([risk / entry * 100 for _, _, entry, risk in trades])
        print(f"\n-- {tf}：{len(trades)} 笔；止损距离中位 {np.median(stops_pct):.2f}% "
              f"(均值 {stops_pct.mean():.2f}%)，即双边 0.10% ≈ "
              f"{0.0010 / (np.median(stops_pct)/100):.3f}R/笔 --")
        print(f"{'场景':<22} {'笔数':>6} {'EV':>7} {'非亏损':>6} {'总R':>10} {'DD':>7} {'MAR':>6}")
        for name, fee_rt in SCENARIOS:
            st = trade_stats(net_trades(trades, fee_rt))
            print(fmt_row(name, st))

    print(f"\n{'='*104}\n===== 1h vs 4h 总利润对比（四币合计 5 年） =====\n{'='*104}")
    print(f"{'场景':<22} {'1h 总R':>10} {'4h 总R':>10} {'差(1h-4h)':>11} {'1hEV':>8} {'4hEV':>8}")
    for name, fee_rt in SCENARIOS:
        st1 = trade_stats(net_trades(by_tf["1h"], fee_rt))
        st4 = trade_stats(net_trades(by_tf["4h"], fee_rt))
        print(f"{name:<22} {st1['totalR']:>+9.1f}R {st4['totalR']:>+9.1f}R "
              f"{st1['totalR']-st4['totalR']:>+10.1f}R {st1['ev']:>+7.3f}R {st4['ev']:>+7.3f}R")

    for fee_rt, tag in ((0.0010, "单边0.05%"), (0.0012, "单边0.06%")):
        print(f"\n{'='*104}\n===== 分币 / 分年（{tag}，双边 {fee_rt*100:.2f}%） =====\n{'='*104}")
        for tf in TFS:
            for sym in SYMBOLS:
                st = trade_stats(net_trades(data[sym][tf], fee_rt))
                print(f"  {tf} {fmt_row(sym, st)}")
        for tf in TFS:
            by_year = defaultdict(list)
            for t, r in net_trades(by_tf[tf], fee_rt):
                by_year[year_of(t)].append(r)
            parts = "  ".join(f"{y}:{np.array(v).sum():+.1f}R" for y, v in sorted(by_year.items()))
            print(f"  {tf} 分年: {parts}")

    print(f"\n{'='*104}\n===== 费用临界点（四币合计 5 年） =====\n{'='*104}")
    sums = {}
    for tf in TFS:
        tr = by_tf[tf]
        sums[tf] = (sum(rr for _, rr, _, _ in tr), sum(entry / risk for _, _, entry, risk in tr))
    for tf in TFS:
        gross, fee_unit = sums[tf]
        f_be = gross / fee_unit
        print(f"  {tf}: 盈亏平衡费率 = 双边 {f_be*100:.3f}%（单边 {f_be*50:.3f}%）；"
              f"笔均费用敏感度 双边0.10% → {0.0010 * fee_unit / len(by_tf[tf]):.3f}R/笔")
    g1, s1 = sums["1h"]
    g4, s4 = sums["4h"]
    if abs(s1 - s4) > 1e-9:
        f_x = (g1 - g4) / (s1 - s4)
        print(f"  1h 与 4h 总利润交叉点 = 双边 {f_x*100:.3f}%（单边 {f_x*50:.3f}%）"
              f"——低于该费率 1h 总利润更高，高于则 4h 反超")

    print(f"\n总耗时 {time.time()-t0:.0f}s（含记录缓存重算）")


if __name__ == "__main__":
    main()
