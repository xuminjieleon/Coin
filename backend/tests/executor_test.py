"""Executor state-machine unit tests (paper broker, no network, no keys).

Run:  python tests/executor_test.py   (from backend/)

White-box harness: patches context.run_analysis (shared closed-bar analysis,
round 55 — same entry point the notifier uses), binance.get_klines
(synthetic 1m feed = the paper fill source), binance.get_exchange_info
(fixed LOT/PRICE/MIN_NOTIONAL filters) and kline_cache.get_klines (trail
bars). Every scenario asserts the SAME management math the journal replay
uses (stop -> +beR half -> stop-to-entry -> trail -> time exit), so these
tests double as the parity check between the live executor and the
backtest semantics. Round 55 adds T8-T13: reconcile unlock, mode-switch
fencing, panic isolation, placement-probe recovery, live-equity refusal,
and a journal-replay parity anchor.
"""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from services import binance, binance_trade, executor, journal_store, kline_cache  # noqa: E402
from services.analysis import context  # noqa: E402

PASS = 0


# ----------------------------------------------------------------- mocks

FEED: dict[str, list] = {}        # symbol -> 1m bars [ts,o,h,l,c,v,tb]
KFEED: dict[tuple, list] = {}     # (symbol, interval) -> rows for trail
PLAN: dict[tuple, dict | None] = {}  # (symbol, interval) -> tradePlan or None

AS_OF_AT = {}


async def mock_run_analysis(symbol, interval, limit=500, as_of=None):
    AS_OF_AT[(symbol, interval)] = as_of
    plan = PLAN.get((symbol, interval))
    return {"summary": {"tradePlan": plan}, "candles": [{"time": as_of}]}


async def mock_get_klines(symbol, interval, limit=500, cache_ttl=0, end_time=None):
    bars = FEED.get(symbol, [])
    return bars[-limit:] if bars else []


async def mock_get_exchange_info():
    def filters(step, tick, min_n):
        return {"symbols": [{"symbol": "BTCUSDT", "filters": [
            {"filterType": "LOT_SIZE", "stepSize": step},
            {"filterType": "PRICE_FILTER", "tickSize": tick},
            {"filterType": "MIN_NOTIONAL", "notional": min_n},
        ]}, {"symbol": "ETHUSDT", "filters": [
            {"filterType": "LOT_SIZE", "stepSize": step},
            {"filterType": "PRICE_FILTER", "tickSize": tick},
            {"filterType": "MIN_NOTIONAL", "notional": min_n},
        ]}]}
    return filters("0.001", "0.1", "5")


async def mock_cache_klines(symbol, interval, limit, end_time=None):
    rows = KFEED.get((symbol, interval), [])
    out = [r for r in rows if end_time is None or r[0] <= end_time]
    return out[-limit:]


def bar(minutes_ago, o, h, low, c):
    now = int(time.time() * 1000)
    ts = (now // 60_000) * 60_000 - minutes_ago * 60_000
    return (ts, o, h, low, c, 10.0, 5.0)


# ----------------------------------------------------------------- setup

TMP = Path(tempfile.mkdtemp(prefix="executor_test_"))


def reset_env(symbols, intervals, **cfg_over):
    global PASS
    if executor._conn is not None:
        executor._conn.close()
        executor._conn = None
    for f in TMP.glob("executor_test.db*"):
        f.unlink()
    executor.DB_PATH = TMP / "executor_test.db"
    executor.CFG_PATH = TMP / "executor_test.json"
    executor._cfg.update({
        "enabled": True, "dryRun": True, "testnet": True,
        "symbols": symbols, "intervals": intervals,
        "riskPct": 1.0, "equityUsd": 10000.0,
        "maxConcurrent": 8, "dailyLossLimitR": 0,
        "postOnlyEntry": True, "pushEvents": False,
        "instance": "test", "leverage": 5,
        "maxNotionalPctPer": 100.0, "maxGrossNotionalPct": 1000.0,
    })
    executor._cfg.update(cfg_over)
    executor._warned.clear()
    executor._reconciled = True
    executor._mode = "paper"
    executor._broker = executor.PaperBroker()
    executor._last_error = None
    executor._save_cfg()  # keep status() re-reads consistent with test cfg
    FEED.clear()
    KFEED.clear()
    PLAN.clear()


def set_plan(sym, itv, entry=100.0, stop=98.0, direction="long", be_r=0.15,
             target_r=0.5, trail_r=None, texit=96, fill_bars=24):
    risk = abs(entry - stop)
    be_t = entry + be_r * risk if direction == "long" else entry - be_r * risk
    t1 = None
    if target_r is not None:
        t1 = entry + target_r * risk if direction == "long" else entry - target_r * risk
    PLAN[(sym, itv)] = {
        "direction": direction, "entry": entry, "stop": stop,
        "target1": t1, "beTrigger": be_t, "beR": be_r, "targetR": target_r,
        "trailR": trail_r, "texitBars": texit, "fillBars": fill_bars,
    }


def one_pos(key):
    return executor._get_pos(key)


def check(name, cond, detail=""):
    global PASS
    if not cond:
        raise AssertionError(f"[FAIL] {name}: {detail}")
    PASS += 1
    print(f"  ok {name}")


# ----------------------------------------------------------------- tests

async def test_1h_full_lifecycle():
    print("T1 1h 计划全生命周期：入场→减半保本→保本止损")
    reset_env(["BTCUSDT"], ["1h"])
    set_plan("BTCUSDT", "1h", entry=100.0, stop=98.0, be_r=0.15, target_r=0.5)
    # bars are appended in ASCENDING time (real kline order); minutes_ago shrinks
    FEED["BTCUSDT"] = [bar(5, 104.0, 104.0, 103.0, 103.5)]  # current price > entry

    await executor._plan_tick()
    pos = one_pos("BTCUSDT|1h")
    check("挂单创建", pos is not None and pos["state"] == "pending", str(pos))
    check("qty=50 (1%×10000 / risk2)", abs(pos["qty"] - 50.0) < 1e-9, str(pos["qty"]))
    check("入场单在场", bool(pos["entry_coid"]) and not pos["awaiting_entry"])

    FEED["BTCUSDT"].append(bar(4, 103.0, 103.2, 102.5, 102.8))  # nothing
    await executor._mgmt_tick()
    check("未回踩不成交", one_pos("BTCUSDT|1h")["state"] == "pending")

    FEED["BTCUSDT"].append(bar(3, 102.8, 103.0, 99.8, 100.2))   # touches entry 100
    await executor._mgmt_tick()
    pos = one_pos("BTCUSDT|1h")
    check("入场成交→open", pos["state"] == "open", str(pos))
    check("成交均价100", abs(pos["avg_price"] - 100.0) < 1e-9)
    check("保本/目标单已挂", bool(pos["be_coid"]) and bool(pos["tgt_coid"]))

    FEED["BTCUSDT"].append(bar(2, 100.2, 100.6, 100.0, 100.5))  # touches be 100.3
    await executor._mgmt_tick()
    pos = one_pos("BTCUSDT|1h")
    check("be成交", pos["be_done"] and abs(pos["be_qty"] - 25.0) < 1e-9, str(pos))
    check("止损移至入场价", abs(pos["stop"] - 100.0) < 1e-9, str(pos["stop"]))

    FEED["BTCUSDT"].append(bar(1, 100.5, 100.6, 99.8, 99.9))    # stops at 100
    await executor._mgmt_tick()
    pos = executor._get_pos_by_id(pos["id"])
    check("be_stop 平仓", pos["state"] == "closed" and pos["exit_reason"] == "be_stop",
          str(pos))
    # R = 0.5×0.15 (be leg @100.3) + 0.5×0 (runner @100) = +0.075
    check("R=+0.075 (回放口径)", abs((pos["r_multiple"] or 0) - 0.075) < 1e-3,
          str(pos["r_multiple"]))
    day = time.strftime("%Y-%m-%d", time.gmtime())
    check("当日R记账", abs(float(executor._meta_get(f"dayR|{day}", "0")) - 0.075) < 1e-3)


async def test_4h_trail_lifecycle():
    print("T2 4h 跟踪止损：入场→减半保本→跟踪收紧→be_stop")
    reset_env(["BTCUSDT"], ["4h"])
    set_plan("BTCUSDT", "4h", entry=100.0, stop=98.0, be_r=0.75,
             target_r=None, trail_r=0.35, texit=48, fill_bars=18)
    FEED["BTCUSDT"] = [bar(5, 104.0, 104.0, 103.0, 103.5)]

    await executor._plan_tick()
    pos = one_pos("BTCUSDT|4h")
    check("挂单创建", pos is not None and pos["state"] == "pending")

    FEED["BTCUSDT"].append(bar(4, 102.8, 103.0, 99.8, 100.2))   # entry
    await executor._mgmt_tick()
    pos = one_pos("BTCUSDT|4h")
    check("入场成交", pos["state"] == "open" and abs(pos["avg_price"] - 100.0) < 1e-9)
    check("4h无固定目标单", pos["tgt_coid"] is None)

    FEED["BTCUSDT"].append(bar(3, 100.2, 102.0, 100.0, 101.8))  # be 101.5
    await executor._mgmt_tick()
    pos = one_pos("BTCUSDT|4h")
    check("be成交", pos["be_done"])
    check("止损→入场", abs(pos["stop"] - 100.0) < 1e-9)

    # pretend the be happened two 4h bars ago and walk closed bars:
    step = 4 * 3_600_000
    now = int(time.time() * 1000)
    be_bar = (now // step) * step - 2 * step
    executor._upd(pos["id"], be_fill_at=now - 2 * step)
    KFEED[("BTCUSDT", "4h")] = [
        (be_bar, 100.0, 101.8, 99.9, 101.5, 10.0, 5.0),
        (be_bar + step, 101.5, 103.0, 101.2, 102.5, 10.0, 5.0),
    ]
    await executor._mgmt_tick()
    pos = one_pos("BTCUSDT|4h")
    # trail = max(entry, MFE 103 − 0.35×risk2=102.3)
    check("跟踪收紧到102.3", abs(pos["stop"] - 102.3) < 0.11, str(pos["stop"]))

    FEED["BTCUSDT"].append(bar(2, 102.4, 102.4, 102.0, 102.1))  # stop 102.3 hit
    await executor._mgmt_tick()
    pos = executor._get_pos_by_id(pos["id"])
    check("be_stop 平仓", pos["state"] == "closed" and pos["exit_reason"] == "be_stop")
    # R = 0.5×0.75 (be @101.5) + 0.5×(102.3−100)/2 = 0.375 + 0.575 = +0.95
    check("R=+0.95", abs((pos["r_multiple"] or 0) - 0.95) < 1e-3, str(pos["r_multiple"]))


async def test_time_exit():
    print("T3 时间退出")
    reset_env(["BTCUSDT"], ["1h"])
    set_plan("BTCUSDT", "1h", entry=100.0, stop=98.0, be_r=0.15, target_r=0.5, texit=2)
    FEED["BTCUSDT"] = [bar(5, 104.0, 104.0, 103.0, 103.5)]
    await executor._plan_tick()
    FEED["BTCUSDT"].append(bar(4, 103.0, 103.0, 99.9, 101.0))   # entry, no be/stop
    await executor._mgmt_tick()
    pos = one_pos("BTCUSDT|1h")
    check("入场成交", pos["state"] == "open")
    step = 3_600_000
    now = int(time.time() * 1000)
    executor._upd(pos["id"], opened_at=now - 2 * step)  # pretend opened 2h ago
    FEED["BTCUSDT"].append(bar(3, 100.2, 100.25, 100.1, 100.15))  # flat, no levels
    await executor._mgmt_tick()
    pos = executor._get_pos_by_id(pos["id"])
    check("时间退出", pos["state"] == "closed" and pos["exit_reason"] == "time", str(pos))
    # R = (100.15−100)/2 = +0.075
    check("R=+0.075", abs((pos["r_multiple"] or 0) - 0.075) < 1e-3, str(pos["r_multiple"]))


async def test_plan_gone_and_amend():
    print("T4 计划消失撤单 / 改单换价 / 转向")
    reset_env(["BTCUSDT"], ["1h"])
    set_plan("BTCUSDT", "1h")
    FEED["BTCUSDT"] = [bar(1, 104.0, 104.0, 103.0, 103.5)]
    await executor._plan_tick()
    check("挂单存在", one_pos("BTCUSDT|1h") is not None)

    PLAN[("BTCUSDT", "1h")] = None  # plan gone
    await executor._plan_tick()
    pos = executor._get_pos_by_id(one_pos("BTCUSDT|1h")["id"]) if one_pos("BTCUSDT|1h") else None
    if pos is None:  # row closed → refetch by scanning
        with executor._lock:
            cur = executor._db().execute(
                "SELECT id FROM positions WHERE key='BTCUSDT|1h' ORDER BY id DESC LIMIT 1")
            pid = cur.fetchone()[0]
        pos = executor._get_pos_by_id(pid)
    check("消失→已撤", pos["state"] == "closed" and pos["exit_reason"] == "plan_gone",
          str(pos))
    check("撤单后无活跃仓", one_pos("BTCUSDT|1h") is None)

    set_plan("BTCUSDT", "1h")  # back
    await executor._plan_tick()
    set_plan("BTCUSDT", "1h", entry=106.0, stop=104.0)  # 6% drift > 0.5% & >40% risk
    await executor._plan_tick()
    pos = one_pos("BTCUSDT|1h")
    check("改单→新价挂单", pos is not None and abs(pos["entry"] - 106.0) < 1e-9, str(pos))

    set_plan("BTCUSDT", "1h", entry=106.0, stop=104.0, direction="short")
    await executor._plan_tick()
    pos = one_pos("BTCUSDT|1h")
    check("转向→空头挂单", pos is not None and pos["direction"] == "short", str(pos))


async def test_guards():
    print("T5 护栏：并发上限 / 同币跨周期共享预算减半")
    reset_env(["BTCUSDT", "ETHUSDT"], ["1h"], maxConcurrent=1)
    set_plan("BTCUSDT", "1h")
    set_plan("ETHUSDT", "1h")
    FEED["BTCUSDT"] = [bar(1, 104.0, 104.0, 103.0, 103.5)]
    FEED["ETHUSDT"] = [bar(1, 104.0, 104.0, 103.0, 103.5)]
    await executor._plan_tick()
    act = executor._active_positions()
    check("并发上限=1", len(act) == 1 and act[0]["symbol"] == "BTCUSDT", str(act))

    reset_env(["BTCUSDT"], ["1h", "4h"])
    set_plan("BTCUSDT", "1h")
    set_plan("BTCUSDT", "4h", be_r=0.75, target_r=None, trail_r=0.35, texit=48, fill_bars=18)
    FEED["BTCUSDT"] = [bar(1, 104.0, 104.0, 103.0, 103.5)]
    await executor._plan_tick()
    p1 = one_pos("BTCUSDT|1h")
    p4 = one_pos("BTCUSDT|4h")
    check("两周期都挂单", p1 is not None and p4 is not None)
    check("共享预算：4h 减半 qty=25", abs(p4["qty"] - 25.0) < 1e-9, str(p4["qty"]))
    check("1h 全额 qty=50", abs(p1["qty"] - 50.0) < 1e-9, str(p1["qty"]))


async def test_post_only_retry():
    print("T6 post-only 拒绝与重挂")
    reset_env(["BTCUSDT"], ["1h"])
    set_plan("BTCUSDT", "1h")
    FEED["BTCUSDT"] = [bar(2, 99.0, 99.5, 98.5, 99.0)]  # price already below entry
    await executor._plan_tick()
    pos = one_pos("BTCUSDT|1h")
    check("GTX被拒→awaiting", pos is not None and pos["awaiting_entry"], str(pos))
    check("止损单仍保护在场", bool(pos["stop_coid"]))

    FEED["BTCUSDT"].append(bar(1, 99.0, 103.6, 99.0, 103.5))  # price back above
    await executor._mgmt_tick()
    pos = one_pos("BTCUSDT|1h")
    check("价格回升→重挂成功", pos is not None and not pos["awaiting_entry"]
          and bool(pos["entry_coid"]), str(pos))


async def test_daily_pause():
    print("T7 日亏停机")
    reset_env(["BTCUSDT"], ["1h"], dailyLossLimitR=6.0)
    day = time.strftime("%Y-%m-%d", time.gmtime())
    executor._meta_set(f"dayR|{day}", "-7.0")
    set_plan("BTCUSDT", "1h")
    FEED["BTCUSDT"] = [bar(1, 104.0, 104.0, 103.0, 103.5)]
    await executor._plan_tick()
    check("日亏超限不开新仓", one_pos("BTCUSDT|1h") is None)
    check("状态显示 paused", executor.status()["paused"] is True)


async def test_reconcile_unlock():
    print("T8 空库对账解锁（testnet 首启死锁修复 C2）")
    reset_env(["BTCUSDT"], ["1h"])
    keep_mode, keep_posmode, keep_open_orders = (
        binance_trade.position_mode, binance_trade.open_orders,
        binance_trade.position_mode)  # noqa: F841
    orig_posmode = binance_trade.position_mode
    orig_open = binance_trade.open_orders

    async def fake_posmode():
        return False  # one-way

    async def fake_open(symbol=None):
        return []

    binance_trade.position_mode = fake_posmode
    binance_trade.open_orders = fake_open
    try:
        executor._cfg.update({"dryRun": False, "testnet": True,
                              "apiKey": "k", "apiSecret": "s"})
        executor._mode = None
        executor._broker = None
        executor._reconciled = False
        executor._one_way_ok = False
        await executor._mgmt_tick()  # empty rows: reconcile must still run
        check("空库也能完成对账", executor._reconciled is True)
        check("模式为 testnet", executor._mode == "testnet")
    finally:
        binance_trade.position_mode = orig_posmode
        binance_trade.open_orders = orig_open


async def test_mode_switch_fencing():
    print("T9 模式切换清理旧环境仓位（C3）")
    reset_env(["BTCUSDT"], ["1h"])
    set_plan("BTCUSDT", "1h")
    FEED["BTCUSDT"] = [bar(5, 104.0, 104.0, 103.0, 103.5)]
    await executor._plan_tick()
    check("挂单存在", one_pos("BTCUSDT|1h") is not None)
    FEED["BTCUSDT"].append(bar(4, 102.8, 103.0, 99.8, 100.2))
    await executor._mgmt_tick()
    check("持仓 open", one_pos("BTCUSDT|1h")["state"] == "open")

    # paper -> testnet: allowed, all stale rows closed
    await executor.update_config({"dryRun": False, "testnet": True,
                                  "apiKey": "k", "apiSecret": "s"})
    check("模式切换清空旧仓", len(executor._active_positions()) == 0)
    with executor._lock:
        cur = executor._db().execute(
            "SELECT exit_reason FROM positions ORDER BY id DESC LIMIT 1")
        reason = cur.fetchone()[0]
    check("关闭原因 mode_switch", reason == "mode_switch", reason)

    # testnet -> live with an open position: refused before any mutation
    with executor._lock:
        executor._db().execute(
            "INSERT INTO positions (key, symbol, interval, direction, plan_json, "
            "entry, stop, qty, filled, state, created_at) "
            "VALUES ('BTCUSDT|1h','BTCUSDT','1h','long','{}',100.0,98.0,50.0,50.0,"
            "'open', ?)", (executor._now_ms(),))
        executor._db().commit()
    try:
        await executor.update_config({"testnet": False, "confirmLive": True,
                                      "apiKey": "k2", "apiSecret": "s2"})
        check("带真实持仓拒切", False, "未抛出 RuntimeError")
    except RuntimeError:
        check("带真实持仓拒切", True)
    check("拒切后模式未变", executor._mode_of_cfg() == "testnet")


async def test_panic_isolation():
    print("T10 紧急全撤逐仓隔离 + 停用落盘（C4）")
    reset_env(["BTCUSDT", "ETHUSDT"], ["1h"])
    set_plan("BTCUSDT", "1h")
    set_plan("ETHUSDT", "1h")
    FEED["BTCUSDT"] = [bar(5, 104.0, 104.0, 103.0, 103.5)]
    FEED["ETHUSDT"] = [bar(5, 104.0, 104.0, 103.0, 103.5)]
    await executor._plan_tick()
    FEED["BTCUSDT"].append(bar(4, 102.8, 103.0, 99.8, 100.2))  # BTC fills
    await executor._mgmt_tick()
    check("BTC 持仓 open", one_pos("BTCUSDT|1h")["state"] == "open")
    check("ETH 挂单 pending", one_pos("ETHUSDT|1h")["state"] == "pending")

    orig_market = type(executor._broker).market

    async def dead_market(self, symbol, side, qty, coid, pos_id=None):
        raise RuntimeError("route down")

    executor._broker.market = dead_market.__get__(executor._broker)
    res = await executor.panic()
    check("panic 返回 ok=False 且标注失败", res["ok"] is False
          and len(res["errors"]) >= 1, str(res))
    check("ETH pending 已处理", one_pos("ETHUSDT|1h") is None)
    check("BTC 失败仓保留待手动处理", one_pos("BTCUSDT|1h") is not None)
    persisted = json.loads(executor.CFG_PATH.read_text(encoding="utf-8"))
    check("停用已原子落盘", persisted["enabled"] is False)
    executor._broker.market = orig_market.__get__(executor._broker)


async def test_place_probe_recovery():
    print("T11 下单超时探针恢复（C1：已送达不误判为未下单）")
    reset_env(["BTCUSDT"], ["1h"])
    set_plan("BTCUSDT", "1h")
    FEED["BTCUSDT"] = [bar(5, 104.0, 104.0, 103.0, 103.5)]
    orig_place = executor._broker.place

    async def flaky_place(spec):
        if spec["otype"] == "LIMIT" and spec.get("pos_id"):
            await orig_place(spec)  # the exchange DID accept it...
            raise binance_trade.AmbiguousRequest(  # ...but the response was lost
                "read timeout after delivery")
        return await orig_place(spec)

    executor._broker.place = flaky_place
    await executor._plan_tick()
    pos = one_pos("BTCUSDT|1h")
    check("探针恢复：挂单未丢", pos is not None and pos["state"] == "pending"
          and bool(pos["entry_coid"]) and not pos["awaiting_entry"], str(pos))
    check("止损单仍在", bool(pos["stop_coid"]))
    info = await executor._broker.get("BTCUSDT", pos["entry_coid"])
    check("入场单实际在场", info is not None and info["status"] == "NEW", str(info))


async def test_live_equity_refusal():
    print("T12 实网余额失败拒开仓（W2）")
    reset_env(["BTCUSDT"], ["1h"])
    set_plan("BTCUSDT", "1h")
    orig_posmode = binance_trade.position_mode
    orig_open = binance_trade.open_orders
    orig_equity = binance_trade.usdt_equity
    orig_xinfo = binance_trade.exchange_info

    async def fake_posmode():
        return False

    async def fake_open(symbol=None):
        return []

    async def dead_equity():
        raise RuntimeError("balance route down")

    async def fake_xinfo():
        return await binance.get_exchange_info()

    binance_trade.position_mode = fake_posmode
    binance_trade.open_orders = fake_open
    binance_trade.usdt_equity = dead_equity
    binance_trade.exchange_info = fake_xinfo
    try:
        executor._cfg.update({"dryRun": False, "testnet": True,
                              "apiKey": "k", "apiSecret": "s"})
        executor._mode = None
        executor._broker = None
        executor._reconciled = False
        executor._one_way_ok = False
        executor._equity_cache = (0.0, 0.0)
        await executor._mgmt_tick()  # reconcile (empty)
        await executor._plan_tick()  # plan exists but equity unknown
        check("未开仓", one_pos("BTCUSDT|1h") is None)
        check("错误已记录", executor._last_error is not None,
              str(executor._last_error))
    finally:
        binance_trade.position_mode = orig_posmode
        binance_trade.open_orders = orig_open
        binance_trade.usdt_equity = orig_equity
        binance_trade.exchange_info = orig_xinfo


async def test_replay_trail_parity():
    print("T13 journal 重放跟踪平仓与执行器同数（W7 共享数学锚点）")
    reset_env(["BTCUSDT"], ["4h"])
    step = 4 * 3_600_000
    now = int(time.time() * 1000)
    be_bar = (now // step) * step - 2 * step
    KFEED[("BTCUSDT", "4h")] = [
        (be_bar, 100.0, 101.8, 99.9, 101.5, 10.0, 5.0),          # be @101.5
        (be_bar + step, 101.5, 103.0, 101.2, 102.5, 10.0, 5.0),  # trail -> 102.3
        (be_bar + 2 * step, 102.4, 102.4, 102.2, 102.3, 10.0, 5.0),  # be_stop
    ]
    trade = {"symbol": "BTCUSDT", "interval": "4h", "direction": "long",
             "entry": 100.0, "stop": 98.0,
             "opened_at": be_bar, "created_at": be_bar, "status": "open",
             "plan": {"stop": 98.0, "beR": 0.75, "targetR": None,
                      "trailR": 0.35, "texitBars": 48}}
    res = await journal_store.replay_plan(trade)
    # R = 0.5×0.75 (be @101.5) + 0.5×(102.3−100)/2 = +0.95 — same number the
    # executor T2 trail scenario asserts: shared trail math stays pinned
    check("replay 跟踪平仓 r=+0.95（与执行器同源）",
          res["reason"] == "be_stop" and abs((res["r"] or 0) - 0.95) < 1e-3,
          str(res))


async def test_place_failed_circuit_breaker():
    print("T14 place_failed 同 bar 熔断（2026-09-03 BCH -4120 连挂 21 次事故）")
    reset_env(["BTCUSDT"], ["1h"])
    set_plan("BTCUSDT", "1h", entry=100.0, stop=98.0)
    FEED["BTCUSDT"] = [bar(5, 104.0, 104.0, 103.0, 103.5)]
    orig_place = executor._broker.place

    async def broken_stop(spec):
        if spec["otype"] == "STOP_MARKET":
            raise binance_trade.TradeError(-4120, "Order type not supported")
        return await orig_place(spec)

    executor._broker.place = broken_stop
    await executor._plan_tick()
    pos = one_pos("BTCUSDT|1h")
    check("首挂 place_failed 已落 closed 行",
          pos is None and executor._db().execute(
              "SELECT COUNT(*) FROM positions WHERE key='BTCUSDT|1h' "
              "AND exit_reason='place_failed'").fetchone()[0] == 1)

    await executor._plan_tick()  # same bar, same plan -> must be suppressed
    n = executor._db().execute(
        "SELECT COUNT(*) FROM positions WHERE key='BTCUSDT|1h'").fetchone()[0]
    check("同 bar 重挂被熔断（仍 1 行）", n == 1, f"rows={n}")
    check("熔断警告已记", any("熔断" in w for w in executor._warned),
          str(executor._warned))

    executor._broker.place = orig_place
    # simulate next bar: backdate the failed row beyond the window anchor
    step = 3_600_000
    old_anchor = executor.last_closed_open(executor._now_ms(), "1h") - step
    executor._db().execute(
        "UPDATE positions SET created_at=? WHERE key='BTCUSDT|1h'", (old_anchor,))
    executor._db().commit()
    await executor._plan_tick()
    pos = one_pos("BTCUSDT|1h")
    check("下一 bar 窗口后允许重挂", pos is not None
          and pos["state"] == "pending", str(pos))


def _insert_row(key, symbol, state, **extra):
    """Direct row insert for sweep/probe tests (schema-required fields only)."""
    vals = {"key": key, "symbol": symbol, "interval": "1h", "direction": "long",
            "plan_json": json.dumps({"entry": 100.0, "stop": 98.0}), "entry": 100.0,
            "stop": 98.0, "qty": 1.0, "state": state, "created_at": executor._now_ms()}
    vals.update(extra)
    cur = executor._db().execute(
        "INSERT INTO positions (key, symbol, interval, direction, plan_json, "
        "entry, stop, qty, state, created_at, exit_reason, entry_coid, stop_coid) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (vals["key"], vals["symbol"], vals["interval"], vals["direction"],
         vals["plan_json"], vals["entry"], vals["stop"], vals["qty"], vals["state"],
         vals["created_at"], vals.get("exit_reason"), vals.get("entry_coid"),
         vals.get("stop_coid")))
    executor._db().commit()
    return int(cur.lastrowid)


def _events_text():
    return [r[0] for r in executor._db().execute("SELECT text FROM events")]


async def test_probe_failure_keeps_tracking():
    print("T15 探针失败不清 coid（2026-09-04 BTC 孤儿成交裸仓事故）")
    reset_env(["BTCUSDT"], ["1h"])
    set_plan("BTCUSDT", "1h", entry=100.0, stop=98.0)
    FEED["BTCUSDT"] = [bar(5, 104.0, 104.0, 103.0, 103.5)]
    await executor._plan_tick()
    pos = one_pos("BTCUSDT|1h")
    check("挂单就绪", pos is not None and pos["state"] == "pending"
          and pos.get("entry_coid"), str(pos))

    orig_get = executor._broker.get

    async def flaky_get(symbol, coid):
        raise RuntimeError("network blip")

    executor._broker.get = flaky_get
    await executor._sync_one(one_pos("BTCUSDT|1h"))
    fresh = one_pos("BTCUSDT|1h")
    check("探针失败后 entry_coid 保留", fresh is not None and fresh["state"] == "pending"
          and bool(fresh["entry_coid"]) and not fresh["awaiting_entry"], str(fresh))
    check("未误报查询不到", not any("查询不到" in t for t in _events_text()),
          str(_events_text()))

    executor._broker.get = orig_get
    info = await executor._broker.get("BTCUSDT", one_pos("BTCUSDT|1h")["entry_coid"])
    check("探针恢复后仍能读到挂单", info is not None and info.get("status") == "NEW",
          str(info))


async def test_margin_clamp():
    print("T16 保证金钳制（-2019 Margin is insufficient 风暴，2026-09-05）")

    class _StubLive:
        def __init__(self):
            self.specs = []
            self.setups = []

        async def equity(self):
            return 10000.0

        async def available(self):
            return 1000.0

        async def filters(self, symbol):
            return {"step": 0.001, "tick": 0.1, "minNotional": 5.0}

        async def ensure_setup(self, symbol, lev=None):
            self.setups.append((symbol, lev))

        async def place(self, spec):
            self.specs.append(dict(spec))
            return {"status": "NEW"}

        async def get(self, symbol, coid):
            return None

        async def cancel(self, symbol, coid):
            pass

        async def open_all(self):
            return []

        async def positions_all(self):
            return []

    reset_env(["BTCUSDT"], ["1h"])
    executor._mode = "testnet"
    executor._broker = _StubLive()
    executor._equity_cache = (0.0, 0.0)
    plan = {"direction": "long", "entry": 100.0, "stop": 98.0, "beTrigger": 100.3,
            "target1": 101.0, "beR": 0.15, "targetR": 0.5, "trailR": None,
            "texitBars": 96, "fillBars": 24}
    ok = await executor._try_place("BTCUSDT", "1h", plan, {})
    pos = one_pos("BTCUSDT|1h")
    # risk 10000×1%=100 → qty 50 → notional 5000；可用 1000×5×0.95=4750 → 47.5
    check("钳制下单成功", ok and pos is not None and pos["state"] == "pending"
          and abs(pos["qty"] - 47.5) < 1e-9, f"qty={pos and pos['qty']}")
    check("止损与入场同量", executor._broker.specs[0]["qty"] == pos["qty"]
          and executor._broker.specs[1]["qty"] == pos["qty"],
          str(executor._broker.specs))

    async def tiny():
        return 0.5  # 撑不起 minNotional=5

    executor._broker.available = tiny
    ok2 = await executor._try_place("ETHUSDT", "1h", plan, {})
    check("保证金不足时干净跳过", ok2 is False and one_pos("ETHUSDT|1h") is None
          and any("撑不起" in w for w in executor._warned), str(executor._warned))


async def test_orphan_sweep():
    print("T17 孤儿挂单清扫 + 无主仓位告警（2026-09-05 FIL 残留止损教训）")
    reset_env(["BTCUSDT", "ETHUSDT"], ["1h"])
    executor._mode = "testnet"
    pid_closed = _insert_row("BTCUSDT|1h", "BTCUSDT", "closed",
                            exit_reason="place_failed",
                            stop_coid="cltest1S0")
    pid_live = _insert_row("ETHUSDT|1h", "ETHUSDT", "pending",
                           entry_coid="cltest2E0", stop_coid="cltest2S0")
    cancelled = []

    class _SweepStub:
        async def open_all(self):
            return [
                {"clientOrderId": "cltest1S0", "symbol": "BTCUSDT"},
                {"clientOrderId": "cltest2E0", "symbol": "ETHUSDT"},
                {"clientOrderId": "clxxxx999E0", "symbol": "BTCUSDT"},  # 他机
            ]

        async def cancel(self, symbol, coid):
            cancelled.append((symbol, coid))

        async def positions_all(self):
            return [{"symbol": "SOLUSDT", "positionAmt": 1.5}]

    executor._broker = _SweepStub()
    await executor._sweep_orphans()
    check("只清 closed 行的孤儿单", cancelled == [("BTCUSDT", "cltest1S0")],
          str(cancelled))
    check("孤儿清理已记事件", any("清理孤儿挂单" in t for t in _events_text()),
          str(_events_text()))
    check("无主仓位已告警", any("非执行器管理的仓位" in w for w in executor._warned),
          str(executor._warned))


async def test_cancel_verified():
    print("T18 撤单必须验证（FIL 止损撤不掉残留 -4067 事故）")

    class _SurvivorStub:
        async def cancel(self, symbol, coid):
            raise RuntimeError("cancel glitch")

        async def get(self, symbol, coid):
            return {"status": "NEW"}

    reset_env(["BTCUSDT"], ["1h"])
    executor._broker = _SurvivorStub()
    await executor._cancel_verified("BTCUSDT", "cltest5S0")
    check("撤不掉时大声报警", any("仍存在" in t for t in _events_text()),
          str(_events_text()))

    class _GoneStub:
        def __init__(self):
            self.n = 0

        async def cancel(self, symbol, coid):
            self.n += 1

        async def get(self, symbol, coid):
            return None

    stub = _GoneStub()
    executor._broker = stub
    executor._warned.clear()
    executor._db().execute("DELETE FROM events")
    executor._db().commit()
    await executor._cancel_verified("BTCUSDT", "cltest6S0")
    check("确认消失后安静返回", stub.n == 1
          and not any("仍存在" in t for t in _events_text()), f"n={stub.n}")


async def test_derived_leverage():
    print("T19 按止损距离推导每仓杠杆（清算价钉在 ≥2×止损距离外，2026-09-05）")

    class _LevStub:
        def __init__(self):
            self.setups = []

        async def equity(self):
            return 10000.0

        async def available(self):
            return 100000.0  # 不构成钳制，只看杠杆推导

        async def filters(self, symbol):
            return {"step": 0.001, "tick": 0.1, "minNotional": 5.0}

        async def ensure_setup(self, symbol, lev=None):
            self.setups.append((symbol, lev))

        async def place(self, spec):
            return {"status": "NEW"}

        async def get(self, symbol, coid):
            return None

        async def cancel(self, symbol, coid):
            pass

        async def open_all(self):
            return []

        async def positions_all(self):
            return []

    # pure derivation math
    lev_tight = executor._derived_leverage(0.02, 100)
    lev_wide = executor._derived_leverage(0.05, 100)
    check("紧止损 2% → 19x", lev_tight == 19, f"lev={lev_tight}")
    check("宽止损 5%（ZEC 型）→ 8x", lev_wide == 8, f"lev={lev_wide}")
    for pct, lev in ((0.02, lev_tight), (0.05, lev_wide)):
        liq_dist = 1.0 / lev - executor._LEV_MMR_PAD
        check(f"清算距离 {liq_dist:.3f} ≥ 2×止损 {pct}", liq_dist >= 2 * pct - 1e-9,
              f"lev={lev}")
    check("配置上限生效", executor._derived_leverage(0.02, 10) == 10
          and executor._derived_leverage(0.05, 5) == 5)

    # end-to-end: ensure_setup receives the derived value, not the cfg cap
    reset_env(["BTCUSDT"], ["1h"], leverage=100)
    executor._mode = "testnet"
    executor._broker = _LevStub()
    executor._equity_cache = (0.0, 0.0)
    plan = {"direction": "long", "entry": 100.0, "stop": 98.0, "beTrigger": 100.3,
            "target1": 101.0, "beR": 0.15, "targetR": 0.5, "trailR": None,
            "texitBars": 96, "fillBars": 24}
    await executor._try_place("BTCUSDT", "1h", plan, {})
    check("下单按推导杠杆 19x 设置", executor._broker.setups == [("BTCUSDT", 19)],
          str(executor._broker.setups))
    pos = one_pos("BTCUSDT|1h")
    check("全额定仓未被保证金钳制", pos is not None and abs(pos["qty"] - 50.0) < 1e-9,
          f"qty={pos and pos['qty']}（保证金 5000/19≈263≈1.5R）")


async def main():
    # shared closed-bar analysis (round 55): ONE stub point for notifier+executor
    context.run_analysis = mock_run_analysis
    binance.get_klines = mock_get_klines
    binance.get_exchange_info = mock_get_exchange_info
    kline_cache.get_klines = mock_cache_klines

    await test_1h_full_lifecycle()
    await test_4h_trail_lifecycle()
    await test_time_exit()
    await test_plan_gone_and_amend()
    await test_guards()
    await test_post_only_retry()
    await test_daily_pause()
    await test_reconcile_unlock()
    await test_mode_switch_fencing()
    await test_panic_isolation()
    await test_place_probe_recovery()
    await test_live_equity_refusal()
    await test_replay_trail_parity()
    await test_place_failed_circuit_breaker()
    await test_probe_failure_keeps_tracking()
    await test_margin_clamp()
    await test_orphan_sweep()
    await test_cancel_verified()
    await test_derived_leverage()
    print(f"\nALL PASS ({PASS} checks)")


if __name__ == "__main__":
    asyncio.run(main())
