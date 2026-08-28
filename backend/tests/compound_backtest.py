"""Compounded (fixed-fractional) returns on the 5y production backtest (2026-08-28).

User question: 之前的收益率报告都是固定注额口径（1R=固定金额，总收益=ΣR，
线性、非复利）。如果复利——每笔交易风险=开仓时账户权益的固定比例 f——
期末是多少？

口径与 tests/fee_compare.py / direction_split.py 完全一致：
  - 四币 BTC/ETH/BNB/SOL × 5 年（2021-09~2026-08），第 13 轮生产几何，
    容量约束串行（同币同周期一仓），1h th=25 原生 / 4h·1d th=10 放宽；
  - 事件账户：开仓（成交 bar）按当时已实现权益快照风险 f×E；平仓 bar
    结算 P&L = f×E_entry×scale×(rr−feeR)，feeR=双边费率×entry/risk
    （fee_compare 同式）；费率场景=毛收益 / 净@双边0.10%（第二十三轮口径）；
  - 1h+4h 共享预算叠加沿用第二十二轮规则：同币两周期仓位时间重叠占比
    折半（scale=1−0.5×重叠占比）；全额叠加=各仓独立 f；
  - 非复利对照 = 1+f×Σscale×(rr−feeR)（同 f 固定注额的期末权益）；
  - 权益曲线仅在平仓事件更新：持仓期间浮亏（MAE）未计入回撤，真实
    回撤更深；
  - 1w 排除（第 11 轮起既定：样本过薄）。
  - §7.8：每 symbol 一个 spawn worker；worker 内单次 asyncio.run；
    记录缓存复用 _5y_cache_*（1d 为第八轮时代文件，本脚本内重算落盘）。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/compound_backtest.py
"""
import asyncio
import multiprocessing as mp
import os
import sys
import time
from datetime import datetime, timezone

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import profit_sweep2 as ps
from backtest_5y import SYMBOLS, W5, CONF5, load_records, sim_outcome_fast
from profit2_r5 import with_loose_plans

TFS = ("1h", "4h", "1d")
NEED_ITVS = ("1h", "4h", "1d")
FEE_NET = 0.0010          # 双边0.10%（单边0.05%，第二十三轮用户口径）
FRACS = (0.005, 0.01, 0.02)
YEAR_MS = 365.25 * 86400 * 1000


def capacity_trades(recs: list[dict], cfg: dict, df) -> list[dict]:
    """容量约束串行重放，保留每笔 entry_t/exit_t/rr/entry/risk/scale。"""
    depth, stopw, be_frac, tgt, texit, trail = tuple(cfg["geo"])
    fill_bars = max(1, int(round(cfg["fill_bars"] * cfg["fill_mult"])))
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    n = len(df)
    times = df["time"].to_numpy()
    tidx = {int(t): i for i, t in enumerate(times)}
    trades: list[dict] = []
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
        out = sim_outcome_fast(highs, lows, closes, n, i, direction, entry, stop,
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


def run_symbol(sym: str):
    try:
        dfs: dict = {}

        async def _fetch_all():
            for itv in NEED_ITVS:
                rows = await ps.kline_cache.get_klines(sym, itv, W5[itv])
                dfs[itv] = ps.kline_cache.rows_to_df(rows)

        asyncio.run(_fetch_all())
        out: dict = {}
        for tf in TFS:
            cfg = CONF5[tf]
            records = load_records(sym, tf, dfs, False)
            recs = records if cfg["th"] == 25 else with_loose_plans(records, cfg["th"])
            out[tf] = capacity_trades(recs, cfg, dfs[tf])
            print(f"[sim] {sym} {tf}: {len(out[tf])} trades", flush=True)
        return {sym: out}
    except SystemExit as exc:
        return {"error": f"{sym}: SystemExit {exc}"}
    except Exception as exc:
        import traceback
        return {"error": f"{sym}: {exc}\n{traceback.format_exc()}"}


def shared_budget_portfolio(data: dict) -> list[dict]:
    """第二十二轮规则：同币 1h+4h 仓位时间重叠 → 按重叠占比折半（返回副本）。"""
    out: list[dict] = []
    for sym in SYMBOLS:
        merged = sorted(data[sym]["1h"] + data[sym]["4h"], key=lambda t: t["entry_t"])
        ent = np.array([t["entry_t"] for t in merged], dtype=np.int64)
        ext = np.array([t["exit_t"] for t in merged], dtype=np.int64)
        for t in merged:
            span = t["exit_t"] - t["entry_t"]
            sc = 1.0
            if span > 0:
                lo = np.maximum(t["entry_t"], ent)
                hi = np.minimum(t["exit_t"], ext)
                ov = float(np.maximum(0, hi - lo).sum()) - span  # 去掉自身
                sc = 1.0 - 0.5 * min(1.0, ov / span)
            c = dict(t)
            c["scale"] = sc
            out.append(c)
    return out


def year_of(ms: int) -> int:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year


def compound(trades: list[dict], f: float, fee_rt: float, period_fn=None) -> dict:
    """事件账户：开仓快照权益、平仓结算 f×E_entry×scale×(rr−feeR)。
    period_fn(exit_ts)→期键（默认按年；可传月/季度键函数做分期拆解）。"""
    pfn = period_fn or (lambda ts: year_of(ts))
    if not trades:
        return None
    events = []
    for idx, t in enumerate(trades):
        events.append((t["entry_t"], 1, idx))
        events.append((t["exit_t"], 0, idx))
    events.sort(key=lambda e: (e[0], e[1]))  # 同时间戳：平仓先于开仓
    E = 1.0
    peak = 1.0
    maxdd = 0.0
    e_entry: dict = {}
    sum_net = 0.0
    cur_p = None
    p_start = 1.0
    periods: dict = {}
    for ts, kind, idx in events:
        t = trades[idx]
        if kind == 1:
            e_entry[idx] = E
            continue
        p = pfn(ts)
        if cur_p is None:
            cur_p = p
        elif p != cur_p:
            periods[cur_p] = E / p_start
            p_start = E
            cur_p = p
        rr_net = t["rr"] - fee_rt * t["entry_px"] / t["risk_px"]
        e0 = e_entry.pop(idx, None)
        if e0 is None:
            e0 = E  # 同 bar 进出（成交当根即触发止损）：入场快照=当前权益
        E += f * e0 * t["scale"] * rr_net
        sum_net += t["scale"] * rr_net
        if E > peak:
            peak = E
        elif peak > 0:
            dd = (peak - E) / peak
            if dd > maxdd:
                maxdd = dd
    if cur_p is not None:
        periods[cur_p] = E / p_start
    first_entry = min(t["entry_t"] for t in trades)
    last_exit = max(t["exit_t"] for t in trades)
    years = (last_exit - first_entry) / YEAR_MS
    cagr = E ** (1.0 / years) if years > 0 and E > 0 else float("nan")
    return {"n": len(trades), "multiple": E, "cagr": cagr, "maxdd": maxdd,
            "yearly": periods, "span_years": years,
            "sum_net": sum_net, "flat": 1.0 + f * sum_net}


def max_concurrent(trades: list[dict]) -> int:
    ev = []
    for t in trades:
        ev.append((t["entry_t"], 1))
        ev.append((t["exit_t"], -1))
    ev.sort(key=lambda e: (e[0], e[1]))
    cur = peak = 0
    for _, d in ev:
        cur += d
        peak = max(peak, cur)
    return peak


def fmt_x(v: float) -> str:
    if v is None or v != v:
        return "-"
    if v < 100:
        return f"{v:.2f}×"
    if v < 1e6:
        return f"{v:,.0f}×"
    return f"{v:.2e}×"


def fmt_cagr(m: float) -> str:
    if m is None or m != m:
        return "-"
    if m < 10:
        return f"+{(m - 1) * 100:.0f}%/年"
    return f"{m:.2f}×/年"


def fmt_yearly(v: float) -> str:
    if v is None or v != v:
        return "-"
    if v < 10:
        return f"{(v - 1) * 100:+.0f}%"
    return f"×{v:.1f}"


def main():
    t0 = time.time()
    ctx = mp.get_context("spawn")
    with ctx.Pool(len(SYMBOLS)) as pool:
        results = pool.map(run_symbol, SYMBOLS)
    data: dict = {}
    for res in results:
        if "error" in res:
            print(f"[worker-error]\n{res['error']}", flush=True)
        else:
            data.update(res)
    if len(data) != len(SYMBOLS):
        raise SystemExit("有 worker 失败，中止（部分结果不可汇总）")
    print(f"[pool] done in {time.time() - t0:.0f}s", flush=True)

    ports: dict = {}
    for tf in TFS:
        ports[f"仅{tf}"] = [t for sym in SYMBOLS for t in data[sym][tf]]
    ports["1h+4h全额"] = ports["仅1h"] + ports["仅4h"]
    ports["1h+4h+1d全额"] = ports["仅1h"] + ports["仅4h"] + ports["仅1d"]
    ports["1h+4h共享预算"] = shared_budget_portfolio(data)
    order = ["仅1h", "仅4h", "仅1d", "1h+4h全额", "1h+4h共享预算", "1h+4h+1d全额"]

    span_lo = min(t["entry_t"] for name in order for t in ports[name])
    span_hi = max(t["exit_t"] for name in order for t in ports[name])
    print(f"\n数据窗口: {ps.fmt_ts(span_lo)} .. {ps.fmt_ts(span_hi)}"
          f"（{(span_hi - span_lo) / YEAR_MS:.2f} 年）")
    print("\n组合交易数 / 毛ΣR（scale 后，对照既往报告口径）:")
    for name in order:
        gross = sum(t["scale"] * t["rr"] for t in ports[name])
        print(f"  {name:<14} n={len(ports[name]):>6}  毛ΣR={gross:+.1f}R"
              f"  峰值并发仓位数={max_concurrent(ports[name])}")

    for fee_rt, label in ((0.0, "毛收益"), (FEE_NET, "净@双边0.10%")):
        print(f"\n{'=' * 100}\n===== 复利 vs 非复利（{label}）=====\n{'=' * 100}")
        print(f"{'组合':<14}{'单笔风险':>8} {'复利期末':>12} {'年化':>10} "
              f"{'最大回撤':>9} {'非复利对照':>12}")
        for name in order:
            for f in FRACS:
                st = compound(ports[name], f, fee_rt)
                print(f"{name:<14}{f * 100:>7.1f}% {fmt_x(st['multiple']):>12} "
                      f"{fmt_cagr(st['cagr']):>10} {st['maxdd'] * 100:>8.1f}% "
                      f"{fmt_x(st['flat']):>12}")
            print()

    print(f"\n{'=' * 100}\n===== 分年复利收益率（f=1%，净@双边0.10%，平仓年份归年）=====\n{'=' * 100}")
    yearly_rows: dict = {}
    all_years: set = set()
    for name in order:
        st = compound(ports[name], 0.01, FEE_NET)
        yearly_rows[name] = st["yearly"]
        all_years |= set(st["yearly"])
    ys = sorted(all_years)
    print(f"{'组合':<14}" + "".join(f"{y:>10}" for y in ys))
    for name in order:
        cells = "".join(f"{fmt_yearly(yearly_rows[name].get(y, float('nan'))):>10}"
                        for y in ys)
        print(f"{name:<14}{cells}")

    st1 = compound(ports["仅1h"], 0.01, FEE_NET)
    st4 = compound(ports["仅4h"], 0.01, FEE_NET)
    print(f"\n示例：10,000 USDT 起步、单笔风险 1%、净@双边0.10%——"
          f"仅1h 期末 ≈ {st1['multiple'] * 10000:,.0f} USDT"
          f"（非复利 {st1['flat'] * 10000:,.0f}）；"
          f"仅4h 期末 ≈ {st4['multiple'] * 10000:,.0f} USDT"
          f"（非复利 {st4['flat'] * 10000:,.0f}）。")

    # ------------------------------------------- 月度/季度拆解（用户问"月化季化"）
    def month_key(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m")

    def quarter_key(ms: int) -> str:
        d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        return f"{d.year}Q{(d.month - 1) // 3 + 1}"

    def pstats(vals: list[float]) -> dict:
        a = np.array(vals, dtype=float)
        geo = float(np.exp(np.mean(np.log(a))))
        return {"n": len(a), "pos": float(np.mean(a > 1.0)),
                "med": float(np.median(a)), "geo": geo,
                "min": float(a.min()), "max": float(a.max())}

    print(f"\n{'=' * 100}\n===== 月度/季度复利收益率分布（f=1%，净@双边0.10%，平仓归期）=====\n{'=' * 100}")
    for name in ("1h+4h共享预算", "仅1d"):
        for label, fn in (("月度", month_key), ("季度", quarter_key)):
            st = compound(ports[name], 0.01, FEE_NET, fn)
            pst = pstats(list(st["yearly"].values()))
            print(f"  {name:<12} {label}: 期数={pst['n']:<3} 正占比={pst['pos']*100:>4.0f}%  "
                  f"中位={fmt_yearly(pst['med']):>8}  几何均值={fmt_yearly(pst['geo']):>8}  "
                  f"最差={fmt_yearly(pst['min']):>8}  最好={fmt_yearly(pst['max']):>8}")
        st = compound(ports[name], 0.01, FEE_NET)
        m = st["cagr"] ** (1.0 / 12.0)
        q = st["cagr"] ** (1.0 / 4.0)
        print(f"  {'':<12} 平滑折算（由 CAGR {fmt_cagr(st['cagr'])} 代数换算）: "
              f"月化 ×{m:.2f}（+{(m-1)*100:.0f}%）/ 季化 ×{q:.2f}（+{(q-1)*100:.0f}%）\n")

    st_m = compound(ports["1h+4h共享预算"], 0.01, FEE_NET, month_key)
    months = st_m["yearly"]
    years_m = sorted({int(k[:4]) for k in months})
    print("  1h+4h共享预算 月度收益率矩阵（f=1%，净；期初/期末不完整月照常计入）：")
    print(f"  {'年份':<6}" + "".join(f"{m:>9}月" for m in range(1, 13)))
    for y in years_m:
        cells = ""
        for m in range(1, 13):
            k = f"{y}-{m:02d}"
            cells += f"{fmt_yearly(months[k]):>10}" if k in months else f"{'—':>10}"
        print(f"  {y:<6}{cells}")

    # ------------------------- 权益轨迹与年化节奏（叙述层数字的机器来源，报告 §5.4 对照）
    # 教训（第二十九轮勘误）：报告中的轨迹/排名等叙述数字曾由心算换算出错（10×滑位/排名错位），
    # 此段把这类数字全部变成脚本输出，写文档只准抄这里。
    shared_tr = ports["1h+4h共享预算"]
    stop_pct = float(np.median([t["risk_px"] / t["entry_px"] for t in shared_tr]))
    notional_mult = 0.01 / stop_pct
    months_by_year: dict = {}
    for k in months:
        months_by_year.setdefault(int(k[:4]), []).append(k)
    print(f"\n{'=' * 100}\n===== 1万 USDT 起步权益轨迹（1h+4h共享预算，f=1%，净@双边0.10%）=====\n{'=' * 100}")
    print(f"止损距离中位 {stop_pct*100:.2f}% → 单仓名义 ≈ {notional_mult:.2f}×权益")
    E = 10000.0
    yearly_st = compound(ports["1h+4h共享预算"], 0.01, FEE_NET)["yearly"]
    paces = []
    for y in sorted(yearly_st):
        m = yearly_st[y]
        E *= m
        n_m = len(months_by_year.get(y, []))
        pace = m ** (12.0 / max(1, n_m)) if m > 0 else float("nan")
        paces.append((pace, y))
        print(f"  {y}（{n_m}个月）: 年内 ×{m:.1f} → 年末权益 ≈ {E:,.0f} USDT"
              f"，单仓名义 ≈ {notional_mult*E:,.0f} USDT（年化节奏 ×{pace:.0f}）")
    print("  年化节奏排名: " + " > ".join(f"{y} ×{p:.0f}" for p, y in sorted(paces, reverse=True)))

    print(f"""
注（诚实口径）：
1. 复利口径=每笔按开仓时已实现权益的 f 提交风险、平仓结算滚入权益（"1R=当前权益的{FRACS[1]*100:.0f}%"纪律）；
   非复利对照=同 f 固定注额（既往报告 ΣR 口径），差异=赢后注变大/输后注变小的几何效应。
2. 表内数字是回测模型内的数学结果，不是可实现承诺：增长率对费率与 EV 近似线性敏感（第二十三轮），
   且未建模容量/市场冲击（复投后期名义额指数膨胀）、参数漂移、黑天鹅（回测亏损截断于 −1R 无跳空穿仓）；
   f 越大对模型误差越脆弱，不建议按表内趋势外推更大 f。
3. 聚合风险：瞬时最大理论亏损≈峰值并发仓位数×f（仅1h 峰值4仓=4%f；1h+4h全额 峰值7仓=7%f；1h+4h+1d 峰值9仓=9%f）。
4. 权益曲线仅在平仓时点更新：持仓中浮亏（MAE）未计入回撤，真实回撤更深；未计资金费率 carry（空单历史为正贡献）。
5. 2021/2026 为不完整年（窗口 2021-09 起）；1w 样本过薄排除（第 11 轮起既定口径）。""")
    print(f"\n总耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
