"""Long/short split + 2026-H1 slice of the 5y production backtest.

User questions (2026-08-28): ① In the backtest data, are long and short
returns similar? ② How did H1 2026 do?

Same harness as tests/fee_compare.py (BTC/ETH/BNB/SOL x 5 years, round-13
production geometry, capacity-constrained serial execution, 1h th=25 native /
4h & 1d th=10 loose), additionally keeping each trade's DIRECTION. 1w
excluded (thin sample, declared limitation since round 11). Multiprocessing:
one worker per symbol (AGENTS.md §7.8); worker does a single asyncio.run.

Trade timestamp = decision time (record time) — same convention as the
backtest_5y / fee_compare year splits. H1 2026 = UTC Jan-Jun decision times.

1h/4h record caches hit the files rebuilt by fee_compare (current source
hash); the 1d cache is stale (round-8 era) and is recomputed + re-saved here.

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/direction_split.py
"""
import asyncio
import multiprocessing as mp
import os
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
from backtest_5y import SYMBOLS, W5, CONF5, load_records, sim_outcome_fast
from backtest_ltc import trade_stats
from profit2_r5 import with_loose_plans

TFS = ("1h", "4h", "1d")
NEED_ITVS = ("1h", "4h", "1d")
FEE_RT_MAKER = 0.0007   # 双边0.07%（maker入+taker出）
FEE_RT_TAKER = 0.0010   # 双边0.10%（单边0.05%，用户口径）

H1_START = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
H1_END = int(datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)


def capacity_trades(recs: list[dict], cfg: dict, df) -> list[tuple]:
    """Capacity-constrained serial run keeping (time, rr, direction, entry, risk)."""
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
        trades.append((r["time"], float(rr), direction, float(entry), float(abs(entry - stop))))
    return trades


def run_symbol(sym: str):
    try:
        dfs: dict = {}

        async def _fetch_all():
            for itv in NEED_ITVS:
                rows = await ps.kline_cache.get_klines(sym, itv, W5[itv])
                dfs[itv] = ps.kline_cache.rows_to_df(rows)

        asyncio.run(_fetch_all())
        out: dict = {}
        for tf in TFS:
            cfg = CONF5[tf]
            records = load_records(sym, tf, dfs, False)
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


def month_of(ms: int) -> int:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).month


def net_rr(tr: tuple, fee_rt: float) -> float:
    _, rr, _, entry, risk = tr
    return rr - fee_rt * entry / risk


def pf_str(st: dict) -> str:
    pf = st["pf"]
    if pf != pf or pf == float("inf"):
        return "inf"
    return f"{pf:.2f}"


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=None,
                    help="全年切片（UTC 1~12 月决策口径）；缺省=2026H1")
    args = ap.parse_args()
    if args.year:
        win_label = f"{args.year} 全年"
        win_start = int(datetime(args.year, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        win_end = int(datetime(args.year + 1, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        win_months = range(1, 13)
    else:
        win_label = "2026 上半年"
        win_start, win_end, win_months = H1_START, H1_END, range(1, 7)

    t0 = time.time()
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
        raise SystemExit("有 worker 失败，中止")
    by_tf = {tf: sorted((tr for sym in SYMBOLS for tr in data[sym][tf]), key=lambda x: x[0])
             for tf in TFS}
    print(f"[pool] done in {time.time()-t0:.0f}s", flush=True)

    # ---------------------------------------------------------------- Q1: long/short split
    print(f"\n{'='*104}\n===== 问题①：做多 vs 做空（四币 5 年 2021-09~2026-08，毛收益，容量约束串行）=====\n{'='*104}")
    for tf in TFS:
        trades = by_tf[tf]
        print(f"\n-- {tf}（共 {len(trades)} 笔）--")
        print(f"{'方向':<5} {'笔数':>6} {'占比':>6} {'胜率':>7} {'非亏损':>7} {'EV':>8} {'总R':>10} {'DD':>6} {'PF':>6}")
        for d, label in (("long", "做多"), ("short", "做空")):
            sub = [(t, rr) for (t, rr, dirn, _, _) in trades if dirn == d]
            st = trade_stats(sub)
            share = len(sub) / len(trades) * 100 if trades else 0
            dd = f"{st['maxdd']:.1f}" if st["maxdd"] == st["maxdd"] else "-"
            print(f"{label:<5} {st['filled']:>6} {share:>5.1f}% {st['winrate']*100:>6.1f}% "
                  f"{st['nonloss']*100:>6.1f}% {st['ev']:>+7.3f}R {st['totalR']:>+9.1f}R "
                  f"{dd:>6}R {pf_str(st):>6}")
        print(f"分年（笔数/总R，毛）:")
        for d, label in (("long", "多"), ("short", "空")):
            by_year = defaultdict(list)
            for (t, rr, dirn, _, _) in trades:
                if dirn == d:
                    by_year[year_of(t)].append(rr)
            parts = "  ".join(f"{y}:{np.array(v).sum():+.1f}R({len(v)})"
                              for y, v in sorted(by_year.items()))
            print(f"  {label}: {parts}")

    # ---------------------------------------------------------------- Q2: window slice
    print(f"\n{'='*104}\n===== 问题②：{win_label}（UTC 决策口径）=====\n{'='*104}")
    print(f"{'周期':<5} {'笔数':>6} {'胜率':>7} {'EV(毛)':>8} {'总R(毛)':>10} "
          f"{'净@0.07%':>10} {'净@0.10%':>10} {'DD(毛)':>7}")
    h1_gross_sum = h1_net_sum = 0.0
    for tf in TFS:
        sub = [tr for tr in by_tf[tf] if win_start <= tr[0] < win_end]
        st = trade_stats([(t, rr) for t, rr, _, _, _ in sub])
        net07 = trade_stats([(tr[0], net_rr(tr, FEE_RT_MAKER)) for tr in sub])
        net10 = trade_stats([(tr[0], net_rr(tr, FEE_RT_TAKER)) for tr in sub])
        h1_gross_sum += st["totalR"]
        h1_net_sum += net10["totalR"]
        dd = f"{st['maxdd']:.1f}" if st["maxdd"] == st["maxdd"] else "-"
        print(f"{tf:<5} {st['filled']:>6} {st['winrate']*100:>6.1f}% {st['ev']:>+7.3f}R "
              f"{st['totalR']:>+9.1f}R {net07['totalR']:>+9.1f}R {net10['totalR']:>+9.1f}R {dd:>6}R")
    print(f"合计（1h+4h+1d）: 毛 {h1_gross_sum:+.1f}R / 净@0.10% {h1_net_sum:+.1f}R")

    print(f"\n{win_label} 分月（毛 R，UTC 决策月份）:")
    print(f"{'周期':<5} " + "".join(f"{m:>9}月" for m in win_months) + f"{'合计':>10}")
    for tf in TFS:
        sub = [tr for tr in by_tf[tf] if win_start <= tr[0] < win_end]
        by_month = defaultdict(list)
        for t, rr, _, _, _ in sub:
            by_month[month_of(t)].append(rr)
        cells = "".join(f"{np.array(by_month[m]).sum():>+9.1f}" if by_month[m] else f"{'—':>9}"
                        for m in win_months)
        print(f"{tf:<5} {cells} {sum(sum(v) for v in by_month.values()):>+10.1f}R")

    print(f"\n{win_label} 分方向（毛 R / 笔数）:")
    for tf in TFS:
        sub = [tr for tr in by_tf[tf] if win_start <= tr[0] < win_end]
        for d, label in (("long", "多"), ("short", "空")):
            vals = [rr for (_, rr, dirn, _, _) in sub if dirn == d]
            print(f"  {tf} {label}: {np.array(vals).sum() if vals else 0.0:+.1f}R ({len(vals)} 笔)"
                  f"  EV {np.mean(vals):+.3f}R" if vals else f"  {tf} {label}: 0 笔")

    print(f"\n{win_label} 分币（毛 R）:")
    for tf in TFS:
        row = []
        for sym in SYMBOLS:
            vals = [rr for (t, rr, _, _, _) in data[sym][tf] if win_start <= t < win_end]
            row.append(f"{sym[:-4]}:{np.array(vals).sum() if vals else 0.0:+.1f}R({len(vals)})")
        print(f"  {tf:<4} " + "  ".join(row))

    print(f"\nR 换算示例：账户 10,000 USDT、单笔风险 1%（1R=100 USDT）→ "
          f"{win_label} 净@0.10% 约 {h1_net_sum*100:+,.0f} USDT（毛 {h1_gross_sum*100:+,.0f}）。")
    print("口径：决策时间归月/归期；未计手续费（除标注净额列）；1w 因样本过薄未列。")
    print(f"\n总耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
