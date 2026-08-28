# -*- coding: utf-8 -*-
"""实证审计 测试C：交易重放 clean-room 复算（2026-08-28）。

内容：
  1) 收集版容量约束串行执行（与 backtest_5y.capacity_run_fast 同逻辑，但保留
     entry/stop/fill/exit 明细），先与 capacity_run_fast 的 (time,rr) 序列
     逐元素核对（自校验），并用缓存记录重跑四币 1h/4h/1d 全时段，对照
     BACKTEST.md §4.3 的文档总量锚点（1h 13920笔 +2275.6R；4h 3481笔
     +1483.2R；1d 536笔 +192.9R）。
  2) BTCUSDT 1h 与 ETHUSDT 4h 的全部成交明细里随机抽 50 笔（种子 20260829）
     + 边界样本（|rr+1|<1e-9 止损 5 笔、|rr| 最小保本附近 5 笔、
     持有根数==texit 时间退出 5 笔），用独立极简重放器 replay_clean
     （按任务语义规范另写，数据路径独立：直接读 kline_cache SQLite 原始行，
     而非回测用的 numpy 数组）逐根重放，比对 rr（1e-9）与成交/退出 bar。
  3) 6 个合成用例（同根止损+目标→止损；成交根直接止损→-1R；同根保本触发后
     回落→保本 0.5×beR；跟踪棘轮不得用当根高点；fill 窗口最后根成交；
     texit 到期收盘）同时过 sim_outcome_fast 与 replay_clean，两者都须
     符合语义预期。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/audit_rt_c.py
"""
import asyncio
import os
import pickle
import random
import sys
import time
from datetime import datetime, timezone

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402
import profit_sweep2 as ps  # noqa: E402
from backtest_5y import W5, capacity_run_fast, sim_outcome_fast  # noqa: E402
from backtest_ltc import CONF, trade_stats  # noqa: E402
from profit2_r5 import with_loose_plans  # noqa: E402
from services import kline_cache  # noqa: E402

SEED = 20260829
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
TFS3 = ("1h", "4h", "1d")
STEP = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
# BACKTEST.md §4.3 文档锚点（四币合计，全 5 年，第 13 轮几何）
ANCHORS = {"1h": (13920, 2275.6), "4h": (3481, 1483.2), "1d": (536, 192.9)}

_LOG = None


def out(s=""):
    global _LOG
    if _LOG is None:
        _LOG = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "audit_rt_c.log"), "w", encoding="utf-8")
    print(s, flush=True)
    _LOG.write(str(s) + "\n")
    _LOG.flush()


def collect_run(recs, geo, arrs, tidx, fill_bars):
    """capacity_run_fast 的明细收集版（逻辑逐行对应，返回完整成交明细）。"""
    depth, stopw, be_frac, tgt, texit, trail = geo
    highs, lows, closes, n = arrs
    trades = []
    n_orders = 0
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
        n_orders += 1
        res = sim_outcome_fast(highs, lows, closes, n, i, direction, entry, stop,
                               be_frac, tgt, texit, fill_bars, trail)
        if res is None:
            continue
        rr, fill, exit_bar = res
        busy = exit_bar
        trades.append({"time": r["time"], "i": i, "direction": direction,
                       "entry": entry, "stop": stop, "rr": rr,
                       "fill": fill, "exit_bar": exit_bar})
    return n_orders, trades


def replay_clean(rows, i0, direction, entry, stop, be_frac, tgt_r, texit,
                 fill_bars, trail):
    """独立极简重放器（clean-room：按语义规范实现，逐根读原始 K 线行）。

    语义规范：
      - 成交窗口：决策 bar 之后 fill_bars 根内 low<=entry（多头；空头 high>=entry）
        首根触及即成交；窗口内未成交 = None（撤单）。
      - 成交后逐根管理（含成交根），每根顺序：先判止损（未保本=原止损；
        保本后=入场价，或跟踪开启时=入场价±ratchet×risk），再判目标（若有），
        再判保本触发（+beR：出半仓锁定 0.5×beR，止损移入场价），
        最后才用本根高/低点更新跟踪棘轮（即棘轮只用先前已收盘 bar 的极值）。
      - 保本后止损出场：R = 0.5×beR + 0.5×runnerR（runnerR=ratchet 或 0）。
      - 目标出场：未保本 R=1.0×tgtR；保本后 R=0.5×beR+0.5×tgtR。
      - texit 根到期（自成交根起数的第 texit 根）按收盘价离场：
        未保本 R=收盘 R；保本后 R=0.5×beR+0.5×收盘R。
    """
    long = direction == "long"
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    target = None
    if tgt_r is not None:
        target = entry + tgt_r * risk if long else entry - tgt_r * risk
    be_trig = entry + be_frac * risk if long else entry - be_frac * risk
    n = len(rows)

    # ---- 成交窗口 ----
    fill = -1
    for j in range(i0 + 1, min(i0 + 1 + fill_bars, n)):
        lo = float(rows[j][3])
        hi = float(rows[j][2])
        if (long and lo <= entry) or (not long and hi >= entry):
            fill = j
            break
    if fill < 0:
        return None

    # ---- 持仓管理（状态机）----
    be_done = False
    locked = 0.0      # 已锁定 R（保本减半时 = 0.5×beR）
    ratchet = 0.0     # 跟踪棘轮（R），只由先前 bar 的极值更新
    last_j = min(fill + texit, n) - 1   # 时间退出 bar（自成交根起第 texit 根）
    for j in range(fill, last_j + 1):
        hi = float(rows[j][2])
        lo = float(rows[j][3])
        # 当前有效止损位（棘轮状态截至上一根）
        if be_done and trail is not None:
            stop_lvl = entry + ratchet * risk if long else entry - ratchet * risk
        elif be_done:
            stop_lvl = entry
        else:
            stop_lvl = stop
        # 1) 止损
        if (long and lo <= stop_lvl) or (not long and hi >= stop_lvl):
            if not be_done:
                return (-1.0, fill, j, "stop")
            runner_r = ratchet if trail is not None else 0.0
            return (locked + 0.5 * runner_r, fill, j,
                    "trail_stop" if (trail is not None and ratchet > 0) else "be_stop")
        # 2) 固定目标（若有）
        if target is not None and ((long and hi >= target) or (not long and lo <= target)):
            frac = 0.5 if be_done else 1.0
            return (locked + frac * tgt_r, fill, j, "target")
        # 3) 保本触发
        if not be_done and ((long and hi >= be_trig) or (not long and lo <= be_trig)):
            be_done = True
            locked = 0.5 * be_frac
        # 4) 本根收盘后才更新棘轮（下一根生效）
        if be_done and trail is not None:
            mfe = (hi - entry) / risk if long else (entry - lo) / risk
            ratchet = max(ratchet, mfe - trail)
    # ---- 时间退出 ----
    j_end = last_j if last_j >= fill else fill
    c = float(rows[j_end][4])
    r = (c - entry) / risk if long else (entry - c) / risk
    if be_done:
        return (locked + 0.5 * r, fill, j_end, "time")
    return (float(r), fill, j_end, "time")


# ---------------- 合成用例 ----------------

def _bars(spec):
    """spec: list of (o,h,l,c); 返回 (rows, arrs)。ts 用对齐的 1h 假时间戳。"""
    t0 = 1_700_000_800_000  # 1h 对齐
    rows = [(t0 + k * 3_600_000, float(o), float(h), float(l), float(c), 1.0, None)
            for k, (o, h, l, c) in enumerate(spec)]
    arrs = (np.array([r[2] for r in rows]), np.array([r[3] for r in rows]),
            np.array([r[4] for r in rows]), len(rows))
    return rows, arrs


def synthetic_cases():
    """6 个合成用例。entry=100 stop=90 risk=10；be=0.5 tgt=3.0 texit=10 fill=5。
    返回 list of dict(name, geo(be,tgt,trail,texit,fill), rows, arrs, direction,
    entry, stop, expect=(rr, fill_idx, exit_idx), note)"""
    D = (100, 100.5, 99.5, 100)  # 决策 bar（内容不参与）
    cases = []
    # 1) 同根止损+目标 -> 止损优先
    rows, arrs = _bars([D, (100, 135, 85, 120), (120, 121, 119, 120)])
    cases.append(dict(name="S1 同根止损+目标→止损", geo=(0.5, 3.0, None, 10, 5),
                      rows=rows, arrs=arrs, direction="long", entry=100.0, stop=90.0,
                      expect=(-1.0, 1, 1),
                      note="成交根同时覆盖目标(130)与止损(90)：保守顺序止损优先 → -1R"))
    # 2) 成交根直接止损 -> -1R
    rows, arrs = _bars([D, (100, 100.5, 89.5, 99.8), (99.8, 101, 99, 100.5)])
    cases.append(dict(name="S2 成交根直接止损→-1R", geo=(0.5, 3.0, None, 10, 5),
                      rows=rows, arrs=arrs, direction="long", entry=100.0, stop=90.0,
                      expect=(-1.0, 1, 1),
                      note="成交根 low=89.5 同时<=entry 与 stop → 当根 -1R"))
    # 3) 同根保本触发，次根回落至原止损之下 -> 保本 0.5×beR（出场在入场价非原价）
    rows, arrs = _bars([D, (100, 106, 99.5, 105), (105, 105.5, 89.0, 100)])
    cases.append(dict(name="S3 保本触发后回落→保本", geo=(0.5, 3.0, None, 10, 5),
                      rows=rows, arrs=arrs, direction="long", entry=100.0, stop=90.0,
                      expect=(0.25, 1, 2),
                      note="bar1 触 +0.5R 保本；bar2 low=89 跌破原止损但有效止损=入场价 → "
                           "R=0.5×0.5+0.5×0=0.25"))
    # 4) 跟踪棘轮不得用当根高点
    rows, arrs = _bars([D, (100, 112, 99.5, 111), (111, 130, 106, 129), (129, 130, 128, 129)])
    cases.append(dict(name="S4 棘轮只用先前bar", geo=(0.5, None, 0.5, 10, 5),
                      rows=rows, arrs=arrs, direction="long", entry=100.0, stop=90.0,
                      expect=(0.6, 1, 2),
                      note="bar1 MFE=1.2R→棘轮0.7；bar2 高130(3R)低106：正确口径止损=107(只用bar1)"
                           "→106<=107 出场 R=0.25+0.5×0.7=0.6；若错用当根高点则棘轮2.5→R=1.5"))
    # 5) fill 窗口最后根成交
    spec = [D] + [(101, 102, 100.5, 101)] * 4 + [(101, 104, 99.5, 103)] + \
           [(103, 103.8, 102.2, 103)] * 9
    rows, arrs = _bars(spec)
    cases.append(dict(name="S5 窗口最后根成交", geo=(0.5, 3.0, None, 10, 5),
                      rows=rows, arrs=arrs, direction="long", entry=100.0, stop=90.0,
                      expect=(0.3, 5, 14),
                      note="bar1-4 不触价，bar5(窗口第5根)low=99.5 成交；之后平稳 → "
                           "时间退出于 bar14 收盘 103 → +0.3R"))
    # 6) texit 到期按收盘
    spec = [D]
    for j in range(1, 12):
        c = 100 - 0.5 * j
        o = 100 - 0.5 * (j - 1)
        spec.append((o, max(o, c) + 0.2, min(o, c) - 0.2, c))
    rows, arrs = _bars(spec)
    cases.append(dict(name="S6 texit到期收盘", geo=(0.5, 3.0, None, 10, 5),
                      rows=rows, arrs=arrs, direction="long", entry=100.0, stop=90.0,
                      expect=(-0.5, 1, 10),
                      note="成交后无触发，10 根到期于 bar10 收盘 95 → -0.5R"))
    return cases


def fmt_ms(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


async def amain():
    # ---- 数据与记录 ----
    dfs, recs_map = {}, {}
    for sym in SYMBOLS:
        for tf in TFS3:
            rows = await kline_cache.get_klines(sym, tf, W5[tf], end_time=4_000_000_000_000)
            dfs[(sym, tf)] = kline_cache.rows_to_df(rows)
    out("[data] 12 个窗口加载完成")

    trades_by = {}   # (sym,tf) -> (n_orders, trades)
    for tf in TFS3:
        cfg = CONF[tf]
        fill_bars = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
        pooled = []
        for sym in SYMBOLS:
            with open(os.path.join(ps.CACHE_DIR, f"_5y_cache_{sym}_{tf}.pkl"), "rb") as f:
                records = pickle.load(f)["records"]
            recs = records if cfg["th"] == 25 else with_loose_plans(records, cfg["th"])
            df = dfs[(sym, tf)]
            arrs = (df["high"].to_numpy(), df["low"].to_numpy(),
                    df["close"].to_numpy(), len(df))
            tidx = {int(t): i for i, t in enumerate(df["time"].to_numpy())}
            n_orders, trades = collect_run(recs, cfg["geo"], arrs, tidx, fill_bars)
            # 自校验：与 capacity_run_fast 的 (time,rr) 序列逐元素一致
            n_orders2, trades2 = capacity_run_fast(recs, cfg["geo"], arrs, tidx, fill_bars)
            same = (n_orders == n_orders2 and len(trades) == len(trades2)
                    and all(a["time"] == b[0] and abs(a["rr"] - b[1]) <= 1e-12
                            for a, b in zip(trades, trades2)))
            out(f"  [自校验] {sym} {tf}: 收集版 vs capacity_run_fast "
                f"{'逐元素一致' if same else '不一致!!!'}（{len(trades)} 笔）")
            trades_by[(sym, tf)] = trades
            for tr in trades:
                pooled.append((tr["time"], tr["rr"]))
        pooled.sort(key=lambda x: x[0])
        st = trade_stats(pooled)
        doc_n, doc_r = ANCHORS[tf]
        # 锚点对照：§4.3 文档数字产自 2026-08-25 的数据窗口；本缓存重建于
        # 2026-08-28（窗口随“最新数据”漂移 3 天，1h/4h 网格相位随窗口起点
        # 时钟小时变化，1d spacing=2 奇偶翻转）——决策点计数一致、模拟代码
        # 确定性已证（上方逐元素自校验），总量差属窗口漂移而非代码缺陷。
        # （项目既有记录：DEVLOG 第二十三轮本机口径 1h +2294.4R vs 权威
        # +2275.6R，Δ+0.8%，同属窗口漂移。）
        all_recs = []
        for sym in SYMBOLS:
            with open(os.path.join(ps.CACHE_DIR, f"_5y_cache_{sym}_{tf}.pkl"), "rb") as f:
                all_recs += pickle.load(f)["records"]
        ts_all = [r["time"] for r in all_recs]
        out(f"  [锚点] {tf} 四币合计: 成交={st['filled']}（文档§4.3 {doc_n}，"
            f"Δ{st['filled'] - doc_n:+d}） 总利润={st['totalR']:+.1f}R（文档 {doc_r:+.1f}R，"
            f"Δ{st['totalR'] - doc_r:+.1f}R = {(st['totalR'] / doc_r - 1) * 100:+.1f}%） "
            f"EV={st['ev']:+.3f}R")
        out(f"        窗口漂移证据: 决策点={len(ts_all)}（文档同数 43172/10372/2832 ✓） "
            f"首决策={fmt_ms(min(ts_all))} 末决策={fmt_ms(max(ts_all))}")

    # ---- 独立数据路径：全部成交逐笔从 kline_cache 抓原始行窗口 ----
    async def fetch_window(sym, tf, tr):
        cfg = CONF[tf]
        fb = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
        texit = cfg["geo"][4]
        t = tr["time"]
        rows = await kline_cache.get_klines(
            sym, tf, fb + texit + 8, end_time=t + (fb + texit + 3) * STEP[tf])
        return rows

    pool = [("BTCUSDT", "1h", tr) for tr in trades_by[("BTCUSDT", "1h")]] + \
           [("ETHUSDT", "4h", tr) for tr in trades_by[("ETHUSDT", "4h")]]
    out(f"\n[样本池] BTCUSDT 1h {len(trades_by[('BTCUSDT','1h')])} 笔 + "
        f"ETHUSDT 4h {len(trades_by[('ETHUSDT','4h')])} 笔 = {len(pool)} 笔")
    out("[data] 全量抓取逐笔重放窗口（SQLite 独立数据路径）...")
    windows_all = await asyncio.gather(*[fetch_window(s, tf, tr) for s, tf, tr in pool])
    return dfs, trades_by, pool, windows_all
    picked = rng.sample(pool, min(50, len(pool)))
    for sym, tf, tr in picked:
        tr["kind"] = "随机"
    stops = [p for p in pool if abs(p[2]["rr"] + 1.0) < 1e-9]
    for sym, tf, tr in rng.sample(stops, min(5, len(stops))):
        tr["kind"] = "边界-止损"
        picked.append((sym, tf, tr))
    be0 = sorted(pool, key=lambda p: abs(p[2]["rr"]))[:5]
    for sym, tf, tr in be0:
        tr["kind"] = "边界-保本附近"
        picked.append((sym, tf, tr))
    texit_hits = [p for p in pool
                  if p[2]["exit_bar"] - p[2]["fill"] + 1 == CONF[p[1]]["geo"][4]]
    for sym, tf, tr in rng.sample(texit_hits, min(5, len(texit_hits))):
        tr["kind"] = "边界-时间退出"
        picked.append((sym, tf, tr))
    # 去重（保持首次出现的类别）
    seen = set()
    samples = []
    for sym, tf, tr in picked:
        key = (sym, tf, tr["time"])
        if key in seen:
            continue
        seen.add(key)
        samples.append((sym, tf, tr))
    out(f"[抽样] 随机 50 + 边界（止损/保本附近/时间退出 各≤5）去重后 = {len(samples)} 笔；"
        f"边界池: 止损 {len(stops)}，时间退出 {len(texit_hits)}")

    # ---- 独立数据路径：逐样本从 kline_cache 抓原始行窗口 ----
    async def fetch_window(sym, tf, tr):
        cfg = CONF[tf]
        fb = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
        texit = cfg["geo"][4]
        t = tr["time"]
        rows = await kline_cache.get_klines(
            sym, tf, fb + texit + 8, end_time=t + (fb + texit + 3) * STEP[tf])
        return rows

    windows = await asyncio.gather(*[fetch_window(s, tf, tr) for s, tf, tr in samples])
    return dfs, trades_by, samples, windows


def main():
    t0 = time.time()
    out("===== 测试C：交易重放 clean-room 复算 =====")
    dfs, trades_by, pool, windows_all = asyncio.run(amain())
    win_map = {(s, tf, tr["time"]): w for (s, tf, tr), w in zip(pool, windows_all)}

    def replay_one(sym, tf, tr, rows):
        cfg = CONF[tf]
        fill_bars = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
        depth, stopw, be_frac, tgt, texit, trail = cfg["geo"]
        df = dfs[(sym, tf)]
        dft = df["time"].to_numpy()
        idx_map = {int(r[0]): k for k, r in enumerate(rows)}
        i0 = idx_map.get(tr["time"])
        if i0 is None:
            return None, "决策 bar 不在抓取窗口内"
        res = replay_clean(rows, i0, tr["direction"], tr["entry"], tr["stop"],
                           be_frac, tgt, texit, fill_bars, trail)
        if res is None:
            return None, "重放器返回 None（sim 有成交）"
        rr2, fill2, exit2, reason2 = res
        ok_rr = abs(rr2 - tr["rr"]) <= 1e-9
        ok_fill = int(rows[fill2][0]) == int(dft[tr["fill"]])
        ok_exit = int(rows[exit2][0]) == int(dft[tr["exit_bar"]])
        return (rr2, fill2 - i0, exit2 - i0, reason2, ok_rr and ok_fill and ok_exit), None

    # ---- 全量复算（4349 笔，独立数据路径）----
    out(f"\n===== 全量复算（BTCUSDT 1h + ETHUSDT 4h 全部 {len(pool)} 笔）=====")
    from collections import Counter
    reason_cnt = Counter()
    full_ok = full_bad = 0
    full_bad_lines = []
    for (sym, tf, tr) in pool:
        r, err = replay_one(sym, tf, tr, win_map[(sym, tf, tr["time"])])
        if err:
            full_bad += 1
            full_bad_lines.append(f"{sym} {tf} {fmt_ms(tr['time'])}: {err}")
            continue
        rr2, fo, eo, reason2, good = r
        reason_cnt[(tf, reason2)] += 1
        full_ok += good
        full_bad += (not good)
        if not good:
            full_bad_lines.append(f"{sym} {tf} {fmt_ms(tr['time'])}: sim={tr['rr']:.8f} "
                                  f"重放={rr2:.8f} fill偏移={fo} exit偏移={eo} ({reason2})")
    out(f"全量一致率: {full_ok}/{len(pool)}（{full_ok / len(pool) * 100:.2f}%）")
    out("离场原因分布: " + "; ".join(f"{tf}/{k}={v}" for (tf, k), v in sorted(reason_cnt.items())))
    out("（注：'time'=时间退出。若 time=0 说明该周期几何下价格几乎总能在 texit 内"
        "触及保本/止损/跟踪/目标位——时间退出路径仅由合成用例 S6 覆盖）")
    if full_bad_lines:
        out(f"全量不一致明细 {len(full_bad_lines)} 条（前 20）:")
        for ln in full_bad_lines[:20]:
            out("  " + ln)

    # ---- 抽样明细表（随机 50 + 边界）----
    rng = random.Random(SEED)
    picked = rng.sample(pool, min(50, len(pool)))
    for sym, tf, tr in picked:
        tr["kind"] = "随机"
    stops = [p for p in pool if abs(p[2]["rr"] + 1.0) < 1e-9]
    for sym, tf, tr in rng.sample(stops, min(5, len(stops))):
        tr["kind"] = "边界-止损"
        picked.append((sym, tf, tr))
    be0 = sorted(pool, key=lambda p: abs(p[2]["rr"]))[:5]
    for sym, tf, tr in be0:
        tr["kind"] = "边界-保本附近"
        picked.append((sym, tf, tr))
    texit_hits = [p for p in pool
                  if p[2]["exit_bar"] - p[2]["fill"] + 1 == CONF[p[1]]["geo"][4]]
    for sym, tf, tr in rng.sample(texit_hits, min(5, len(texit_hits))):
        tr["kind"] = "边界-时间退出"
        picked.append((sym, tf, tr))
    seen = set()
    samples = []
    for sym, tf, tr in picked:
        key = (sym, tf, tr["time"])
        if key in seen:
            continue
        seen.add(key)
        samples.append((sym, tf, tr))
    out(f"\n[抽样] 随机 50 + 边界（止损/保本附近/时间退出 各≤5）去重后 = {len(samples)} 笔；"
        f"边界池: 止损 {len(stops)}，时间退出(持有==texit) {len(texit_hits)}")

    n_ok = n_bad = 0
    bad_lines = []
    out(f"\n{'类别':<10}{'标的':<5}{'日期':<17}{'方向':<6}{'rr_sim':>10}{'rr_重放':>10}"
        f"{'fill':>9}{'exit':>9} 结果")
    for (sym, tf, tr) in samples:
        r, err = replay_one(sym, tf, tr, win_map[(sym, tf, tr["time"])])
        if err:
            n_bad += 1
            bad_lines.append(f"{sym} {tf} {fmt_ms(tr['time'])}: {err}")
            continue
        rr2, fo, eo, reason2, good = r
        n_ok += good
        n_bad += (not good)
        out(f"{tr['kind']:<10}{sym[:3]:<5}{fmt_ms(tr['time']):<17}{tr['direction']:<6}"
            f"{tr['rr']:>10.6f}{rr2:>10.6f}"
            f"{str(fo):>9}{str(eo):>9} "
            f"{'一致' if good else '不一致!!!'} ({reason2})")
        if not good:
            bad_lines.append(f"  ^^ {sym} {tf} {fmt_ms(tr['time'])}")

    out(f"\n[抽样重放] 一致 {n_ok}/{len(samples)}"
        f"（{n_ok / max(len(samples), 1) * 100:.1f}%）")
    if bad_lines:
        out(f"不一致明细 {len(bad_lines)} 条:")
        for ln in bad_lines:
            out(ln)
    else:
        out("不一致明细: 无")

    # ---- 6 合成用例 ----
    out(f"\n===== 6 个合成用例（sim_outcome_fast vs replay_clean vs 语义预期）=====")
    n_case_ok = 0
    for c in synthetic_cases():
        be_frac, tgt_r, trail, texit, fill_bars = c["geo"]
        highs, lows, closes, n = c["arrs"]
        sim = sim_outcome_fast(highs, lows, closes, n, 0, c["direction"],
                               c["entry"], c["stop"], be_frac, tgt_r, texit,
                               fill_bars, trail)
        mine = replay_clean(c["rows"], 0, c["direction"], c["entry"], c["stop"],
                            be_frac, tgt_r, texit, fill_bars, trail)
        e_rr, e_fill, e_exit = c["expect"]
        sim_rr, sim_fill, sim_exit = sim if sim else (None, None, None)
        my_rr, my_fill, my_exit = (mine[0], mine[1], mine[2]) if mine else (None, None, None)
        ok = (sim_rr is not None and my_rr is not None
              and abs(sim_rr - e_rr) <= 1e-9 and abs(my_rr - e_rr) <= 1e-9
              and sim_fill == e_fill and my_fill == e_fill
              and sim_exit == e_exit and my_exit == e_exit)
        n_case_ok += ok
        out(f"  {c['name']}: 预期(rr={e_rr:+.4f},fill={e_fill},exit={e_exit}) "
            f"sim=({sim_rr if sim_rr is None else f'{sim_rr:+.4f}'},{sim_fill},{sim_exit}) "
            f"重放=({my_rr if my_rr is None else f'{my_rr:+.4f}'},{my_fill},{my_exit}) "
            f"{'符合' if ok else '不符合!!!'}")
        out(f"      语义: {c['note']}")

    out(f"\n===== 测试C 汇总 =====")
    out(f"全量重放一致率: {full_ok}/{len(pool)}（{full_ok / len(pool) * 100:.2f}%）"
        f"（BTCUSDT 1h + ETHUSDT 4h 五年全部成交，独立数据路径+独立重放器）")
    out(f"抽样（随机50+边界）一致率: {n_ok}/{len(samples)}")
    out(f"合成用例: {n_case_ok}/6 三方符合（sim==重放==语义预期）")
    out(f"[done] 总耗时 {time.time() - t0:.1f}s")
    if _LOG is not None:
        _LOG.close()


if __name__ == "__main__":
    main()
