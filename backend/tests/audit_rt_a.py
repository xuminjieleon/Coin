# -*- coding: utf-8 -*-
"""实证审计 测试A：5年回测记录 == 生产引擎逐点重算（2026-08-28）。

方法：从 _5y_cache_BTCUSDT_1h.pkl / ETHUSDT_4h.pkl / BNBUSDT_1d.pkl 各随机抽
15 条记录（固定种子 20260828），每条 (sym, tf, t)：
  1) kline_cache.get_klines(sym, tf, window, end_time=4_000_000_000_000) 取数据；
  2) 按 backtest_ltc.CONF 的 mtf 配置用 profit_sweep2.tf_summary_closed 构造 htf；
  3) profit_sweep2.decide_at(df, htf, t, warmup, min_bars) 重算；
  4) 与缓存记录比对 score/bias/regime/plan/atr/price（容差 1e-9），
     另附 alignment/cvd_conf/vol_state/zone 字段作信息性比对。
预期 100% 一致（回测记录由同一函数同一数据口径生成）。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/audit_rt_a.py
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
from services import kline_cache  # noqa: E402

SEED = 20260828
SETS = [("BTCUSDT", "1h"), ("ETHUSDT", "4h"), ("BNBUSDT", "1d")]
NEED = [("BTCUSDT", "1h"), ("BTCUSDT", "4h"), ("BTCUSDT", "1d"),
        ("ETHUSDT", "4h"), ("ETHUSDT", "1d"), ("BNBUSDT", "1d")]

_LOG = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "audit_rt_a.log"), "w", encoding="utf-8")


def out(s=""):
    print(s, flush=True)
    _LOG.write(str(s) + "\n")
    _LOG.flush()


async def load_all():
    dfs = {}
    for sym, tf in NEED:
        rows = await kline_cache.get_klines(sym, tf, W5[tf], end_time=4_000_000_000_000)
        dfs[(sym, tf)] = kline_cache.rows_to_df(rows)
        out(f"[data] {sym} {tf}: {len(rows)} bars "
            f"({ps.fmt_ts(int(rows[0][0]))}..{ps.fmt_ts(int(rows[-1][0]))})")
    return dfs


def fnum(x):
    return "None" if x is None else f"{x:.8f}"


def main():
    t0 = time.time()
    dfs = asyncio.run(load_all())
    out(f"[data] 加载耗时 {time.time()-t0:.1f}s")

    total = 0
    ok = 0
    mismatches = []
    for sym, tf in SETS:
        with open(os.path.join(ps.CACHE_DIR, f"_5y_cache_{sym}_{tf}.pkl"), "rb") as f:
            entry = pickle.load(f)
        records = entry["records"]
        out(f"\n== {sym} {tf}: {len(records)} 条缓存记录；cache key={entry.get('key')}")
        rng = random.Random(SEED)
        idxs = sorted(rng.sample(range(len(records)), 15))
        cfg = CONF[tf]
        df = dfs[(sym, tf)]
        out(f"{'日期':<12}{'time':>14} {'score':>11} {'bias':>15} {'regime':>17} "
            f"{'plan':>9} {'Δatr':>10} {'Δprice':>10} 结果")
        for k in idxs:
            rec = records[k]
            t = int(rec["time"])
            htf = []
            for itv, span in cfg["mtf"]:
                m = ps.tf_summary_closed(dfs[(sym, itv)], t, span)
                if m:
                    htf.append(m)
            r2 = ps.decide_at(df, htf, t, cfg["warmup"], cfg["min_bars"])
            total += 1
            if r2 is None:
                mismatches.append((sym, tf, t, "decide_at returned None"))
                out(f"{ps.fmt_ts(t):<12}{t:>14}  decide_at=None  <== 不一致")
                continue
            checks = []
            checks.append(("score", rec["score"] == r2["score"],
                           f"{rec['score']}/{r2['score']}"))
            checks.append(("bias", rec["bias"] == r2["bias"],
                           f"{rec['bias']}/{r2['bias']}"))
            checks.append(("regime", rec["regime"] == r2["regime"],
                           f"{rec['regime']}/{r2['regime']}"))
            checks.append(("plan", rec["plan"] == r2["plan"],
                           f"{rec['plan']}/{r2['plan']}"))
            d_atr = abs((rec["atr"] or 0) - (r2["atr"] or 0))
            d_price = abs(rec["price"] - r2["price"])
            checks.append(("atr", d_atr <= 1e-9, f"{d_atr:.2e}"))
            checks.append(("price", d_price <= 1e-9, f"{d_price:.2e}"))
            good = all(c[1] for c in checks)
            # informational extras (expected equal too)
            extras = []
            for f_ in ("alignment", "cvd_conf", "vol_state"):
                extras.append(f"{f_}={'OK' if rec.get(f_) == r2.get(f_) else 'DIFF'}")
            for f_ in ("zone_bull_top", "zone_bear_bottom"):
                a, b = rec.get(f_), r2.get(f_)
                same = (a is None and b is None) or (
                    a is not None and b is not None and abs(a - b) <= 1e-9)
                extras.append(f"{f_}={'OK' if same else 'DIFF'}")
            if good:
                ok += 1
            else:
                bad = [f"{n}:{d}" for n, ok_, d in checks if not ok_]
                mismatches.append((sym, tf, t, ";".join(bad)))
            out(f"{ps.fmt_ts(t):<12}{t:>14} "
                f"{checks[0][2]:>11} {checks[1][2]:>15} {checks[2][2]:>17} "
                f"{checks[3][2]:>9} {d_atr:>10.2e} {d_price:>10.2e} "
                f"{'一致' if good else '不一致'}  [{'; '.join(extras)}]")

    out(f"\n===== 测试A 汇总 =====")
    out(f"样本量: {total}（BTCUSDT 1h / ETHUSDT 4h / BNBUSDT 1d 各 15，种子 {SEED}）")
    out(f"一致: {ok}/{total}（{ok/max(total,1)*100:.1f}%）")
    if mismatches:
        out(f"不一致明细 {len(mismatches)} 条:")
        for m in mismatches:
            out(f"  {m}")
    else:
        out("不一致明细: 无 —— 回测记录与生产引擎逐点重算 100% 一致")
    out(f"[done] 总耗时 {time.time()-t0:.1f}s")
    _LOG.close()


if __name__ == "__main__":
    main()
