# -*- coding: utf-8 -*-
"""实证审计 测试D：交易日记重放（journal_store.replay_plan）vs 回测重放
（sim_outcome_fast / clean-room replay_clean）（2026-08-28）。

样本：
  - 6 个合成用例（复用测试C的构造，外加前置 bar 模拟真实缓存条件）
    + 追加 S7（同根 目标+保本触发）以覆盖已知差异 (a)；
  - 20 笔真实交易：从测试C全量明细（BTCUSDT 1h + ETHUSDT 4h）中选取——
    约束 openedAt 落在最新 470 根 K 线内（replay_plan 走 live 模式取最新
    bars_needed 根，需覆盖交易全程），并兼顾离场原因多样性
    （stop/be_stop(含跟踪)/target、晚成交、满仓持）。

对照口径：
  trade dict: {symbol, interval, direction, entry, stop, opened_at=决策bar时间戳,
               plan=_default_plan(interval, entry, stop)（生产 PLAN_GEOMETRY 口径）}
  replay_plan(trade)（until_ts=None → 至今；合成用例 until_ts=末根 bar）

已知差异（预登记，逐项验证表现是否符合预期）：
  (a) 同根 目标 vs 保本触发顺序：sim 先判目标（未保本→全额 tgtR）；
      replay 先判保本（再判目标→0.5×beR+0.5×tgtR）。
      1h 例：同根双触发 replay=0.5×0.15+0.5×0.5=0.325 vs sim=0.5。
  (b) 时间退出计数起点：replay 从 opened-step 的前置 bar 起数（bars_held 含
      决策前一根），且无成交窗口概念 → 时间退出早于 sim（sim 自成交根起数
      texit 根）。真实环境偏移 = 成交滞后根数 + 1（前置 bar）。
  (c) replay 假设已成交、无 fill 窗口：sim 尚未成交的 bar 上 replay 已在做
      止损/保本/目标判定 → 提前触发时结果不同。
除上述外不应有意外不一致。replay 的 r 保留 3 位小数 → 数值容差 5e-4。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/audit_rt_d.py
"""
import asyncio
import os
import pickle
import sys
import time

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audit_rt_c as ac  # noqa: E402  复用 collect_run / replay_clean / synthetic_cases
import profit_sweep2 as ps  # noqa: E402
from backtest_5y import W5, sim_outcome_fast  # noqa: E402
from backtest_ltc import CONF  # noqa: E402
from profit2_r5 import with_loose_plans  # noqa: E402
from services import journal_store, kline_cache  # noqa: E402

SEED = 20260829
STEP = {"1h": 3_600_000, "4h": 14_400_000}
TOL = 5e-4 + 1e-9  # replay 的 r 保留 3 位小数

_LOG = None


def out(s=""):
    global _LOG
    if _LOG is None:
        _LOG = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "audit_rt_d.log"), "w", encoding="utf-8")
    print(s, flush=True)
    _LOG.write(str(s) + "\n")
    _LOG.flush()


def reason_map(sim_reason):
    return {"stop": "stop", "be_stop": "be_stop", "trail_stop": "be_stop",
            "target": "target", "time": "time"}[sim_reason]


async def amain():
    dfs, trades_by = {}, {}
    for sym, tf in (("BTCUSDT", "1h"), ("ETHUSDT", "4h")):
        rows = await kline_cache.get_klines(sym, tf, W5[tf], end_time=4_000_000_000_000)
        dfs[(sym, tf)] = kline_cache.rows_to_df(rows)
        with open(os.path.join(ps.CACHE_DIR, f"_5y_cache_{sym}_{tf}.pkl"), "rb") as f:
            records = pickle.load(f)["records"]
        cfg = CONF[tf]
        recs = records if cfg["th"] == 25 else with_loose_plans(records, cfg["th"])
        df = dfs[(sym, tf)]
        arrs = (df["high"].to_numpy(), df["low"].to_numpy(),
                df["close"].to_numpy(), len(df))
        tidx = {int(t): i for i, t in enumerate(df["time"].to_numpy())}
        fill_bars = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
        _, trades = ac.collect_run(recs, cfg["geo"], arrs, tidx, fill_bars)
        trades_by[(sym, tf)] = trades
    out("[data] BTCUSDT 1h / ETHUSDT 4h 加载 + 全量成交收集完成"
        f"（{len(trades_by[('BTCUSDT','1h')])} + {len(trades_by[('ETHUSDT','4h')])} 笔）")

    # ---- 真实交易抽样（近期 + 覆盖各离场类型/边界形态）----
    cands = []
    for sym, tf in (("BTCUSDT", "1h"), ("ETHUSDT", "4h")):
        df = dfs[(sym, tf)]
        dft = df["time"].to_numpy()
        top_ts = int(dft[-1])
        cfg = CONF[tf]
        depth, stopw, be_frac, tgt_r, texit, trail = cfg["geo"]
        for tr in trades_by[(sym, tf)]:
            if tr["time"] < top_ts - 470 * STEP[tf]:
                continue
            # sim 侧参考：用测试C全量验证过的 replay_clean（df 切片数据路径）
            rows = [tuple(r) for r in
                    df.iloc[max(0, tr["i"] - 2):tr["exit_bar"] + 2].itertuples(index=False, name=None)]
            # itertuples 列序 = time,open,high,low,close,volume,takerBuy
            i0 = 2 if tr["i"] >= 2 else tr["i"]
            ref = ac.replay_clean(rows, i0, tr["direction"], tr["entry"], tr["stop"],
                                  be_frac, tgt_r, texit,
                                  max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"]))), trail)
            if ref is None or abs(ref[0] - tr["rr"]) > 1e-9:
                continue
            tr["sim_reason"] = ref[3]
            cands.append((sym, tf, tr))
    out(f"[抽样] 近期（最新470根内）候选 {len(cands)} 笔")

    import random
    rng = random.Random(SEED)
    buckets = {}
    for c in cands:
        sym, tf, tr = c
        b = [tr["sim_reason"]]
        if tr["fill"] - tr["i"] >= 3:
            b.append("晚成交")
        if tr["sim_reason"] == "target" and abs(tr["rr"] - CONF[tf]["geo"][3]) < 1e-9:
            b.append("全额目标")  # 保本未先于目标触发 -> 已知差异(a)触发面
        if tr["exit_bar"] - tr["i"] + 1 >= CONF[tf]["geo"][4] - 2:
            b.append("临界持有")  # 持有接近 texit -> replay 时间退出抢先面(b)
        for k in b:
            buckets.setdefault(k, []).append(c)
    picked, seen = [], set()
    order = ["stop", "be_stop", "target", "trail_stop", "全额目标", "晚成交", "临界持有", "time"]
    for k in order:
        for c in rng.sample(buckets.get(k, []), min(4, len(buckets.get(k, [])))):
            key = (c[0], c[1], c[2]["time"])
            if key not in seen:
                seen.add(key)
                picked.append(c)
            if len(picked) >= 20:
                break
        if len(picked) >= 20:
            break
    out(f"[抽样] 分桶 { {k: len(v) for k, v in buckets.items()} } -> 选取 {len(picked)} 笔")

    # ---- 真实交易 replay_plan ----
    real = []
    for sym, tf, tr in picked:
        plan = journal_store._default_plan(tf, tr["entry"], tr["stop"])
        trade = {"symbol": sym, "interval": tf, "direction": tr["direction"],
                 "entry": tr["entry"], "stop": tr["stop"], "opened_at": tr["time"],
                 "plan": plan, "status": "open"}
        try:
            rp = await journal_store.replay_plan(trade)
        except Exception as exc:
            rp = {"r": None, "reason": f"异常 {type(exc).__name__}: {exc}"}
        real.append((sym, tf, tr, rp))
        await asyncio.sleep(0.05)

    # ---- 合成用例（6 + S7）----
    cases = ac.synthetic_cases()
    s7rows, s7arrs = ac._bars([(100, 100.5, 99.5, 100), (100, 135, 99, 120),
                               (120, 121, 119, 120)])
    cases.append(dict(name="S7 同根目标+保本触发→顺序差", geo=(0.5, 3.0, None, 10, 5),
                      rows=s7rows, arrs=s7arrs, direction="long", entry=100.0, stop=90.0,
                      expect=(3.0, 1, 1),
                      note="成交根 high=135 同时越目标(130)与保本线(105)：sim 先判目标→全 tgtR=3.0；"
                           "replay 先保本→0.5×0.5+0.5×3.0=1.75（已知差异a）"))
    synth = []
    orig_get = kline_cache.get_klines
    for c in cases:
        # 前置 bar（平坦于入场价）：模拟真实缓存中 opened-step 存在的条件
        pre = (c["rows"][0][0] - 3_600_000, 100.0, 100.0, 100.0, 100.0, 1.0, None)
        rows = [pre] + list(c["rows"])
        opened = c["rows"][0][0]
        be_frac, tgt_r, trail, texit, fill_bars = c["geo"]

        async def fake_get(symbol, interval, limit, end_time=None, _rows=rows):
            return _rows

        kline_cache.get_klines = fake_get
        try:
            trade = {"symbol": "SYNTHUSDT", "interval": "1h", "direction": c["direction"],
                     "entry": c["entry"], "stop": c["stop"], "opened_at": opened,
                     "plan": {"stop": c["stop"], "beR": be_frac, "targetR": tgt_r,
                              "trailR": trail, "texitBars": texit, "fillBars": fill_bars},
                     "status": "open"}
            rp = await journal_store.replay_plan(trade, until_ts=rows[-1][0])
        finally:
            kline_cache.get_klines = orig_get
        highs, lows, closes, n = c["arrs"]
        # sim/clean 在同样的“含前置 bar”序列上跑（决策 bar 索引=1）
        import numpy as np
        w_highs = np.concatenate(([100.0], highs))
        w_lows = np.concatenate(([100.0], lows))
        w_closes = np.concatenate(([100.0], closes))
        sim = sim_outcome_fast(w_highs, w_lows, w_closes, n + 1, 1, c["direction"],
                               c["entry"], c["stop"], be_frac, tgt_r, texit, fill_bars, trail)
        mine = ac.replay_clean(rows, 1, c["direction"], c["entry"], c["stop"],
                               be_frac, tgt_r, texit, fill_bars, trail)
        synth.append((c, sim, mine, rp))
    return dfs, real, synth


def classify(sym, tf, tr, rp, dfs):
    cfg = CONF[tf]
    be_frac, tgt_r = cfg["geo"][2], cfg["geo"][3]
    step = STEP[tf]
    sim_rr, sim_reason = tr["rr"], tr["sim_reason"]
    rp_r, rp_reason = rp.get("r"), rp.get("reason")
    if rp_r is None:
        return "意外", f"replay 返回 r=None reason={rp_reason}"
    diff = rp_r - sim_rr
    dft = dfs[(sym, tf)]["time"].to_numpy()
    sim_fill_ts = int(dft[tr["fill"]])
    sim_exit_ts = int(dft[tr["exit_bar"]])
    rp_exit_ts = tr["time"] - step + (rp.get("barsHeld", 1) - 1) * step
    detail = (f"sim rr={sim_rr:+.4f}({sim_reason} fill+{tr['fill'] - tr['i']} exit+{tr['exit_bar'] - tr['i']}) "
              f"vs replay r={rp_r:+.4f}({rp_reason} barsHeld={rp.get('barsHeld')}) Δ={diff:+.4f}")
    if abs(diff) <= TOL and rp_reason == reason_map(sim_reason):
        return "一致", detail
    if abs(diff) <= TOL:
        return "数值一致(路径略异)", detail
    # 已知差异 (a)：同根 目标 vs 保本顺序
    if sim_reason == "target" and rp_reason == "target" and tgt_r is not None:
        expect = -0.5 * (tgt_r - be_frac)
        if abs(diff - expect) <= TOL:
            return "已知差异(a)同根目标vs保本顺序", detail + f"（预期 Δ=-0.5×(tgt-be)={expect:+.3f} ✓）"
        return "意外", detail + f"（target/target 但 Δ 不等于 {expect:+.3f}）"
    # 已知差异 (b)：replay 时间退出计数起点提前 -> 抢先于 sim 的管理事件
    if rp_reason == "time" and sim_reason != "time":
        off = (sim_exit_ts - rp_exit_ts) // step
        return "已知差异(b)时间退出计数偏移", detail + f"（replay 提前 {off} 根退出）"
    if rp_reason == "time" and sim_reason == "time":
        off = (sim_exit_ts - rp_exit_ts) // step
        return ("一致" if abs(diff) <= TOL else "已知差异(b)时间退出计数偏移"), \
            detail + f"（退出 bar 偏移 {off} 根）"
    # 已知差异 (c)：无成交窗口 -> 成交前 bar 提前触发
    if rp_exit_ts < sim_fill_ts:
        return "已知差异(c)无fill窗口提前触发", detail + \
            f"（replay 退出 {time.strftime('%m-%d %H:%M', time.gmtime(rp_exit_ts / 1000))} " \
            f"< sim 成交 {time.strftime('%m-%d %H:%M', time.gmtime(sim_fill_ts / 1000))}）"
    return "意外", detail


def main():
    t0 = time.time()
    out("===== 测试D：日记重放（replay_plan）vs 回测重放（sim/clean-room）=====")
    dfs, real, synth = asyncio.run(amain())

    # ---- 合成用例 ----
    out(f"\n===== 合成用例（7 个；replay_plan vs sim_outcome_fast vs 语义预期）=====")
    for c, sim, mine, rp in synth:
        be_frac, tgt_r, trail, texit, fill_bars = c["geo"]
        sim_rr = sim[0] if sim else None
        my_rr = mine[0] if mine else None
        rp_r = rp.get("r")
        out(f"  {c['name']}:")
        out(f"      sim=({sim_rr if sim_rr is None else f'{sim_rr:+.4f}'},fill={sim[1]},exit={sim[2]})  "
            f"clean=({my_rr if my_rr is None else f'{my_rr:+.4f}'},fill={mine[1]},exit={mine[2]})  "
            f"replay_plan=(r={rp_r},reason={rp.get('reason')},barsHeld={rp.get('barsHeld')},beDone={rp.get('beDone')})")
        out(f"      语义: {c['note']}")

    # ---- 真实交易 ----
    out(f"\n===== 真实交易（{len(real)} 笔；replay_plan vs 回测口径）=====")
    from collections import Counter
    cls_cnt = Counter()
    for sym, tf, tr, rp in real:
        cls, detail = classify(sym, tf, tr, rp, dfs)
        cls_cnt[cls] += 1
        out(f"  {sym[:3]} {tf} {ac.fmt_ms(tr['time'])} {tr['direction']:<6} [{cls}]")
        out(f"      {detail}")
    out(f"\n===== 测试D 汇总 =====")
    for k, v in cls_cnt.most_common():
        out(f"  {k}: {v}")
    unexpected = [x for x in real if classify(x[0], x[1], x[2], x[3], dfs)[0] == "意外"]
    out(f"意外不一致: {len(unexpected)} 笔"
        + ("（需人工核查）" if unexpected else "——除预登记的三类已知差异外无意外不一致"))
    out(f"[done] 总耗时 {time.time() - t0:.1f}s")
    if _LOG is not None:
        _LOG.close()


if __name__ == "__main__":
    main()
