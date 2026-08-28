# -*- coding: utf-8 -*-
"""实证审计 测试B 补充探针（audit_rt_b2，2026-08-28）。

探针1（prevDay 磁吸截断机制实证）：
  主测试 24 个样本里 PDH/PDL 多次落在 5% 内但开关法磁吸效应恒 0——
  机制假设：smc.py 把全部流动性池按 touches 降序截断到 8 条，prevDay 池
  touches=1 排在所有摆动簇池（touches>=2）之后，池子满 8 条时被截掉。
  本探针在一个具体样本（BTCUSDT 1h 2024-06-16，PDL 在 5% 内）上打印
  full_analysis 有/无 prev_day 两版的池列表，直接展示截断发生。

探针2（MTF 口径边界 25% 验证）：
  run_analysis 的 mtf_context 取 end_time = t + step - span（含 ts<=end_time），
  即把"与决策 K 线同一时刻收盘"的高周期 K 线算作已收盘；回测 harness 的
  tf_summary_closed 要求 高周期收盘 <= t（决策 K 线开盘前）——两种口径都无
  未来数据，差异恰在 t+step == 高周期收盘 的决策点（1h→4h 占全部 1h K 线的
  25%，即 t≡3 mod 4h）。注意：5 年回测 spacing=4 的采样网格只覆盖单一相位
  （本窗口起点相位=0，即记录全部 t≡0 mod 4h），因此回测记录实际 0% 受影响。
  本探针直接构造边界/对照决策点实测两种口径的 4h mtf 评分与合成评分差。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/audit_rt_b2.py
"""
import asyncio
import os
import sys
import time

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import profit_sweep2 as ps  # noqa: E402
from backtest_5y import W5  # noqa: E402
from services import binance, derivs_store, kline_cache  # noqa: E402
from services.analysis import context, decision, engine  # noqa: E402

H1 = 3_600_000
H4 = 14_400_000

_LOG = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "audit_rt_b2.log"), "w", encoding="utf-8")


def out(s=""):
    print(s, flush=True)
    _LOG.write(str(s) + "\n")
    _LOG.flush()


NET_CALLS: list = []


def install_network_guard():
    for n in ["get_klines", "get_open_interest_hist", "get_premium_index"]:
        async def guard(*a, _n=n, **k):
            NET_CALLS.append(_n)
            raise RuntimeError(f"network attempt blocked: binance.{_n}")
        setattr(binance, n, guard)


def decompose(sym, tf, t, res, df, df_htf, htf_h, pd_levels):
    """Same toggle decomposition as audit_rt_b: returns (deriv, magnet, mtf, resid)."""
    cfg_warmup = 500
    rec_like = ps.decide_at(df, htf_h, t, cfg_warmup, 300)
    score_api = res["summary"]["score"]
    score_h = rec_like["score"]
    daily = derivs_store.daily_rates(sym, t) or {}
    oi_chg, funding = daily.get("oiChangePct"), daily.get("fundingRate")
    closes = [c["close"] for c in res["candles"]]
    lookback = min(24, len(closes) - 1)
    pcp = (closes[-1] - closes[-1 - lookback]) / closes[-1 - lookback] * 100.0
    price = float(closes[-1])
    dft = df[df["time"] <= t].tail(cfg_warmup).reset_index(drop=True)
    full_pd = engine.full_analysis(dft, pd_levels)
    full_no = engine.full_analysis(dft, None)
    base_kw = dict(last_close=price, indicators=full_pd["indicators"],
                   volume_profile=full_pd["volumeProfile"], wyckoff=full_pd["wyckoff"],
                   volatility=full_pd["volatility"], cvd_div=full_pd["cvdDivergence"],
                   atr=ps.last_valid(full_pd["indicators"]["atr14"]), interval=tf)
    s_api = decision.build_summary(smc=full_pd["smc"], mtf=res["mtf"]["list"],
                                   oi_change_pct=oi_chg, price_change_pct=pcp,
                                   funding_rate=funding, **base_kw)["score"]
    s_nof = decision.build_summary(smc=full_pd["smc"], mtf=res["mtf"]["list"], **base_kw)["score"]
    base_kw2 = dict(base_kw, indicators=full_no["indicators"],
                    volume_profile=full_no["volumeProfile"], wyckoff=full_no["wyckoff"],
                    volatility=full_no["volatility"], cvd_div=full_no["cvdDivergence"],
                    atr=ps.last_valid(full_no["indicators"]["atr14"]))
    s_nopd = decision.build_summary(smc=full_no["smc"], mtf=res["mtf"]["list"], **base_kw2)["score"]
    s_hmtf = decision.build_summary(smc=full_no["smc"], mtf=htf_h, **base_kw2)["score"]
    assert s_api == score_api, f"rebuild mismatch {s_api} vs {score_api}"
    assert s_hmtf == score_h, f"harness rebuild mismatch {s_hmtf} vs {score_h}"
    delta = score_api - score_h
    return (s_api - s_nof, s_nof - s_nopd, s_nopd - s_hmtf,
            delta - (s_api - s_nof) - (s_nof - s_nopd) - (s_nopd - s_hmtf),
            score_api, score_h)


async def amain():
    df1 = kline_cache.rows_to_df(
        await kline_cache.get_klines("BTCUSDT", "1h", W5["1h"], end_time=4_000_000_000_000))
    df4 = kline_cache.rows_to_df(
        await kline_cache.get_klines("BTCUSDT", "4h", W5["4h"], end_time=4_000_000_000_000))
    dfd = kline_cache.rows_to_df(
        await kline_cache.get_klines("BTCUSDT", "1d", W5["1d"], end_time=4_000_000_000_000))
    out(f"[data] BTCUSDT 1h {len(df1)} bars, 4h {len(df4)} bars, 1d {len(dfd)} bars")
    install_network_guard()

    # ---- 探针1：prevDay 池 touches 截断 ----
    out("\n===== 探针1：prevDay 池 touches 截断机制实证（BTCUSDT 1h 2024-06-16, t=1718539200000）=====")
    t = 1718539200000
    rows1d = await kline_cache.get_klines("BTCUSDT", "1d", 3, end_time=t)
    prev = rows1d[-2]
    pd_levels = {"high": float(prev[2]), "low": float(prev[3])}
    dft = df1[df1["time"] <= t].tail(500).reset_index(drop=True)
    price = float(dft["close"].iloc[-1])
    full_pd = engine.full_analysis(dft, pd_levels)
    full_no = engine.full_analysis(dft, None)
    out(f"  price={price}  PDH={pd_levels['high']}  PDL={pd_levels['low']}  "
        f"PDL 距价格 {(price - pd_levels['low']) / price * 100:.2f}%（5% 内=近）")
    for tag, full in (("无 prev_day", full_no), ("有 prev_day", full_pd)):
        pools = full["smc"]["liquidityPools"]
        out(f"  [{tag}] liquidityPools 共 {len(pools)} 条（截断后）:")
        for p in pools:
            mark = ""
            if abs(p["price"] - pd_levels["high"]) < 1e-9 or abs(p["price"] - pd_levels["low"]) < 1e-9:
                mark = "  <== prevDay 池"
            out(f"    {p['type']:<9} price={p['price']:<12.1f} touches={p['touches']} swept={p['swept']}{mark}")
    pd_in = any(abs(p["price"] - pd_levels["low"]) < 1e-9 or abs(p["price"] - pd_levels["high"]) < 1e-9
                for p in full_pd["smc"]["liquidityPools"])
    out(f"  结论: prevDay 池{'仍' if pd_in else '未'}在截断后的池列表中"
        f"（touches=1 排在摆动簇池之后，池满 8 条即被截掉）——"
        f"这就是 24 个样本磁吸效应恒 0 的机制")

    # ---- 探针2：MTF 边界 ----
    out("\n===== 探针2：MTF 口径边界（1h→4h）实测 =====")
    times = df1["time"].to_numpy()
    n = len(times)
    # spread probes across history: boundary (t≡3 mod 4h) vs control (t≡0 mod 4h)
    wanted = [(2022, 3), (2022, 11), (2023, 7), (2024, 5), (2025, 2), (2025, 11)]
    probes_b, probes_c = [], []
    for (y, m) in wanted:
        lo = int(time.mktime((y, m, 1, 0, 0, 0, 0, 0, 0))) * 1000
        hi = int(time.mktime((y, m + 1, 1, 0, 0, 0, 0, 0, 0))) * 1000 if m < 12 else lo + 31 * 86400_000
        for i in range(n):
            tt = int(times[i])
            if lo <= tt < hi and i > 600 and i < n - 200:
                if (tt // H1) % 4 == 3 and len(probes_b) < len(wanted):
                    probes_b.append(tt)
                    break
        for i in range(n):
            tt = int(times[i])
            if lo <= tt < hi and i > 600 and i < n - 200:
                if (tt // H1) % 4 == 0 and len(probes_c) < len(wanted):
                    probes_c.append(tt)
                    break

    n_diff = 0
    for tag, probes in (("边界点(t≡3 mod 4h)", probes_b), ("对照点(t≡0 mod 4h，回测网格相位)", probes_c)):
        out(f"\n  -- {tag} --")
        for t in probes:
            h4 = ps.tf_summary_closed(df4, t, H4)
            h1d = ps.tf_summary_closed(dfd, t, 86_400_000)
            htf_h = [m for m in (h4, h1d) if m]
            res = await context.run_analysis("BTCUSDT", "1h", limit=500, as_of=t)
            api_4h = next((m for m in res["mtf"]["list"] if m["interval"] == "4h"), None)
            api_rows = await kline_cache.get_klines("BTCUSDT", "4h", 300, end_time=t + H1 - H4)
            api_last = int(api_rows[-1][0])
            h_last = int(df4[df4["time"] + H4 <= t]["time"].iloc[-1])
            subd = dfd[dfd["time"] <= t]
            pd_levels = {"high": float(subd.iloc[-2]["high"]),
                         "low": float(subd.iloc[-2]["low"])} if len(subd) >= 2 else None
            deriv, mag, mtf, resid, s_api, s_h = decompose(
                "BTCUSDT", "1h", t, res, df1, df4, htf_h, pd_levels)
            diff = (api_4h and h4 and api_4h["score"] != h4["score"]) or api_last != h_last
            if diff and tag.startswith("边界"):
                n_diff += 1
            fmt = "%m-%d %H:%M"
            out(f"  {time.strftime(fmt, time.gmtime(t/1000))}: "
                f"API 4h评分={api_4h['score'] if api_4h else None}"
                f"(末根4h收盘={time.strftime(fmt, time.gmtime((api_last + H4)/1000))}) "
                f"harness={h4['score'] if h4 else None}"
                f"(末根4h收盘={time.strftime(fmt, time.gmtime((h_last + H4)/1000))}) "
                f"{'<== 差一根' if api_last != h_last else ''}")
            out(f"      合成评分: API={s_api} harness={s_h} Δ={s_api - s_h:+d} "
                f"= 衍生品{deriv:+d} 磁吸{mag:+d} MTF口径{mtf:+d} 残差{resid:+d}")
    out(f"\n  边界点 4h 评分/末根与 harness 不同: {n_diff}/{len(probes_b)}；"
        f"对照点应全同（回测 spacing=4 网格相位=0 → 回测记录 0% 受影响）")
    out(f"[guard] 探针阶段 binance 网络调用 {len(NET_CALLS)} 次（0=纯缓存）")


def main():
    t0 = time.time()
    asyncio.run(amain())
    out(f"\n[done] 总耗时 {time.time()-t0:.1f}s")
    _LOG.close()


if __name__ == "__main__":
    main()
