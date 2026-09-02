"""候选标的回测（第五十二轮，2026-09-02）——币安链上美股 + 币圈候选 TRX/LINK/ADA/PYTH

用户指令：币安现在有链上美股（tokenized stocks），分析有没有适合加入推送列表的；
实在没有，再从币圈里加。

发现（tests/_discover_stocks.py，运行时探测）：
- 链上美股 = "B" 后缀家族（AAPLB/TSLAB/MSTRB/CRCLB/QQQB/SPYB…，Backed 系代币化股票/ETF），
  25 个 USDT 交易对全部 2026-06-11 起分批上市，**最老仅 83 天**（CRCLB/NVDAB/TSLAB/MUB），
  其余 21~71 天；名字撞车的山寨币（EGLD/DIA/HMSTR/MUBARAK/1000CHEEMS 等）已排除；
- 与第五十轮 SNDKB（83 天 → "样本太短不下结论，半年后复测"）完全同处境。

本脚本：
- A 组（25 个链上美股）：1h 描述性回测（R13 几何零调参），4h/1d/1w 由 warmup 门如实跳过；
  结论按深度门判定（不因跑出的数字好而放宽标准——SNDKB 先例）；
- B 组（TRX/LINK/ADA/PYTH）：完整 SUI 流程——R13 生产几何、容量约束串行、1h 下界口径
  （sim_journal_order）、5 年窗（PYTH 上市起全历史）、th 原生/放宽同生产、未计费主表+
  费率敏感性单列；**四个标的从未参与任何调参（R8/R13 调参标的=BTC/ETH/BNB/SOL），
  纯样本外**；PYTH 第七轮旧几何 +274.9R 未入列表，本轮按 R13 重测。

工程口径：
- A 组数据源：现货镜像 data-api.binance.vision 直连向后分页（B 后缀标的是否上合约
  未经确认，绕开 fapi 链的不确定性），落盘 klines.db 后走标准 harness；
- B 组数据源：标准优先级链（kline_cache.get_klines，fapi→代理→镜像自动探测）；
- 评估窗口钉 NOW_MS=2026-09-02 07:00 UTC（第四十七轮标准：活进程实时追加的坑）；
- 记录缓存 `_cand_cache_*`（独立命名空间）：**key window=实际喂入 df 根数**
  （第五十一轮假窗口缓存教训：key 声称 43800 实际只喂 18168），load 端天然防错；
- §7.8：阶段1 Semaphore 并发抓取（A 组直连镜像 4 并发、B 组链上 2 并发）+ 根数核对
  （第三十六轮标准）；阶段2 每 symbol 一 spawn worker；
- 零调参、零生产改动。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/backtest_newcands.py [--fetch-only]
"""
import argparse
import asyncio
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

import httpx
import numpy as np

import profit_sweep2 as ps
from backtest_5y import W5, CONF5, compute_records, sim_outcome_fast
from audit_order_and_entry import sim_journal_order
from backtest_ltc import trade_stats
from profit2_r5 import with_loose_plans

NOW_MS = 1788332400000  # 2026-09-02 07:00 UTC — 评估窗口钉 end_time（第四十七轮标准）
YEAR_MS = 365.25 * 86400 * 1000
FEE_NET = 0.0010  # 双边 0.10%
TFS = ("1h", "4h", "1d", "1w")
SPOT = "https://data-api.binance.vision"
STEP_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}

GROUP_A = [b + "USDT" for b in [
    "CRCLB", "NVDAB", "TSLAB", "MUB", "AMDB", "INTCB", "MSTRB", "METAB",
    "MSFTB", "PLTRB", "QQQB", "COINB", "GOOGLB", "QCOMB", "SPYB", "AVGOB",
    "HOODB", "MUUB", "ORCLB", "TQQQB", "AAPLB", "AMZNB", "IRENB", "NFLXB", "GMEB",
]]
GROUP_B = ["TRXUSDT", "LINKUSDT", "ADAUSDT", "PYTHUSDT"]
SYMBOLS = GROUP_A + GROUP_B


def fmt_ts(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%y-%m-%d")


def year_of(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year


# ---------------- phase 1a: group A spot-mirror fetch ----------------

async def fetch_a_one(client, sem, sym, itv):
    step = STEP_MS[itv]
    async with sem:
        for attempt in range(3):
            try:
                et = NOW_MS
                total = []
                for _ in range(60):
                    r = await client.get(f"{SPOT}/api/v3/klines", params={
                        "symbol": sym, "interval": itv, "limit": 1000, "endTime": et})
                    r.raise_for_status()
                    raw = r.json()
                    if not raw:
                        break
                    total = ps.kline_cache._raw_to_rows(raw) + total
                    if len(raw) < 1000:
                        break
                    et = int(raw[0][0]) - 1
                ps.kline_cache._persist(sym, itv, total)
                have = ps.kline_cache._read_rows(sym, itv, NOW_MS, 10 ** 9)
                n = len(have)
                if n == 0:
                    print(f"[fetch-empty] {sym} {itv}: 无K线（可能已下架）", flush=True)
                    return True
                first, last = int(have[0][0]), int(have[-1][0])
                gaps = (last - first) // step + 1 - n
                print(f"[fetch-ok] {sym} {itv}: {n} bars {fmt_ts(first)}..{fmt_ts(last)} gaps={gaps}",
                      flush=True)
                return True
            except Exception as exc:
                print(f"[warn] {sym} {itv} attempt{attempt}: {exc}", flush=True)
                await asyncio.sleep(5 * (attempt + 1))
        print(f"[fetch-fail] {sym} {itv}: 3 次失败，该标的按缺失处理", flush=True)
        return False


async def fetch_group_a():
    sem = asyncio.Semaphore(4)
    async with httpx.AsyncClient(timeout=20.0) as client:
        await asyncio.gather(*[fetch_a_one(client, sem, s, t)
                               for s in GROUP_A for t in TFS])


# ---------------- phase 1b: group B standard-chain fetch ----------------

async def fetch_b_one(sem, sym, itv):
    async with sem:
        for attempt in range(4):
            try:
                rows = await ps.kline_cache.get_klines(sym, itv, W5[itv], end_time=NOW_MS)
                # 根数核对（第三十六轮标准）：上市久于窗口的标的应满 W5；不足重试一次
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


async def fetch_group_b():
    sem = asyncio.Semaphore(2)
    await asyncio.gather(*[fetch_b_one(sem, s, t) for s in GROUP_B for t in TFS])


# ---------------- phase 2: per-symbol worker (pure CPU) ----------------

def load_df(sym, itv):
    rows = ps.kline_cache._read_rows(sym, itv, NOW_MS, W5[itv])
    return ps.kline_cache.rows_to_df(rows)


def load_cached_records(sym, tf, df, dfs):
    """_cand_cache_*：key window=实际 df 根数（防假窗口，第五十一轮教训）。"""
    cache_file = os.path.join(ps.CACHE_DIR, f"_cand_cache_{sym}_{tf}.pkl")
    key = {"ver": 1, "tf": tf, "symbol": sym, "window": len(df), "src": ps.source_hash()}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                entry = pickle.load(f)
            if entry.get("key") == key:
                return entry["records"]
        except Exception:
            pass
    records = compute_records(sym, tf, dfs)
    tmp = cache_file + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump({"key": key, "records": records}, f)
    os.replace(tmp, cache_file)
    print(f"[rec] {sym} {tf}: {len(records)} records computed", flush=True)
    return records


def capacity_trades(recs, cfg, df, sim):
    depth, stopw, be_frac, tgt, texit, trail = tuple(cfg["geo"])
    fill_bars = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    times = df["time"].to_numpy()
    n = len(df)
    tidx = {int(t): i for i, t in enumerate(times)}
    trades = []
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
        out = sim(highs, lows, closes, n, i, direction, entry, stop,
                  be_frac, tgt, texit, fill_bars, trail)
        if out is None:
            continue
        rr, fill, exit_bar = out
        busy = exit_bar
        trades.append({"entry_t": int(times[fill]), "exit_t": int(times[exit_bar]),
                        "dir": direction, "rr": float(rr),
                        "entry_px": float(entry), "risk_px": float(abs(entry - stop)),
                        "scale": 1.0})
    return trades


def worker(sym):
    try:
        dfs = {itv: load_df(sym, itv) for itv in TFS}
        out = {}
        for tf in TFS:
            cfg = CONF5[tf]
            df = dfs[tf]
            if len(df) <= cfg["warmup"] + cfg["fwd_room"] + cfg["spacing"]:
                out[tf] = {"skipped": True, "bars": len(df), "trades": []}
                continue
            records = load_cached_records(sym, tf, df, dfs)
            recs = records if cfg["th"] == 25 else with_loose_plans(records, cfg["th"])
            sim = sim_journal_order if tf == "1h" else sim_outcome_fast
            out[tf] = {"skipped": False, "bars": len(df),
                       "trades": capacity_trades(recs, cfg, df, sim)}
        return {"sym": sym, "data": out}
    except Exception:
        import traceback
        return {"sym": sym, "error": traceback.format_exc()}


def stats_of(trades):
    return trade_stats([(t["entry_t"], t["rr"]) for t in trades])


def print_tf_cell(sym, data, tf):
    cell = data[tf]
    if cell["skipped"]:
        print(f"  {tf:<3} skipped: short history ({cell['bars']} bars < warmup+fwd_room)")
        return 0.0
    st = stats_of(cell["trades"])
    if st.get("filled"):
        pf = f"{st['pf']:.2f}" if st["pf"] != float("inf") else "inf"
        print(f"  {tf:<3} filled={st['filled']:>5} win={st['winrate']*100:.1f}% "
              f"nonloss={st['nonloss']*100:.1f}% EV={st['ev']:+.3f}R "
              f"total={st['totalR']:+.1f}R DD={st['maxdd']:.1f}R PF={pf}")
        return st["totalR"]
    print(f"  {tf:<3} no fills")
    return 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch-only", action="store_true")
    args = ap.parse_args()
    t0 = time.time()

    print(f"== A 组：链上美股 {len(GROUP_A)} 个（现货镜像直连）==", flush=True)
    asyncio.run(fetch_group_a())
    print(f"[group A fetch] {time.time()-t0:.0f}s\n", flush=True)
    print(f"== B 组：币圈候选 {len(GROUP_B)} 个（标准优先级链）==", flush=True)
    asyncio.run(fetch_group_b())
    print(f"[group B fetch] {time.time()-t0:.0f}s", flush=True)
    if args.fetch_only:
        return

    ctx = mp.get_context("spawn")
    with ctx.Pool(min(10, len(SYMBOLS))) as pool:
        results = pool.map(worker, SYMBOLS)
    data = {}
    for res in results:
        if "error" in res:
            print(f"[worker-error]\n{res['error']}", flush=True)
        else:
            data[res["sym"]] = res["data"]
    if len(data) != len(SYMBOLS):
        missing = [s for s in SYMBOLS if s not in data]
        raise SystemExit(f"worker failed: {missing}")
    print(f"[pool] done in {time.time()-t0:.0f}s\n")

    # ---- A 组报告：深度门 + 1h 描述性 ----
    print(f"{'='*104}")
    print(f"===== A 组：链上美股（B 后缀家族）——1h 描述性回测，R13 零调参，毛口径 =====")
    print(f"{'='*104}")
    print(f"{'symbol':<12}{'上市':>9}{'1h bars':>8}{'filled':>7}{'nonloss%':>9}{'EV':>8}{'totalR':>9}  其余周期")
    a_rows = []
    for sym in GROUP_A:
        if sym not in data:
            print(f"{sym:<12} 无数据")
            continue
        df1 = load_df(sym, "1h")
        if len(df1) == 0:
            print(f"{sym:<12} 无K线")
            continue
        first = fmt_ts(int(df1["time"].iloc[0]))
        c1 = data[sym]["1h"]
        skipped_tfs = [tf for tf in ("4h", "1d", "1w") if data[sym][tf]["skipped"]]
        if c1["skipped"]:
            print(f"{sym:<12}{first:>9}{len(df1):>8}  1h 亦不足 warmup（{c1['bars']} bars）")
            a_rows.append((sym, first, len(df1), 0, float("nan"), 0.0))
            continue
        st = stats_of(c1["trades"])
        if st.get("filled"):
            print(f"{sym:<12}{first:>9}{len(df1):>8}{st['filled']:>7}"
                  f"{st['nonloss']*100:>8.1f}%{st['ev']:>+8.3f}{st['totalR']:>+9.1f}  "
                  f"{'/'.join(skipped_tfs)} skipped")
            a_rows.append((sym, first, len(df1), st["filled"], st["ev"], st["totalR"]))
        else:
            print(f"{sym:<12}{first:>9}{len(df1):>8}{'no fills':>7}"
                  f"{'':>9}{'':>8}{'':>9}  {'/'.join(skipped_tfs)} skipped")
            a_rows.append((sym, first, len(df1), 0, float("nan"), 0.0))
    oldest = min((r[1] for r in a_rows), default="-")
    print(f"\n深度门判定：最老上市 {oldest}（83 天量级）= 第五十轮 SNDKB 同处境 →")
    print(f"  按既有标准（样本太短不下结论）**全部不接受**，最早 2026-12 复测；")
    print(f"  上表 1h 数字仅为描述性记录，不构成加入依据。")

    # ---- B 组报告：SUI 流程 ----
    print(f"\n{'='*104}")
    print(f"===== B 组：TRX/LINK/ADA/PYTH ——完整 SUI 流程（R13 零调参纯样本外，毛口径，1h=下界）=====")
    print(f"{'='*104}")
    for sym in GROUP_B:
        df1 = load_df(sym, "1h")
        span = f"{fmt_ts(int(df1['time'].iloc[0]))}..{fmt_ts(int(df1['time'].iloc[-1]))}" if len(df1) else "-"
        print(f"\n{sym} (pure out-of-sample) window {span}")
        tot = 0.0
        for tf in TFS:
            tot += print_tf_cell(sym, data[sym], tf)
        print(f"  TOTAL (4 tf gross): {tot:+.1f}R")

    print(f"\n-- direction split (long/short, gross R) --")
    for sym in GROUP_B:
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
    for sym in GROUP_B:
        for tf in TFS:
            cell = data[sym][tf]
            if cell["skipped"] or not cell["trades"]:
                continue
            by_year = defaultdict(list)
            for t in cell["trades"]:
                by_year[year_of(t["entry_t"])].append(t["rr"])
            parts = "  ".join(f"{y}:{np.sum(v):+.1f}(n={len(v)})" for y, v in sorted(by_year.items()))
            print(f"  {sym:<10} {tf:<3} {parts}")

    print(f"\n-- fee sensitivity (feeR=roundtrip*entry/risk) --")
    print(f"  {'symbol':<10}{'tf':<5}{'gross':>10}{'rt0.05%':>11}{'rt0.10%':>11}")
    for sym in GROUP_B:
        for tf in ("1h", "4h", "1d"):
            cell = data[sym][tf]
            if cell["skipped"] or not cell["trades"]:
                continue
            trades = cell["trades"]
            cells = []
            for fee in (0.0, 0.0005, 0.0010):
                net = [(t["entry_t"], t["rr"] - fee * t["entry_px"] / t["risk_px"]) for t in trades]
                cells.append(trade_stats(net)["totalR"])
            print(f"  {sym:<10}{tf:<5}{cells[0]:>+9.1f}R {cells[1]:>+10.1f}R {cells[2]:>+10.1f}R")

    print(f"\n-- annualized (fixed-stake gross R; per-coin actual window) --")
    print(f"  {'symbol':<10}{'totalR':>10}{'years':>7}{'annR/yr':>9}{'netAnn@0.10%':>13}")
    for sym in GROUP_B:
        merged = sorted([t for tf in TFS for t in data[sym][tf]["trades"]], key=lambda x: x["entry_t"])
        if not merged:
            print(f"  {sym:<10} no fills")
            continue
        st_c = trade_stats([(t["entry_t"], t["rr"]) for t in merged])
        df1h = load_df(sym, "1h")
        t_start = int(df1h["time"].iloc[min(CONF5["1h"]["warmup"], len(df1h) - 1)])
        t_end = int(df1h["time"].iloc[-1])
        span_y = max(0.1, (t_end - t_start) / YEAR_MS)
        net_tot = sum(t["rr"] - FEE_NET * t["entry_px"] / t["risk_px"] for t in merged)
        print(f"  {sym:<10}{st_c['totalR']:>+9.1f}R {span_y:>6.2f} {st_c['totalR']/span_y:>+8.1f} "
              f"{net_tot/span_y:>+12.1f}")

    print(f"\n[total] {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()