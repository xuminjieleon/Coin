"""窗口实际单子重放（2026-08-31，第四十五轮追加）

用户要求：期望不要"日均×天数"的平均折算，要按这几天**实际成交的单子**逐笔算，
能和实操对账。

口径：
- 窗口 = 2026-08-28 00:00 -> 2026-08-31 13:09（北京，UTC+8）；
- 币种 = 用户实际推送列表时间轴：08-28 00:00~08-29 16:30 为 4 币（BTC/ETH/BNB/SOL），
  08-29 16:30 起为 8 币（+XRP/ZEC/DOGE/SUI；git commit 3aeb828 实证）。
  XRP/ZEC/SUI 本机无当前哈希记录缓存 -> 标 "本机无缓存"；DOGE 1h 缓存后台重建中。
- 信号/成交/离场模拟与生产回测完全同码：CONF5 几何、with_loose_plans(th)、
  ps.build_plan、sim_outcome_fast（1h 同时跑 sim_journal_order 下界对照）、
  容量串行（busy=exit_bar）；
- 每笔打印：信号时刻、方向、入场/止损、成交时刻、回测 EV（该周期该币五年均值）、
  窗口内实际离场（若已出）或"在场"（按窗口结束价折算浮盈 R）；
- 汇总：窗口内实际已平仓 R vs 这些单子的回测期望 R。

零生产改动。Usage: PYTHONIOENCODING=utf-8 ..\\.venv\\Scripts\\python.exe tests\\window_trades_replay.py
"""
import os
import pickle
import sys
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
from audit_order_and_entry import sim_journal_order, fetch_df

CST = timezone(timedelta(hours=8))
WIN_START = datetime(2026, 8, 28, 0, 0, tzinfo=CST)
EXPAND = datetime(2026, 8, 29, 16, 30, tzinfo=CST)   # 4->8 币
WIN_END = datetime(2026, 8, 31, 13, 9, tzinfo=CST)
MS0, MS_EXP, MS1 = (int(d.timestamp() * 1000) for d in (WIN_START, EXPAND, WIN_END))

SYMS_4 = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
SYMS_NEW = ["XRPUSDT", "ZECUSDT", "DOGEUSDT", "SUIUSDT"]

# 第三十六轮分币 EV（毛，下界口径仅 1h；4h/1d 为毛口径；单位 R/笔）
EV = {
    "BTCUSDT": {"1h": 452.8 / 3641, "4h": 365.8 / 876},
    "ETHUSDT": {"1h": 411.2 / 3374, "4h": 360.7 / 834},
    "BNBUSDT": {"1h": 415.8 / 3365, "4h": 347.4 / 874},
    "SOLUSDT": {"1h": 469.9 / 3524, "4h": 408.2 / 898},
    "DOGEUSDT": {"1h": 0.10, "4h": 0.30},   # 第三十六轮分币 EV 区间估计（无本机重放前用池化值）
}


def fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=CST).strftime("%m-%d %H:%M")


def replay_window(sym, tf):
    cache = os.path.join(ps.CACHE_DIR, f"_5y_cache_{sym}_{tf}.pkl")
    if not os.path.exists(cache):
        return None, "本机无缓存"
    df = fetch_df(sym, tf)
    with open(cache, "rb") as f:
        recs = pickle.load(f)["records"]
    cfg = CONF[tf]
    recs = recs if cfg["th"] == 25 else with_loose_plans(recs, cfg["th"])
    highs = df["high"].to_numpy(); lows = df["low"].to_numpy()
    closes = df["close"].to_numpy(); times = df["time"].to_numpy()
    n = len(df)
    tidx = {int(t): k for k, t in enumerate(times)}
    fb = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
    geo = tuple(cfg["geo"])
    sim = sim_journal_order if tf == "1h" else sim_outcome_fast
    trades = []
    busy = -1
    for r in recs:
        if r.get("plan") is None:
            continue
        i = tidx.get(r["time"])
        if i is None or i <= busy:
            continue
        built = ps.build_plan(r, geo[0], geo[1])
        if built is None:
            continue
        direction, entry, stop = built
        out = sim(highs, lows, closes, n, i, direction, entry, stop,
                  geo[2], geo[3], geo[4], fb, geo[5])
        if out is None:
            continue
        rr, fill, exit_bar = out
        busy = exit_bar
        trades.append({"sig": int(r["time"]), "dir": direction, "entry": float(entry),
                       "stop": float(stop), "fill": int(times[fill]),
                       "exit": int(times[exit_bar]), "rr": float(rr),
                       "exit_bar": int(exit_bar)})
    return (df, trades), None


def main():
    print(f"窗口: {fmt(MS0)} -> {fmt(MS1)}（北京）；4币 -> {fmt(MS_EXP)} 起 8币\n")
    all_rows = []
    for tf in ("4h", "1h"):
        print(f"===== {tf} =====")
        for sym in SYMS_4 + SYMS_NEW:
            in_scope = sym in SYMS_4 or True  # 新币只取扩容后成交的
            res, err = replay_window(sym, tf)
            if err:
                print(f"  {sym:<9} {err}")
                continue
            df, trades = res
            closes = df["close"].to_numpy(); times = df["time"].to_numpy()
            end_px = float(closes[-1])
            # 窗口内成交的单子：fill 在 [MS0, MS1]；新币要求 fill >= MS_EXP
            for t in trades:
                if not (MS0 <= t["fill"] <= MS1):
                    continue
                if sym in SYMS_NEW and t["fill"] < MS_EXP:
                    continue
                risk = t["entry"] - t["stop"] if t["dir"] == "long" else t["stop"] - t["entry"]
                risk = abs(risk)
                exited = t["exit"] <= MS1
                if exited:
                    actual = t["rr"]
                    state = f"已离场 {fmt(t['exit'])}"
                else:
                    unr = ((end_px - t["entry"]) / risk) if t["dir"] == "long" else ((t["entry"] - end_px) / risk)
                    actual = unr
                    state = f"在场(按{fmt(MS1)}价浮盈{unr:+.2f}R)"
                ev = EV.get(sym, {}).get(tf, 0.12 if tf == "1h" else 0.30)
                all_rows.append({"sym": sym, "tf": tf, "dir": t["dir"], "sig": t["sig"],
                                 "fill": t["fill"], "entry": t["entry"], "stop": t["stop"],
                                 "exited": exited, "actual": actual, "ev": ev, "state": state})
                print(f"  {sym:<9} {t['dir']:<5} 信号{fmt(t['sig'])} 成交{fmt(t['fill'])} "
                      f"入{t['entry']:.4g} 止{t['stop']:.4g} | 回测EV{ev:+.3f}R | {state} "
                      f"实际{actual:+.2f}R")
        print()

    if not all_rows:
        print("窗口内无成交单子。")
        return
    df_out = [r for r in all_rows if r["exited"]]
    print("=" * 70)
    print(f"窗口内成交单子共 {len(all_rows)} 笔（已离场 {len(df_out)}，在场 {len(all_rows)-len(df_out)}）")
    print(f"这些单子的回测期望合计 = {sum(r['ev'] for r in all_rows):+.2f}R")
    print(f"窗口内实际（已离场实结 + 在场按{fmt(MS1)}价浮盈）= {sum(r['actual'] for r in all_rows):+.2f}R")
    print(f"其中已离场实结 = {sum(r['actual'] for r in df_out):+.2f}R（{len(df_out)} 笔）")
    # 用户实操对照锚点
    print(f"\n用户实操对照：今晨 BTC/BNB/DOGE 三张 4h 多单集群止损 = -3.00R（第四十四轮）")
    trio = [r for r in all_rows if r["tf"] == "4h" and r["sym"] in ("BTCUSDT", "BNBUSDT", "DOGEUSDT")
            and r["dir"] == "long" and r["exited"] and r["actual"] <= -0.99]
    print(f"本重放中对应的全止损 4h 多单: {len(trio)} 笔 "
          f"({', '.join(f'{r['sym'][:4]}@{fmt(r['fill'])}' for r in trio)})")


if __name__ == "__main__":
    main()
