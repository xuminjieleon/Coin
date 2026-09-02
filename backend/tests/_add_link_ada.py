"""LINK/ADA 入列表落地（第五十二轮追加）——加法汇总与单币复利/费率列的机器输出。

用户拍板：只加 LINK/ADA（9→11 币）；TRX/PYTH 档留档（DEVLOG R52）。
本脚本为 BACKTEST §2/§3 增补提供全部机器输出（§7.9：叙述数字只抄机器输出）：
- 重建 LINK/ADA 交易流（_cand_cache_* 记录缓存命中，秒级）；
- 逐周期毛/净（@0.05%/0.06%/0.10%）、笔数、逐年；
- 单币复利（f=1%、净@0.10%、单币独立资金流、事件账户）与月化；
- 11 币池化加法汇总（九币基数=第五十轮正典机器输出、LINK/ADA=本轮本机输出；
  容量串行按币独立 → 池化 ΣR 精确可加；锚差 1 天如实标注）。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/_add_link_ada.py
"""
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import compound_backtest as cb
import backtest_newcands as bc

YEAR_MS = 365.25 * 86400 * 1000
TFS = ("1h", "4h", "1d", "1w")
BPS = (5, 6, 10)  # 双边费率基点（×0.0001）

# 九币正典（第五十轮机器输出，BACKTEST §2.2/§3；部署机、窗口尾 2026-09-01）
# gross/net10 为 R 总额（R50 直抄）；net05/net06 只有年化%口径（BACKTEST §3），总额=年化×span（脚本内换算打印）
POOL9 = {
    "1h": {"gross": 3955.3, "net10": 2301.2, "ann_net05": 633.0, "ann_net06": 600.0, "n": 31603},
    "4h": {"gross": 3052.6, "net10": 2675.1, "ann_net05": 604.0, "ann_net06": 596.0, "n": 7527},
    "1d": {"gross": 420.2, "net10": 402.8, "ann_net05": 106.0, "ann_net06": 106.0, "n": 1124},
}
SPAN9_Y = 4.94


def year_of(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year


def net_sum(trades, bp):
    fee = bp * 1e-4
    return sum(t["rr"] - fee * t["entry_px"] / t["risk_px"] for t in trades)


def main():
    t0 = time.time()
    out = {}
    for sym in ("LINKUSDT", "ADAUSDT"):
        res = bc.worker(sym)
        if "error" in res:
            print(f"[worker-error] {res['sym']}\n{res['error']}")
            raise SystemExit(1)
        out[sym] = res["data"]
        print(f"[trades] {sym} rebuilt", flush=True)

    print("\n== 逐周期 毛/净/笔数（本轮本机，1h=下界，毛口径主表）==")
    per = {}
    for sym, data in out.items():
        per[sym] = {}
        for tf in TFS:
            cell = data[tf]
            if cell["skipped"] or not cell["trades"]:
                continue
            tr = cell["trades"]
            entry = {"gross": sum(t["rr"] for t in tr), "n": len(tr)}
            for bp in BPS:
                entry[f"net{bp:02d}"] = net_sum(tr, bp)
            per[sym][tf] = entry
            cells = "  ".join(f"net@{bp/100:.2f}%={entry[f'net{bp:02d}']:+.1f}" for bp in BPS)
            print(f"  {sym:<9} {tf:<3} gross={entry['gross']:+.1f}R  n={entry['n']:>5}  {cells}")

    print("\n== 逐年（毛 R）==")
    for sym, data in out.items():
        for tf in ("1h", "4h", "1d"):
            cell = data[tf]
            if cell["skipped"] or not cell["trades"]:
                continue
            by_year = defaultdict(list)
            for t in cell["trades"]:
                by_year[year_of(t["entry_t"])].append(t["rr"])
            parts = "  ".join(f"{y}:{np.sum(v):+.1f}(n={len(v)})" for y, v in sorted(by_year.items()))
            print(f"  {sym:<9} {tf:<3} {parts}")

    print("\n== 单币合计/年化（固定注额毛口径；净@0.10%）==")
    for sym, data in out.items():
        merged = sorted([t for tf in TFS for t in data[tf]["trades"]], key=lambda x: x["entry_t"])
        gross = sum(t["rr"] for t in merged)
        net10 = net_sum(merged, 10)
        df1 = bc.load_df(sym, "1h")
        t_start = int(df1["time"].iloc[min(bc.CONF5["1h"]["warmup"], len(df1) - 1)])
        t_end = int(df1["time"].iloc[-1])
        span_y = (t_end - t_start) / YEAR_MS
        print(f"  {sym:<9} 合计毛 {gross:+.1f}R / {span_y:.2f}年 → 年化 {gross/span_y:+.1f}R/年 "
              f"（净@0.10% {net10/span_y:+.1f}R/年）")

    print("\n== 单币复利（f=1%、净@0.10%、单币独立资金流、事件账户、平仓时点记账）==")
    for sym, data in out.items():
        merged = sorted([t for tf in TFS for t in data[tf]["trades"]], key=lambda x: x["entry_t"])
        st = cb.compound(merged, 0.01, 0.0010)
        cagr_pct = (st["cagr"] - 1.0) * 100
        monthly = st["cagr"] ** (1.0 / 12.0)
        print(f"  {sym:<9} 期末 {st['multiple']:.1f}×  CAGR {st['cagr']:.4f}（{cagr_pct:+.1f}%/年）"
              f" 月化 ×{monthly:.3f}（{(monthly-1)*100:+.2f}%/月）  maxDD {st['maxdd']*100:.1f}%  n={st['n']}")

    print("\n== 九币正典换算（net05/net06 年化→总额，供加法用；span=4.94）==")
    pool9_tot = {}
    for tf, p in POOL9.items():
        pool9_tot[tf] = {"gross": p["gross"], "net10": p["net10"], "n": p["n"],
                         "net05": p["ann_net05"] * SPAN9_Y, "net06": p["ann_net06"] * SPAN9_Y}
        print(f"  九币 {tf}: gross={p['gross']:+.1f}R net10={p['net10']:+.1f}R "
              f"net05≈{pool9_tot[tf]['net05']:+.1f}R net06≈{pool9_tot[tf]['net06']:+.1f}R n={p['n']}")

    print("\n== 11 币池化加法汇总（九币=R50 正典[部署机/尾09-01] + LINK/ADA=本轮[本机/尾09-02]；"
          "容量串行按币独立 → ΣR 精确可加；固定注额口径）==")
    print(f"  {'周期':<4}{'11币毛':>10}{'年化毛':>9}{'净@0.05%':>10}{'净@0.06%':>10}{'净@0.10%':>10}{'保留%':>7}{'笔数':>8}")
    for tf in ("1h", "4h", "1d"):
        add = {k: sum(per[s][tf][k] for s in out if tf in per[s])
               for k in ["gross", "n"] + [f"net{bp:02d}" for bp in BPS]}
        g11 = pool9_tot[tf]["gross"] + add["gross"]
        n05 = pool9_tot[tf]["net05"] + add["net05"]
        n06 = pool9_tot[tf]["net06"] + add["net06"]
        n10 = pool9_tot[tf]["net10"] + add["net10"]
        n11 = pool9_tot[tf]["n"] + add["n"]
        print(f"  {tf:<4}{g11:>+9.1f}R {g11/SPAN9_Y:>+8.1f}% {n05:>+9.1f}R {n06:>+9.1f}R {n10:>+9.1f}R "
              f"{n10/g11*100:>6.0f}% {n11:>8}")
        print(f"        其中 LINK+ADA 贡献: 毛 {add['gross']:+.1f}R / 净@0.10% {add['net10']:+.1f}R / {add['n']} 笔")

    print(f"\n[total] {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()