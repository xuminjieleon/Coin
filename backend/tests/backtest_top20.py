"""候选标的回测（第五十二轮，2026-09-02）——成交量前20 ∪ 市值前20 未入列表者。

用户指令：根据当前策略，回测目前成交量和市值前20的币种，按年/月化收益率排序，
考虑可以加入列表的币。

榜单与甄别（全部机器实测，脚本 `_rank_top20.py` / `_probe_cands.py` 2026-09-02）：
- 成交量前20（币安 USDT 合约链 24h quoteVolume 07:43 UTC，剔稳定币/黄金/杠杆代币）：
  BTC ETH SOL XRP ZEC UNI BNB ENA DOGE TRX NEAR SUI HEMI ARB LINK SNDKB FIL TRUMP AAVE ACE
- 市值前20（CMC r.jina.ai 代理抓取 09-02，剔稳定币重排）：BTC ETH BNB XRP SOL TRX HYPE
  ZEC DOGE XMR LEO LINK ADA XLM BCH CC UNI LTC GRAM AVAX
- 剔已在推送列表 9 币（BTC ETH SOL BNB XRP ZEC DOGE SUI LTC）→ 并集候选 20 个；
- `_probe_cands.py` 探测可交易性与历史深度：
  * 不在币安（合约现货皆无）：HYPE（市值#9）/CC（#19）/LEO（#13）；XMR 已下架 → 排除；
  * 深度门否决（SNDKB 先例）：GRAM 62 天（TON 更名新对 2026-07-02 上市）、SNDKB 82.5 天
    （第五十轮已否决）→ 排除，GRAM 最早 2027-01 复测；
  * 入围回测 15 + PYTH（上会话 Group B 遗留对照）：
    TRX LINK ADA UNI NEAR AVAX XLM BCH FIL AAVE（≥5 年全历史）
    ARB(3.4y) ACE(2.7y) ENA(2.4y) PYTH(2.6y) TRUMP(1.6y) HEMI(0.94y)

口径（与第五十轮 SUI/LTC 流程完全一致）：
- R13 生产几何 CONF5（backtest_5y 单源）零调参、纯样本外（16 币无一参与过任何调参）；
- 5 年窗（上市不足者上市起全历史）、容量约束串行、1h 下界口径 sim_journal_order、
  4h/1d/1w 跟踪族 sim_outcome_fast、1h th=25 原生 / 其余 th=10 放宽；
- 评估窗口钉 NOW_MS=1788332400000（2026-09-02 07:00 UTC，第四十七轮标准；与
  backtest_newcands 相同 → `_cand_cache_*` 记录缓存直接复用 ADA 1h / PYTH 1h/4h/1d）；
- 记录缓存 key window=实际 df 根数（第五十一轮假窗口教训）；
- §7.8 两阶段：Semaphore(2) 并发抓取+根数核对（第三十六轮标准）→ 每 symbol 一 spawn worker
  （22 核，16 worker 全并行）；
- 主表未计费，费率敏感性单列（feeR=双边×entry/risk）；年化=毛/净 R÷窗口年数（固定注额
  1R=1% 本金），月均=÷窗口月数；对照池内区间（第五十轮九币分币年化 +160.6~+184.2 +
  第五十二轮 LINK/ADA +197.6/+187.1 R/年）。
- 加入判定门（第三十六轮 SUI 门）：①年化在九币区间内或之上；②逐年全正（无 2022 样本者
  如实标注、不构成否决，SUI 先例）；③多空对称；④EV 落在主流带内（1h +0.09~0.14 /
  4h +0.25~0.40）；⑤历史 ≥2.4 年（ENA/PYTH 起步档；TRUMP/HEMI 样本短只作描述性参考）。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/backtest_top20.py [--fetch-only]
"""
import argparse
import asyncio
import multiprocessing as mp
import sys
import time
from collections import defaultdict

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import backtest_newcands as bnc
import profit_sweep2 as ps
from backtest_5y import W5

NOW_MS = bnc.NOW_MS
TFS = bnc.TFS
YEAR_MS = bnc.YEAR_MS
STEP_MS = bnc.STEP_MS
FEE_NET = bnc.FEE_NET
fmt_ts = bnc.fmt_ts
year_of = bnc.year_of

GROUP_C = ["TRXUSDT", "LINKUSDT", "ADAUSDT", "UNIUSDT", "NEARUSDT", "AVAXUSDT",
           "XLMUSDT", "BCHUSDT", "FILUSDT", "AAVEUSDT", "ARBUSDT", "ACEUSDT",
           "ENAUSDT", "TRUMPUSDT", "HEMIUSDT", "PYTHUSDT"]

# 探测否决登记（_probe_cands.py 2026-09-02 机器输出）
EXCLUDED = [
    ("HYPE", "市值#9 $20.8B", "币安无合约/现货交易对（Hyperliquid 自家 DEX 为主）"),
    ("XMR", "市值#12 $9.8B", "币安已下架（历史 K 线在库但不可交易）"),
    ("LEO", "市值#13 $8.5B", "Bitfinex 交易所币，币安无交易对"),
    ("CC", "市值#19 $4.5B", "币安无合约/现货交易对"),
    ("GRAM", "市值#24 $3.7B", "上市仅 62 天（TON 更名新对 2026-07-02），深度门否决，最早 2027-01 复测"),
    ("SNDKB", "成交量#19", "上市 82.5 天，第五十轮已按样本太短否决（1h 109 笔 +14.0R 仅供参考）"),
]

NINE_COIN_ANNR = {  # 第五十轮 backtest_7coins 机器输出（毛 R/年，固定注额）
    "ETH": 184.2, "SOL": 183.3, "LTC": 183.0, "ZEC": 180.3, "BTC": 179.0,
    "SUI": 171.3, "BNB": 168.3, "XRP": 166.4, "DOGE": 160.6,
}


async def fetch_group_c():
    sem = asyncio.Semaphore(2)

    async def one(sym, itv):
        async with sem:
            for attempt in range(4):
                try:
                    rows = await ps.kline_cache.get_klines(sym, itv, W5[itv], end_time=NOW_MS)
                    if rows:
                        first = int(rows[0][0])
                        expected = min(W5[itv], (NOW_MS - first) // STEP_MS[itv] + 1)
                        if len(rows) + 2 < expected and attempt < 3:
                            print(f"[short] {sym} {itv}: {len(rows)}/{expected}，重试补页", flush=True)
                            await asyncio.sleep(3)
                            continue
                    print(f"[fetch-ok] {sym} {itv}: {len(rows)} bars "
                          f"{fmt_ts(int(rows[0][0]))}..{fmt_ts(int(rows[-1][0]))}", flush=True)
                    return
                except Exception as exc:
                    print(f"[warn] {sym} {itv} attempt{attempt}: {exc}", flush=True)
                    await asyncio.sleep(8 * (attempt + 1))
            raise SystemExit(f"{sym} {itv} unavailable")

    await asyncio.gather(*[one(s, t) for s in GROUP_C for t in TFS])


def coin_summary(sym, data):
    """单币合并（四周期全部成交按时间合并）窗口/年化/月均。"""
    merged = sorted([t for tf in TFS for t in data[tf]["trades"]], key=lambda x: x["entry_t"])
    if not merged:
        return None
    df1h = bnc.load_df(sym, "1h")
    t_start = int(df1h["time"].iloc[min(bnc.CONF5["1h"]["warmup"], len(df1h) - 1)])
    t_end = int(df1h["time"].iloc[-1])
    span_y = max(0.1, (t_end - t_start) / YEAR_MS)
    total = sum(t["rr"] for t in merged)
    net = sum(t["rr"] - FEE_NET * t["entry_px"] / t["risk_px"] for t in merged)
    by_year = defaultdict(list)
    for t in merged:
        by_year[year_of(t["entry_t"])].append(t["rr"])
    years = {y: sum(v) for y, v in by_year.items()}
    return {
        "sym": sym, "n": len(merged), "total": total, "net": net,
        "span_y": span_y, "ann": total / span_y, "ann_net": net / span_y,
        "mon": total / (span_y * 12.0), "years": years,
        "has_2022": 2022 in years,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch-only", action="store_true")
    args = ap.parse_args()
    t0 = time.time()

    print(f"== C 组：前20并集候选 {len(GROUP_C)} 个（标准优先级链）==", flush=True)
    asyncio.run(fetch_group_c())
    print(f"[fetch] {time.time()-t0:.0f}s", flush=True)
    if args.fetch_only:
        return

    ctx = mp.get_context("spawn")
    with ctx.Pool(len(GROUP_C)) as pool:
        results = pool.map(bnc.worker, GROUP_C)
    data = {}
    for res in results:
        if "error" in res:
            print(f"[worker-error] {res['sym']}\n{res['error']}", flush=True)
        else:
            data[res["sym"]] = res["data"]
    if len(data) != len(GROUP_C):
        missing = [s for s in GROUP_C if s not in data]
        raise SystemExit(f"worker failed: {missing}")
    print(f"[pool] done in {time.time()-t0:.0f}s\n", flush=True)

    print(f"{'='*108}")
    print("===== 探测否决登记（机器实测 _probe_cands.py 2026-09-02）=====")
    for name, rank, why in EXCLUDED:
        print(f"  {name:<8}{rank:<14}{why}")

    print(f"\n{'='*108}")
    print("===== C 组明细：R13 生产几何零调参纯样本外（毛口径，1h=下界）=====")
    print(f"{'='*108}")
    for sym in GROUP_C:
        df1 = bnc.load_df(sym, "1h")
        span = f"{fmt_ts(int(df1['time'].iloc[0]))}..{fmt_ts(int(df1['time'].iloc[-1]))}" if len(df1) else "-"
        print(f"\n{sym} (pure out-of-sample) window {span}")
        tot = 0.0
        for tf in TFS:
            tot += bnc.print_tf_cell(sym, data[sym], tf)
        print(f"  TOTAL (available tfs, gross): {tot:+.1f}R")

    print(f"\n-- direction split (long/short, gross R) --")
    import numpy as np
    for sym in GROUP_C:
        for tf in TFS:
            cell = data[sym][tf]
            if cell["skipped"] or not cell["trades"]:
                continue
            longs = [t["rr"] for t in cell["trades"] if t["dir"] == "long"]
            shorts = [t["rr"] for t in cell["trades"] if t["dir"] == "short"]
            le = np.mean(longs) if longs else float("nan")
            se = np.mean(shorts) if shorts else float("nan")
            print(f"  {sym:<10} {tf:<3} long: n={len(longs):>4} sum{np.sum(longs):>+8.1f}R EV{le:>+.3f} | "
                  f"short: n={len(shorts):>4} sum{np.sum(shorts):>+8.1f}R EV{se:>+.3f}")

    print(f"\n-- yearly (gross R) --")
    for sym in GROUP_C:
        parts_all = []
        for tf in TFS:
            cell = data[sym][tf]
            if cell["skipped"] or not cell["trades"]:
                continue
            by_year = defaultdict(list)
            for t in cell["trades"]:
                by_year[year_of(t["entry_t"])].append(t["rr"])
            parts_all.append("  ".join(f"{y}:{np.sum(v):+.1f}(n={len(v)})"
                                        for y, v in sorted(by_year.items())))
        if parts_all:
            print(f"  {sym:<10} " + "  ||  ".join(parts_all))

    print(f"\n-- fee sensitivity (feeR=roundtrip*entry/risk) --")
    print(f"  {'symbol':<10}{'tf':<5}{'gross':>10}{'rt0.05%':>11}{'rt0.10%':>11}")
    for sym in GROUP_C:
        for tf in ("1h", "4h", "1d"):
            cell = data[sym][tf]
            if cell["skipped"] or not cell["trades"]:
                continue
            trades = cell["trades"]
            cells = []
            for fee in (0.0, 0.0005, 0.0010):
                net = [(t["entry_t"], t["rr"] - fee * t["entry_px"] / t["risk_px"]) for t in trades]
                cells.append(bnc.trade_stats(net)["totalR"])
            print(f"  {sym:<10}{tf:<5}{cells[0]:>+9.1f}R {cells[1]:>+10.1f}R {cells[2]:>+10.1f}R")

    # ---- 主表：年化/月均收益率排序（用户指令） ----
    print(f"\n{'='*108}")
    print("===== 年/月化收益率排序（四周期合并，固定注额 1R=1% 本金，窗口=各币实际可得）=====")
    print("  对照：池内区间 = 九币分币年化（第五十轮）+160.6~+184.2 + LINK/ADA（第五十二轮）+197.6/+187.1 R/年")
    print(f"{'='*108}")
    print(f"{'symbol':<10}{'笔数':>7}{'窗口年':>7}{'累计R':>9}{'年化R':>9}{'月均R':>9}"
          f"{'年化%':>9}{'月均%':>8}{'净年化R@0.10%':>14}{'2022样本':>9}")
    summaries = [coin_summary(s, data[s]) for s in GROUP_C]
    summaries = [s for s in summaries if s]
    for s in sorted(summaries, key=lambda x: -x["ann"]):
        print(f"{s['sym']:<10}{s['n']:>7}{s['span_y']:>6.2f}y{s['total']:>+8.1f}{s['ann']:>+8.1f}"
              f"{s['mon']:>+8.2f}{s['ann']:>8.1f}%{s['mon']:>7.1f}%{s['ann_net']:>+12.1f}"
              f"{'有' if s['has_2022'] else '无':>7}")

    # ---- SUI 门判定（机器输出） ----
    print(f"\n{'='*108}")
    print("===== SUI 门判定（第三十六轮标准；无 2022 样本=如实标注不否决，SUI 先例）=====")
    print(f"{'='*108}")
    ev_band = {"1h": (0.09, 0.14), "4h": (0.25, 0.40)}
    band_lo = min(NINE_COIN_ANNR.values())
    verdicts = {}
    for s in sorted(summaries, key=lambda x: -x["ann"]):
        sym = s["sym"]
        yearly_ok = all(v > 0 for v in s["years"].values())
        # 方向对称与 EV：取 1h 与 4h 的多空 EV
        dir_ok, ev_ok, ev_note = True, True, []
        for tf in ("1h", "4h"):
            cell = data[sym][tf]
            if cell["skipped"] or not cell["trades"]:
                continue
            longs = [t["rr"] for t in cell["trades"] if t["dir"] == "long"]
            shorts = [t["rr"] for t in cell["trades"] if t["dir"] == "short"]
            if longs and shorts:
                le, se = float(np.mean(longs)), float(np.mean(shorts))
                if le <= 0 or se <= 0:
                    dir_ok = False
                if abs(le - se) > max(0.10, 0.5 * max(le, se)):
                    dir_ok = False
                lo, hi = ev_band[tf]
                both = [x for x in (le, se) if x != 0]
                if both and (min(both) < lo - 0.02 or max(both) > hi + 0.10):
                    ev_ok = False
                    ev_note.append(f"{tf} EV {le:+.3f}/{se:+.3f} 出带")
        g1 = s["ann"] >= band_lo
        g3 = dir_ok
        g4 = ev_ok
        g5 = s["span_y"] >= 2.4
        notes, fails = [], []
        if not s["has_2022"]:
            notes.append("无2022样本(如实标注,SUI先例)")
        if not g1:
            fails.append(f"①年化{s['ann']:+.1f} < 九币下限{band_lo:+.1f}")
        if not yearly_ok:
            fails.append(f"②存在亏损年:{ {y: round(v,1) for y,v in s['years'].items() if v<=0} }")
        if not g3:
            fails.append("③多空不对称")
        if not g4:
            fails.append("④EV出带 " + "; ".join(ev_note))
        if not g5:
            fails.append(f"⑤样本{s['span_y']:.2f}年<2.4年")
        if fails:
            verdict = "FAIL(" + "；".join(fails) + ("；" + "；".join(notes) if notes else "") + ")"
        elif notes:
            verdict = "PASS(" + "；".join(notes) + ")"
        else:
            verdict = "PASS"
        verdicts[sym] = verdict
        print(f"  {sym:<10} 年化{s['ann']:>+7.1f}R/年 {verdict}")

    # ---- 可入候选合计（PASS 且未入 11 币列表且未留档） ----
    CURRENT = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ZECUSDT",
               "DOGEUSDT", "SUIUSDT", "LTCUSDT", "LINKUSDT", "ADAUSDT"}
    SHELVED = {"TRXUSDT": "第五十二轮拍板留档（1h 费后保留仅 8%，价值在 4h）",
               "PYTHUSDT": "第五十二轮拍板留档（无 2022 样本）"}
    new_cands = [s for s in summaries
                 if s["sym"] not in CURRENT and s["sym"] not in SHELVED
                 and verdicts[s["sym"]].startswith("PASS")]
    new_cands.sort(key=lambda x: -x["ann"])
    print(f"\n{'='*108}")
    print("===== 可入候选合计（PASS 门、未入 11 币列表、未留档）=====")
    print(f"{'='*108}")
    tot_r = sum(s["total"] for s in new_cands)
    tot_n = sum(s["n"] for s in new_cands)
    spans = [s["span_y"] for s in new_cands]
    print("  " + " ".join(s["sym"] for s in new_cands))
    print(f"  合计毛 {tot_r:+.1f}R / {tot_n} 笔；年化合计 {sum(s['ann'] for s in new_cands):+.1f}R/年"
          f"（各币实际窗口 {min(spans):.2f}~{max(spans):.2f} 年）")
    per_tf = defaultdict(float)
    for s in new_cands:
        for tf in TFS:
            cell = data[s["sym"]][tf]
            per_tf[tf] += sum(t["rr"] for t in cell["trades"])
    print("  分周期毛：" + "  ".join(f"{tf} {per_tf[tf]:+.1f}R" for tf in ("1h", "4h", "1d", "1w")))
    for name, why in SHELVED.items():
        print(f"  留档（不重复推荐）: {name} —— {why}")

    print(f"\n[total] {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
