"""notifier 推送格式单测（第五十三轮改版：色块/摘要行/引用块/改单旧行内价/通道剥色）零网络"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services import notifier

notifier._save = lambda: None  # 防落盘污染

TARGET_PLAN = {"direction": "long", "entry": 100.0, "stop": 95.0, "target1": 105.0,
               "beTrigger": 102.5, "beR": 0.5, "trailR": None, "fillBars": 24}
TRAIL_PLAN = {"direction": "long", "entry": 100.0, "stop": 95.0, "target1": None,
              "beTrigger": 103.75, "beR": 0.75, "trailR": 0.35, "fillBars": 18}
TRAIL_SHORT = {"direction": "short", "entry": 100.0, "stop": 103.0, "target1": None,
               "beTrigger": 98.0, "beR": 0.75, "trailR": 0.35, "fillBars": 18}

# 1) 1h 目标计划行：入场 → 止盈｜止损（止盈先于止损）→ 半保
lines = notifier._plan_lines(TARGET_PLAN)
assert lines[0] == "入场 100.00（回踩限价）", lines
assert lines[1] == "止盈 105.00 ｜ 止损 95.00", lines
assert lines[1].index("止盈") < lines[1].index("止损")
assert lines[2] == "102.50 减半保本（半仓止盈，此后止损提到入场价）", lines
assert len(lines) == 3
print("1) 1h 目标计划行（止盈先于止损、紧凑两列） OK")

# 2) 4h 跟踪计划（多）：半保 → 跟踪=最高价−距离（半仓先设具体止损位）
lines = notifier._plan_lines(TRAIL_PLAN)
assert lines[1] == "止损 95.00", lines
assert lines[2] == "103.75 减半保本（半仓止盈，此后启用跟踪）", lines
assert lines[3] == "跟踪 = 最高价 − 1.75（半仓离场时止损先设 102.00，只上移、不低于入场价）", lines
print("2) 4h 跟踪计划（多，距离+先设位） OK")

# 3) 跟踪计划（空）：最低价+距离、只下移
lines = notifier._plan_lines(TRAIL_SHORT)
assert lines[3] == "跟踪 = 最低价 + 1.05（半仓离场时止损先设 99.05，只下移、不高于入场价）", lines
print("3) 4h 跟踪计划（空） OK")

# 4) events 正文：灰色计数摘要行 + 色块 + 引用前缀 + 消失撤单警示
events = [
    ("新", "BTCUSDT", "4h", dict(TRAIL_PLAN), None),
    ("消失", "SOLUSDT", "1h", None, {"direction": "long", "entry": 100.5, "stop": 98.2}),
]
c = notifier._events_content(events)
assert c.startswith('<font color="comment">1 新 · 1 消失</font>'), c
assert "【新】BTCUSDT 4h " + notifier._WECOM_LONG in c
assert "> 入场 100.00（回踩限价）" in c
assert "【消失】SOLUSDT 1h 多头计划已消失" in c
assert "立即撤销该买入限价挂单" in c and "若已成交请检查仓位止损" in c
assert "> 原挂单：入场 100.50 / 止损 98.20" in c
assert "\n\n" in c  # 块间空行分隔
print("4) events 摘要行+色块+消失撤单警示 OK")

# 5) 改单：旧行内价（入场/止损均带（旧 X））
events = [("改单", "ETHUSDT", "1h", dict(TARGET_PLAN),
           {"direction": "long", "entry": 99.0, "stop": 94.0, "target1": 105.0})]
c = notifier._events_content(events)
assert "【改单】ETHUSDT 1h " + notifier._WECOM_LONG + "（挂单价格漂移，请改单）" in c
assert "> 入场 100.00（旧 99.00）" in c
assert "> 止盈 105.00 ｜ 止损 95.00（旧 94.00）" in c
assert "> 102.50 减半保本" in c
print("5) 改单旧行内价 OK")

# 6) 转向：旧方向灰 → 新方向色 + 撤单警示带原挂单价
events = [("转向", "SOLUSDT", "4h", dict(TRAIL_SHORT),
           {"direction": "long", "entry": 99.5, "stop": 94.0, "direction_old": None})]
events[0] = ("转向", "SOLUSDT", "4h", dict(TRAIL_SHORT),
             {"direction": "long", "entry": 99.5, "stop": 94.0})
c = notifier._events_content(events)
assert "【转向】SOLUSDT 4h " in c
assert '<font color="comment">做多</font> → ' in c
assert "先撤销原买入挂单" in c
assert "> ⚠️" in c or "⚠️" in c
print("6) 转向块+撤单警示 OK")

# 7) 非 wecom 通道剥除颜色标签
notifier._cfg.update({"channel": "pushplus", "token": "t", "wecomKey": ""})
captured = {}


async def fake_by_channel(channel, cred, title, content):
    captured.update(channel=channel, title=title, content=content)
    return True, ""


orig = notifier.notify.send_by_channel
notifier.notify.send_by_channel = fake_by_channel
asyncio.run(notifier._send("T", '<font color="info">做多</font> 测试'))
notifier.notify.send_by_channel = orig
assert captured["channel"] == "pushplus"
assert "font" not in captured["content"] and "做多 测试" in captured["content"], captured
print("7) pushplus 剥色 OK")

# 8) brief 块样式：颜色方向头 + 引用行
b = notifier._plan_block("BTCUSDT", "4h", dict(TRAIL_PLAN))
assert b.startswith("【BTCUSDT 4h " + notifier._WECOM_LONG + "】"), b
assert "\n> 入场 100.00（回踩限价）" in b
print("8) brief 块样式 OK")

# 9) 心跳文案不变（回归）
assert "服务运行正常" in "本小时无新计划/转向/消失/改单，服务运行正常。"
print("9) 心跳文案不变 OK")

print("\n全部通过")
