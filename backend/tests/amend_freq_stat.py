"""改单(AMEND)事件频率与阈值敏感性回放（2026-09-01）

用户体感：上线一天的【改单】推送较频繁、且改动价格看起来很接近，要求确认
AMEND_DRIFT_FRAC（原 0.20×单笔风险）是否需要扩大。
结论（本轮已采纳）：扩为 0.40×风险 且 ≥0.5%价格 双条件（日均 67→~30 条）。

方法（与线上推送同一口径，无前视）：
- 近 30 天窗口，8 推送币 × {1h,4h}，每根**已收盘** bar 调
  services/analysis/context.run_analysis(as_of=该bar开盘) 取 summary.tradePlan
  ——与每小时微信推送同一份代码、同一 asOf 回放语义；
- 完整复刻 notifier 状态机：首轮静默播种（不进 pushedPlans）、
  新/转向/消失/改单事件、pushedPlans 基线更新与清除规则（notifier.py:382-445）；
- 对阈值 {0.20,0.25,0.30,0.40,0.50} 各自**独立**模拟（阈值改变基线重置时点，
  不能共用一条 pushedPlans 轨迹）；
- 输出：每阈值改单总量/日均、分周期分币种、触发时价格漂移幅度分布
  （|Δentry|/entry % 与 drift/risk），以及 0.20 阈值下最近改单明细——
  直接回答"改单多不多、价格近不近、扩到多少合适"。

4h 键只在 4h bar 收盘处评估（中间小时 as_of 相同、计划不变，事件流严格等价）。

Usage: PYTHONIOENCODING=utf-8 ..\\.venv\\Scripts\\python.exe tests\\amend_freq_stat.py
"""
import asyncio
import multiprocessing as mp
import os
import sys
import time
from datetime import datetime, timedelta, timezone

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))
STEP = {"1h": 3_600_000, "4h": 14_400_000}
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
           "XRPUSDT", "ZECUSDT", "DOGEUSDT", "SUIUSDT"]
INTERVALS = ["1h", "4h"]
WINDOW_DAYS = 30
THRESHOLDS = [0.20, 0.25, 0.30, 0.40, 0.50]


def fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=CST).strftime("%m-%d %H:%M")


def _last_closed_open(now_ms: int, interval: str) -> int:
    step = STEP[interval]
    return now_ms // step * step - step


# ------------------------------------------------------------------ 采集层
async def _collect(symbol: str, interval: str, ms0: int, ms1: int):
    """逐根收盘 bar 回放计划序列: [(as_of, direction|None, entry, stop), ...]"""
    from services.analysis.context import NoKlinesError, run_analysis
    step = STEP[interval]
    series = []
    t = (ms0 // step) * step
    if t < ms0:
        t += step
    while t <= ms1:
        try:
            res = await run_analysis(symbol, interval, 500, as_of=t)
            plan = (res.get("summary") or {}).get("tradePlan")
            if plan:
                series.append((t, plan["direction"], float(plan["entry"]), float(plan["stop"])))
            else:
                series.append((t, None, None, None))
        except NoKlinesError:
            series.append((t, "FAILED", None, None))
        except Exception:
            series.append((t, "FAILED", None, None))
        t += step
    return series


def _worker(args):
    symbol, interval, ms0, ms1 = args
    t0 = time.time()
    series = asyncio.run(_collect(symbol, interval, ms0, ms1))
    ok = sum(1 for s in series if s[1] != "FAILED")
    return symbol, interval, series, len(series) - ok, time.time() - t0


# ------------------------------------------------------------------ 模拟层
def simulate(series, frac, min_price_pct=0.0):
    """对单键计划序列按 notifier 状态机模拟，返回事件列表。
    series: [(as_of, direction|None|'FAILED', entry, stop), ...] 按时间升序。
    事件元组末位 idx 为该事件在 series 中的下标（用于 bar 间隔统计）。"""
    events = []  # (kind, as_of, direction, entry, stop, old_entry, old_stop, drift, base, idx)
    seen = None      # 方向指纹（None=无计划）
    pushed = None    # 上次推送的 {"entry","stop","direction"}
    pushed_idx = -1  # 基线设立时的 series 下标
    first = True
    for idx, (as_of, direction, entry, stop) in enumerate(series):
        if direction == "FAILED":
            continue  # 失败键保留旧状态，不参与比对
        if first:
            seen = direction
            first = False  # 首轮静默播种，不进 pushedPlans（用户无挂单可改）
            continue
        if direction == seen:
            if direction is not None and pushed is not None:
                risk = abs(entry - stop)
                if risk > 0:
                    old_risk = abs(pushed["entry"] - pushed["stop"])
                    base = max(risk, old_risk)
                    drift = max(abs(entry - pushed["entry"]), abs(stop - pushed["stop"]))
                    if (base > 0 and drift / base >= frac
                            and drift / entry * 100 >= min_price_pct):
                        events.append(("改单", as_of, direction, entry, stop,
                                       pushed["entry"], pushed["stop"], drift, base,
                                       idx, idx - pushed_idx))
                        pushed = {"entry": entry, "stop": stop, "direction": direction}
                        pushed_idx = idx
            continue
        if seen is None and direction is not None:
            events.append(("新", as_of, direction, entry, stop, None, None, None, None, idx))
            pushed = {"entry": entry, "stop": stop, "direction": direction}
            pushed_idx = idx
        elif seen is not None and direction is None:
            events.append(("消失", as_of, seen, None, None,
                           pushed["entry"] if pushed else None,
                           pushed["stop"] if pushed else None, None, None, idx))
            pushed = None
        elif seen is not None and direction is not None:
            events.append(("转向", as_of, direction, entry, stop,
                           pushed["entry"] if pushed else None,
                           pushed["stop"] if pushed else None, None, None, idx))
            pushed = {"entry": entry, "stop": stop, "direction": direction}
            pushed_idx = idx
        seen = direction
    return events


# ------------------------------------------------------------------ 主流程
def main():
    now_ms = int(time.time() * 1000)
    ms1 = min(_last_closed_open(now_ms, itv) for itv in INTERVALS)
    ms0 = (ms1 - WINDOW_DAYS * 86_400_000)
    print(f"回放窗口: {fmt(ms0)} -> {fmt(ms1)}（北京，{WINDOW_DAYS} 天）"
          f"  {len(SYMBOLS)} 币 × {INTERVALS}\n")

    tasks = [(s, itv, ms0, ms1) for itv in INTERVALS for s in SYMBOLS]
    t0 = time.time()
    with mp.Pool(processes=min(8, len(tasks))) as pool:
        results = pool.map(_worker, tasks)
    print(f"采集完成 {time.time()-t0:.0f}s；失败轮次："
          + ", ".join(f"{s[:3]}{i}={f}" for s, i, _, f, _ in results if f) + "\n")

    series_map = {(s, i): ser for s, i, ser, _, _ in results}

    # ---- 阈值无关事件（新/转向/消失只依赖方向指纹，各阈值相同）
    base_events = {k: simulate(v, frac=9e9) for k, v in series_map.items()}
    n_new = sum(1 for evs in base_events.values() for e in evs if e[0] == "新")
    n_flip = sum(1 for evs in base_events.values() for e in evs if e[0] == "转向")
    n_gone = sum(1 for evs in base_events.values() for e in evs if e[0] == "消失")
    days = (ms1 - ms0) / 86_400_000
    print(f"阈值无关事件（30 天全系统）：新 {n_new} / 转向 {n_flip} / 消失 {n_gone}"
          f"（合计 {n_new+n_flip+n_gone}，日均 {(n_new+n_flip+n_gone)/days:.1f}）\n")

    # ---- 每阈值独立模拟
    per_thr = {}
    for frac in THRESHOLDS:
        evs = {}
        for k, ser in series_map.items():
            evs[k] = [e for e in simulate(ser, frac) if e[0] == "改单"]
        per_thr[frac] = evs

    print("=" * 78)
    print("改单事件总量 vs 阈值（全系统 16 键，30 天）")
    print(f"{'阈值':>6} {'总数':>5} {'日均':>6} {'1h':>4} {'4h':>4}   触发时 |Δ入场|/价格%  p50 / p90 / max")
    for frac in THRESHOLDS:
        evs = per_thr[frac]
        total = sum(len(v) for v in evs.values())
        n1 = sum(len(v) for (s, i), v in evs.items() if i == "1h")
        n4 = total - n1
        drifts = sorted(e[7] / e[3] * 100 for v in evs.values() for e in v)
        if drifts:
            dist = f"{drifts[len(drifts)//2]:.3f} / {drifts[int(len(drifts)*0.9)]:.3f} / {drifts[-1]:.3f}"
        else:
            dist = "-"
        print(f"{frac:>6.2f} {total:>5} {total/days:>6.1f} {n1:>4} {n4:>4}   {dist}")

    # ---- 混合口径：阈值×风险 且 ≥ 价格%
    print("\n混合口径（drift ≥ frac×风险 且 ≥ min×价格%）")
    print(f"{'frac':>6} {'min%':>5} {'总数':>5} {'日均':>6} {'1h':>4} {'4h':>4}")
    for frac, minp in [(0.20, 0.30), (0.20, 0.50), (0.30, 0.30), (0.30, 0.50), (0.40, 0.50)]:
        evs = {k: [e for e in simulate(ser, frac, minp) if e[0] == "改单"]
               for k, ser in series_map.items()}
        total = sum(len(v) for v in evs.values())
        n1 = sum(len(v) for (s, i), v in evs.items() if i == "1h")
        print(f"{frac:>6.2f} {minp:>5.2f} {total:>5} {total/days:>6.1f} {n1:>4} {total-n1:>4}")

    # ---- 改单触发时距基线设立的 bar 数（0.20 口径）——衡量"刚推完又改"
    print("\n改单距上次推送基线的 bar 数分布（阈值 0.20；1h 单位=小时，4h 单位=4 小时）")
    for itv in INTERVALS:
        gaps = sorted(e[10] for k in series_map if k[1] == itv for e in per_thr[0.20][k])
        if gaps:
            print(f"  {itv}: p25={gaps[len(gaps)//4]} p50={gaps[len(gaps)//2]} "
                  f"p75={gaps[int(len(gaps)*0.75)]} p90={gaps[int(len(gaps)*0.9)]} "
                  f"（≤2bar 占比 {sum(1 for g in gaps if g <= 2)/len(gaps)*100:.0f}%）")

    # ---- 分币种明细（当前阈值 0.20 vs 候选 0.30/0.50）
    print("\n分币种改单数（0.20 → 0.30 → 0.50）")
    for k in sorted(series_map):
        s, i = k
        row = f"  {s:<9} {i}: "
        for frac in (0.20, 0.30, 0.50):
            row += f"{len(per_thr[frac][k]):>3}   "
        print(row)

    # ---- 当前阈值下的改单明细（最近 25 条）
    cur = [(*e, k) for k, v in per_thr[0.20].items() for e in v]
    cur.sort(key=lambda e: e[1])
    print(f"\n当前阈值 0.20 改单明细（共 {len(cur)} 条，列最近 25 条；推送时间=收盘+5min）")
    print(f"  {'时间(bar收盘)':<13} {'键':<15} {'向':<4} {'旧入场→新入场':<27} "
          f"{'旧止损→新止损':<25} {'漂移/价%':>8} {'漂移/风险':>8}")
    for e in cur[-25:]:
        kind, as_of, d, entry, stop, oe, ostop, drift, base, _i, _g, k = e
        close_t = as_of + STEP[k[1]]
        print(f"  {fmt(close_t):<13} {k[0][:6]+'|'+k[1]:<15} "
              f"{'多' if d=='long' else '空':<4} "
              f"{oe:.6g}→{entry:.6g}".ljust(27) +
              f"{ostop:.6g}→{stop:.6g}".ljust(25) +
              f"{drift/entry*100:>8.3f} {drift/base:>8.2f}")

    # ---- 若扩阈值，被抑制的改单其漂移有多大
    print("\n被抑制改单的漂移画像（0.20 触发但 0.30 不触发的轮次，价格%口径）")
    print("  （说明：扩阈值后基线不重置，漂移会累积到更大才触发——上表 p50/p90 即实际推送时的幅度）")


if __name__ == "__main__":
    main()
