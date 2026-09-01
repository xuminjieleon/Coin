"""notifier 改单/撤单逻辑单测（零网络，直接测 _amend_event 与配置迁移）"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services import notifier

# 防落盘污染：状态写操作走内存
notifier._save = lambda: None

def reset():
    notifier._cfg["pushedPlans"] = {}
    notifier._cfg["seenPlans"] = {}

# 1) 无 pushed 记录 -> 不改单（首轮静默播种后用户无挂单）
reset()
plan = {"direction": "long", "entry": 100.0, "stop": 95.0}
assert notifier._amend_event("BTCUSDT|4h", plan) is None
print("1) 无挂单记录 -> 不改单  OK")

# 2) 同方向、漂移小于阈值 -> 不改单
reset()
notifier._cfg["pushedPlans"] = {"BTCUSDT|4h": {"entry": 100.0, "stop": 95.0, "direction": "long"}}
plan2 = {"direction": "long", "entry": 100.5, "stop": 95.5}  # risk=5, drift=0.5 = 10% < 40%
assert notifier._amend_event("BTCUSDT|4h", plan2) is None
print("2) 漂移10% < 阈值40% -> 不改单  OK")

# 3) 同方向、entry 漂移超阈值 -> 改单
reset()
notifier._cfg["pushedPlans"] = {"BTCUSDT|4h": {"entry": 100.0, "stop": 95.0, "direction": "long"}}
plan3 = {"direction": "long", "entry": 102.0, "stop": 97.0}  # 平行平移：risk=5=base, drift 2.0/5 = 40% >= 40%；2/102=1.96% >= 0.5%价
ev = notifier._amend_event("BTCUSDT|4h", plan3)
assert ev and ev[0] == "改单" and ev[1] == "BTCUSDT" and ev[2] == "4h"
print(f"3) 漂移40%风险+1.96%价 >= 双阈值 -> 改单  OK  ({ev[0]} {ev[1]} {ev[2]})")

# 3b) 风险比例达标但价格幅度不足 -> 不改单（低波期窄风险防噪音）
reset()
notifier._cfg["pushedPlans"] = {"BTCUSDT|4h": {"entry": 10000.0, "stop": 9990.0, "direction": "long"}}
plan3b = {"direction": "long", "entry": 10004.0, "stop": 9994.0}  # drift 4/10=40% >= 40%，但 4/10004=0.04% < 0.5%价
assert notifier._amend_event("BTCUSDT|4h", plan3b) is None
print("3b) 40%风险但仅0.04%价 < 0.5%价格下限 -> 不改单  OK")

# 3c) 价格下限是决定性条件的通过例（窄风险、大价格漂移）
reset()
notifier._cfg["pushedPlans"] = {"BTCUSDT|4h": {"entry": 10000.0, "stop": 9990.0, "direction": "long"}}
plan3c = {"direction": "long", "entry": 10060.0, "stop": 10050.0}  # drift 60/10=600% 且 60/10060=0.60% >= 0.5%价
ev = notifier._amend_event("BTCUSDT|4h", plan3c)
assert ev and ev[0] == "改单"
print("3c) 窄风险+0.60%价 >= 双阈值 -> 改单  OK")

# 4) 同方向、stop 漂移超阈值 -> 改单
reset()
notifier._cfg["pushedPlans"] = {"BTCUSDT|4h": {"entry": 100.0, "stop": 95.0, "direction": "long"}}
plan4 = {"direction": "long", "entry": 100.2, "stop": 97.0}  # stop drift 2.0/max(3,5)=40%
ev = notifier._amend_event("BTCUSDT|4h", plan4)
assert ev and ev[0] == "改单"
print("4) stop漂移超阈值 -> 改单  OK")

# 5) 方向变了 -> 不是改单（由转向事件处理）
reset()
notifier._cfg["pushedPlans"] = {"BTCUSDT|4h": {"entry": 100.0, "stop": 95.0, "direction": "long"}}
plan5 = {"direction": "short", "entry": 102.0, "stop": 107.0}
assert notifier._amend_event("BTCUSDT|4h", plan5) is None
print("5) 方向翻转 -> 不算改单（走转向事件）  OK")

# 6) 旧配置无 pushedPlans 字段 -> _load 迁移补默认
import json, tempfile
from pathlib import Path
tmp = Path(tempfile.mkdtemp()) / "notify.json"
tmp.write_text(json.dumps({"enabled": True, "symbols": ["BTCUSDT"], "intervals": ["1h", "4h"]}), encoding="utf-8")
orig = notifier.DATA_PATH
notifier.DATA_PATH = tmp
notifier._cfg = dict(notifier.DEFAULT_CFG)
notifier._load()
assert "pushedPlans" in notifier._cfg and notifier._cfg["pushedPlans"] == {}
assert notifier._cfg["intervals"] == ["1h", "4h"]
notifier.DATA_PATH = orig
notifier._cfg = dict(notifier.DEFAULT_CFG)
print("6) 旧配置迁移补 pushedPlans 默认值  OK")

print("\n全部通过")
