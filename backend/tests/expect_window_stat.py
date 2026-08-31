"""窗口期望收益统计（2026-08-31，第四十五轮）

用户问题：从上周五（2026-08-28 00:00 北京）到现在（2026-08-31 13:09 北京），
策略收益的期望应该是多少？

口径：
- 窗口 = 85.15 小时（3.548 天，加密 7x24 无周末）；
- 实盘口径 = 推送列表 8 币 x 1h+4h（1d 另行列出）；
- 期望主口径 = 第三十六轮 8 币 5 年机器输出（1h 已按第三十轮裁定为日记下界）；
- 波动带 = 本机当前哈希记录（BTC/ETH/BNB/SOL）重放：1h 用 sim_journal_order
  （下界/日记语义），4h 用 sim_outcome_fast（两顺序等价实测差为 0），
  组合按离场时刻入 UTC 小时桶，滑动 85.15h 窗口（步长 1h）取经验分布；
  净口径 feeR = 双边 0.10% x entry/risk（同 fee_compare）；
- 实际对照 = journal.db 周五 00:00（北京）以来 closed 交易（仅用户登记口径）。
- 本轮纯统计，零生产改动。

Usage: PYTHONIOENCODING=utf-8 ..\\.venv\\Scripts\\python.exe tests\\expect_window_stat.py
"""
import os
import pickle
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import profit_sweep2 as ps
from profit2_r5 import with_loose_plans
from backtest_ltc import CONF
from backtest_5y import W5, sim_outcome_fast
from audit_order_and_entry import sim_journal_order, capacity_run, fetch_df

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
FEE_RT = 0.0010  # 双边 0.10%
CST = timezone(timedelta(hours=8))

# 窗口：2026-08-28 00:00 +08 -> 2026-08-31 13:09 +08
WIN_START = datetime(2026, 8, 28, 0, 0, tzinfo=CST)
WIN_END = datetime(2026, 8, 31, 13, 9, tzinfo=CST)
WIN_H = (WIN_END - WIN_START).total_seconds() / 3600.0
WIN_D = WIN_H / 24.0

# 第三十六轮 8 币 5 年机器输出（AGENTS §5 / BACKTEST §6；1h 为下界口径）
DOC8 = {"1h_gross": 3426.1, "4h_gross": 2729.4, "1d_gross": 379.4, "1h_net10": 1967.9}
DAYS_5Y = 5 * 365.25


def replay(sym, tf, sim):
    df = fetch_df(sym, tf)
    with open(os.path.join(ps.CACHE_DIR, f"_5y_cache_{sym}_{tf}.pkl"), "rb") as f:
        recs = pickle.load(f)["records"]
    cfg = CONF[tf]
    recs = recs if cfg["th"] == 25 else with_loose_plans(recs, cfg["th"])
    arrs = (df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), len(df))
    tidx = {int(t): k for k, t in enumerate(df["time"].to_numpy())}
    fb = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
    times = df["time"].to_numpy()
    out = []
    for (sig_t, rr, direction, i, fill, exit_bar, entry, stop) in capacity_run(
            recs, tuple(cfg["geo"]), arrs, tidx, fb, sim):
        risk = abs(entry - stop)
        fee = FEE_RT * entry / risk if risk > 0 else 0.0
        out.append({"sym": sym, "tf": tf, "exit": int(times[exit_bar]),
                    "rr": float(rr), "net": float(rr) - fee})
    return out


def band(vals, label):
    v = np.array(vals)
    print(f"{label}: n={len(v)} 均值={v.mean():+.2f}R 中位={np.median(v):+.2f}R "
          f"p10={np.percentile(v,10):+.2f} p25={np.percentile(v,25):+.2f} "
          f"p75={np.percentile(v,75):+.2f} p90={np.percentile(v,90):+.2f} "
          f"最差={v.min():+.2f} 最好={v.max():+.2f}")
    print(f"{' ' * len(label)}  P(<0)={100*(v<0).mean():.1f}%  P(<=-3R)={100*(v<=-3).mean():.1f}%  "
          f"P(>=+8R)={100*(v>=8).mean():.1f}%")
    return v


def main():
    print(f"窗口: {WIN_START:%Y-%m-%d %H:%M} -> {WIN_END:%Y-%m-%d %H:%M} (北京) = {WIN_H:.2f}h = {WIN_D:.4f}d")

    trades = []
    for sym in SYMBOLS:
        for tf, sim in (("1h", sim_journal_order), ("4h", sim_outcome_fast)):
            trs = replay(sym, tf, sim)
            trades.extend(trs)
            g = sum(t["rr"] for t in trs)
            n = sum(t["net"] for t in trs)
            print(f"[replay] {sym} {tf}: {len(trs)} 笔 毛={g:+.1f}R 净@0.10%={n:+.1f}R")
    trades.sort(key=lambda t: t["exit"])

    tot_g = sum(t["rr"] for t in trades)
    tot_n = sum(t["net"] for t in trades)
    span_d = (trades[-1]["exit"] - trades[0]["exit"]) / 86_400_000
    print(f"\n== 四币 1h+4h 五年合计（本机重放口径）==")
    print(f"笔数={len(trades)} 毛={tot_g:+.1f}R 净@0.10%={tot_n:+.1f}R "
          f"跨度={span_d:.0f}天 日均毛={tot_g/span_d:+.3f}R 日均净={tot_n/span_d:+.3f}R")
    g1h = sum(t["rr"] for t in trades if t["tf"] == "1h")
    g4h = sum(t["rr"] for t in trades if t["tf"] == "4h")
    n4h = sum(t["net"] for t in trades if t["tf"] == "4h")
    print(f"其中 1h(下界) 毛={g1h:+.1f}R / 4h 毛={g4h:+.1f}R 净={n4h:+.1f}R "
          f"(4h 净/毛={n4h/g4h:.3f})")
    print(f"对照锚点: 四币 1h 下界文档值 +1770R / 上界 +2313R（第三十轮）")

    # ---- 经验波动带：UTC 小时桶 + 滑动 85.15h 窗口 ----
    t0 = trades[0]["exit"] // 3_600_000
    t1 = trades[-1]["exit"] // 3_600_000
    nb = t1 - t0 + 2
    bg = np.zeros(nb)
    bn = np.zeros(nb)
    for t in trades:
        k = t["exit"] // 3_600_000 - t0
        bg[k] += t["rr"]
        bn[k] += t["net"]
    wh = int(WIN_H)          # 85 整小时
    frac = WIN_H - wh        # 0.15
    cg = np.concatenate(([0.0], np.cumsum(bg)))
    cn = np.concatenate(([0.0], np.cumsum(bn)))
    starts = range(0, nb - wh - 1)
    wg = [cg[s + wh] - cg[s] + frac * bg[s + wh] for s in starts]
    wn = [cn[s + wh] - cn[s] + frac * bn[s + wh] for s in starts]
    print(f"\n== 四币组合 {WIN_H:.1f}h 滑动窗口经验分布（5 年，重叠样本）==")
    band(wg, "毛口径")
    band(wn, "净@0.10%")

    # ---- 期望：8 币文档口径 ----
    print(f"\n== 窗口期望（8 币文档口径 x {WIN_D:.4f} 天）==")
    d_1h = DOC8["1h_gross"] / DAYS_5Y
    d_4h = DOC8["4h_gross"] / DAYS_5Y
    d_1d = DOC8["1d_gross"] / DAYS_5Y
    d_1h_net = DOC8["1h_net10"] / DAYS_5Y
    d_4h_net = d_4h * (n4h / g4h)
    print(f"日均毛: 1h={d_1h:+.3f}R 4h={d_4h:+.3f}R 1h+4h={d_1h+d_4h:+.3f}R (1d 另加 {d_1d:+.3f}R)")
    print(f"日均净@0.10%: 1h={d_1h_net:+.3f}R 4h≈{d_4h_net:+.3f}R(按本机净毛比) 合计≈{d_1h_net+d_4h_net:+.3f}R")
    print(f"窗口期望 毛(1h+4h) = {(d_1h+d_4h)*WIN_D:+.2f}R")
    print(f"窗口期望 净(1h+4h) = {(d_1h_net+d_4h_net)*WIN_D:+.2f}R")
    print(f"窗口期望 毛(含1d)  = {(d_1h+d_4h+d_1d)*WIN_D:+.2f}R")
    scale8 = (DOC8["1h_gross"] + DOC8["4h_gross"]) / tot_g
    print(f"8币/4币 总量比 = {scale8:.3f} -> 波动带均值缩放参考 x{scale8:.2f}")
    print(f"按 1R=1% 本金固定注额: 毛 {(d_1h+d_4h)*WIN_D:+.2f}R ≈ 本金 {(d_1h+d_4h)*WIN_D:+.2f}%，"
          f"净 ≈ {(d_1h_net+d_4h_net)*WIN_D:+.2f}%")

    # ---- 用户实际窗口口径：窗口内推送列表 4→8 币（git 实证） ----
    # commit 3aeb828 08-29 16:30 symbols 4→8（本机）；08-28 15:48 起推送由部署机运行，
    # 部署机同步时间未知（今晨 DOGE 计划存在 => 部署机最迟周末内已含 DOGE）。
    print(f"\n== 用户实际窗口口径（推送列表 4 币 -> 7/8 币）==")
    d4_g = tot_g / span_d
    d4_n = tot_n / span_d
    sui_d = (300.9 + 226.9) / (3.32 * 365.25)   # 第三十六轮 SUI 机器输出
    d7_g = (DOC8["1h_gross"] + DOC8["4h_gross"]) / DAYS_5Y - sui_d
    t4 = (datetime(2026, 8, 29, 16, 30, tzinfo=CST) - WIN_START).total_seconds() / 86400
    t_rest = WIN_D - t4
    print(f"4 币日均 毛={d4_g:+.3f}R 净={d4_n:+.3f}R（本机重放）；7 币日均毛={d7_g:+.3f}R "
          f"(8币-{sui_d:+.3f}SUI)；8 币日均毛={d_1h+d_4h:+.3f}R")
    print(f"时间轴: 4币 {t4:.3f} 天（周五00:00->周六16:30）+ 扩容后 {t_rest:.3f} 天")
    for name, d_g, d_n in (("扩容后=8币", d_1h + d_4h, d_1h_net + d_4h_net),
                           ("扩容后=7币", d7_g, d7_g * ((d_1h_net + d_4h_net) / (d_1h + d_4h)))):
        g = t4 * d4_g + t_rest * d_g
        n = t4 * d4_n + t_rest * d_n
        print(f"  {name}: 窗口期望 毛={g:+.2f}R 净≈{n:+.2f}R（1R=1%本金）")
    g_all4 = WIN_D * d4_g
    n_all4 = WIN_D * d4_n
    print(f"  全程4币（对照下限）: 毛={g_all4:+.2f}R 净={n_all4:+.2f}R")
    print(f"  实际已知: 今晨 BTC/BNB/DOGE 三张4h多单集群止损 -3R（第四十四轮）+ 1h 平仓未统计")
    print(f"  左尾参考（四币带）: 净窗口 P(<0)=8.8% P(<=-3R)=1.8%；"
          f"3币同天全止损≈每年2次（第四十四轮）")

    # ---- 实际对照：journal.db ----
    db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "journal.db")
    con = sqlite3.connect(db)
    ms0 = int(WIN_START.timestamp() * 1000)
    rows = con.execute(
        "SELECT symbol, interval, direction, r_multiple, exit_reason, closed_at, adherence "
        "FROM trades WHERE status='closed' AND closed_at>=? ORDER BY closed_at", (ms0,)).fetchall()
    opens = con.execute("SELECT symbol, interval, direction, entry, stop, opened_at FROM trades "
                        "WHERE status='open'").fetchall()
    print(f"\n== 实际对照（journal.db，用户登记口径）==")
    if rows:
        tot = 0.0
        for s, itv, d, r, reason, cl, adh in rows:
            tot += r or 0.0
            ts = datetime.fromtimestamp(cl / 1000, tz=CST)
            print(f"  {ts:%m-%d %H:%M} {s} {itv} {d} {r:+.2f}R {reason} {adh or ''}")
        print(f"  窗口内已平仓合计 = {tot:+.2f}R（{len(rows)} 笔）")
    else:
        print("  窗口内无已平仓登记")
    print(f"  当前持仓登记 {len(opens)} 笔（浮盈未计）: " +
          (", ".join(f"{s} {itv} {d}" for s, itv, d, *_ in opens) if opens else "无"))


if __name__ == "__main__":
    main()
