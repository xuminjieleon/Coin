"""窗口实际单子逐根重放（2026-08-31，第四十五轮核心）

用户要求：期望必须按这几天**实际成交的单子**逐笔算，能和实操对账——
不要"日均×天数"的平均折算。

方法（与推送/图表回放同一决策口径，无前视）：
- 窗口内每根**已收盘** bar，调用 services/analysis/context.run_analysis(as_of=该bar)
  取 summary.tradePlan——这正是每小时微信推送用的同一份代码、同一 asOf 回放语义；
- 指纹 symbol|interval|direction：计划方向出现/转向时记一笔候选单；
- 成交模拟：从信号 bar 的下一根起，在 fillBars 窗口内价格触及 entry 即成交
  （回踩挂单语义，与回测 harness 一致）；容量串行（一单一结）；
- 已成交单用生产几何逐 bar 走到窗口结束：已离场记实结 R，在场按窗口结束价记浮盈 R；
- 每笔对照回测 EV（第三十六轮分币值），汇总"这批单子的回测期望"vs"窗口内实际"。

币种时间轴（git 实证）：08-28 00:00~08-29 16:30 = 4 币；08-29 16:30 起 = 8 币。
XRP/ZEC/SUI 的 1h 信号同样重放（run_analysis 不依赖记录缓存，K线走 kline_cache）。

零生产改动。Usage: PYTHONIOENCODING=utf-8 ..\\.venv\\Scripts\\python.exe tests\\window_live_replay.py
"""
import asyncio
import sys
import os
from datetime import datetime, timezone, timedelta

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
from services.analysis import context
from services.analysis.context import NoKlinesError
from audit_order_and_entry import sim_journal_order, sim_outcome_fast

CST = timezone(timedelta(hours=8))
WIN_START = datetime(2026, 8, 28, 0, 0, tzinfo=CST)
EXPAND = datetime(2026, 8, 29, 16, 30, tzinfo=CST)
WIN_END = datetime(2026, 8, 31, 13, 9, tzinfo=CST)
MS0 = int(WIN_START.timestamp() * 1000)
MS_EXP = int(EXPAND.timestamp() * 1000)
MS1 = int(WIN_END.timestamp() * 1000)

STEP = {"1h": 3_600_000, "4h": 14_400_000}
SYMS_4 = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
SYMS_NEW = ["XRPUSDT", "ZECUSDT", "DOGEUSDT", "SUIUSDT"]

# 第三十六轮分币 EV（毛 R/笔；1h 为下界口径）
EV = {
    "BTCUSDT": {"1h": 0.124, "4h": 0.418}, "ETHUSDT": {"1h": 0.122, "4h": 0.433},
    "BNBUSDT": {"1h": 0.124, "4h": 0.397}, "SOLUSDT": {"1h": 0.133, "4h": 0.455},
    "XRPUSDT": {"1h": 0.11, "4h": 0.35}, "ZECUSDT": {"1h": 0.11, "4h": 0.35},
    "DOGEUSDT": {"1h": 0.11, "4h": 0.35}, "SUIUSDT": {"1h": 0.129, "4h": 0.391},
}


def fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=CST).strftime("%m-%d %H:%M")


def simulate(direction, entry, stop, atr, plan, future):
    """future = list of (t, high, low, close) bars AFTER the signal bar.
    与回测 harness 的 sim_journal_order 同序：止损 -> 保本触发 -> 跟踪 -> 目标 -> 时间退出。
    修正：止损每根按当前止损位判断（含跟踪后的收紧），且保本触发当根即生效。"""
    long = direction == "long"
    risk = abs(entry - stop)
    if risk <= 0:
        return {"filled": False}
    fill_bars = plan.get("fillBars") or 12
    be_frac = plan.get("beR")
    tgt_r = plan.get("targetR")
    trail = plan.get("trailR")
    texit = plan.get("texitBars") or 48
    fill_i = None
    for j in range(min(fill_bars, len(future))):
        t, h, l, c = future[j]
        if (h >= entry) if long else (l <= entry):
            fill_i = j
            break
    if fill_i is None:
        return {"filled": False}
    target = entry + tgt_r * risk if (tgt_r and long) else (entry - tgt_r * risk if tgt_r else None)
    be_trig = entry + be_frac * risk if (be_frac and long) else (entry - be_frac * risk if be_frac else None)
    be = False
    locked = 0.0
    cur_stop = stop
    best = entry
    n = len(future)
    j_end = min(fill_i + texit, n - 1)
    for j in range(fill_i, j_end + 1):
        t, h, l, c = future[j]
        # 1) 止损（当前止损位，含跟踪收紧后）
        if (l <= cur_stop) if long else (h >= cur_stop):
            r = (cur_stop - entry) / risk if long else (entry - cur_stop) / risk
            # 保本后止损在 entry（r=0），已锁 locked；跟踪位更高时 r>0
            frac = 0.5 if be else 1.0
            return {"filled": True, "fill": future[fill_i][0], "exit": t,
                    "rr": locked + frac * r, "why": "be" if be else "stop"}
        # 2) 保本触发（当根生效）
        if not be and be_trig is not None and ((h >= be_trig) if long else (l <= be_trig)):
            be = True
            locked = 0.5 * be_frac
            cur_stop = entry
        # 3) 跟踪收紧
        if trail:
            best = max(best, h) if long else min(best, l)
            tsl = best - trail * risk if long else best + trail * risk
            cur_stop = max(cur_stop, tsl) if long else min(cur_stop, tsl)
        # 4) 目标
        if target is not None and ((h >= target) if long else (l <= target)):
            frac = 0.5 if be else 1.0
            return {"filled": True, "fill": future[fill_i][0], "exit": t,
                    "rr": locked + frac * tgt_r, "why": "target"}
    t, h, l, c = future[j_end]
    r = (c - entry) / risk if long else (entry - c) / risk
    closed = (fill_i + texit) <= (n - 1)
    return {"filled": True, "fill": future[fill_i][0], "exit": t if closed else None,
            "rr": locked + (0.5 * r if be else r), "why": "time" if closed else "open"}


async def replay(symbol, interval):
    step = STEP[interval]
    # 已收盘 bar 序列：窗口内每根收盘 bar 的开盘时刻 as_of
    bars = []
    t = MS0 - (MS0 % step)  # 对齐
    if t < MS0:
        t += step
    while t + step <= MS1 + step:  # bar [t, t+step) 收盘于 t+step
        if t + step <= MS1:
            bars.append(t)
        t += step
    # 只取信号 bar 在窗口内的
    bars = [b for b in bars if MS0 <= b < MS1]
    trades = []
    last_dir = None
    busy_until = -1
    for b in bars:
        if symbol in SYMS_NEW and b < MS_EXP:
            continue
        try:
            res = await context.run_analysis(symbol, interval, 500, as_of=b)
        except NoKlinesError:
            continue
        except Exception:
            continue
        candles = res["candles"]
        if not candles:
            continue
        plan = res["summary"].get("tradePlan")
        sig_close = candles[-1]["time"]
        direction = plan.get("direction") if plan else None
        # 指纹事件：方向出现/转向
        if direction != last_dir:
            if direction and sig_close >= busy_until:
                # 取信号 bar 之后的完整 K 线（含信号 bar，索引用 i），用生产 sim_journal_order
                df = await context.klines_df(symbol, interval, 400, end_time=None)
                df = df.sort_values("time").reset_index(drop=True)
                highs = df["high"].to_numpy(); lows = df["low"].to_numpy()
                closes = df["close"].to_numpy(); times = df["time"].to_numpy()
                n = len(df)
                hit = np.where(times == sig_close)[0]
                if len(hit) == 0:
                    last_dir = direction
                    continue
                i = int(hit[0])
                entry = float(plan["entry"]); stop = float(plan["stop"])
                be = plan.get("beR"); tgt = plan.get("targetR"); trail = plan.get("trailR")
                texit = plan.get("texitBars") or 48; fbars = plan.get("fillBars") or 12
                sim_fn = sim_journal_order if interval == "1h" else sim_journal_order
                out = sim_fn(highs, lows, closes, n, i, direction, entry, stop,
                             be, tgt, texit, fbars, trail)
                if out is not None:
                    rr, fill, exit_bar = out
                    trades.append({"sym": symbol, "tf": interval, "dir": direction,
                                   "sig": sig_close, "fill": int(times[fill]), "entry": entry,
                                   "stop": stop, "exit": int(times[exit_bar]), "rr": float(rr),
                                   "why": "prod", "score": res["summary"]["score"]})
                    busy_until = int(times[exit_bar])
            last_dir = direction
    return trades


async def main():
    print(f"窗口: {fmt(MS0)} -> {fmt(MS1)}（北京）；4币 -> {fmt(MS_EXP)} 起 8币\n")
    all_tr = []
    for tf in ("4h", "1h"):
        for sym in SYMS_4 + SYMS_NEW:
            trs = await replay(sym, tf)
            all_tr.extend(trs)
            for t in trs:
                ev = EV.get(sym, {}).get(tf, 0.12)
                t["ev"] = ev
                state = f"已离场 {fmt(t['exit'])}[{t['why']}]" if t["exit"] else "在场"
                print(f"  {tf} {sym:<9} {t['dir']:<5} 信号{fmt(t['sig'])} 成交{fmt(t['fill'])} "
                      f"入{t['entry']:.4g} 止{t['stop']:.4g} | EV{ev:+.3f} | {state} 实际{t['rr']:+.2f}R")
    if not all_tr:
        print("窗口内无成交。")
        return
    print("=" * 72)
    closed = [t for t in all_tr if t["exit"]]
    open_ = [t for t in all_tr if not t["exit"]]
    print(f"窗口内成交 {len(all_tr)} 笔（已离场 {len(closed)} / 在场 {len(open_)}）")
    print(f"这批单子的回测期望合计 = {sum(t['ev'] for t in all_tr):+.2f}R")
    print(f"窗口内实际合计（实结+在场浮盈）= {sum(t['rr'] for t in all_tr):+.2f}R")
    print(f"  其中已离场实结 = {sum(t['rr'] for t in closed):+.2f}R")
    trio = [t for t in all_tr if t["tf"] == "4h" and t["sym"] in ("BTCUSDT", "BNBUSDT", "DOGEUSDT")
            and t["dir"] == "long" and t["rr"] <= -0.99]
    print(f"\n用户实操锚点：今晨 BTC/BNB/DOGE 三单 4h 多止损 -3R；本重放全止损4h多单 = "
          f"{len(trio)} 笔 ({', '.join(t['sym'][:4] for t in trio)})")


if __name__ == "__main__":
    asyncio.run(main())
