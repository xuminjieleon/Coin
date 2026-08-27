"""A股 T+0 ETF 第二轮：扩池 + 隔夜外部风险闸门（2026-08-27，第二十一轮，预登记）。

用户pitch的原方案 = A(T+0 扩池+60m 提频) + B(隔夜外部因子)。**数据层探测修正**：
东财 push2his 的 60m K 线只有最近 ~128 根（end=过去日期返回空）——**60m 提频路线
死于数据可得性，如实放弃**；全球指数日线（NDX/SPX/N225/COMEX金，经 VPN 系统代理）
可达。本轮可测部分 = T+0 扩池（3→9 只，仍日线）+ 隔夜风险闸门。

**预登记协议（写于跑数前）**：
  - 池（9 只 T+0，驱动映射）：
      美股：513100 纳指(NDX)、513500 标普(SPX)、159941 纳指深(NDX)
      黄金：518880 黄金(GC00Y)、159934 黄金深(GC00Y)
      港股：513330 恒生互联网(HSTECH)、513180 恒生科技(HSTECH)、159920 恒生(HSI)
      日本：513520 日经(N225)
      驱动指数不可达的 ETF → 不闸（如实降级，不伪造）
  - 决策与执行：生产 1d 引擎+几何原封不动（同第十九轮），long-only、T+0 当日可止损、
    跳空按开盘成交、双边 0.06% 费——全部净口径
  - 变体：V0 = 纯扩池基线（无因子）；V1 = V0 + 隔夜闸门（驱动指数最近**已收盘**日
    收盘涨跌 < −1.0% → 该 ETF 当日不做多；美股/金用 D−1（北京 4am/2:30 收）、
    日本用 D 当日（15:00 前收）、港股用 D−1（16:00 收）——按"北京可用时刻"统一对齐）
  - 选择：池化决策点时间折 A 40%；V1 需在 A 段净 totalR 胜 V0 且守卫
    （净 EV≥+0.03R、filled≥V0 的 50%）；否则选 V0
  - 盲测：B+C 30%+30% 一次性，只对**选中变体**判验收：
      H1 盲段池化净 totalR > 0
      H2 盲段年化净 R ≥ 10R/年（对照：第十九轮 T+0 3只 ~5.5R/年；币圈 1d 家族 ~29R/年）
      H3 盲段最差 ETF 净 EV > −0.15R
    三关全过 = 方向成立；任一不过 = 如实记录不足
  - 敏感性（只报不选）：闸门阈值 −0.5% / −1.5%
  - 归因表（非验收）：旧 3 只 vs 新 6 只、V0 vs V1（拆扩池贡献与因子贡献）
  - §7.8：决策记录多进程每 ETF 一 worker；数据抓取幂等慢速（5s 间隔+退避，
    直连→VPN 代理双路由，逐条落盘）——东财对 burst 探测会掐连接（本轮实测）。
    **ETF 日线备胎**：东财单次快试失败 → 腾讯 ifzq.gtimg.cn fqkline 前复权自动
    顶上（按 end 日期翻页 800/页；2026-08-27 实测 sh513100 可达）；全球指数
    暂无已验证的腾讯代码，仍走东财双路由等封禁窗。新浪 60m（1023 根≈13 个月，
    datalen 上限 1023）已探明未接入——留作先导研究选项。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/t0_overnight.py [--fetch-only]
"""
import argparse
import asyncio
import os
import pickle
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from multiprocessing import Pool

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx
import numpy as np
import pandas as pd

import profit_sweep2 as ps
from profit2_r5 import with_loose_plans
from backtest_ltc import trade_stats
from backtest_ashare import (FILL_BARS, FEE_RT, load_records, sim_ashare,
                             year_of, fmt_res)
import ashare_data as ad

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ashare")

# T+0 pool: code -> (secid, name, driver_key)
T0_POOL = {
    "513100": ("1.513100", "纳指ETF", "NDX"),
    "513500": ("1.513500", "标普ETF", "SPX"),
    "159941": ("0.159941", "纳指ETF深", "NDX"),
    "518880": ("1.518880", "黄金ETF", "GOLD"),
    "159934": ("0.159934", "黄金ETF深", "GOLD"),
    "513330": ("1.513330", "恒生互联网ETF", "HSTECH"),
    "513180": ("1.513180", "恒生科技ETF", "HSTECH"),
    "159920": ("0.159920", "恒生ETF", "HSI"),
    "513520": ("1.513520", "日经ETF", "N225"),
}
# driver_key -> (secid, availability offset in Beijing hours after the index date 00:00)
# US indices close 04:00 Beijing next day; COMEX gold ~02:30 next day; Japan 14:45 same day;
# HK 16:00 same day. Offset conservative-rounded.
DRIVERS = {
    "NDX": ("100.NDX", 29.0),     # date D usable at D+1 05:00 Beijing
    "SPX": ("100.SPX", 29.0),
    "GOLD": ("101.GC00Y", 27.0),  # D+1 03:00
    "N225": ("100.N225", 15.5),   # D 15:30 same day
    "HSTECH": ("124.HSTECH", 17.0),  # D 17:00 same day (placeholder secid, verified at fetch)
    "HSI": ("100.HSI", 17.0),
}
GATE_TH = -0.010  # pre-registered overnight risk-off threshold

# union of pool A-share bar stamps (UTC-midnight convention), filled in main()
sorted_targets: list[int] = []


# ---------------- data layer (idempotent, slow, dual-route) ----------------

def _proxy() -> str | None:
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        enable, _ = winreg.QueryValueEx(k, "ProxyEnable")
        server, _ = winreg.QueryValueEx(k, "ProxyServer")
        if enable and server:
            s = server.split(";")[0]
            return s if "://" in s else f"http://{s}"
    except Exception:
        pass
    return None


async def _fetch_json(url, sem, attempts=5):
    """GET json, direct first then VPN sys-proxy, with backoff. Route-agnostic:
    no eastmoney-shape assumptions (caller validates)."""
    px = _proxy()
    last = None
    for attempt in range(attempts):
        for use_proxy in (False, True):
            if use_proxy and not px:
                continue
            try:
                async with sem:
                    if use_proxy:
                        async with httpx.AsyncClient(timeout=25, proxy=px) as c:
                            r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
                    else:
                        async with httpx.AsyncClient(timeout=25) as c:
                            r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
                return r.json()
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(min(60, 8 * (attempt + 1)))
    raise RuntimeError(last or "unreachable")


def _ts_of_date(s: str) -> int:
    return int(datetime.strptime(s[:10], "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def _row_of(date_s: str, o: float, c: float, h: float, l: float, v: float, a: float) -> tuple:
    """(ts, open, high, low, close, volume, amount) — ashare_data pickle schema.
    volume in 手 -> 股; tencent pages carry no amount -> estimate v*close (probe-only)."""
    return (_ts_of_date(date_s), o, h, l, c, v * 100.0, a)


def _parse_daily(data: dict) -> list[tuple]:
    rows = []
    for line in data.get("klines") or []:
        p = line.split(",")
        ts = _ts_of_date(p[0])
        # global index rows: date,open,close,high,low (no volume) OR with volume
        o, c = float(p[1]), float(p[2])
        h, l = float(p[3]), float(p[4])
        rows.append((ts, o, h, l, c))
    return rows


def _parse_etf_daily(data: dict) -> list[tuple]:
    rows = []
    for line in data.get("klines") or []:
        p = line.split(",")
        o, c, h, l = float(p[1]), float(p[2]), float(p[3]), float(p[4])
        v = float(p[5]) * 100.0
        a = float(p[6])
        rows.append((_ts_of_date(p[0]), o, h, l, c, v, a))
    return rows


def _tencent_symbol(secid: str) -> str:
    mkt, code = secid.split(".")
    return f"{'sh' if mkt == '1' else 'sz'}{code}"


async def fetch_etf_daily_tencent(secid: str, sem, target=9000) -> list[tuple]:
    """Tencent fqkline qfq daily, paged backwards by end-date (800/page).
    Verified 2026-08-27: sh513100 OK; eastmoney-burst-ban-proof fallback.
    Row: [date, open, close, high, low, volume(手), ...] (same col order as EM)."""
    sym = _tencent_symbol(secid)
    rows: dict[int, tuple] = {}
    end = ""
    for _page in range(20):  # hard cap 16000 bars
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={sym},day,,{end},{min(800, target)},qfq")
        j = await _fetch_json(url, sem, attempts=2)
        node = ((j or {}).get("data") or {}).get(sym) or {}
        arr = node.get("qfqday") or node.get("day") or []
        page_rows = []
        for it in arr:
            if not isinstance(it, (list, tuple)) or len(it) < 6:
                continue
            o, c, h, l, v = float(it[1]), float(it[2]), float(it[3]), float(it[4]), float(it[5])
            page_rows.append(_row_of(str(it[0]), o, c, h, l, v, v * 100.0 * c))
        if not page_rows:
            break
        for r in page_rows:
            rows[r[0]] = r
        earliest = min(r[0] for r in page_rows)
        if len(page_rows) < 300 or len(rows) >= target:
            break
        prev = datetime.fromtimestamp(earliest / 1000, tz=timezone.utc)
        end = (prev - timedelta(days=1)).strftime("%Y-%m-%d")  # dash dates REQUIRED (compact -> param error)
        await asyncio.sleep(5.0)
    return [rows[t] for t in sorted(rows)]


async def fetch_missing():
    """Fetch 6 new pool ETFs + 6 driver indices, one pickle each, resumable."""
    os.makedirs(DATA_DIR, exist_ok=True)
    sem = asyncio.Semaphore(1)
    jobs = []
    for code, (secid, name, _drv) in T0_POOL.items():
        f = os.path.join(DATA_DIR, f"{code}.pkl")
        if not os.path.exists(f):
            jobs.append(("etf", code, secid, name, f))
    for drv, (secid, _off) in DRIVERS.items():
        f = os.path.join(DATA_DIR, f"idx_{drv}.pkl")
        if not os.path.exists(f):
            jobs.append(("idx", drv, secid, "", f))
    em_alive = True  # flipped False on first eastmoney ETF failure this run
    for kind, key, secid, name, path in jobs:
        if kind == "idx":
            # indices remain eastmoney-only; skip while its burst-ban window is
            # active. NDX/GOLD already fall back to the local macro.db cache in
            # load_idx(), so this only affects SPX/N225/HSI/HSTECH (degraded
            # per pre-registration: ungated ETFs, loudly announced).
            print(f"[skip] {key}: 东财指数任务跳过（封禁窗；NDX/GOLD 走 macro.db 本地缓存）", flush=True)
            continue
        print(f"[fetch] {key} ({secid}) ...", flush=True)
        try:
            rows = []
            if kind == "etf":
                # eastmoney quick-try (1 pass; burst-ban -> immediate fallback)
                try:
                    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
                           f"&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=1&end=20500101&lmt=9000")
                    data = await _fetch_json(url, sem, attempts=1)
                    rows = _parse_etf_daily((data or {}).get("data") or {})
                    src = "eastmoney"
                    em_alive = True
                except Exception as exc:
                    em_alive = False
                    print(f"[fallback] {key} eastmoney failed ({exc}); trying tencent ...", flush=True)
                if not rows:
                    rows = await fetch_etf_daily_tencent(secid, sem)
                    src = "tencent"
            else:
                url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
                       f"&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55&klt=101&fqt=1&end=20500101&lmt=9000")
                data = await _fetch_json(url, sem)
                rows = _parse_daily((data or {}).get("data") or {})
                src = "eastmoney"
            if not rows:
                print(f"[fail] {key}: empty")
                continue
            with open(path + ".tmp", "wb") as f:
                pickle.dump({"name": name or key, "rows": rows, "source": src,
                             "fetched": datetime.now(timezone.utc).isoformat()}, f)
            os.replace(path + ".tmp", path)
            print(f"[ok] {key} {name} via {src}: {len(rows)} bars "
                  f"({datetime.fromtimestamp(rows[0][0]/1000, tz=timezone.utc).date()}..)", flush=True)
        except Exception as exc:
            print(f"[fail] {key}: {exc}", flush=True)
        await asyncio.sleep(5.0)


def load_idx(drv: str) -> pd.DataFrame | None:
    f = os.path.join(DATA_DIR, f"idx_{drv}.pkl")
    if os.path.exists(f):
        with open(f, "rb") as fh:
            entry = pickle.load(fh)
        df = pd.DataFrame(entry["rows"], columns=["time", "open", "high", "low", "close"])
        return df.sort_values("time").reset_index(drop=True)
    return _macro_driver_df(drv)


def _macro_driver_df(drv: str) -> pd.DataFrame | None:
    """NDX/GOLD from the LOCAL macro.db cache (zero network). Gate uses close
    only; o/h/l mirror close as placeholders. 2021-08+ coverage."""
    key = {"NDX": "ndx", "GOLD": "gold"}.get(drv)
    if not key:
        return None
    db = os.path.join(os.path.dirname(DATA_DIR), "cache", "macro.db")
    if not os.path.exists(db):
        return None
    import sqlite3
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT date, close FROM macro_series WHERE key=? ORDER BY date", (key,)
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    data = [(_ts_of_date(d), c, c, c, c) for d, c in rows]
    return pd.DataFrame(data, columns=["time", "open", "high", "low", "close"])


def overnight_map(drv: str) -> dict[int, float | None] | None:
    """A-share bar timestamp(ms, UTC-midnight-of-date convention) -> latest CLOSED
    driver daily close-to-close return available at the 15:00 Beijing decision.

    usable_ms = index-date UTC-midnight + availability offset (Beijing hours) - 8h.
    Both sequences ascending -> merge walk. None/nan = no driver info yet (no gate).
    """
    df = load_idx(drv)
    if df is None or len(df) < 30:
        return None
    off_ms = int(DRIVERS[drv][1] * 3600 * 1000)
    times = df["time"].to_numpy()
    closes = df["close"].to_numpy()
    usable = times + off_ms - 8 * 3600 * 1000
    rets = np.concatenate(([np.nan], closes[1:] / closes[:-1] - 1.0))
    out: dict[int, float | None] = {}
    j = -1
    for t in sorted_targets:
        dec_ms = t + 7 * 3600 * 1000  # 15:00 Beijing decision moment (UTC+8)
        while j + 1 < len(usable) and usable[j + 1] <= dec_ms:
            j += 1
        out[t] = float(rets[j]) if j >= 0 else None
    return out


def build_gate_maps(required: set[str]) -> dict[str, dict[int, float]]:
    """Driver -> overnight map. Per the ORIGINAL pre-registration: 驱动指数
    不可达的 ETF → 不闸（如实降级，不伪造）. Missing drivers are announced
    loudly; the affected ETFs run ungated inside V1 (V0 unaffected)."""
    maps = {}
    missing = []
    for drv in DRIVERS:
        m = overnight_map(drv)
        if m is None:
            missing.append(drv)
        maps[drv] = m or {}
    if missing:
        affected = sorted(c for c in T0_POOL if T0_POOL[c][2] in missing)
        print(f"[gate] 降级（预登记条款）: 驱动缺失 {missing}；以下 ETF 在 V1 中不闸: {affected}")
    return maps


# ---------------- backtest ----------------

GEO = (1.0, 1.2, 0.50, None, 12, 0.35)  # production 1d, unchanged


class T0Book:
    def __init__(self, code):
        self.code = code
        df = ad.load_df(code)
        self.times = df["time"].to_numpy()
        self.opens = df["open"].to_numpy()
        self.highs = df["high"].to_numpy()
        self.lows = df["low"].to_numpy()
        self.closes = df["close"].to_numpy()
        self.n = len(df)
        self.tidx = {int(t): k for k, t in enumerate(self.times)}
        self.records = load_records(code, df, False)
        self.driver = T0_POOL[code][2]


def run_book(book, gate_map, gate_th=None):
    recs = with_loose_plans(book.records, 10)
    trades = []
    n_orders = 0
    busy = -1
    for r in recs:
        if r.get("plan") != "long":
            continue
        i = book.tidx.get(r["time"])
        if i is None or i <= busy:
            continue
        if gate_th is not None and gate_map:
            g = gate_map.get(int(book.times[i]))
            if g is not None and g == g and g < gate_th:
                continue
        built = ps.build_plan(r, GEO[0], GEO[1])
        if built is None:
            continue
        _, entry, stop = built
        n_orders += 1
        out = sim_ashare(book.opens, book.highs, book.lows, book.closes, book.n,
                         i, entry, stop, GEO[2], GEO[4], FILL_BARS, GEO[5], True)
        if out is None:
            continue
        rr, fill, exit_bar = out
        busy = exit_bar
        risk = entry - stop
        fill_px = book.opens[fill] if book.opens[fill] <= entry else entry
        trades.append((int(book.times[fill]), rr - FEE_RT * fill_px / risk))
    return n_orders, trades


def year_span_days(t0, t1):
    return max(1.0, (t1 - t0) / 86400000.0)


_STATE = {"gate_maps": {}, "refresh": False}


def _init_worker():
    pass


def _worker(code):
    book = T0Book(code)
    return code, book.driver, book.records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch-only", action="store_true")
    args = ap.parse_args()
    asyncio.run(fetch_missing())
    if args.fetch_only:
        return

    # verify pool caches exist
    codes = [c for c in T0_POOL if os.path.exists(os.path.join(DATA_DIR, f"{c}.pkl"))]
    missing = [c for c in T0_POOL if c not in codes]
    if missing:
        print(f"[warn] 无缓存剔除: {missing}")

    # gate maps need global sorted target list = union of pool bar timestamps
    global sorted_targets
    tset = set()
    for c in codes:
        df = ad.load_df(c)
        tset.update(int(t) for t in df["time"].to_numpy())
    sorted_targets = sorted(tset)
    gate_maps = build_gate_maps({T0_POOL[c][2] for c in codes})

    with Pool(processes=min(9, len(codes))) as pool:
        books_raw = pool.map(_worker, codes, chunksize=1)

    pooled = sorted((r for _c, _d, recs in books_raw for r in recs), key=lambda r: r["time"])
    a = int(len(pooled) * 0.4)
    b = int(len(pooled) * 0.7)
    t_a, t_b = pooled[a]["time"], pooled[b]["time"]
    t_end = pooled[-1]["time"] + 1
    print(f"[folds] 决策点 {len(pooled)}；A 止于 {ps.fmt_ts(t_a)}，B 止于 {ps.fmt_ts(t_b)}")

    # build books in-process (records already computed in workers via cache)
    books = {c: T0Book(c) for c in codes}

    def run_variant(th):
        per = {}
        all_trades = []
        for c, book in books.items():
            gm = gate_maps.get(book.driver, {})
            n_o, tr = run_book(book, gm, th)
            per[c] = (n_o, tr)
            all_trades.extend(tr)
        all_trades.sort(key=lambda x: x[0])
        return per, all_trades

    def fold_trades(all_trades, fold):
        return [(t, r) for t, r in all_trades if fold[0] <= t < fold[1]]

    v0_per, v0_all = run_variant(None)
    v1_per, v1_all = run_variant(GATE_TH)

    st0_a = trade_stats(fold_trades(v0_all, (0, t_a)))
    st1_a = trade_stats(fold_trades(v1_all, (0, t_a)))
    print(f"\n-- A 段变体选择 --")
    print(f"  V0 扩池基线:      {fmt_res(st0_a)}")
    print(f"  V1 隔夜闸门(-1%): {fmt_res(st1_a)}")
    if st0_a.get("filled") == st1_a.get("filled") and st0_a.get("totalR") == st1_a.get("totalR"):
        print("  [注] NDX/GOLD 闸门覆盖自 2021-08 起 > A 段终点——A 段上 V1 与 V0 恒同，")
        print("       选择无法区分（结构性数据限制，如实声明）；隔夜闸门假设由盲段敏感性诊断观察")
    guard = (st1_a.get("filled", 0) >= st0_a.get("filled", 1) * 0.5
             and st1_a.get("ev", 0) >= 0.03)
    sel_v1 = guard and st1_a["totalR"] > st0_a["totalR"]
    print(f"  守卫(EV>=+0.03, filled>=50%): {'过' if guard else '不过'}；选择: {'V1' if sel_v1 else 'V0'}")

    sel_all = v1_all if sel_v1 else v0_all
    sel_per = v1_per if sel_v1 else v0_per

    blind = fold_trades(sel_all, (t_b, t_end))
    st_blind = trade_stats(blind)
    v0_blind = trade_stats(fold_trades(v0_all, (t_b, t_end)))

    print(f"\n{'='*86}\n===== B+C 盲测（一次性，选中变体）=====\n{'='*86}")
    print(f"  选中变体盲段: {fmt_res(st_blind)}")
    print(f"  V0 同段(对照): {fmt_res(v0_blind)}")

    span_days = year_span_days(blind[0][0], blind[-1][0]) if blind else 1.0
    per_etf_blind = {}
    for c in codes:
        tr = [(t, r) for _n, val in sel_per.items() if _n == c for t, r in val[1] if t_b <= t < t_end]
        st = trade_stats(tr)
        per_etf_blind[c] = st
    n_pos = sum(1 for s in per_etf_blind.values() if s.get("filled") and s["totalR"] > 0)
    n_traded = sum(1 for s in per_etf_blind.values() if s.get("filled"))
    worst_ev = min((s["ev"] for s in per_etf_blind.values() if s.get("filled")), default=float("nan"))
    annualized = st_blind["totalR"] / (span_days / 365.0) if st_blind.get("filled") else 0.0

    h1 = st_blind.get("filled", 0) > 0 and st_blind["totalR"] > 0
    h2 = annualized >= 10.0
    h3 = worst_ev == worst_ev and worst_ev > -0.15
    print(f"\n-- 预登记验收 --")
    print(f"  H1 盲段净totalR>0: {st_blind['totalR']:+.1f}R -> {'过' if h1 else '不过'}")
    print(f"  H2 年化净R>=10R/年: {annualized:+.1f}R/年（盲段 {span_days/365:.1f} 年）-> {'过' if h2 else '不过'}")
    print(f"  H3 最差ETF净EV>-0.15R: {worst_ev:+.3f}R -> {'过' if h3 else '不过'}")
    verdict = all((h1, h2, h3))
    print(f"  结论: {'方向成立' if verdict else '不成立——如实记录不足'}")

    # sensitivity (report only)
    print(f"\n-- 敏感性（只报不选）--")
    for th in (-0.005, -0.015):
        _, sall = run_variant(th)
        st = trade_stats(fold_trades(sall, (t_b, t_end)))
        print(f"  闸门 {th*100:.1f}%: 盲段 {fmt_res(st)}")

    # attribution
    print(f"\n-- 归因（全时段净 totalR）--")
    old3 = {"513100", "518880", "513330"}
    for tag, subset in (("旧3只(第十九轮同款)", old3), ("新6只", set(codes) - old3)):
        tr = [(t, r) for _n, val in sel_per.items() if _n in subset for t, r in val[1]]
        st = trade_stats(tr)
        print(f"  {tag}: {fmt_res(st)}")
    yt = defaultdict(list)
    for t, r in sel_all:
        yt[year_of(t)].append(r)
    print(f"\n-- 选中变体逐年（全时段）--")
    print("  " + "  ".join(f"{y}:{np.sum(v):+.1f}R(n={len(v)})" for y, v in sorted(yt.items())))
    print(f"\n-- 逐 ETF 盲段 --")
    for c in codes:
        st = per_etf_blind[c]
        drv = T0_POOL[c][2]
        print(f"  {c} {T0_POOL[c][1]:<8} 驱动={drv:<7} 闸={('有' if sel_v1 and gate_maps.get(drv) else '无')} {fmt_res(st)}")


if __name__ == "__main__":
    main()
