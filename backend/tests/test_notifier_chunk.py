# -*- coding: utf-8 -*-
"""Chunked-send split logic tests (2026-09-04 40058 markdown>4096 整条被拒修复).

Run:  python tests/test_notifier_chunk.py   (from backend/)

Covers _split_content / _hard_cut / _send_chunked without network:
single-message passthrough, block-boundary packing, per-chunk byte cap,
title-only-on-first-chunk, single-oversized-block hard cut with no broken
UTF-8 multibyte char, and failure aggregation.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from services import notifier  # noqa: E402

PASS = 0


def check(name, cond, detail=""):
    global PASS
    if not cond:
        raise AssertionError(f"[FAIL] {name}: {detail}")
    PASS += 1
    print(f"  ok {name}")


def ul(s):
    return len(s.encode("utf-8"))


# --- _hard_cut ---
head, rest = notifier._hard_cut("abcdef", 3)
check("硬切英文按字节", head == "abc" and rest == "def", repr((head, rest)))

s = "入场 1,234 止损"  # CJK 3 字节/字
head, rest = notifier._hard_cut(s, 10)
check("硬切不破多字节字符", ul(head) <= 10 and head + rest == s, repr((head, rest)))

head, rest = notifier._hard_cut("短", 100)
check("短串原样返回", head == "短" and rest == "")


# --- _split_content: passthrough ---
chunks = notifier._split_content("T", "小消息")
check("单条够用原样返回（无标题包装）", chunks == ["小消息"], repr(chunks))


# --- _split_content: block packing + byte cap ---
blocks = [f"块{i} " + "汉" * 120 for i in range(8)]  # 每块 ~360+ 字节
content = "\n\n".join(blocks)
title = "CoinLens 信号 09-04 00:05"
chunks = notifier._split_content(title, content, limit=1000)
check("超限必分段", len(chunks) > 1, f"chunks={len(chunks)}")
check("首段+标题行不超上限", ul(f"**{title}**\n{chunks[0]}") <= 1000,
      str(ul(f"**{title}**\n{chunks[0]}")))
for i, ch in enumerate(chunks[1:], 1):
    check(f"后续段 {i} 不超上限", ul(ch) <= 1000, str(ul(ch)))
check("分段不丢内容",
      "".join(c.replace("\n\n", "") for c in chunks) == content.replace("\n\n", ""))


# --- single oversized block hard cut ---
big = "单块 " + "字" * 900  # ~2700 字节单块
chunks = notifier._split_content("T", big, limit=1000)
check("单块超限硬切成多段", len(chunks) > 1, f"chunks={len(chunks)}")
check("硬切不丢字符", "".join(chunks) == big)
for i, ch in enumerate(chunks):
    check(f"硬切段 {i} 不超上限", ul(ch) <= 1000, str(ul(ch)))


# --- _send_chunked: mock channel ---
sent = []


async def run_chunked():
    orig = notifier.notify.send_by_channel
    notifier._cfg.update({"channel": "wecom", "wecomKey": "k", "token": ""})
    try:
        async def fake_ok(channel, cred, title, content):
            sent.append((title, content))
            return True, ""

        notifier.notify.send_by_channel = fake_ok
        # 单条够用 → 原路径一条
        sent.clear()
        ok, err = await notifier._send_chunked("T", "小消息")
        check("分段入口单条直发", ok and len(sent) == 1 and sent[0][0] == "T",
              repr((ok, err, sent)))
        # 多条 → 首条带标题、后续带序号
        sent.clear()
        content = "\n\n".join(f"块{i} " + "汉" * 200 for i in range(10))
        ok, err = await notifier._send_chunked("CoinLens 信号", content)
        check("分段发送全成功", ok, repr(err))
        check("多条发出", len(sent) > 1, f"sent={len(sent)}")
        check("首条标题无序号", sent[0][0] == "CoinLens 信号", sent[0][0])
        check("后续条带序号", sent[1][0].startswith("CoinLens 信号（2/"),
              sent[1][0])
        # 失败传播
        async def fail_second(channel, cred, title, content):
            sent.append(title)
            if len(sent) == 2:
                return False, "boom"
            return True, ""

        notifier.notify.send_by_channel = fail_second
        sent.clear()
        ok, err = await notifier._send_chunked("T", content)
        check("分段失败聚合报错", not ok and "分段 2/" in err, repr(err))
    finally:
        notifier.notify.send_by_channel = orig


asyncio.run(run_chunked())

print(f"\nALL PASS ({PASS} checks)")
