"""US crypto-related equities backtest (2026-08-30, round 38, PRE-REGISTERED).

User direction: 回测美股和币圈相关的股票，4h/1d。

标的池（写于跑数之前；美股加密相关高流动性代表；NDX 的可交易代理 QQQ 已
于第三十二轮覆盖（1d +90.9R），不重复；^NDX 指数本身无可交易成交量，不纳入）:
  MSTR  Strategy（BTC 金库，1998 起，2020-08 进入比特币时代）
  COIN  Coinbase（2021-04 上市，全程比特币时代）
  MARA  MARA Holdings（矿）
  RIOT  Riot Platforms（矿）
  CLSK  CleanSpark（矿）
  HOOD  Robinhood（部分加密业务）

协议（本 docstring 定稿后才开始跑数；零调参——美股调参机会已于第三十三
轮烧掉）:
  - 几何：1d 主口径 = 第三十三轮已采纳的美股专用几何 depth 0.75 / stop 1.2 /
    be 0.5 / trail 0.5 / texit 24（th 10、fill 9、warmup 300、min_bars 220、
    spacing 2、fwd_room 110 与第三十二轮一致）；另报 incumbent（生产 1d：
    depth 1.0 / stop 1.2 / be 0.50 / trail 0.35 / texit 12）作敏感性对照——
    两者都报告，不依据本轮结果挑选（挑选=对本池调参，禁止）。
    4h = 生产 4h 几何原封不动（depth 0.75 / stop 1.0 / be 0.75 / texit 48 /
    trail 0.35，fill 18，th 10，MTF=1d）。
  - era 切片 = 成交时间 >= 2020-08-01（MSTR 首次宣布购 BTC 之前）= "比特币
    时代"。本轮被检验的主张是加密相关标的，三关在 era 切片上判定；全史
    （MSTR 1998 起、矿商 2012/2016 起的加密前时代）只作背景报告、不设关。
  - 费率主口径双边 0.06%（第三十二/三十三轮口径）；敏感性 0.02% / 0.12%。
  - 执行层与第三十二轮完全相同：双向 T+0、gap 保守成交（开盘穿越按开盘价）、
    同根止损先判（journal 重放口径）、跟踪族两顺序等价（第三十轮已证）、
    做空借券成本不计（如实声明）、CVD 组件中性降级（无 takerBuy → v/2）。
  - 4h 窗口仅 730 天（Yahoo 60m 硬限制，2023-10 起）——如实声明。
  - 预登记验收（净口径，1d 主几何 / 1d incumbent / 4h 分别判定）:
      G1: era 合并净 totalR > 0
      G2: era 净 totalR > 0 标的占比 >= 2/3（有成交标的中）
      G3: era 最差标的净 EV > -0.15R
    1d 主几何三关 = 可行性结论；4h 三关只作方向参考（窗口短，同三十二轮）。
  - 朴素对照（预声明参数同三十二轮，非生产、不调优）：B&H（era 起点 =
    max(warmup, era 首根)，报告 % 收益与 close 口径 maxDD）、Donchian 55/20
    双向 + 2×ATR 止损、EMA50/200 交叉双向——均在 era 切片上与引擎同费率
    （双边 0.06%）对比，若占优如实报告。
  - §7.8：multiprocessing 每 (symbol, tf) 一 worker；§7.9：叙述性数字全部
    由本脚本打印输出，文档只抄输出。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/backtest_us_crypto.py [--refresh]
"""
import argparse
import os
import pickle
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from multiprocessing import Pool

import numpy as np

import profit_sweep2 as ps
from profit2_r5 import with_loose_plans
from backtest_ltc import trade_stats
import us_data
from backtest_us import (capacity_run, netize, stats3, CONF_US, FEES, fmt_res,
                         bench_donchian, bench_ema)

# era = MSTR's bitcoin era (first purchase announced 2020-08-11; cutoff before it)
ERA_MS = int(datetime(2020, 8, 1, tzinfo=timezone.utc).timestamp() * 1000)
CRYPTO_POOL = ["MSTR", "COIN", "MARA", "RIOT", "CLSK", "HOOD"]

# 1d primary = round-33 adopted US geometry; sensitivity = round-32 incumbent.
CONF_1D_ADOPTED = dict(CONF_US["1d"])
CONF_1D_ADOPTED["geo"] = (0.75, 1.2, 0.50, None, 24, 0.5)
CONF_1D_INCUMBENT = dict(CONF_US["1d"])
CONF_4H = dict(CONF_US["4h"])

STATE = {"refresh": False}


def year_of(ms: int) -> int:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year


# ---------------- decision records (decision-layer key only; shared by 1d variants) ----------------

def load_records(sym: str, tf: str, df, df1d, refresh: bool) -> list[dict]:
    cfg = CONF_4H if tf == "4h" else CONF_1D_ADOPTED
    # records depend only on decision params (not geo/fill/th) — 1d variants share one cache
    key = {"ver": 1, "sym": sym, "tf": tf, "src": ps.source_hash(),
           "warmup": cfg["warmup"], "min_bars": cfg["min_bars"],
           "spacing": cfg["spacing"], "fwd_room": cfg["fwd_room"]}
    cache_file = os.path.join(ps.CACHE_DIR, f"_usc_rec_{sym}_{tf}.pkl")
    if not refresh and os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                entry = pickle.load(f)
            if entry.get("key") == key:
                return entry["records"]
        except Exception:
            pass
    times = df["time"].to_numpy()
    closes = df["close"].to_numpy()
    n = len(df)
    records = []
    t0 = time.time()
    cnt = 0
    for i in range(cfg["warmup"], n - cfg["fwd_room"], cfg["spacing"]):
        t = int(times[i])
        htf = []
        for itv, span in cfg["mtf"]:
            m = ps.tf_summary_closed(df1d, t, span)
            if m:
                htf.append(m)
        rec = ps.decide_at(df, htf, t, cfg["warmup"], cfg["min_bars"])
        if rec is None:
            continue
        rec["symbol"] = sym
        for h in (6, 24, 48):
            rec[f"ret_{h}"] = float(closes[i + h] / closes[i] - 1.0) if i + h < n else float("nan")
        records.append(rec)
        cnt += 1
        if cnt % 1000 == 0:
            print(f"[calc] {sym} {tf}: {cnt} ({time.time()-t0:.0f}s)", flush=True)
    records.sort(key=lambda r: r["time"])
    tmp = cache_file + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump({"key": key, "records": records}, f)
    os.replace(tmp, cache_file)
    print(f"[rec] {sym} {tf}: {len(records)} records cached", flush=True)
    return records


def diracc_era(records) -> dict:
    """Direction accuracy (|score|>=15) on era records, lookahead-return sign match."""
    out = {}
    for h in (6, 24, 48):
        dirs, rets = [], []
        for r in records:
            if r["time"] < ERA_MS:
                continue
            s = r["score"]
            if abs(s) < 15:
                continue
            ret = r.get(f"ret_{h}")
            if ret is None or np.isnan(ret) or ret == 0:
                continue
            dirs.append(1 if s > 0 else -1)
            rets.append(ret)
        out[h] = (len(dirs), float(np.mean(np.sign(dirs) == np.sign(rets))) if dirs else float("nan"))
    return out


# ---------------- worker ----------------

def worker(job):
    sym, tf = job
    df = us_data.load_df(sym, tf)
    df1d = us_data.load_df(sym, "1d") if tf == "4h" else None
    records = load_records(sym, tf, df, df1d, STATE["refresh"])
    recs = with_loose_plans(records, 10)
    times = df["time"].to_numpy()
    opens = df["open"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    n = len(df)
    tidx = {int(t): k for k, t in enumerate(times)}
    variants = (CONF_1D_ADOPTED, CONF_1D_INCUMBENT) if tf == "1d" else (CONF_4H,)
    out = {"sym": sym, "tf": tf, "n_bars": n,
           "t0": int(times[0]), "t1": int(times[-1]), "recs": len(records)}
    for cfg in variants:
        n_orders, trades = capacity_run(recs, cfg, times, opens, highs, lows, closes, n, tidx)
        out.setdefault("orders", []).append(n_orders)
        out.setdefault("trades", []).append(trades)  # [adopted, incumbent] or [4h]
    if tf == "1d":
        warmup = CONF_1D_ADOPTED["warmup"]
        i0 = max(warmup, int(np.searchsorted(times, ERA_MS)))
        seg = closes[i0:]
        peak = np.maximum.accumulate(seg)
        out["bh_era"] = float(seg[-1] / seg[0] - 1.0)
        out["bh_era_dd"] = float(np.max((peak - seg) / peak))
        out["era_start_ms"] = int(times[i0])
        dc = bench_donchian(opens, highs, lows, closes, n)
        em = bench_ema(opens, highs, lows, closes, n)
        out["dc"] = [(int(times[i]), r, d) for i, r, d in dc if i < len(times) and times[i] >= ERA_MS]
        out["ema"] = [(int(times[i]), r, d) for i, r, d in em if i < len(times) and times[i] >= ERA_MS]
        out["diracc"] = diracc_era(records)
    return out


# ---------------- reporting ----------------

def _dd(st):
    return f"{st['maxdd']:.1f}" if st.get("filled") and st["maxdd"] == st["maxdd"] else "-"


def _gates(per, pooled, tag, extra=""):
    pos = sum(1 for s in per.values() if s.get("filled") and s["totalR"] > 0)
    n_traded = sum(1 for s in per.values() if s.get("filled"))
    worst_ev = min((s["ev"] for s in per.values() if s.get("filled")), default=float("nan"))
    g1 = pooled.get("filled", 0) > 0 and pooled["totalR"] > 0
    g2 = n_traded > 0 and pos / n_traded >= 2 / 3
    g3 = worst_ev == worst_ev and worst_ev > -0.15
    print(f"\n-- 预登记验收 {tag}（era 切片，净口径，写于跑数前）--")
    print(f"  G1 era合并净totalR>0: {pooled.get('totalR', 0):+.1f}R -> {'过' if g1 else '不过'}")
    print(f"  G2 正收益占比>=2/3: {pos}/{n_traded} -> {'过' if g2 else '不过'}")
    print(f"  G3 最差标的净EV>-0.15R: {worst_ev:+.3f}R -> {'过' if g3 else '不过'}")
    if extra:
        print(f"  {extra}")
    print(f"  结论: {'三关全过' if all((g1, g2, g3)) else '未全过 —— 如实记录，不回捞参数'}")


def report_1d(results):
    fee = FEES[1]
    cfg = CONF_1D_ADOPTED
    print(f"\n{'='*110}")
    print(f"===== 美股加密相关 1d：第三十三轮已采纳几何（depth0.75/stop1.2/be0.5/trail0.5/texit24）=====")
    print(f"几何: {ps.geo_str(tuple(cfg['geo']))} fill={cfg['fill']} th=10 "
          f"warmup={cfg['warmup']} spacing={cfg['spacing']}  费率双边 {fee*100:.2f}%  era>=2020-08-01")
    print(f"{'='*110}")
    header = (f"{'标的':<6} {'bar':>5} {'起':>5} {'全史净R':>8} {'era净R':>8} {'笔':>4} {'胜率':>6} "
              f"{'非亏':>6} {'eraEV':>8} {'DD':>5} {'多R':>7} {'空R':>7} {'R/年':>6} {'B&H%':>8} {'BHDD%':>6}")
    print(f"\n-- 逐标的（净口径；全史列为背景，era 为判关口径）--\n{header}")
    per_era = {}
    for r in sorted(results, key=lambda x: x["sym"]):
        net_all = netize(r["trades"][0], fee)
        net_era = [(t, rr, d) for t, rr, d in net_all if t >= ERA_MS]
        st = stats3(net_era)
        st_all = stats3(net_all)
        per_era[r["sym"]] = st
        lo = sum(rr for _, rr, d in net_era if d == "long")
        sh = sum(rr for _, rr, d in net_era if d == "short")
        span = max(0.25, (r["t1"] - r["era_start_ms"]) / (365.25 * 86400000))
        y0 = year_of(r["t0"])
        print(f"{r['sym']:<6} {r['n_bars']:>5} {y0:>5} {st_all['totalR']:>+8.1f} "
              f"{st['totalR']:>+8.1f} {st.get('filled',0):>4} "
              f"{st['winrate']*100 if st.get('filled') else 0:>6.1f} "
              f"{st['nonloss']*100 if st.get('filled') else 0:>6.1f} "
              f"{st['ev'] if st.get('filled') else 0:>+8.3f} {_dd(st):>5} "
              f"{lo:>+7.1f} {sh:>+7.1f} {st['totalR']/span:>+6.1f} "
              f"{r['bh_era']*100:>+8.1f} {r['bh_era_dd']*100:>6.1f}")

    all_era = []
    for r in results:
        all_era.extend([(t, rr, d) for t, rr, d in netize(r["trades"][0], fee) if t >= ERA_MS])
    all_era.sort(key=lambda x: x[0])
    st_e = stats3(all_era)
    t0e = min(t for t, _, _ in all_era) if all_era else ERA_MS
    span = max(0.25, (max(r["t1"] for r in results) - t0e) / (365.25 * 86400000))
    print(f"\n-- era 合并（净，双边 0.06%）--\n  {fmt_res(st_e)}")
    print(f"  era 年化净R = {st_e['totalR']/span:+.1f}R/年（池化，未除重叠；era 首笔 {datetime.fromtimestamp(t0e/1000, tz=timezone.utc).date()}）")
    by_year = defaultdict(list)
    for t, rr, d in all_era:
        by_year[year_of(t)].append(rr)
    print(f"  era 分年: " + "  ".join(f"{y}:{np.sum(v):+.1f}(n={len(v)})" for y, v in sorted(by_year.items())))
    lo_all = sum(rr for _, rr, d in all_era if d == "long")
    sh_all = sum(rr for _, rr, d in all_era if d == "short")
    print(f"  era 方向: 多 {lo_all:+.1f}R / 空 {sh_all:+.1f}R")

    all_full = []
    for r in results:
        all_full.extend(netize(r["trades"][0], fee))
    all_full.sort(key=lambda x: x[0])
    st_f = stats3(all_full)
    by_year_f = defaultdict(list)
    for t, rr, d in all_full:
        by_year_f[year_of(t)].append(rr)
    print(f"\n-- 全史合并（背景参考：含加密前时代）--\n  {fmt_res(st_f)}")
    print(f"  全史分年: " + "  ".join(f"{y}:{np.sum(v):+.1f}" for y, v in sorted(by_year_f.items())))

    _gates(per_era, st_e, "1d 已采纳几何")

    print(f"\n-- 费率敏感性（era 合并）--")
    raw = [tr for r in results for tr in r["trades"][0]]
    for f in FEES:
        stf = stats3(sorted([(t, rr, d) for t, rr, d in netize(raw, f) if t >= ERA_MS],
                            key=lambda x: x[0]))
        print(f"  双边 {f*100:.2f}%: {fmt_res(stf)}")

    # incumbent sensitivity
    print(f"\n-- incumbent 敏感性（生产 1d 几何 depth1.0/trail0.35/texit12，era 切片，净）--")
    inc_per = {}
    for r in sorted(results, key=lambda x: x["sym"]):
        net_era = [(t, rr, d) for t, rr, d in netize(r["trades"][1], fee) if t >= ERA_MS]
        st = stats3(net_era)
        inc_per[r["sym"]] = st
        print(f"  {r['sym']:<6} {st['totalR']:+.1f}R  EV {st['ev']:+.3f}  n={st.get('filled',0)}")
    inc_all = [(t, rr, d) for r in results for t, rr, d in netize(r["trades"][1], fee) if t >= ERA_MS]
    inc_all.sort(key=lambda x: x[0])
    st_inc = stats3(inc_all)
    print(f"  合并: {fmt_res(st_inc)}   (vs 已采纳几何 {st_e['totalR']:+.1f}R)")
    _gates(inc_per, st_inc, "1d incumbent 敏感性", extra="主判关以已采纳几何为准，本段为对照")

    # naive benchmarks (era)
    print(f"\n-- 朴素对照（era 切片，同费率双边 0.06%，预声明参数，非生产）--")
    print(f"  {'标的':<6} {'引擎净R':>8} {'DC55/20净R':>10} {'DC笔':>5} {'EMA50/200净R':>12} {'EMA笔':>5} {'B&H%era':>9} {'BHDD%':>6}")
    dc_pool, ema_pool = [], []
    for r in sorted(results, key=lambda x: x["sym"]):
        st_dc = stats3(r["dc"])
        st_em = stats3(r["ema"])
        dc_pool.extend(r["dc"])
        ema_pool.extend(r["ema"])
        st_eng = per_era[r["sym"]]
        print(f"  {r['sym']:<6} {st_eng['totalR']:>+8.1f} {st_dc['totalR']:>+10.1f} {st_dc.get('filled',0):>5} "
              f"{st_em['totalR']:>+12.1f} {st_em.get('filled',0):>5} {r['bh_era']*100:>+9.1f} {r['bh_era_dd']*100:>6.1f}")
    dc_pool.sort(key=lambda x: x[0])
    ema_pool.sort(key=lambda x: x[0])
    print(f"  合并: 引擎 {st_e['totalR']:+.1f}R | Donchian {fmt_res(stats3(dc_pool))} | EMA {fmt_res(stats3(ema_pool))}")

    print(f"\n-- 方向准确率（era，|score|>=15，决策点前瞻收益符号）--")
    for r in sorted(results, key=lambda x: x["sym"]):
        acc = r["diracc"]
        parts = "  ".join(f"{h}d n={acc[h][0]} {acc[h][1]*100:.1f}%" for h in (6, 24, 48))
        print(f"  {r['sym']:<6} {parts}")


def report_4h(results):
    fee = FEES[1]
    cfg = CONF_4H
    print(f"\n{'='*110}")
    print(f"===== 美股加密相关 4h：生产 4h 几何（RTH 两段制，窗口 730 天 2023-10 起）=====")
    print(f"几何: {ps.geo_str(tuple(cfg['geo']))} fill={cfg['fill']} th=10 warmup={cfg['warmup']} "
          f"spacing={cfg['spacing']} MTF=1d  费率双边 {fee*100:.2f}%")
    print(f"{'='*110}")
    header = (f"{'标的':<6} {'bar':>5} {'挂单':>4} {'笔':>4} {'胜率':>6} {'非亏':>6} {'净EV':>8} "
              f"{'净总R':>8} {'DD':>5} {'多R':>8} {'空R':>8}")
    print(f"\n-- 逐标的（净口径；窗口全程在 era 内）--\n{header}")
    per = {}
    for r in sorted(results, key=lambda x: x["sym"]):
        net = netize(r["trades"][0], fee)
        st = stats3(net)
        per[r["sym"]] = st
        lo = sum(rr for _, rr, d in net if d == "long")
        sh = sum(rr for _, rr, d in net if d == "short")
        print(f"{r['sym']:<6} {r['n_bars']:>5} {r['orders'][0]:>4} {st.get('filled',0):>4} "
              f"{st['winrate']*100 if st.get('filled') else 0:>6.1f} "
              f"{st['nonloss']*100 if st.get('filled') else 0:>6.1f} "
              f"{st['ev'] if st.get('filled') else 0:>+8.3f} {st['totalR']:>+8.1f} {_dd(st):>5} "
              f"{lo:>+8.1f} {sh:>+8.1f}")
    all_tr = [(t, rr, d) for r in results for t, rr, d in netize(r["trades"][0], fee)]
    all_tr.sort(key=lambda x: x[0])
    st_all = stats3(all_tr)
    span = max(0.25, (max(r["t1"] for r in results) - min(r["t0"] for r in results)) / (365.25 * 86400000))
    print(f"\n-- 合并（净，双边 0.06%）--\n  {fmt_res(st_all)}")
    print(f"  年化净R = {st_all['totalR']/span:+.1f}R/年（池化，未除重叠；窗口 ~2 年）")
    by_year = defaultdict(list)
    for t, rr, d in all_tr:
        by_year[year_of(t)].append(rr)
    print(f"  分年: " + "  ".join(f"{y}:{np.sum(v):+.1f}(n={len(v)})" for y, v in sorted(by_year.items())))
    lo_all = sum(rr for _, rr, d in all_tr if d == "long")
    sh_all = sum(rr for _, rr, d in all_tr if d == "short")
    print(f"  方向: 多 {lo_all:+.1f}R / 空 {sh_all:+.1f}R")
    _gates(per, st_all, "4h 生产几何", extra="[声明] 4h 窗口仅 730 天（Yahoo 60m 硬限制），样本短，三关只作方向参考")
    print(f"\n-- 费率敏感性（合并净）--")
    raw = [tr for r in results for tr in r["trades"][0]]
    for f in FEES:
        stf = stats3(sorted(netize(raw, f), key=lambda x: x[0]))
        print(f"  双边 {f*100:.2f}%: {fmt_res(stf)}")


def _init_worker(refresh: bool):
    STATE["refresh"] = refresh


def main():
    global STATE
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    STATE["refresh"] = args.refresh
    jobs = [(sym, tf) for tf in ("1d", "4h") for sym in CRYPTO_POOL]
    t0 = time.time()
    with Pool(processes=min(8, len(jobs)), initializer=_init_worker,
              initargs=(args.refresh,)) as pool:
        results = pool.map(worker, jobs, chunksize=1)
    print(f"[pool] {len(jobs)} jobs done in {time.time()-t0:.0f}s")
    report_1d([r for r in results if r["tf"] == "1d"])
    report_4h([r for r in results if r["tf"] == "4h"])


if __name__ == "__main__":
    main()
