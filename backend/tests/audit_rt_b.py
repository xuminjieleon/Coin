# -*- coding: utf-8 -*-
"""实证审计 测试B：生产 API 回放（context.run_analysis as_of）vs 回测记录——
差异归因（2026-08-28）。

样本：与测试A同一批（同种子 20260828 抽 15 取前 8），BTCUSDT 1h / ETHUSDT 4h /
BNBUSDT 1d 各 8 条。

每样本验证：
  (a) run_analysis 返回 candles 最后一根时间戳 == t；
  (b) mtf.list 各周期评分基于的高周期 K 线收盘 <= t+step（无未来数据）；
      并用同一数据手动重算 mtf 评分核对；同时与回测 harness 的
      tf_summary_closed 口径对比（预期 t+step 恰好落在高周期边界时——
      1h→4h 为 25% 的决策点——API 口径多含一根"与决策 K 线同时收盘"的
      高周期 K 线，harness 口径要求收盘 <= t 更保守）；
  (c) score_api − score_record 是否被 funding/OI/prevDay-magnet/MTF 边界
      四个可识别组件定量解释。funding/OI 取自 derivs_store.daily_rates(sym, t)
      （本机 derivs.db 存在 Gate.io 回填，覆盖率一并报告）；
      分解方法 = 开关法（同一 build_summary，逐项切换输入）+ 直接规则估计交叉核对。

零外网验证：数据加载完成后给 services.binance 的全部网络入口装上"调用即抛错"
计数器，再跑全部 run_analysis 样本——as_of 回放模式应 100% 走本地缓存。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/audit_rt_b.py
"""
import asyncio
import os
import pickle
import random
import sys
import time

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import profit_sweep2 as ps  # noqa: E402
from backtest_5y import W5  # noqa: E402
from backtest_ltc import CONF  # noqa: E402
from services import binance, derivs_store, kline_cache  # noqa: E402
from services.analysis import context, decision, engine  # noqa: E402

SEED = 20260828
SETS = [("BTCUSDT", "1h"), ("ETHUSDT", "4h"), ("BNBUSDT", "1d")]
NEED = [("BTCUSDT", "1h"), ("BTCUSDT", "4h"), ("BTCUSDT", "1d"),
        ("ETHUSDT", "4h"), ("ETHUSDT", "1d"), ("BNBUSDT", "1d")]
STEP = context.STEP_MS

_LOG = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "audit_rt_b.log"), "w", encoding="utf-8")


def out(s=""):
    print(s, flush=True)
    _LOG.write(str(s) + "\n")
    _LOG.flush()


NET_CALLS: list = []


def install_network_guard():
    """Phase-2 guard: any Binance network entry raises (and is counted)."""
    names = ["get_klines", "get_open_interest_hist", "get_premium_index",
             "get_funding_rate_hist", "get_long_short_ratio", "get_taker_ratio"]

    def mk(name):
        async def guard(*a, **k):
            NET_CALLS.append(name)
            raise RuntimeError(f"network attempt blocked: binance.{name}")
        return guard

    for n in names:
        if hasattr(binance, n):
            setattr(binance, n, mk(n))


async def load_dfs():
    dfs = {}
    for sym, tf in NEED:
        rows = await kline_cache.get_klines(sym, tf, W5[tf], end_time=4_000_000_000_000)
        dfs[(sym, tf)] = kline_cache.rows_to_df(rows)
    return dfs


async def phase2(samples):
    """Run all run_analysis calls + manual MTF recomputes (must be cache-pure)."""
    results = {}
    for (sym, tf, t) in samples:
        cfg = CONF[tf]
        results[(sym, tf, t)] = {}
        try:
            res = await context.run_analysis(sym, tf, limit=cfg["warmup"], as_of=t)
        except Exception as exc:
            results[(sym, tf, t)] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        # manual MTF recompute with the exact context.one_tf data window
        mtf_manual = {}
        for itv, span in cfg["mtf"]:
            end_time = t + STEP[tf] - span
            rows = await kline_cache.get_klines(sym, itv, 300, end_time=end_time)
            dfm = kline_cache.rows_to_df(rows)
            info = {"last_ts": int(dfm["time"].iloc[-1]) if len(dfm) else None,
                    "close_le_decision_close": bool(len(dfm) and
                                                  int(dfm["time"].iloc[-1]) + span <= t + STEP[tf])}
            if len(dfm) >= 60:
                full = engine.full_analysis(dfm)
                closes = dfm["close"]
                lookback = min(24, len(closes) - 1)
                pcp = None
                if lookback > 0:
                    base = float(closes.iloc[-1 - lookback])
                    if base > 0:
                        pcp = (float(closes.iloc[-1]) - base) / base * 100.0
                s = decision.build_summary(
                    last_close=float(closes.iloc[-1]), smc=full["smc"],
                    indicators=full["indicators"], volume_profile=full["volumeProfile"],
                    wyckoff=full["wyckoff"], volatility=full["volatility"],
                    cvd_div=full["cvdDivergence"], price_change_pct=pcp,
                    atr=ps.last_valid(full["indicators"]["atr14"]), interval=itv)
                info["score"] = s["score"]
                info["bias"] = s["bias"]
            results.setdefault((sym, tf, t), {})["mtf_manual_" + itv] = info
        results[(sym, tf, t)]["res"] = res
    return results


def prev_day_from_df(d1, t):
    sub = d1[d1["time"] <= t]
    if len(sub) < 2:
        return None
    prev = sub.iloc[-2]
    return {"high": float(prev["high"]), "low": float(prev["low"])}


def magnet_estimate(smc_no_pd, pd_levels, price, w):
    """Direct-rule estimate of the prevDay-pool magnet delta. MUST replicate the
    production pool pipeline: PD pools are appended with touches=1, then the
    combined list is sorted by -touches and capped at 8 (smc.py L248-249) —
    so PD pools only survive when fewer than 8 swing-cluster pools exist."""
    def near(pool_price, side):
        if side == "buy":
            return pool_price >= price and (pool_price - price) / price <= decision.MAGNET_PCT
        return pool_price <= price and (price - pool_price) / price <= decision.MAGNET_PCT

    def contrib(pl):
        nb = any(p["type"] == "buy_side" and near(p["price"], "buy") for p in pl)
        ns = any(p["type"] == "sell_side" and near(p["price"], "sell") for p in pl)
        return (w["magnet"] if nb else 0) - (w["magnet"] if ns else 0)

    pools = list(smc_no_pd["liquidityPools"])
    pd_pools = []
    pdh_near = pdl_near = False
    if pd_levels:
        pdh, pdl = float(pd_levels["high"]), float(pd_levels["low"])
        pdh_near, pdl_near = near(pdh, "buy"), near(pdl, "sell")
        pd_pools = [{"price": pdh, "type": "buy_side", "touches": 1},
                    {"price": pdl, "type": "sell_side", "touches": 1}]
    merged = sorted(pools + pd_pools, key=lambda p: -p["touches"])[:8]
    pd_survive = any(p.get("touches") == 1 for p in merged) if pd_pools else False
    return contrib(merged) - contrib(pools), pdh_near, pdl_near, pd_survive


def main():
    t0 = time.time()
    out("===== 测试B：生产 API 回放 vs 回测记录（差异归因）=====")
    records_map = {}
    samples = []
    for sym, tf in SETS:
        with open(os.path.join(ps.CACHE_DIR, f"_5y_cache_{sym}_{tf}.pkl"), "rb") as f:
            entry = pickle.load(f)
        records = entry["records"]
        rng = random.Random(SEED)
        sel = rng.sample(range(len(records)), 15)[:8]
        for k in sorted(sel):
            t = int(records[k]["time"])
            samples.append((sym, tf, t))
            records_map[(sym, tf, t)] = records[k]
    out(f"样本: {len(samples)} 条（与测试A同种子子集，每 (sym,tf) 8 条）")

    async def amain():
        dfs = await load_dfs()
        out("[data] 5 窗口加载完成，开启零外网守卫（binance 网络入口调用即抛错）")
        install_network_guard()
        res = await phase2(samples)
        return dfs, res

    dfs, results = asyncio.run(amain())
    out(f"[guard] 回放阶段 binance 网络入口被调用次数: {len(NET_CALLS)}"
        f"（0 = as_of 回放零外网请求，与预期一致）")

    exact = 0
    n_run = 0
    n_deriv_active = 0
    n_magnet_active = 0
    n_mtf_eff = 0
    boundary_stats = {"boundary": 0, "boundary_diff": 0, "nonboundary_diff": 0}
    assert_fails = []
    differing = []

    for (sym, tf, t) in samples:
        rec = records_map[(sym, tf, t)]
        cfg = CONF[tf]
        pack = results[(sym, tf, t)]
        if "error" in pack:
            out(f"\n-- {sym} {tf} {ps.fmt_ts(t)} ({t})  run_analysis 失败: {pack['error']}")
            continue
        res = pack["res"]
        n_run += 1
        score_api = res["summary"]["score"]
        score_rec = rec["score"]
        regime = res["summary"]["regime"]
        w = decision.WEIGHTS[regime]
        price = float(res["candles"][-1]["close"])

        # (a) last candle == t
        ok_last = int(res["candles"][-1]["time"]) == t

        # (b) MTF checks
        mtf_lines = []
        for m in res["mtf"]["list"]:
            itv = m["interval"]
            span = STEP[itv]
            man = pack.get("mtf_manual_" + itv, {})
            htf_h = ps.tf_summary_closed(dfs[(sym, itv)], t, span)
            boundary = (t + STEP[tf]) % span == 0
            man_score = man.get("score")
            h_score = htf_h["score"] if htf_h else None
            diff_h = (man_score is not None and h_score is not None
                      and man_score != h_score)
            if boundary:
                boundary_stats["boundary"] += 1
                if diff_h:
                    boundary_stats["boundary_diff"] += 1
            elif diff_h:
                boundary_stats["nonboundary_diff"] += 1
            mtf_lines.append(
                f"    {itv}: API_mtf_score={m['score']} 手动重算={man_score} "
                f"harness_tf_summary_closed={h_score} "
                f"边界(收盘==决策K线收盘)={'是' if boundary else '否'} "
                f"最后高周期K线={ps.fmt_ts(man['last_ts'])}+{itv} "
                f"收盘<=t+step:{'是' if man.get('close_le_decision_close') else '否'}"
                f"{'  <== 与harness口径差一根' if diff_h else ''}")

        # (c) decomposition
        daily = derivs_store.daily_rates(sym, t) or {}
        oi_chg, funding = daily.get("oiChangePct"), daily.get("fundingRate")
        closes = [c["close"] for c in res["candles"]]
        lookback = min(24, len(closes) - 1)
        pcp = (closes[-1] - closes[-1 - lookback]) / closes[-1 - lookback] * 100.0

        # direct-rule estimates
        fund_c = 0.0
        if funding is not None:
            if funding > decision.FUNDING_THRESHOLD:
                fund_c = -w["funding"]
            elif funding < -decision.FUNDING_THRESHOLD:
                fund_c = w["funding"]
        oi_c = 0.0
        if oi_chg is not None:
            if pcp > 0 and oi_chg > 0:
                oi_c = w["oi"]
            elif pcp < 0 and oi_chg > 0:
                oi_c = -w["oi"]
            elif pcp > 0 and oi_chg < 0:
                oi_c = -w["oi"] * 0.5
            elif pcp < 0 and oi_chg < 0:
                oi_c = w["oi"] * 0.5

        df = dfs[(sym, tf)]
        dft = df[df["time"] <= t].tail(cfg["warmup"]).reset_index(drop=True)
        ok_df = (len(dft) == len(res["candles"])
                 and int(dft["time"].iloc[0]) == int(res["candles"][0]["time"])
                 and int(dft["time"].iloc[-1]) == t)
        pd_levels = prev_day_from_df(dfs[(sym, "1d")], t) if tf != "1d" else None
        full_pd = engine.full_analysis(dft, pd_levels)
        full_no = engine.full_analysis(dft, None)
        htf_h = []
        for itv, span in cfg["mtf"]:
            m = ps.tf_summary_closed(dfs[(sym, itv)], t, span)
            if m:
                htf_h.append(m)
        rec2 = ps.decide_at(df, htf_h, t, cfg["warmup"], cfg["min_bars"])
        atr_v = ps.last_valid(full_pd["indicators"]["atr14"])
        base_kw = dict(last_close=price, indicators=full_pd["indicators"],
                       volume_profile=full_pd["volumeProfile"], wyckoff=full_pd["wyckoff"],
                       volatility=full_pd["volatility"], cvd_div=full_pd["cvdDivergence"],
                       atr=atr_v, interval=tf)
        s_api = decision.build_summary(smc=full_pd["smc"], mtf=res["mtf"]["list"],
                                       oi_change_pct=oi_chg, price_change_pct=pcp,
                                       funding_rate=funding, **base_kw)["score"]
        s_nof = decision.build_summary(smc=full_pd["smc"], mtf=res["mtf"]["list"],
                                       **base_kw)["score"]
        base_kw2 = dict(base_kw, indicators=full_no["indicators"],
                        volume_profile=full_no["volumeProfile"],
                        wyckoff=full_no["wyckoff"], volatility=full_no["volatility"],
                        cvd_div=full_no["cvdDivergence"],
                        atr=ps.last_valid(full_no["indicators"]["atr14"]))
        s_nopd = decision.build_summary(smc=full_no["smc"], mtf=res["mtf"]["list"],
                                        **base_kw2)["score"]
        s_hmtf = decision.build_summary(smc=full_no["smc"], mtf=htf_h, **base_kw2)["score"]

        deriv_eff = s_api - s_nof
        mag_eff = s_nof - s_nopd
        mtf_eff = s_nopd - s_hmtf
        mag_direct, pdh_near, pdl_near, pd_survive = magnet_estimate(full_no["smc"], pd_levels, price, w)
        delta = score_api - score_rec
        resid = delta - (deriv_eff + mag_eff + mtf_eff)

        if s_api != score_api:
            assert_fails.append((sym, tf, t, f"重建评分 {s_api} != API {score_api}"))
        if s_hmtf != score_rec or (rec2 and rec2["score"] != score_rec):
            assert_fails.append((sym, tf, t,
                                 f"harness口径重建 {s_hmtf}/decide_at={rec2['score'] if rec2 else None} != 记录 {score_rec}"))

        if deriv_eff != 0:
            n_deriv_active += 1
        if mag_eff != 0:
            n_magnet_active += 1
        if mtf_eff != 0:
            n_mtf_eff += 1
        if delta == 0:
            exact += 1
        else:
            differing.append((sym, tf, t))

        tp = res["summary"].get("tradePlan")
        tp_dir = tp["direction"] if tp else None
        loose_dir = rec["plan"] or (
            ("long" if score_rec > 0 else "short") if abs(score_rec) >= decision.PLAN_THRESHOLD.get(tf, 25) else None)

        out(f"\n-- {sym} {tf} {ps.fmt_ts(t)} ({t})")
        out(f"   score: 记录={score_rec} API={score_api} Δ={delta:+d}   "
            f"regime={regime} bias={res['summary']['bias']}/{rec['bias']} "
            f"plan(API)={tp_dir} plan(记录/放宽)={rec['plan']}/{loose_dir}")
        out(f"   (a) candles末根==t: {'是' if ok_last else '否!!!'}   "
            f"重建数据窗==API数据窗: {'是' if ok_df else '否!!!'}")
        for ln in mtf_lines:
            out(ln)
        out(f"   (c) 归因分解（开关法）: funding/OI={deriv_eff:+d} "
            f"(funding={funding} oiChangePct={oi_chg} price_chg24={pcp:+.2f}% "
            f"直接估计 fund={fund_c:+.1f} oi={oi_c:+.1f})  "
            f"prevDay磁吸={mag_eff:+d} (PDH近={'是' if pdh_near else '否'} "
            f"PDL近={'是' if pdl_near else '否'} PD池过touches截断={'是' if pd_survive else '否'} "
            f"直接估计={mag_direct:+d})  "
            f"MTF口径={mtf_eff:+d}  残差={resid:+d}")
        verdict = "一致" if delta == 0 else (
            "完全归因" if resid == 0 else f"未解释残差 {resid:+d}!!!")
        out(f"   结论: Δ={delta:+d} = {deriv_eff:+d}(衍生品) {mag_eff:+d}(prevDay磁吸) "
            f"{mtf_eff:+d}(MTF口径) -> {verdict}")

    out(f"\n===== 测试B 汇总 =====")
    out(f"样本量: {len(samples)}；run_analysis 成功: {n_run}")
    out(f"零外网验证: 回放阶段 binance 网络调用 {len(NET_CALLS)} 次"
        f"（{'符合预期：as_of 回放纯本地' if not NET_CALLS else '异常!!! ' + str(NET_CALLS[:5])}）")
    out(f"score 完全一致: {exact}/{n_run}（{exact/max(n_run,1)*100:.1f}%）")
    out(f"有差异样本的归因: 衍生品组件(funding/OI) 非零 {n_deriv_active} 例；"
        f"prevDay 磁吸非零 {n_magnet_active} 例；MTF 口径效应非零 {n_mtf_eff} 例")
    out(f"MTF 边界统计: 落在高周期边界的样本 {boundary_stats['boundary']} 个，"
        f"其中 mtf 评分与 harness 口径不同 {boundary_stats['boundary_diff']} 个；"
        f"非边界样本评分不同 {boundary_stats['nonboundary_diff']} 个"
        f"（预期非边界=0，边界处差一根已收盘高周期K线）")
    if assert_fails:
        out(f"自校验失败 {len(assert_fails)} 条:")
        for a in assert_fails:
            out(f"  {a}")
    else:
        out("自校验: 全部样本满足 [重建评分==API评分] 且 [harness口径重建==回测记录]"
            " —— 归因分解在算术上闭合（残差恒 0）")
    if differing:
        out(f"差异样本清单: {[(s, tf_, ps.fmt_ts(t)) for s, tf_, t in differing]}")
    out(f"[done] 总耗时 {time.time()-t0:.1f}s")
    _LOG.close()


if __name__ == "__main__":
    main()
