"""推送格式预览（第五十三轮追加）——一次性把三种候选格式发到企业微信群实测渲染。

用户问"能否把推送消息显示成表格"。企业微信群机器人 markdown 官方支持集不含
管道表格（标题/加粗/行内代码/引用/字体颜色），但客户端实况以实测为准——本脚本
一条消息内发三种格式（①管道表格 ②结构化色块 ③现行格式），由用户手机端判读。

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/_push_format_probe.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx

WECOM_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"

CONTENT = (
    "<font color=\"comment\">三种候选格式（示例数据），看哪种顺眼</font>\n"
    "\n"
    "**① 管道表格**（企微机器人官方不支持表格——若下方显示成竖线排版即不可用）\n"
    "事件|币|周期|方向|入场|止盈|止损|半保\n"
    "-|-|-|-|-|-|-|-\n"
    "新|BTCUSDT|4h|做多|77,600|78,900|76,400|78,000\n"
    "改单|ETHUSDT|1h|做空|3,180|3,100|3,260|3,220\n"
    "\n"
    "**② 结构化色块**（推荐：颜色+引用块+紧凑两列）\n"
    "<font color=\"comment\">1 新 · 1 改单 · 1 消失</font>\n"
    "\n"
    "【新】BTCUSDT 4h <font color=\"info\">做多</font>\n"
    "> 入场 77,600（回踩限价）\n"
    "> 止盈 78,900 ｜ 止损 76,400\n"
    "> 78,000 减半保本（半仓止盈，此后启用跟踪）\n"
    "> 跟踪 = 最高价 − 1,200（半仓离场时止损先设 78,000）\n"
    "\n"
    "【改单】ETHUSDT 1h <font color=\"warning\">做空</font>（挂单价格漂移）\n"
    "> 入场 3,180（旧 3,200）｜ 止损 3,260（旧 3,250）\n"
    "> 止盈 3,100 ｜ 3,220 减半保本\n"
    "\n"
    "【消失】SOLUSDT 4h 多头计划已消失\n"
    "> <font color=\"warning\">⚠️ 立即撤销原买入挂单</font>：入场 100.5 / 止损 98.2\n"
    "\n"
    "**③ 现行格式对照**（今天的线上格式）\n"
    "【新】BTCUSDT 4h 做多\n"
    "入场 77,600（回踩限价）\n"
    "止盈 78,900\n"
    "止损 76,400\n"
    "78,000 减半保本（半仓止盈，此后启用跟踪止损）\n"
    "跟踪止损 = 最高价−1,200（半仓离场时止损设 78,000，只上移，不低于入场价）"
)


async def main():
    cfg = json.loads(open(
        os.path.join(os.path.dirname(__file__), "..", "data", "notify.json"),
        encoding="utf-8").read())
    key = cfg.get("wecomKey") or ""
    if not key:
        raise SystemExit("no wecom key in notify.json")
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(WECOM_WEBHOOK, params={"key": key}, json={
            "msgtype": "markdown",
            "markdown": {"content": f"**CoinLens 推送格式预览**\n{CONTENT}"},
        })
        r.raise_for_status()
        data = r.json()
    print(f"wecom errcode={data.get('errcode')} errmsg={data.get('errmsg')}")


if __name__ == "__main__":
    asyncio.run(main())
