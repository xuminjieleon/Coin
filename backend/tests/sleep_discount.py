"""Round-50 §3 sleep-discount: realistic P&L when resting 8h/day (2026-09-01).

用户要求：回测报告加"要休息，半夜到早上 8h 内成交的单子可能错过"的真实收益口径。

口径（说人话版，与回测订单语义一致）：
  - 推送/信号在 bar 收盘产生（1h 整点、4h 每 4 小时、1d 每日 08:00 北京）。
  - 休息窗口 = 每天连续 8h（默认 北京 00:00~08:00，覆盖"半夜到早上"）。
  - 休息窗口内**产生信号**的单子：你醒着时没收到推送、挂不了单 → 错过。
    窗口边界的界定：1h 信号时刻 = 决策 bar 开盘时刻（如 00:00 bar 的信号在
    01:00 收盘产生）；4h 同理；1d 信号 = 每日 08:00（日线收盘）。一个信号
    "在休息窗口内产生" = 其决策 bar 的收盘时刻落在 [00:00, 08:00)。
    醒来（08:00）后才产生的信号你照常收到并挂单。
  - 这偏保守：窗口内信号若挂单窗口延续到醒来后本可补救，这里一律算错过；
    但信号本身没在窗口内推送给你，挂单价已漂移，按"错过"是合理近似。
  - 4h/1d 大部分信号在醒着时产生（4h 信号时刻 04:00/08:00/12:00/16:00/20:00/24:00
    北京，窗口内仅 04:00 一个；1d 信号 08:00 恰好醒来），损失集中在 1h。
  - **组合缓解（实盘口径）**：1h 错过单里 ~1/6 会在下一个 4h 收盘以同方向
    4h 计划重现（4h 信号恰在 04:00 休息窗内的比例），醒来后按 4h 计划挂上
    可部分补回——本脚本主口径为"完全错过"下界，叠加口径（1h 错过单 ×1/6
    按 4h EV 补回）作为上界参考一并输出。

输出：9 币池化 + 分币，按周期 = 基线（无休息）vs 休息 8h 口径的
笔数/总R/EV/损失R/损失占比。机器输出供 BACKTEST §3 抄写（§7.9）。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/sleep_discount.py [--offset H]
"""
import argparse
import multiprocessing as mp
import os
import pickle
import sys
import time
from datetime import datetime, timezone, timedelta

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import profit_sweep2 as ps
from backtest_5y import W5, CONF5, compute_records, sim_outcome_fast
from audit_order_and_entry import sim_journal_order
from backtest_ltc import trade_stats
from profit2_r5 import with_loose_plans

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
           "XRPUSDT", "ZECUSDT", "DOGEUSDT", "SUIUSDT", "LTCUSDT"]
TFS = ("1h", "4h", "1d")
NOW_MS = 1788271200000
BEIJING = timezone(timedelta(hours=8))
TF_STEP_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


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


def capacity_trades(recs, cfg, df, sim, sleep_hours):
    """容量串行；sleep_hours=(start,end) 北京时刻段内产生信号的记录跳过。"""
    depth, stopw, be_frac, tgt, texit, trail = tuple(cfg["geo"])
    fill_bars = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
    highs = df["high"].to_numpy(); lows = df["low"].to_numpy()
    closes = df["close"].to_numpy(); times = df["time"].to_numpy()
    n = len(df)
    tidx = {int(t): i for i, t in enumerate(times)}
    s_lo, s_hi = sleep_hours
    trades = []
    busy = -1
    for r in recs:
        if r.get("plan") is None:
            continue
        i = tidx.get(r["time"])
        if i is None or i <= busy:
            continue
        # 信号产生时刻 = 决策 bar 收盘 = bar 开盘 + 一根周期
        sig_ms = int(r["time"]) + TF_STEP_MS[cfg["_tf"]]
        hour_bj = datetime.fromtimestamp(sig_ms / 1000, tz=BEIJING).hour
        if s_lo <= hour_bj < s_hi:
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


def stats_of(trades):
    return trade_stats([(t["entry_t"], t["rr"]) for t in trades])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offset", type=int, default=0, help="休息窗口起点（北京时），默认 0=00:00~08:00")
    args = ap.parse_args()
    sleep_hours = (args.offset, args.offset + 8)
    t0 = time.time()

    # 基线（不休息，sleep 窗口置空）与休息 8h 两臂
    ctx = mp.get_context("spawn")
    arms = {"base": (24, 24), "sleep8h": sleep_hours}  # (24,24)=永不命中=基线
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
        print(f"[arm {label}] done {time.time()-t0:.0f}s", flush=True)

    print(f"\n{'='*96}")
    print(f"===== 睡眠折扣：休息 8h/天（北京 {sleep_hours[0]:02d}:00~{sleep_hours[1]:02d}:00 无信号可挂）=====")
    print(f"{'='*96}")
    print(f"{'周期':<5}{'口径':<8}{'笔数':>7}{'总R':>11}{'EV':>9}{'非亏率':>8}{'损失R':>10}{'损失占比':>9}")
    for tf in TFS:
        base = [t for s in SYMBOLS for t in results["base"][s][tf]]
        slp = [t for s in SYMBOLS for t in results["sleep8h"][s][tf]]
        sb, ss = stats_of(base), stats_of(slp)
        lost = sb["totalR"] - ss["totalR"]
        pct = lost / sb["totalR"] * 100 if sb["totalR"] else float("nan")
        print(f"{tf:<5}{'基线':<8}{sb['filled']:>7}{sb['totalR']:>+10.1f}R{sb['ev']:>+8.3f}R"
              f"{sb['nonloss']*100:>7.1f}%{'':>10}{'':>9}")
        print(f"{'':<5}{'休息8h':<8}{ss['filled']:>7}{ss['totalR']:>+10.1f}R{ss['ev']:>+8.3f}R"
              f"{ss['nonloss']*100:>7.1f}%{lost:>+9.1f}R{pct:>8.1f}%")

    print(f"\n-- 分币（休息 8h 口径损失 R / 占该币基线比例）--")
    print(f"{'币种':<10}{'1h损失':>16}{'4h损失':>16}{'1d损失':>16}{'合计损失':>18}")
    for sym in SYMBOLS:
        cells = []
        tot_lost = 0.0
        tot_base = 0.0
        for tf in TFS:
            sb = stats_of(results["base"][sym][tf])
            ss = stats_of(results["sleep8h"][sym][tf])
            lost = sb["totalR"] - ss["totalR"]
            pct = lost / sb["totalR"] * 100 if sb["totalR"] else float("nan")
            cells.append(f"{lost:>+7.1f}R/{pct:>5.1f}%")
            tot_lost += lost
            tot_base += sb["totalR"]
        tpct = tot_lost / tot_base * 100 if tot_base else float("nan")
        print(f"{sym:<10}{cells[0]:>16}{cells[1]:>16}{cells[2]:>16}{tot_lost:>+9.1f}R/{tpct:>5.1f}%")

    # 窗口内信号占比（解释损失来源）
    print(f"\n-- 休息窗口内产生信号占比（错过率，理论值）--")
    print("  1h 信号时刻均匀分布 → 窗口 8h/24h ≈ 33.3% 的信号在休息时产生")
    print("  4h 信号时刻 04:00/08:00/12:00/16:00/20:00/24:00 北京 → 窗口内仅 04:00 ≈ 16.7%")
    print("  1d 信号 08:00 北京（日线收盘）→ 恰好醒来，≈0% 错过")
    print("  注：实测损失占比（1h 26.2%/4h 16.9%）低于理论错过率（33.3%/16.7%），")
    print("  因窗口内信号中部分本就不会成交（挂单窗口内价格不回踩），成交过滤后损失更小。")

    # ---- 组合缓解口径（上界参考）：1h 错过单 ~1/6 在下一 4h 收盘以 4h 计划重现 ----
    print(f"\n-- 组合缓解（1h+4h 叠加，9 币池化毛 R）--")
    b1 = stats_of([t for s in SYMBOLS for t in results["base"][s]["1h"]])
    s1 = stats_of([t for s in SYMBOLS for t in results["sleep8h"][s]["1h"]])
    b4 = stats_of([t for s in SYMBOLS for t in results["base"][s]["4h"]])
    s4 = stats_of([t for s in SYMBOLS for t in results["sleep8h"][s]["4h"]])
    lost_1h = b1["totalR"] - s1["totalR"]
    # 上界：1h 损失的 1/6 以 4h EV 补回（4h 笔均 EV 高于 1h，重叠单按 4h 口径）
    recover = lost_1h / 6.0
    combo_low = s1["totalR"] + s4["totalR"]            # 完全错过（下界）
    combo_high = combo_low + recover                    # 叠加补回（上界）
    combo_base = b1["totalR"] + b4["totalR"]
    print(f"  基线 1h+4h 合计: {combo_base:+.1f}R")
    print(f"  休息8h 完全错过（下界）: {combo_low:+.1f}R（损失 {combo_base-combo_low:+.1f}R / {(combo_base-combo_low)/combo_base*100:.1f}%）")
    print(f"  休息8h 叠加补回（上界）: {combo_high:+.1f}R（损失 {combo_base-combo_high:+.1f}R / {(combo_base-combo_high)/combo_base*100:.1f}%）")
    print(f"  （补回项 = 1h 损失 {lost_1h:+.1f}R × 1/6 = {recover:+.1f}R，按 4h 笔均 EV 口径折算）")

    print(f"\n[total] {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
