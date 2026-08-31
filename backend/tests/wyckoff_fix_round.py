"""Round 45 (2026-08-31): Wyckoff spring/utad dead-code fix — PRE-REGISTERED
validation round under AGENTS.md §7 protocol.

PRE-REGISTRATION (frozen BEFORE any candidate result was computed)
==================================================================

背景
----
Round-30 audit finding F1: `services/analysis/wyckoff.py:11-12` computes the
range extremes (rng_hi/rng_lo) over a window that INCLUDES the last-15 bars
that `:32-36` then scans for spring/utad — `lows[k] < rng_lo` / `highs[k] >
rng_hi` are constant-false by construction, so the spring/utad scoring branch
in decision.py (±w["wyckoff"] = 6 trending / 8 ranging per event) has NEVER
fired. All historical calibration (rounds 11/13) is self-consistent with the
branch dead. R30 ruled: cannot be silently fixed (fixing = changing score
composition) — requires a §7 validation round. User authorized that round on
2026-08-31 ("Wyckoff 死代码修复轮").

候选改动（唯一一处，逐字冻结）
------------------------------
In wyckoff.py, ONLY the spring/utad scan switches to a PRIOR range that
excludes the scanned bars (minimal activation of the dead branch):

    scan = min(15, n)
    prior = df.iloc[n - window:n - scan]
    if len(prior) >= 10:
        prior_hi = float(prior["high"].max()); prior_lo = float(prior["low"].min())
        for k in range(max(0, n - scan), n):
            if lows[k] < prior_lo and closes[k] > prior_lo:  -> spring
            if highs[k] > prior_hi and closes[k] < prior_hi: -> utad

Phase classification (width/ranging/pos), the SOS branch, decision.py, all
weights, PLAN_GEOMETRY, PLAN_THRESHOLD: UNTOUCHED. Any observed delta is
attributable to spring/utad events alone.

宇宙与口径
----------
- BTC/ETH/BNB/SOL × 1h/4h/1d（第 13 轮校准宇宙）。1w 预登记排除（样本薄 +
  R30 登记的 warmup-170 口径差）；SUI 排除（3.32 年短历史，校准时代之后）。
- 执行：CONF5 生产几何、容量约束串行、1h th=25 原生 / 4h,1d th=10 放宽
  （with_loose_plans）；1h = sim_journal_order（下界口径，用户裁定）、
  4h/1d = sim_outcome_fast（跟踪族两顺序等价）。
- 主口径毛 R；净 @双边0.10%（feeR=双边×entry/risk）仅作信息段。

AMENDMENT（2026-08-31 候选运行前登记——执行层对齐，闸与候选定义不变）
----------------------------------------------------------------------
本轮诊断发现：①本机 kline 缓存被运行中的后端/推送进程实时追加（1h 尾部在
两次诊断间从 08-28 移到 08-31）；②既有 `_5y_cache_*` 记录网格（08-28 构
建）与今日窗口求值结果和 §6.1 锚点存在分币混合方向差（BTC +176 笔 vs
ETH −60 等）——记录构建时的 df 与今日 df 内容存在历史漂移，无法 retroactively
裁定哪次构建更"干净"。为保证两臂严格同窗同码可比：
- 两臂统一钉窗：`_read_rows(end_time=PIN_MS=2026-08-31T03:00:00Z)`（北京
  11:00 已收盘 bar，12:05 推送轮已完整落库；钉窗后读数不可变，免疫活进程追加）。
- 两臂记录**全部现算**（不读任何既有缓存），同一 compute_records_w 代码
  路径（唯一差异 = wyckoff.py 修复与否），网格逐 bar 对齐；
  缓存写本轮独立命名空间 `_wyckfix_cache_{inc,cand}_*`，不覆盖正典 `_5y_cache_*`。
- 现役臂落盘 `tests/_wyckfix_trades_incumbent.pkl`、候选臂
  `tests/_wyckfix_trades_candidate.pkl`（含 wyckoff 事件计数）。
- 该 Amendment 不改变候选定义、宇宙、执行口径与六道闸；仅保证比较公平。

验收闸（全部通过才采纳；一次性，只见一次）
------------------------------------------
- K0 激活：候选 spring+utad 事件计数 > 0（现役臂同口径计数 = 0 为对照）。
- K1 利润：全窗池化总 R（4 币 × 3 周期）候选 ≥ 现役 × 1.02（+2% 为
  "有意义"下限——低于窗口漂移噪声 ±1~2% 的激活不值得引入新活信号）。
- K2 全币守卫：每币池化（3 周期合计）总 R 候选 > 0。
- K3 最差币守卫：候选最差币池化总 R ≥ 现役最差币 × 0.95。
- K4 周期守卫：1h/4h/1d 各自池化总 R 候选 ≥ 现役同周期 × 0.99
  （不允许"劫一个周期济另一个"）。
- K5 近段守卫：C 段（entry_t ≥ 2025-02-01，第 13 轮盲段）池化总 R
  候选 ≥ 现役 C 段 × 0.95（最近 regime 不允许实质变差）。
- K6 逐年守卫：每币 × 每自然年池化（3 周期）总 R 候选 > 0（保住
  "每币每年全正"的生产文档属性）。

裁决
----
六道闸全过 → 保留修复，更新 AGENTS.md / DEVLOG.md / BACKTEST.md。
任一闸挂 → `git checkout -- services/analysis/wyckoff.py` 还原死代码现状，
轮次记录存档，不再试第二套阈值（本评估只看一次；改阈值=调参，须另开轮）。

设计说明（为什么全窗而非留盲段）：候选零自由参数、单一二元裁决，无选择偏
差通道；spring/utad 为稀有事件，C 段 18 个月单独裁决严重欠 power。K5 以
"近段不得实质变差"的形式纳入最近 regime 检验。多假设风险 = 1 次检验。

Usage (from backend/):
  .venv/Scripts/python.exe tests/wyckoff_fix_round.py --arm incumbent   # 改码前
  <edit services/analysis/wyckoff.py — the frozen diff above>
  .venv/Scripts/python.exe tests/wyckoff_fix_round.py --arm candidate
  .venv/Scripts/python.exe tests/wyckoff_fix_round.py --arm compare
"""
import argparse
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
from backtest_5y import W5, CONF5, sim_outcome_fast
from backtest_7coins import capacity_trades
from audit_order_and_entry import sim_journal_order
from backtest_ltc import trade_stats
from profit2_r5 import with_loose_plans
from services.analysis import decision, engine

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
TFS3 = ("1h", "4h", "1d")
PIN_MS = int(datetime(2026, 8, 31, 3, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
C_BOUNDARY_MS = int(datetime(2025, 2, 1, tzinfo=timezone.utc).timestamp() * 1000)
FEE_NET = 0.0010
HERE = os.path.dirname(os.path.abspath(__file__))
INC_PKL = os.path.join(HERE, "_wyckfix_trades_incumbent.pkl")
CAND_PKL = os.path.join(HERE, "_wyckfix_trades_candidate.pkl")


def year_of(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year


def load_df(sym, itv):
    rows = ps.kline_cache._read_rows(sym, itv, PIN_MS, W5[itv])
    return ps.kline_cache.rows_to_df(rows)


# ---------------- record computation (both arms, identical code path) ----------------

def decide_at_w(df, htf, t, warmup, min_bars):
    """Verbatim ps.decide_at (same full_analysis) + wyck event types exposed."""
    sub = df[df["time"] <= t].tail(warmup).reset_index(drop=True)
    if len(sub) < min_bars:
        return None
    full = engine.full_analysis(sub)
    closes = sub["close"]
    price_now = float(closes.iloc[-1])

    biases = [m["bias"] for m in htf if m["bias"] != "neutral"]
    if not htf:
        alignment = "none"
    elif biases and all(b == biases[0] for b in biases):
        alignment = "aligned"
    elif "bullish" in biases and "bearish" in biases:
        alignment = "conflict"
    else:
        alignment = "mixed"

    summary = decision.build_summary(
        last_close=price_now,
        smc=full["smc"],
        indicators=full["indicators"],
        volume_profile=full["volumeProfile"],
        wyckoff=full["wyckoff"],
        volatility=full["volatility"],
        cvd_div=full["cvdDivergence"],
        mtf=htf,
        atr=ps.last_valid(full["indicators"]["atr14"]),
    )
    plan = summary.get("tradePlan")
    atr_v = ps.last_valid(full["indicators"]["atr14"])
    zones_bull = [z for z in full["smc"]["orderBlocks"] + full["smc"]["fvgs"]
                  if z["type"] == "bullish" and not z["mitigated"] and z["top"] <= price_now]
    near_bull = [z for z in zones_bull if price_now - z["top"] <= 1.5 * (atr_v or 0)]
    zones_bear = [z for z in full["smc"]["orderBlocks"] + full["smc"]["fvgs"]
                  if z["type"] == "bearish" and not z["mitigated"] and z["bottom"] >= price_now]
    near_bear = [z for z in zones_bear if z["bottom"] - price_now <= 1.5 * (atr_v or 0)]
    return {
        "time": t, "score": summary["score"], "bias": summary["bias"],
        "regime": summary["regime"], "alignment": alignment,
        "cvd_conf": (summary.get("cvdConfluence") or {}).get("direction"),
        "plan": (plan or {}).get("direction"), "atr": atr_v, "price": price_now,
        "vol_state": (full["volatility"] or {}).get("state"),
        "zone_bull_top": max(near_bull, key=lambda z: z.get("quality") or 0)["top"] if near_bull else None,
        "zone_bear_bottom": max(near_bear, key=lambda z: z.get("quality") or 0)["bottom"] if near_bear else None,
        "wyck_ev": [e["type"] for e in (full["wyckoff"] or {}).get("events", [])],
    }


def compute_records_w(sym, tf, dfs):
    """backtest_5y.compute_records grid, using decide_at_w."""
    cfg = CONF5[tf]
    df = dfs[tf]
    n = len(df)
    times = df["time"].to_numpy()
    records = []
    t0 = time.time()
    cnt = 0
    for i in range(cfg["warmup"], n - cfg["fwd_room"], cfg["spacing"]):
        t = int(times[i])
        htf = []
        for itv, span in cfg["mtf"]:
            m = ps.tf_summary_closed(dfs[itv], t, span)
            if m:
                htf.append(m)
        rec = decide_at_w(df, htf, t, cfg["warmup"], cfg["min_bars"])
        if rec is None:
            continue
        rec["symbol"] = sym
        records.append(rec)
        cnt += 1
        if cnt % 4000 == 0:
            print(f"[calc] {sym} {tf}: {cnt} ({time.time()-t0:.0f}s)", flush=True)
    records.sort(key=lambda r: r["time"])
    return records


def load_round_records(arm, sym, tf, dfs):
    cache_file = os.path.join(ps.CACHE_DIR, f"_wyckfix_cache_{arm}_{sym}_{tf}.pkl")
    key = {"ver": 1, "tf": tf, "symbol": sym, "window": W5[tf],
           "pin": PIN_MS, "src": ps.source_hash()}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                entry = pickle.load(f)
            if entry.get("key") == key:
                return entry["records"]
        except Exception:
            pass
    records = compute_records_w(sym, tf, dfs)
    tmp = cache_file + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump({"key": key, "records": records}, f)
    os.replace(tmp, cache_file)
    print(f"[rec:{arm}] {sym} {tf}: {len(records)} records computed", flush=True)
    return records


# ---------------- worker (both arms) ----------------

def worker(args):
    arm, sym = args
    try:
        dfs = {itv: load_df(sym, itv) for itv in TFS3}
        spans = {itv: (int(dfs[itv]["time"].iloc[0]), int(dfs[itv]["time"].iloc[-1]),
                       len(dfs[itv])) for itv in TFS3}
        out = {}
        ev_count = defaultdict(int)
        nrec = {}
        for tf in TFS3:
            cfg = CONF5[tf]
            records = load_round_records(arm, sym, tf, dfs)
            for r in records:
                for e in r.get("wyck_ev") or []:
                    ev_count[e] += 1
            nrec[tf] = len(records)
            recs = records if cfg["th"] == 25 else with_loose_plans(records, cfg["th"])
            sim = sim_journal_order if tf == "1h" else sim_outcome_fast
            out[tf] = capacity_trades(recs, cfg, dfs[tf], sim)
        return {"sym": sym, "data": out, "events": dict(ev_count),
                "nrec": nrec, "spans": spans}
    except Exception:
        import traceback
        return {"sym": sym, "error": traceback.format_exc()}


def run_arm(arm):
    t0 = time.time()
    ctx = mp.get_context("spawn")
    with ctx.Pool(len(SYMBOLS)) as pool:
        results = pool.map(worker, [(arm, s) for s in SYMBOLS])
    data, events, nrec, spans = {}, defaultdict(int), {}, {}
    for res in results:
        if "error" in res:
            print(f"[worker-error]\n{res['error']}", flush=True)
        else:
            data[res["sym"]] = res["data"]
            for k, v in (res.get("events") or {}).items():
                events[k] += v
            nrec[res["sym"]] = res["nrec"]
            spans[res["sym"]] = res["spans"]
    if len(data) != len(SYMBOLS):
        raise SystemExit("worker 失败，中止")
    for sym in SYMBOLS:
        s1 = spans[sym]["1h"]
        print(f"[窗口:{arm}] {sym} 1h {ps.fmt_ts(s1[0])}..{ps.fmt_ts(s1[1])} ({s1[2]} bars)", flush=True)
    pkl = INC_PKL if arm == "inc" else CAND_PKL
    with open(pkl, "wb") as f:
        pickle.dump({"src": ps.source_hash(), "pin": PIN_MS,
                     "data": data, "events": dict(events)}, f)
    print(f"[{arm}] trades dumped -> {pkl} ({time.time()-t0:.0f}s)")
    print(f"[{arm}] 记录数: {nrec}")
    print(f"[{arm}] wyckoff 事件计数（全决策点）: {dict(events)}")
    for tf in TFS3:
        st = trade_stats([(t["entry_t"], t["rr"]) for s in SYMBOLS for t in data[s][tf]])
        print(f"  {tf:<3} 池化: 成交={st['filled']} EV={st['ev']:+.3f}R 总={st['totalR']:+.1f}R")


# ---------------- compare (one-shot gates) ----------------

def pooled(data, tf=None, sym=None, since=None):
    out = []
    for s in SYMBOLS:
        if sym and s != sym:
            continue
        for t in TFS3:
            if tf and t != tf:
                continue
            for tr in data[s][t]:
                if since and tr["entry_t"] < since:
                    continue
                out.append(tr)
    return out


def tot(trades):
    return float(np.sum([t["rr"] for t in trades])) if trades else 0.0


def arm_compare():
    with open(INC_PKL, "rb") as f:
        inc_pkg = pickle.load(f)
    with open(CAND_PKL, "rb") as f:
        cand_pkg = pickle.load(f)
    inc, inc_ev = inc_pkg["data"], inc_pkg.get("events", {})
    cand, events = cand_pkg["data"], cand_pkg.get("events", {})

    print(f"{'='*100}")
    print("===== 第 45 轮 Wyckoff 死代码修复：候选 vs 现役（一次性验收）=====")
    print(f"{'='*100}")
    print(f"钉窗 PIN={ps.fmt_ts(PIN_MS)}；现役事件 {inc_ev}；候选事件 {events}")

    # ---- informational: per-tf table ----
    print("\n-- 分周期池化（毛口径）--")
    print(f"  {'周期':<4} {'现役总R':>10} {'候选总R':>10} {'ΔR':>8} {'Δ%':>7}  "
          f"{'现役n/EV':>18} {'候选n/EV':>18}")
    inc_tf, cand_tf = {}, {}
    for tf in TFS3:
        ti, tc = pooled(inc, tf=tf), pooled(cand, tf=tf)
        sti = trade_stats([(t["entry_t"], t["rr"]) for t in ti])
        stc = trade_stats([(t["entry_t"], t["rr"]) for t in tc])
        inc_tf[tf], cand_tf[tf] = sti["totalR"], stc["totalR"]
        dp = (stc["totalR"] / sti["totalR"] - 1) * 100 if sti["totalR"] else float("nan")
        print(f"  {tf:<4} {sti['totalR']:>+9.1f}R {stc['totalR']:>+9.1f}R "
              f"{stc['totalR']-sti['totalR']:>+7.1f} {dp:>+6.2f}%  "
              f"{sti['filled']:>6}/{sti['ev']:+.3f}R   {stc['filled']:>6}/{stc['ev']:+.3f}R")
    inc_all = sum(inc_tf.values())
    cand_all = sum(cand_tf.values())
    print(f"  {'合计':<4} {inc_all:>+9.1f}R {cand_all:>+9.1f}R "
          f"{cand_all-inc_all:>+7.1f} {(cand_all/inc_all-1)*100:>+6.2f}%")

    # ---- informational: per-coin ----
    print("\n-- 分币池化（3 周期合计，毛 R）--")
    inc_coin, cand_coin = {}, {}
    for s in SYMBOLS:
        inc_coin[s] = tot(pooled(inc, sym=s))
        cand_coin[s] = tot(pooled(cand, sym=s))
        print(f"  {s:<9} 现役 {inc_coin[s]:>+9.1f}R  候选 {cand_coin[s]:>+9.1f}R  "
              f"Δ {cand_coin[s]-inc_coin[s]:>+7.1f}")

    # ---- informational: C segment + fee-net + bootstrap ----
    inc_c, cand_c = tot(pooled(inc, since=C_BOUNDARY_MS)), tot(pooled(cand, since=C_BOUNDARY_MS))
    print(f"\n-- C 段（≥2025-02-01，第 13 轮盲段）--  现役 {inc_c:>+9.1f}R  候选 {cand_c:>+9.1f}R")
    inc_net = sum(t["rr"] - FEE_NET * t["entry_px"] / t["risk_px"] for t in pooled(inc))
    cand_net = sum(t["rr"] - FEE_NET * t["entry_px"] / t["risk_px"] for t in pooled(cand))
    print(f"-- 净 @双边0.10%（信息段）--  现役 {inc_net:>+9.1f}R  候选 {cand_net:>+9.1f}R")

    rng = np.random.default_rng(45)
    ri = np.array([t["rr"] for t in pooled(inc)])
    rc = np.array([t["rr"] for t in pooled(cand)])
    boots = []
    for _ in range(10000):
        bi = rng.choice(ri, size=len(ri), replace=True).mean()
        bc = rng.choice(rc, size=len(rc), replace=True).mean()
        boots.append(bc - bi)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    print(f"-- EV 差 bootstrap 95%CI（信息段）--  候选−现役 = {rc.mean()-ri.mean():+.4f}R  "
          f"CI [{lo:+.4f}, {hi:+.4f}]")

    # ---- informational: record-level join (score/plan flips) ----
    print("\n-- 记录级对照（同钉窗网格 join）--")
    n_join = n_score_chg = n_plan_chg = 0
    for s in SYMBOLS:
        for tf in TFS3:
            cache_i = os.path.join(ps.CACHE_DIR, f"_wyckfix_cache_inc_{s}_{tf}.pkl")
            cache_c = os.path.join(ps.CACHE_DIR, f"_wyckfix_cache_cand_{s}_{tf}.pkl")
            try:
                with open(cache_i, "rb") as f:
                    rec_i = {r["time"]: r for r in pickle.load(f)["records"]}
                with open(cache_c, "rb") as f:
                    rec_c = {r["time"]: r for r in pickle.load(f)["records"]}
            except Exception as exc:
                print(f"  [join-skip] {s} {tf}: {exc}")
                continue
            for t, ri_ in rec_i.items():
                rc_ = rec_c.get(t)
                if rc_ is None:
                    continue
                n_join += 1
                if ri_["score"] != rc_["score"]:
                    n_score_chg += 1
                if ri_.get("plan") != rc_.get("plan"):
                    n_plan_chg += 1
    print(f"  join {n_join} 决策点：评分改变 {n_score_chg}（{n_score_chg/max(1,n_join)*100:.2f}%）、"
          f"计划方向改变 {n_plan_chg}（{n_plan_chg/max(1,n_join)*100:.2f}%）")

    # ---- GATES (one-shot) ----
    print(f"\n{'='*100}")
    print("===== 验收闸（预登记，一次性）=====")
    k0 = events.get("spring", 0) + events.get("utad", 0) > 0
    print(f"  K0 激活: 候选 spring+utad = {events.get('spring', 0)+events.get('utad', 0)} > 0 "
          f"（现役对照 {inc_ev.get('spring', 0)+inc_ev.get('utad', 0)}）-> {'过' if k0 else '挂'}")
    k1 = cand_all >= inc_all * 1.02
    print(f"  K1 利润: 候选 {cand_all:+.1f}R vs 现役×1.02 {inc_all*1.02:+.1f}R "
          f"（{(cand_all/inc_all-1)*100:+.2f}%）-> {'过' if k1 else '挂'}")
    k2 = all(cand_coin[s] > 0 for s in SYMBOLS)
    print(f"  K2 全币>0: {dict((s, round(cand_coin[s], 1)) for s in SYMBOLS)} -> {'过' if k2 else '挂'}")
    worst_i, worst_c = min(inc_coin.values()), min(cand_coin.values())
    k3 = worst_c >= worst_i * 0.95
    print(f"  K3 最差币守卫: 候选 {worst_c:+.1f}R vs 现役最差×0.95 {worst_i*0.95:+.1f}R -> {'过' if k3 else '挂'}")
    k4 = all(cand_tf[tf] >= inc_tf[tf] * 0.99 for tf in TFS3)
    print(f"  K4 周期守卫: " + "  ".join(
        f"{tf} {cand_tf[tf]:+.1f} vs ≥{inc_tf[tf]*0.99:+.1f}" for tf in TFS3) + f" -> {'过' if k4 else '挂'}")
    k5 = cand_c >= inc_c * 0.95
    print(f"  K5 近段守卫: 候选C {cand_c:+.1f}R vs 现役C×0.95 {inc_c*0.95:+.1f}R -> {'过' if k5 else '挂'}")
    bad_cells = []
    for s in SYMBOLS:
        by_year_i, by_year_c = defaultdict(float), defaultdict(float)
        for t in pooled(inc, sym=s):
            by_year_i[year_of(t["entry_t"])] += t["rr"]
        for t in pooled(cand, sym=s):
            by_year_c[year_of(t["entry_t"])] += t["rr"]
        for y in sorted(set(by_year_i) | set(by_year_c)):
            if by_year_c.get(y, 0.0) <= 0:
                bad_cells.append(f"{s} {y} {by_year_c.get(y, 0.0):+.1f}R(现役{by_year_i.get(y, 0.0):+.1f})")
    k6 = not bad_cells
    print(f"  K6 逐年守卫: {'全部 币×年 >0' if k6 else '破坏: ' + '; '.join(bad_cells)} -> {'过' if k6 else '挂'}")

    verdict = all([k0, k1, k2, k3, k4, k5, k6])
    print(f"\n  裁决: {'采纳——保留修复，更新 AGENTS/DEVLOG/BACKTEST' if verdict else '拒绝——git 还原 wyckoff.py，死代码现状维持，轮次存档'}")
    print(f"{'='*100}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True, choices=["incumbent", "candidate", "compare"])
    args = ap.parse_args()
    if args.arm == "incumbent":
        run_arm("inc")
    elif args.arm == "candidate":
        run_arm("cand")
    else:
        arm_compare()


if __name__ == "__main__":
    main()
