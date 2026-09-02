"""拦截影响回测（第五十六轮追加，2026-09-02）——波动率极值追单拦截的收益影响。

用户问题：expanded + 贴区间顶/底 10% 的顺势计划拦截（decision.VOL_CHASE_RANGE_EDGE=0.90，
2026-09-02 用户裁定）解决了什么问题、对回测收益影响多少。

设计（遵循 §7.8 并发标准 + 第四十七轮钉窗标准）：
- 标的=推送列表 19 币；周期=4h（用户复盘的亏损单所在周期，且 EV 最高的周期）；
  UNI 补跑 1h（回测 harness 对 4h 已足够，1h 供参考）。
- 评估窗口钉 NOW_MS=2026-09-02 07:00 UTC（第五十二/五十三轮同钉点）。
- 两臂严格同数据、同窗口、同代码：
    OFF 臂（拦截关）：decision.VOL_CHASE_RANGE_EDGE=2.0（pdPct clamp 到 [0,1]，永不触发）
    ON  臂（拦截开）：decision.VOL_CHASE_RANGE_EDGE=0.90（生产配置）
  记录按 (symbol, tf, arm) 缓存 `_chase_cache_*`，key window=实际 df 根数（防假窗口）；
  两臂先后各跑一遍——第二遍命中第一遍对侧臂缓存（src hash 相同则零重算，不同则重算），
  任一臂缓存损坏都不影响另一臂（key 含 arm）。
- 判定臂差异：trades 对齐后输出 被拦截交易明细（entry_t/dir/rr）、两臂合计/EV/逐年。
- 描述性补充：expanded 记录中计划占比、拦截单的多空分布。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/chase_gate_impact.py [--fetch-only] [--arm off|on] [--symbols UNIUSDT,...] [--tfs 4h]
"""
import argparse
import asyncio
import multiprocessing as mp
import os
import pickle
import sys
import time
from collections import defaultdict

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import profit_sweep2 as ps
from services.analysis import decision
from backtest_5y import W5, CONF5, compute_records, sim_outcome_fast
from audit_order_and_entry import sim_journal_order
from backtest_ltc import trade_stats
from profit2_r5 import with_loose_plans

NOW_MS = 1788332400000  # 2026-09-02 07:00 UTC
YEAR_MS = 365.25 * 86400 * 1000
STEP_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}

SYMBOLS19 = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ZECUSDT",
             "DOGEUSDT", "SUIUSDT", "LTCUSDT", "LINKUSDT", "ADAUSDT",
             "AAVEUSDT", "NEARUSDT", "AVAXUSDT", "XLMUSDT", "BCHUSDT",
             "FILUSDT", "UNIUSDT", "ARBUSDT"]

EDGE = {"off": 2.0, "on": 0.90}


def fmt_ts(ms):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%y-%m-%d")


def year_of(ms):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year


async def fetch_one(sem, sym, itv):
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


async def fetch_all(symbols, tfs):
    sem = asyncio.Semaphore(2)
    await asyncio.gather(*[fetch_one(sem, s, t) for s in symbols for t in tfs])


def load_df(sym, itv):
    rows = ps.kline_cache._read_rows(sym, itv, NOW_MS, W5[itv])
    return ps.kline_cache.rows_to_df(rows)


def load_records_arm(sym, tf, df, dfs, arm):
    """_chase_cache_*：key 含 arm（off=常量2.0重算臂 / on=常量0.90生产臂）+ window=df 根数。"""
    cache_file = os.path.join(ps.CACHE_DIR, f"_chase_cache_{sym}_{tf}_{arm}.pkl")
    key = {"ver": 1, "tf": tf, "symbol": sym, "arm": arm, "window": len(df),
           "src": ps.source_hash()}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                entry = pickle.load(f)
            if entry.get("key") == key:
                return entry["records"]
        except Exception:
            pass
    decision.VOL_CHASE_RANGE_EDGE = EDGE[arm]
    records = compute_records(sym, tf, dfs)
    tmp = cache_file + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump({"key": key, "records": records}, f)
    os.replace(tmp, cache_file)
    print(f"[rec] {sym} {tf} arm={arm}: {len(records)} records computed", flush=True)
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
                       "entry_px": float(entry), "risk_px": float(abs(entry - stop))})
    return trades


def worker(job):
    sym, tf, arm = job
    try:
        dfs = {itv: load_df(sym, itv) for itv in ("1h", "4h", "1d", "1w")}
        cfg = CONF5[tf]
        df = dfs[tf]
        if len(df) <= cfg["warmup"] + cfg["fwd_room"] + cfg["spacing"]:
            return {"job": job, "skipped": True, "bars": len(df), "trades": []}
        records = load_records_arm(sym, tf, df, dfs, arm)
        recs = records if cfg["th"] == 25 else with_loose_plans(records, cfg["th"])
        sim = sim_journal_order if tf == "1h" else sim_outcome_fast
        trades = capacity_trades(recs, cfg, df, sim)
        # 描述性：expanded 记录中的计划统计
        n_exp = sum(1 for r in records if r.get("vol_state") == "expanded")
        n_exp_plan = sum(1 for r in records
                         if r.get("vol_state") == "expanded" and r.get("plan"))
        return {"job": job, "skipped": False, "bars": len(df), "trades": trades,
                "n_records": len(records), "n_exp": n_exp, "n_exp_plan": n_exp_plan}
    except Exception:
        import traceback
        return {"job": job, "error": traceback.format_exc()}


def stats_line(trades):
    st = trade_stats([(t["entry_t"], t["rr"]) for t in trades])
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch-only", action="store_true")
    ap.add_argument("--arm", choices=["off", "on"], default=None,
                    help="只跑单臂（默认两臂依次跑）")
    ap.add_argument("--symbols", default=",".join(SYMBOLS19))
    ap.add_argument("--tfs", default="4h")
    args = ap.parse_args()
    t0 = time.time()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    tfs = [t.strip() for t in args.tfs.split(",") if t.strip()]
    arms = [args.arm] if args.arm else ["off", "on"]

    print(f"== 抓取：{len(symbols)} 币 × {tfs}（钉窗 {fmt_ts(NOW_MS)}）==", flush=True)
    asyncio.run(fetch_all(symbols, tfs))
    print(f"[fetch] {time.time()-t0:.0f}s", flush=True)
    if args.fetch_only:
        return

    results = {}
    for arm in arms:
        print(f"\n===== arm={arm} (VOL_CHASE_RANGE_EDGE={EDGE[arm]}) =====", flush=True)
        jobs = [(s, tf, arm) for s in symbols for tf in tfs]
        ctx = mp.get_context("spawn")
        with ctx.Pool(min(8, len(jobs))) as pool:
            for res in pool.imap_unordered(worker, jobs):
                if "error" in res:
                    print(f"[worker-error] {res['job']}\n{res['error']}", flush=True)
                else:
                    results[res["job"]] = res
                    j = res["job"]
                    if res.get("skipped"):
                        print(f"  {j[0]:<10}{j[1]:<3} skipped ({res['bars']} bars)", flush=True)
                    else:
                        st = stats_line(res["trades"])
                        print(f"  {j[0]:<10}{j[1]:<3} filled={st.get('filled', 0):>5} "
                              f"total={st.get('totalR', 0):+8.1f}R EV={st.get('ev', 0):+.3f} "
                              f"nonloss={st.get('nonloss', 0)*100:.1f}% DD={st.get('maxdd', 0):.1f}R "
                              f"[recs={res['n_records']} exp={res['n_exp']} expPlan={res['n_exp_plan']}]",
                              flush=True)
        print(f"[arm {arm}] done {time.time()-t0:.0f}s", flush=True)

    if len(arms) == 2:
        print(f"\n{'='*100}")
        print("===== 两臂对比（OFF=拦截关 / ON=拦截开=生产；被拦截=OFF有ON无的交易）=====")
        print(f"{'='*100}")
        tot_off = tot_on = 0.0
        blocked_all = []
        for sym in symbols:
            for tf in tfs:
                off = results.get((sym, tf, "off"))
                on = results.get((sym, tf, "on"))
                if not off or not on or off.get("skipped") or on.get("skipped"):
                    continue
                so, sn = stats_line(off["trades"]), stats_line(on["trades"])
                tot_off += so.get("totalR", 0)
                tot_on += sn.get("totalR", 0)
                on_keys = {(t["entry_t"], t["dir"]) for t in on["trades"]}
                blocked = [t for t in off["trades"] if (t["entry_t"], t["dir"]) not in on_keys]
                blocked_all += [(sym, tf, t) for t in blocked]
                mark = f" 拦截{len(blocked)}笔" if blocked else ""
                print(f"  {sym:<10}{tf:<3} OFF {so.get('totalR', 0):+8.1f}R(n={so.get('filled', 0)})"
                      f"  ON {sn.get('totalR', 0):+8.1f}R(n={sn.get('filled', 0)})"
                      f"  Δ={sn.get('totalR', 0)-so.get('totalR', 0):+7.1f}R{mark}")
        print(f"\n  池化合计：OFF {tot_off:+.1f}R  ON {tot_on:+.1f}R  Δ{tot_on-tot_off:+.1f}R "
              f"（拦截净影响，正=拦截增利/减亏，负=拦截代价）")
        if blocked_all:
            wr = sum(1 for _, _, t in blocked_all if t["rr"] > 0)
            br = sum(t["rr"] for _, _, t in blocked_all)
            print(f"\n  被拦截交易明细（{len(blocked_all)} 笔，胜率 {wr/len(blocked_all)*100:.1f}%，"
                  f"合计 {br:+.1f}R，EV {br/len(blocked_all):+.3f}R/笔）：")
            by_year = defaultdict(float)
            for sym, tf, t in sorted(blocked_all, key=lambda x: x[2]["entry_t"]):
                from datetime import datetime, timezone
                et = datetime.fromtimestamp(t["entry_t"] / 1000, tz=timezone.utc)
                print(f"    {sym:<10}{tf:<3}{et.strftime('%y-%m-%d %H:%M')} {t['dir']:<5} "
                      f"rr={t['rr']:+.2f} entry={t['entry_px']:.4f}")
                by_year[year_of(t["entry_t"])] += t["rr"]
            print("  被拦截逐年：" + "  ".join(f"{y}:{v:+.1f}" for y, v in sorted(by_year.items())))
        else:
            print("  无被拦截交易（两臂完全一致）。")

    print(f"\n[total] {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
