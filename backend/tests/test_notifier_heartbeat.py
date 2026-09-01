"""notifier 心跳推送单测（零网络，stub _send/run_analysis 后驱动 _run_once）"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services import notifier

# 防落盘污染：所有状态写操作走内存（update_config/_run_once 都会 _save）
notifier._save = lambda: None

PLAN = {"direction": "long", "entry": 100.0, "stop": 95.0, "target1": None,
        "beTrigger": 102.5, "beR": 0.5, "trailR": None, "fillBars": 12}


def setup(enabled=True, heartbeat=True, seeded=True, seen=None):
    notifier._cfg.update({
        "enabled": enabled, "mode": "events", "channel": "wecom",
        "symbols": ["BTCUSDT"], "intervals": ["1h"], "wecomKey": "k",
        "seeded": seeded, "seenPlans": seen or {}, "pushedPlans": {},
        "heartbeat": heartbeat,
    })
    notifier._state["planStates"] = {}


async def fake_analysis(symbol, interval, limit=500, as_of=None):
    # 与 run_analysis 同形：末根 K 时间=as_of（通过 _safe_analysis 守卫）
    return {"candles": [{"time": as_of, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
            "summary": {"score": 20, "bias": "bullish", "tradePlan": PLAN}}


async def fake_send(title, content):
    fake_send.calls.append((title, content))
    return True, ""


fake_send.calls = []
notifier.run_analysis = fake_analysis
notifier._send = fake_send

# 1) 无事件 + heartbeat 开 -> 推心跳（seen 已有同方向 -> 不产生事件）
setup(seen={"BTCUSDT|1h": "long"})
asyncio.run(notifier._run_once(allow_push=True))
assert len(fake_send.calls) == 1, fake_send.calls
t, c = fake_send.calls[0]
assert t.startswith("CoinLens 无变化"), t
assert "服务运行正常" in c and "⚠️" not in c
print(f"1) 无事件+心跳开 -> 推心跳  OK  标题={t!r}")

# 2) 无事件 + heartbeat 关 -> 不推
fake_send.calls.clear()
setup(heartbeat=False, seen={"BTCUSDT|1h": "long"})
asyncio.run(notifier._run_once(allow_push=True))
assert not fake_send.calls, fake_send.calls
print("2) 无事件+心跳关 -> 静默  OK")

# 3) 有事件 + heartbeat 开 -> 推事件（不是心跳）
fake_send.calls.clear()
setup(seen={"BTCUSDT|1h": "short"})  # 方向翻转 -> 转向事件
asyncio.run(notifier._run_once(allow_push=True))
assert len(fake_send.calls) == 1
t, c = fake_send.calls[0]
assert t.startswith("CoinLens 信号"), t
assert "【转向】" in c
print(f"3) 有事件 -> 推事件不推心跳  OK  标题={t!r}")

# 4) 首轮播种 -> 静默（心跳不抢跑）
fake_send.calls.clear()
setup(seeded=False, seen={})
asyncio.run(notifier._run_once(allow_push=True))
assert not fake_send.calls, fake_send.calls
print("4) 首轮静默播种 -> 不推心跳  OK")

# 5) 全部分析失败 -> 不推（沿用既有快速失败，不误报"运行正常"）
async def failing_analysis(symbol, interval, limit=500, as_of=None):
    raise notifier.NoKlinesError("x")

fake_send.calls.clear()
setup(seen={"BTCUSDT|1h": "long"})
notifier.run_analysis = failing_analysis
asyncio.run(notifier._run_once(allow_push=True))
assert not fake_send.calls, fake_send.calls
assert "analysis failed" in (notifier._state["lastError"] or "")
print("5) 全部分析失败 -> 不推心跳并记错误  OK")

# 6) 部分失败 -> 心跳里如实标注失败键
fake_send.calls.clear()
setup(seen={"BTCUSDT|1h": "long"})
notifier._cfg["symbols"] = ["BTCUSDT", "ETHUSDT"]
notifier._cfg["seenPlans"] = {"BTCUSDT|1h": "long", "ETHUSDT|1h": "long"}

async def partial_analysis(symbol, interval, limit=500, as_of=None):
    if symbol == "ETHUSDT":
        raise notifier.NoKlinesError("x")
    return await fake_analysis(symbol, interval, limit, as_of)

notifier.run_analysis = partial_analysis
asyncio.run(notifier._run_once(allow_push=True))
assert len(fake_send.calls) == 1
t, c = fake_send.calls[0]
assert t.startswith("CoinLens 无变化") and "ETHUSDT 1h" in c and "⚠️" in c
print(f"6) 部分失败 -> 心跳标注失败键  OK  内容={c!r}")

# 7) status() 暴露 heartbeat 字段
assert notifier.status()["heartbeat"] is True
notifier.update_config(heartbeat=False)
assert notifier.status()["heartbeat"] is False
assert notifier._cfg["heartbeat"] is False
print("7) status/update_config 接线 heartbeat  OK")

print("\n全部通过")
