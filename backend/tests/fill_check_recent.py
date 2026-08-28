"""Recent-window 1h event/fill audit (2026-08-28).

User report: "since yesterday 11:00 (北京 08-27 11:00) only ~3 possibly-filled
1h events" — expected ~8 fills/day per the backtest. Ground truth via the
SAME decision pipeline as the notifier:

A. Hourly closed-bar 1h replay since 08-26 00:00 UTC (all four symbols) ->
   plan epochs & events. Push window for an epoch-start bar m = the live
   forming-bar runs during bar m, i.e. 北京 m+5min ~ m+1h+5min (round-24
   alignment: live HH:05 decision ~= replay mark HH on partial data).
B. Per actionable epoch (新/转向): did price touch the epoch-start entry
   while the plan was alive? Order semantics = the live plan watcher:
   entry drift does NOT re-fire, the order sits at the event-hour entry,
   cancelled at the 消失/转向 push. Conservative fill window = bars AFTER
   the event bar (backtest convention: order lives from next bar), capped
   at 24 bars (1h fill window) and the plan end; the event bar itself is
   reported separately (intra-bar push timing not resolvable).
C. Backtest context: 1h fills/day distribution + order fill rate + rolling
   28h window distribution — is "3 fills in ~28h" normal?

Standing plans that started BEFORE the window (e.g. SOL's long) generate no
event since 11:00 — their original order's fill is checked informationally.

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/fill_check_recent.py
"""
import asyncio
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import profit_sweep2 as ps
from backtest_5y import SYMBOLS, W5, CONF5, load_records, sim_outcome_fast
from services import derivs_store
from services.analysis.context import run_analysis

H1 = 3_600_000
H4 = 14_400_000


def uts(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def bj(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000 + 8 * 3600, tz=timezone.utc) \
        .strftime("%m-%d %H:%M")


REPLAY_START = uts("2026-08-26 00:00")
WINDOW_START = uts("2026-08-27 02:00")   # 北京 08-27 10:00 起的K线
NOW_MS = int(datetime.now(timezone.utc).timestamp() * 1000)
LAST_MARK = ((NOW_MS - H1) // H1) * H1   # last closed 1h bar open

dfs: dict = {}
states: dict = {}   # sym -> {mark_ms: (direction|None|'FAIL', entry, score)}
live_now: dict = {}  # sym -> (direction, entry, score) from forming-bar analysis


async def fetch_live():
    for sym in SYMBOLS:
        for itv, lim in (("1h", 1000), ("4h", 1000), ("1d", 120)):
            await ps.kline_cache.get_klines(sym, itv, lim)  # top up cache
        rows = await ps.kline_cache.get_klines(sym, "1h", 1500)
        dfs[sym] = ps.kline_cache.rows_to_df(rows)
        print(f"[data] {sym} 1h: {len(dfs[sym])} bars, 最新K {bj(int(dfs[sym]['time'].iloc[-1]))}",
              flush=True)


sem = asyncio.Semaphore(8)


async def state_at(sym: str, m: int):
    for _ in range(2):
        async with sem:
            try:
                a = await run_analysis(sym, "1h", 500, as_of=m)
                plan = (a.get("summary") or {}).get("tradePlan")
                score = (a.get("summary") or {}).get("score")
                if not plan:
                    return (None, None, score)
                return (plan["direction"], float(plan["entry"]), score)
            except Exception:
                await asyncio.sleep(1)
    return ("FAIL", None, None)


def epochs_of(sym: str) -> list[dict]:
    marks = sorted(m for m in states[sym] if m >= REPLAY_START)
    out: list[dict] = []
    cur = None
    for m in marks:
        d, e, _ = states[sym][m]
        if d in (None, "FAIL"):
            if cur:
                out.append(cur)
                cur = None
            continue
        if cur and cur["dir"] == d:
            cur["end"] = m
            cur["entries"].append(e)
        else:
            if cur:
                out.append(cur)
            cur = {"dir": d, "start": m, "end": m, "entries": [e]}
    if cur:
        out.append(cur)
    return out


async def backwalk(sym: str, ep: dict) -> None:
    """Find the true start of an epoch that already existed at REPLAY_START."""
    m = REPLAY_START - H1
    while m >= REPLAY_START - 72 * H1:
        d, e, _ = await state_at(sym, m)
        if d != ep["dir"]:
            break
        ep["start"] = m
        ep["entries"].insert(0, e)
        m -= H1


def check_fill(sym: str, ep: dict) -> dict:
    df = dfs[sym]
    times = df["time"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    long = ep["dir"] == "long"
    entry = ep["entries"][0]
    active = ep["end"] == LAST_MARK and states[sym][LAST_MARK][0] == ep["dir"]
    lo = ep["start"] + H1
    hi = min(ep["start"] + 24 * H1, int(times[-1]))
    if not active:
        hi = min(hi, ep["end"] + H1)
    fill = None
    for i, t in enumerate(times):
        if lo <= t <= hi:
            if (long and lows[i] <= entry) or ((not long) and highs[i] >= entry):
                fill = int(t)
                break
    evbar = None
    for i, t in enumerate(times):
        if t == ep["start"]:
            evbar = bool((long and lows[i] <= entry) or ((not long) and highs[i] >= entry))
            break
    return {"entry": entry, "active": active, "fill": fill, "evbar": evbar}


async def five_year_stats() -> None:
    print(f"\n{'='*104}\n===== 回测口径：1h 成交频率（四币合计，容量约束串行，2021-09~2026-08）=====\n{'='*104}")
    per_day: dict = defaultdict(int)
    all_times: list = []
    total_orders = 0
    total_fills = 0
    cfg = CONF5["1h"]
    depth, stopw, be_frac, tgt, texit, trail = tuple(cfg["geo"])
    fill_bars = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
    for sym in SYMBOLS:
        rows = await ps.kline_cache.get_klines(sym, "1h", W5["1h"])
        df = ps.kline_cache.rows_to_df(rows)
        records = load_records(sym, "1h", {"1h": df}, False)
        highs = df["high"].to_numpy()
        lows = df["low"].to_numpy()
        closes = df["close"].to_numpy()
        n = len(df)
        tidx = {int(t): i for i, t in enumerate(df["time"].to_numpy())}
        busy = -1
        for r in records:
            if r.get("plan") is None:
                continue
            i = tidx.get(r["time"])
            if i is None or i <= busy:
                continue
            built = ps.build_plan(r, depth, stopw)
            if built is None:
                continue
            total_orders += 1
            out = sim_outcome_fast(highs, lows, closes, n, i, *built,
                                   be_frac, tgt, texit, fill_bars, trail)
            if out is None:
                continue
            total_fills += 1
            busy = out[2]
            t = r["time"]
            all_times.append(t)
            per_day[datetime.fromtimestamp(t / 1000, tz=timezone.utc).date()] += 1
    days = sorted(per_day)
    counts = np.array([per_day[d] for d in days])
    print(f"挂单次数（每4小时决策重挂口径）={total_orders}  成交={total_fills}  "
          f"填充率={total_fills/total_orders*100:.0f}%")
    print(f"成交/天: 均值 {counts.mean():.1f}  中位 {np.median(counts):.0f}  "
          f"min {counts.min()}  max {counts.max()}  (n={len(days)} 天)")
    for thr in (3, 4, 5, 6):
        print(f"  ≤{thr} 单的天数: {(counts <= thr).sum()} 天 ({(counts <= thr).mean()*100:.1f}%)")
    by_year = defaultdict(list)
    for d in days:
        by_year[d.year].append(per_day[d])
    print("分年 均值: " + "  ".join(f"{y}:{np.mean(v):.1f}/天" for y, v in sorted(by_year.items())))
    print(f"最近 60 天: 均值 {np.mean([per_day[d] for d in days[-60:]]):.1f}/天, "
          f"≤3 单 {(np.array([per_day[d] for d in days[-60:]]) <= 3).sum()}/60 天")

    # rolling 28h windows (~user's window length)
    ts = np.array(sorted(all_times))
    marks = np.arange(ts[0], ts[-1], H1)
    win = np.searchsorted(ts, marks + 28 * H1) - np.searchsorted(ts, marks)
    print(f"滚动 28h 窗口成交数: 均值 {win.mean():.1f}  中位 {np.median(win):.0f}  "
          f"≤3 占比 {(win <= 3).mean()*100:.1f}%  ≤5 占比 {(win <= 5).mean()*100:.1f}%  (n={len(marks)})")


async def main() -> None:
    t0 = time.time()
    print("[backfill] daily_rates ...", flush=True)
    await asyncio.gather(*[derivs_store.ensure_backfill(s) for s in SYMBOLS])
    await fetch_live()

    marks = list(range(REPLAY_START, LAST_MARK + 1, H1))
    jobs = [(s, m) for s in SYMBOLS for m in marks]
    res = await asyncio.gather(*[state_at(s, m) for s, m in jobs])
    for (s, m), r in zip(jobs, res):
        states.setdefault(s, {})[m] = r

    for s in SYMBOLS:
        try:
            a = await run_analysis(s, "1h", 500)
            plan = (a.get("summary") or {}).get("tradePlan")
            live_now[s] = (plan["direction"] if plan else None,
                           float(plan["entry"]) if plan else None,
                           (a.get("summary") or {}).get("score"))
        except Exception:
            live_now[s] = ("FAIL", None, None)

    eps_all: dict = {}
    for s in SYMBOLS:
        eps = epochs_of(s)
        for ep in eps:
            if ep["start"] == REPLAY_START:
                await backwalk(s, ep)
        eps_all[s] = eps

    print(f"\n{'='*104}\n===== 昨天 11:00（北京 08-27 11:00 = {bj(WINDOW_START)}）以来的 1h 事件 =====\n{'='*104}")
    print(f"{'推送窗(北京)':<24}{'标的':<9}{'事件':<10}{'入场价':>12}  {'保守成交':<14}{'事件K触及'}")
    n_events = n_filled = 0
    for s in SYMBOLS:
        eps = eps_all[s]
        for ep in eps:
            if ep["start"] < WINDOW_START:
                continue
            prev = states[s].get(ep["start"] - H1, (None,))[0]
            kind = "新" if prev in (None, "FAIL") else "转向"
            fc = check_fill(s, ep)
            n_events += 1
            fill_txt = (f"✓ {bj(fc['fill'])}" if fc["fill"]
                        else ("挂单中" if fc["active"] else "未成交"))
            if fc["fill"]:
                n_filled += 1
            dirn = "做多" if ep["dir"] == "long" else "做空"
            pw = f"{bj(ep['start']+5*60*1000)}~{bj(ep['start']+H1+5*60*1000)}"
            print(f"{pw:<24}{s[:-4]:<9}{kind}{dirn:<7}{fc['entry']:>12,.1f}  "
                  f"{fill_txt:<14}{'是' if fc['evbar'] else '否'}")

    print(f"\n可下单事件（新/转向）: {n_events} 个；保守口径已回踩成交: {n_filled} 个"
          f"（事件K本身已触及入场的另有标注，见上表）")

    print(f"\n--- 跨窗口老计划（事件发生在 11:00 前，之后无推送）---")
    for s in SYMBOLS:
        for ep in eps_all[s]:
            if ep["start"] >= WINDOW_START:
                continue
            if ep["end"] < WINDOW_START - H1:
                continue
            fc = check_fill(s, ep)
            dirn = "做多" if ep["dir"] == "long" else "做空"
            fill_txt = f"原始挂单已于 {bj(fc['fill'])} 回踩成交" if fc["fill"] else "原始挂单未成交（已过期或仍挂着）"
            print(f"  {s[:-4]:<8} {dirn}: 始于 {bj(ep['start'])}（≥），终于 {bj(ep['end']+H1)}；"
                  f"起始入场 {fc['entry']:,.1f} → 最新 {ep['entries'][-1]:,.1f}；{fill_txt}")

    print(f"\n--- 当前实时状态（notifier 下一轮 HH:05 将看到的口径）---")
    for s in SYMBOLS:
        d, e, sc = live_now[s]
        print(f"  {s[:-4]:<8} {'无计划' if d is None else ('做多' if d=='long' else '做空')}"
              f"  score={sc:+.0f}" + (f"  entry={e:,.1f}" if e else ""))

    print(f"\n--- 4h 复核（昨天 11:00 以来应无事件）---")
    m4 = list(range(uts("2026-08-27 00:00"), LAST_MARK + 1, H4))
    st4 = {}
    for s in SYMBOLS:
        st4[s] = {}
    jobs4 = [(s, m) for s in SYMBOLS for m in m4]
    res4 = await asyncio.gather(*[state_at(s, m) for s, m in jobs4])
    for (s, m), r in zip(jobs4, res4):
        st4[s][m] = r[0]
    trans4 = 0
    for s in SYMBOLS:
        prev = None
        for m in m4:
            cur = st4[s][m]
            if prev is not None and cur != prev:
                trans4 += 1
                print(f"  [4h 转变] {s} {bj(m)}: {prev} -> {cur}")
            prev = cur
    if trans4 == 0:
        print("  4h 四币计划方向零变化（全部为做多）→ 无事件，正确行为")

    await five_year_stats()
    print(f"\n总耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    asyncio.run(main())
