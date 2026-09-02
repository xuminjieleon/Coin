"""8 币入列表落地（第五十三轮拍板：全加）——19 币加法汇总机器输出。

用户拍板（2026-09-02）：过门 8 币 AAVE/NEAR/AVAX/XLM/BCH/FIL/UNI/ARB 全部加入
推送列表（11→19 币）。本脚本为 BACKTEST §2/§3 增补提供全部机器输出（§7.9）：
- 8 币逐周期毛/净（@0.05/0.06/0.10%）、笔数、逐年合计；
- 单币合计/年化/月均/非亏损率/净年化/单币复利（f=1%、净@0.10%）；
- 19 币池化加法汇总 = 11 币（第五十二轮机器输出，BACKTEST §2.2/§3 常数直抄）
  + 8 币（本轮本机，_cand_cache_* 命中重建）；容量串行按币独立 → ΣR 精确可加；
  年化按"各币年化相加"口径（8 币窗口 3.39~4.94 年不一，不强行同除一个 span）。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/_add_top20.py
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
from backtest_ltc import trade_stats

YEAR_MS = 365.25 * 86400 * 1000
TFS = ("1h", "4h", "1d", "1w")
BPS = (5, 6, 10)
NEW8 = ["AAVEUSDT", "NEARUSDT", "AVAXUSDT", "XLMUSDT", "BCHUSDT",
        "FILUSDT", "UNIUSDT", "ARBUSDT"]

# 11 币基数（第五十二轮机器输出，BACKTEST §2.2/§3 直抄；锚差 1 天如实标注）
# gross=R 总额，ann/netXX=年化 R/年（净@XX 双边费率），n=笔数
POOL11 = {
    "1h": {"gross": 4970.0, "ann": 1006.0, "net05": 805.0, "net06": 765.0, "net10": 605.0, "n": 39078},
    "4h": {"gross": 3832.0, "ann": 808.0, "net05": 760.0, "net06": 751.0, "net10": 713.0, "n": 9318},
    "1d": {"gross": 523.0, "ann": 136.0, "net05": 132.0, "net06": 132.0, "net10": 130.0, "n": 1404},
}
# 1w 加法用（九币=第五十轮正典 +72.8R；LINK/ADA=第五十二轮明细 +2.6/+1.4）
W1_9COIN, W1_LINK, W1_ADA = 72.8, 2.6, 1.4


def year_of(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year


def net_sum(trades, bp):
    fee = bp * 1e-4
    return sum(t["rr"] - fee * t["entry_px"] / t["risk_px"] for t in trades)


def coin_span_y(sym):
    df1 = bc.load_df(sym, "1h")
    t_start = int(df1["time"].iloc[min(bc.CONF5["1h"]["warmup"], len(df1) - 1)])
    t_end = int(df1["time"].iloc[-1])
    return (t_end - t_start) / YEAR_MS


def main():
    t0 = time.time()
    out = {}
    for sym in NEW8:
        res = bc.worker(sym)
        if "error" in res:
            print(f"[worker-error] {res['sym']}\n{res['error']}")
            raise SystemExit(1)
        out[sym] = res["data"]
        print(f"[trades] {sym} rebuilt", flush=True)

    print("\n== 8 币逐周期 毛/净/笔数（本机档，1h=下界）==")
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
            print(f"  {sym:<9} {tf:<3} gross={entry['gross']:+.1f}R n={entry['n']:>5}  {cells}")

    print("\n== 8 币池化逐年（毛 R 合计）==")
    for tf in ("1h", "4h", "1d"):
        by_year = defaultdict(float)
        cnt = defaultdict(int)
        for sym, data in out.items():
            cell = data[tf]
            if cell["skipped"] or not cell["trades"]:
                continue
            for t in cell["trades"]:
                by_year[year_of(t["entry_t"])] += t["rr"]
                cnt[year_of(t["entry_t"])] += 1
        parts = "  ".join(f"{y}:{by_year[y]:+.1f}(n={cnt[y]})" for y in sorted(by_year))
        print(f"  8币 {tf:<3} {parts}")

    print("\n== 单币合计/年化/月均/非亏损率/净年化（固定注额毛口径；净@0.10%）==")
    print(f"  {'symbol':<10}{'窗口年':>7}{'累计R':>9}{'年化R':>8}{'月均R':>8}{'净年化':>8}"
          f"{'非亏损率 1h/4h/1d':>22}")
    comp_rows = []
    for sym in NEW8:
        data = out[sym]
        merged = sorted([t for tf in TFS for t in data[tf]["trades"]], key=lambda x: x["entry_t"])
        gross = sum(t["rr"] for t in merged)
        net10 = net_sum(merged, 10)
        span_y = coin_span_y(sym)
        ann = gross / span_y
        mon = gross / (span_y * 12.0)
        nl = []
        for tf in ("1h", "4h", "1d"):
            cell = data[tf]
            st = bc.stats_of(cell["trades"]) if cell["trades"] else {}
            nl.append(f"{st.get('nonloss', 0)*100:.1f}" if st else "—")
        print(f"  {sym:<10}{span_y:>6.2f}y{gross:>+8.1f}{ann:>+8.1f}{mon:>+8.2f}"
              f"{net10/span_y:>+7.1f}   {' / '.join(nl)}")
        st_c = cb.compound(merged, 0.01, 0.0010)
        monthly = st_c["cagr"] ** (1.0 / 12.0)
        comp_rows.append((sym, st_c, monthly))
        print(f"           复利 f=1% 净@0.10%: 期末 {st_c['multiple']:.1f}× "
              f"CAGR {(st_c['cagr']-1)*100:+.1f}%/年 月化 {(monthly-1)*100:+.2f}%/月 "
              f"maxDD {st_c['maxdd']*100:.1f}% n={st_c['n']}")

    print("\n== 19 币池化加法汇总（11 币=R52 机器输出常数 + 8 币=本轮；ΣR 精确可加；固定注额）==")
    print(f"  {'周期':<4}{'19币毛R':>10}{'19币年化R':>10}{'净@0.05%':>10}{'净@0.06%':>10}"
          f"{'净@0.10%':>10}{'保留%':>7}{'笔数':>8}")
    for tf in ("1h", "4h", "1d"):
        g8 = sum(per[s][tf]["gross"] for s in NEW8 if tf in per[s])
        n_8 = sum(per[s][tf]["n"] for s in NEW8 if tf in per[s])
        ann8 = sum(per[s][tf]["gross"] / coin_span_y(s) for s in NEW8 if tf in per[s])
        ann8_net = {}
        for bp in BPS:
            ann8_net[bp] = sum(per[s][tf][f"net{bp:02d}"] / coin_span_y(s)
                               for s in NEW8 if tf in per[s])
        g19 = POOL11[tf]["gross"] + g8
        ann19 = POOL11[tf]["ann"] + ann8
        net19 = {bp: POOL11[tf][f"net{bp:02d}"] + ann8_net[bp] for bp in BPS}
        n19 = POOL11[tf]["n"] + n_8
        print(f"  {tf:<4}{g19:>+9.1f} {ann19:>+9.1f} {net19[5]:>+9.1f} {net19[6]:>+9.1f} "
              f"{net19[10]:>+9.1f} {net19[10]/ann19*100:>6.0f}% {n19:>8}")
        print(f"        其中 8 币贡献: 毛 {g8:+.1f}R / 年化 {ann8:+.1f} / 净@0.10% {ann8_net[10]:+.1f} / {n_8} 笔")

    w1_8 = sum(per[s]["1w"]["gross"] for s in NEW8 if "1w" in per[s])
    w1_19 = W1_9COIN + W1_LINK + W1_ADA + w1_8
    print(f"  1w  19币 ≈ {w1_19:+.1f}R（九币 {W1_9COIN:+.1f} + LINK/ADA {W1_LINK+W1_ADA:+.1f} + 8币 {w1_8:+.1f}；样本薄仅参考）")

    print(f"\n[total] {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
