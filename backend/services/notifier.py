"""Hourly WeChat push of trade-plan signals (PushPlus channel).

Design (agreed with the user, 2026-08-27):
- Runs inside the FastAPI process (startup task, strong ref anti-GC — same
  pattern as the derivatives background backfill).
- Fires at HH:05 every hour so the 1h candle is closed and cached.
- Decision path is EXACTLY the API's: services.analysis.context.run_analysis.
- mode "events" (default): push only on new plan / direction flip / plan
  gone / amend, one aggregated message. First cycle after (re)seed is silent.
  Heartbeat (config "heartbeat", default on, 2026-09-01): when a cycle
  produced NO events, a short "CoinLens 无变化 HH:MM" message is pushed so
  the user can tell the service is alive; analysis failures in the round are
  flagged in it. Brief mode always has content so no heartbeat there.
- mode "brief": one message per hour with every configured symbol's plan
  snapshot in the same multi-line 【】 block format as events (idle symbols
  listed compactly per interval; plan fields drift with ATR — each message
  is the latest plan).
- Multi-interval (2026-08-27): config "intervals" is a list (e.g.
  ["1h","4h"]); every hourly cycle analyses symbols x intervals
  concurrently. Messages carry the interval tag
  (e.g. "【新】BTCUSDT 4h 做多"). Legacy single "interval" strings migrate
  to a one-element list (fingerprint reseeded — key shape changed from
  symbol to symbol|interval).
- Closed-bar-only push semantics (2026-08-29, option B — user saw 4h
  events firing at hourly checkpoints overnight and asked why; the old
  docstring claim "4h events at most every 4h" was an assumption the code
  never guaranteed): every cycle runs run_analysis in REPLAY mode with
  as_of = open time of the newest CLOSED bar. Plan states therefore only
  change when a bar closes — 4h events fire at most every 4 hours
  (checkpoints right after UTC 00/04/08/12/16/20 closes), 1h decisions
  drop the 5-minute forming tail, and the forming-bar intraday flicker
  documented in DEVLOG rounds 24/26 can no longer push events. Push,
  chart replay and journal replay now share ONE decision semantics. A
  cycle whose returned last candle is not the requested as_of bar (data
  gap) is treated as failed — the previous fingerprint stands.
- Position management after entry is NOT pushed (user decision): the pushed
  plan is the pending-order signal; entry-time geometry freezes on fill,
  see journal_store.replay_plan / App position panel.
- Fingerprint = "symbol|interval" -> direction for NEW/GONE/FLIP events.
  Separately, when the direction is UNCHANGED but the pending order's
  entry/stop have drifted materially (ATR moves the retrace limit), an
  "改单" (amend) event fires with the fresh prices — the user's resting
  limit order still sits at the stale price otherwise (2026-08-31: BNB
  4h filled at a drifted price the user never saw). Amend events carry the
  full plan block so the user can re-place entry+stop. Drift is measured
  against the last PUSHED prices (persisted), not the last analysis.
- Message prices need NO user-side math (2026-08-28): trail plans (4h/1d/1w)
  print the trail DISTANCE in price units and the concrete stop right after
  the half-off (journal replay semantics: stop = max(entry, MFE -
  trailR*risk)) instead of the old "trail activation" price, which was below
  the actual arming level on 4h/1d and required the user to compute
  trailR x risk by hand.
- Message format (2026-09-02, round 53 — user picked "structured color
  blocks" after a live three-format preview on their phone; WeCom bot
  markdown has no real tables): events message = gray count summary line
  (N 新 · N 转向 · N 改单 · N 消失) + per-event blocks — 【事件】symbol interval
  + colored direction word (green 做多 / orange-red 做空), plan lines inside
  quote blocks ("> " prefix), compact two-column rows (入场 / 止盈｜止损 /
  减半保本 / 跟踪), amend events carry old prices inline (（旧 X）), 消失/转向
  carry a red cancel-order warning line with the resting prices. Brief mode
  keeps the ---------- separators + idle list with the same block styling.
  The <font> color tags are stripped for non-wecom channels (pushplus).
- Symbols whose analysis FAILED are excluded from fingerprint comparison
  (a network hiccup must not push a fake "plan gone").
- Config + state persist to backend/data/notify.json (gitignored: token).
"""

import asyncio
import json
import math
import re
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from services import notify
from services.analysis.context import (
    ALLOWED_INTERVALS,
    closed_bar_analysis,
    run_analysis,  # noqa: F401  (tests patch this name on the notifier module)
)

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "notify.json"

MODES = {"events", "brief"}

DEFAULT_CFG = {
    "enabled": False,
    "mode": "events",
    "channel": "wecom",  # "wecom" (企业微信群机器人) | "pushplus"
    "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"],
    "intervals": ["1h"],
    "token": "",       # pushplus token
    "wecomKey": "",    # WeCom group robot webhook key
    "seeded": False,
    "seenPlans": {},
    "pushedPlans": {},  # "symbol|interval" -> {"entry","stop","direction"} of last PUSHED plan
    "heartbeat": True,  # events 模式无事件时也推一条"无变化"心跳（2026-09-01 用户要求：确认服务活着）
}

# Amend event fires when entry OR stop drifted by >= this fraction of the
# order's own risk (entry-stop distance) AND by >= AMEND_MIN_PRICE_PCT of
# price. The price floor exists because in low-vol regimes the risk distance
# is so tight that frac×risk alone fires on sub-0.5% price moves — 30d replay
# (tests/amend_freq_stat.py, 2026-09-01): 0.20 frac = 67 amends/day system-wide,
# median drift 0.58% of price, 78~90% firing within 1-2 bars of the last push
# (plan breathing noise, not a stale order). 0.40 + 0.5% floor ≈ 30/day,
# median drift ~1% of price at fire time.
AMEND_DRIFT_FRAC = 0.40
AMEND_MIN_PRICE_PCT = 0.5


def _norm_intervals(raw) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return ["1h"]
    out: list[str] = []
    for itv in raw:
        if itv in ALLOWED_INTERVALS and itv not in out:
            out.append(itv)
    return out or ["1h"]

_cfg: dict = dict(DEFAULT_CFG)
_state: dict = {"lastRun": None, "lastError": None, "recent": [], "planStates": {}}
_task: asyncio.Task | None = None


# ---------------------------------------------------------------- persistence

def _load() -> None:
    global _cfg
    try:
        raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    if isinstance(raw, dict):
        merged = dict(DEFAULT_CFG)
        merged.update({k: raw[k] for k in DEFAULT_CFG if k in raw})
        # legacy single-interval config -> list; fingerprint key shape changes
        # (symbol -> symbol|interval), so reseed silently instead of firing a
        # storm of fake events
        if "intervals" not in raw:
            legacy = raw.get("interval")
            merged["intervals"] = _norm_intervals([legacy] if legacy else None)
            merged["seeded"] = False
            merged["seenPlans"] = {}
        merged["intervals"] = _norm_intervals(merged["intervals"])
        _cfg = merged


def _save() -> None:
    try:
        DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        DATA_PATH.write_text(
            json.dumps(_cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _record_push(title: str, ok: bool, error: str) -> None:
    _state["recent"] = ([{"time": int(time.time() * 1000), "title": title,
                          "ok": ok, "error": error}] + _state["recent"])[:10]
    _state["lastError"] = None if ok else error


def _set_error(msg: str) -> None:
    _state["lastError"] = f"{msg} (@ {datetime.now().strftime('%m-%d %H:%M:%S')})"


# ---------------------------------------------------------------- formatting

# 事件色块格式（2026-09-02 第五十三轮改版，用户手机预览三种候选后拍板"结构化色块"）：
# 企微机器人 markdown 支持集（粗体/引用/字体颜色）内的元素——真表格不支持；
# 颜色标签在非 wecom 通道（pushplus markdown）发送前剥除。
_FONT_RE = re.compile(r"</?font[^>]*>")
_WECOM_LONG = '<font color="info">做多</font>'
_WECOM_SHORT = '<font color="warning">做空</font>'


def _dir_word(direction: str) -> str:
    """带颜色的方向词（wecom 绿=做多 / 橙红=做空）。"""
    return _WECOM_LONG if direction == "long" else _WECOM_SHORT


def _quote(lines: list[str]) -> list[str]:
    return ["> " + l for l in lines]


def _fmt(p: float) -> str:
    if p >= 1000:
        return f"{p:,.0f}"
    if p >= 1:
        return f"{p:,.2f}"
    # <1: 4 significant digits, no exponent (prices and trail distances)
    if p == 0:
        return "0"
    exp = math.floor(math.log10(abs(p)))
    return f"{p:.{max(0, 3 - exp)}f}".rstrip("0").rstrip(".")


def _trail_ref(plan: dict) -> float | None:
    """Legacy field: entry ± trailR x risk (kept in the status API's
    planStates for compatibility; NOT printed in messages anymore — for
    4h/1d the trail actually engages at the (higher) beTrigger, so this
    price is not an actionable level)."""
    trail = plan.get("trailR")
    if trail is None or plan.get("target1") is not None:
        return None
    entry, stop = plan["entry"], plan["stop"]
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    return entry + trail * risk if plan["direction"] == "long" else entry - trail * risk


def _trail_dist(plan: dict) -> float | None:
    """Trail distance in PRICE units (trailR x risk) for trail plans."""
    trail = plan.get("trailR")
    if trail is None or plan.get("target1") is not None:
        return None
    risk = abs(plan["entry"] - plan["stop"])
    if risk <= 0:
        return None
    return trail * risk


def _exit_lines(plan: dict, old_stop: float | None = None) -> list[str]:
    """入场之后的计划行（_plan_lines/_amend_lines 共用）：
    止盈(若有)｜止损 → 减半保本 → 跟踪(若跟踪族)。old_stop 提供时在止损后附（旧 X）。
    第二十八/五十二轮语义保留：止盈先于止损、跟踪族直接打印跟踪距离与
    半仓离场时的具体止损位、只含实际价格。"""
    lines = []
    stop_txt = f"止损 {_fmt(plan['stop'])}"
    if old_stop is not None:
        stop_txt += f"（旧 {_fmt(old_stop)}）"
    if plan.get("target1") is not None:
        lines.append(f"止盈 {_fmt(plan['target1'])} ｜ {stop_txt}")
    else:
        lines.append(stop_txt)
    be = f"{_fmt(plan['beTrigger'])} 减半保本"
    dist = _trail_dist(plan)
    if dist is None:
        lines.append(f"{be}（半仓止盈，此后止损提到入场价）")
    else:
        lines.append(f"{be}（半仓止盈，此后启用跟踪）")
        if plan["direction"] == "long":
            z = max(plan["entry"], plan["beTrigger"] - dist)
            lines.append(f"跟踪 = 最高价 − {_fmt(dist)}"
                         f"（半仓离场时止损先设 {_fmt(z)}，只上移、不低于入场价）")
        else:
            z = min(plan["entry"], plan["beTrigger"] + dist)
            lines.append(f"跟踪 = 最低价 + {_fmt(dist)}"
                         f"（半仓离场时止损先设 {_fmt(z)}，只下移、不高于入场价）")
    return lines


def _plan_lines(plan: dict) -> list[str]:
    """单源计划行（events/brief 共用）：入场 → 止盈(若有)｜止损 → 半保 → 跟踪。"""
    return [f"入场 {_fmt(plan['entry'])}（回踩限价）"] + _exit_lines(plan)


def _amend_lines(plan: dict, old: dict) -> list[str]:
    """改单块行：入场（旧 X）→ 止盈(若有)｜止损（旧 Y）→ 半保 → 跟踪。"""
    head = f"入场 {_fmt(plan['entry'])}"
    if old.get("entry") is not None:
        head += f"（旧 {_fmt(old['entry'])}）"
    return [head] + _exit_lines(plan, old.get("stop"))


def _plan_block(sym: str, itv: str, plan: dict) -> str:
    """【sym itv 方向】 header + 引用块计划行（brief 模式用）。"""
    head = f"【{sym} {itv} {_dir_word(plan['direction'])}】"
    return head + "\n" + "\n".join(_quote(_plan_lines(plan)))


def _events_content(events: list) -> str:
    """events 模式正文（第五十三轮格式）：灰色计数摘要行 + 逐事件色块。
    事件块 = 【事件】币 周期 + 颜色方向词 + 引用块（> 前缀）计划行；
    消失/转向带红色撤单警示行与原挂单价。"""
    cnt = Counter(kind for kind, *_ in events)
    summary = " · ".join(f"{cnt[k]} {k}" for k in ("新", "转向", "改单", "消失") if cnt.get(k))
    parts = [f'<font color="comment">{summary}</font>'] if summary else []
    for kind, sym, itv, plan, prev in events:
        tag = f"{sym} {itv} "
        if kind == "消失":
            pd_ = (prev or {}).get("direction")
            d = "多头" if pd_ == "long" else "空头"
            side = "买入" if pd_ == "long" else "卖出"
            warn = (f'> <font color="warning">⚠️ 立即撤销该{side}限价挂单</font>'
                    f"（若已成交请检查仓位止损）")
            px = ""
            if (prev or {}).get("entry") is not None:
                px = f"\n> 原挂单：入场 {_fmt(prev['entry'])} / 止损 {_fmt(prev['stop'])}"
            parts.append(f"【消失】{tag}{d}计划已消失\n{warn}{px}")
            continue
        if kind == "改单":
            head = f"【改单】{tag}{_dir_word(plan['direction'])}（挂单价格漂移，请改单）"
            parts.append(head + "\n" + "\n".join(_quote(_amend_lines(plan, prev or {}))))
            continue
        head = f"【{kind}】{tag}"
        lines = _quote(_plan_lines(plan))
        if kind == "转向":
            pd_ = (prev or {}).get("direction")
            old_side = "买入" if pd_ == "long" else "卖出"
            head += f'<font color="comment">{"做多" if pd_ == "long" else "做空"}</font> → '
            old_txt = ""
            if (prev or {}).get("entry") is not None:
                old_txt = f"：入场 {_fmt(prev['entry'])} / 止损 {_fmt(prev['stop'])}"
            lines = [f'> <font color="warning">⚠️ 先撤销原{old_side}挂单</font>{old_txt}'] + lines
        head += _dir_word(plan["direction"])
        parts.append(head + "\n" + "\n".join(lines))
    return "\n\n".join(parts)


def _brief_content(intervals: list[str], symbols: list[str],
                   plans: dict, failed: set) -> str:
    """brief mode content: full multi-line plan blocks (【】 headers,
    ---------- separators — 2026-08-28 user preference, clearer than
    one-liners) with a compact idle list per interval."""
    multi = len(intervals) > 1
    sections: list[str] = []
    for itv in intervals:
        blocks: list[str] = []
        idle: list[str] = []
        for sym in symbols:
            if (sym, itv) in failed:
                idle.append(f"{sym}：本轮分析失败")
            elif plans.get((sym, itv)):
                blocks.append(_plan_block(sym, itv, plans[(sym, itv)]))
            else:
                idle.append(f"{sym}：观望")
        seg = "\n----------\n".join(blocks)
        if idle:
            idle_txt = "\n".join(idle)
            seg = f"{seg}\n\n{idle_txt}" if seg else idle_txt
        if multi:
            seg = f"— {itv} —\n{seg}"
        sections.append(seg)
    return "\n\n".join(sections)


def _now_label() -> str:
    return datetime.now().strftime("%m-%d %H:%M")


def _amend_event(key: str, plan: dict):
    """Return an ("改单", ...) event tuple when the current plan's entry/stop
    drifted materially from the last PUSHED resting order, else None.

    Drift is measured against the last PUSHED prices (persisted), not the
    previous analysis, so we don't re-push every hour when nothing was
    acted on. The threshold metric itself is shared with the executor
    (drift_material)."""
    pushed = (_cfg.get("pushedPlans") or {}).get(key)
    if not pushed:
        return None  # never pushed (e.g. seeded silently): nothing resting to amend
    if not drift_material(pushed, plan):
        return None
    sym, itv = key.split("|", 1)
    return ("改单", sym, itv, plan, pushed)


def drift_material(old_plan: dict, new_plan: dict) -> bool:
    """True when entry/stop drifted materially (AMEND_DRIFT_FRAC x the
    order's own risk AND >= AMEND_MIN_PRICE_PCT of price) with the
    direction unchanged.

    SHARED SEMANTICS (round 55): this single metric decides BOTH the pushed
    【改单】 event and the executor's automatic amend — the executor imports
    it; do not re-implement."""
    if old_plan.get("direction") != new_plan.get("direction"):
        return False  # direction flip is its own event, not an amend
    risk = abs(new_plan["entry"] - new_plan["stop"])
    old_risk = abs(old_plan["entry"] - old_plan["stop"])
    if risk <= 0 or old_risk <= 0:
        return False
    base = max(risk, old_risk)
    drift = max(abs(new_plan["entry"] - old_plan["entry"]),
                abs(new_plan["stop"] - old_plan["stop"]))
    return (drift / base >= AMEND_DRIFT_FRAC
            and drift / new_plan["entry"] * 100 >= AMEND_MIN_PRICE_PCT)


def _credential() -> str:
    """Active channel credential ('' when unconfigured)."""
    if _cfg["channel"] == "wecom":
        return _cfg.get("wecomKey") or ""
    return _cfg.get("token") or ""


async def _send(title: str, content: str) -> tuple[bool, str]:
    channel = _cfg["channel"]
    if channel != "wecom":
        # 颜色标签为企微 markdown 方言；pushplus 等通道剥除后按纯文本/markdown 发送
        content = _FONT_RE.sub("", content)
    return await notify.send_by_channel(channel, _credential(), title, content)


# 企业微信机器人 markdown 单条上限 4096 字节（2026-09-04 00:05 轮 40058
# 整条被拒事故）。分段口径：标题随首条（_post_wecom 将标题拼进首行，占量
# 计入）、按 \n\n 块边界贪心装包、单块超限按字节硬切、条间 0.4s 防限频
# （机器人 20 条/分钟额度内安全）。上限留余量给标题行与边界。
_WECOM_LIMIT = 3800


def _utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


def _hard_cut(s: str, limit: int) -> tuple[str, str]:
    """按 UTF-8 字节硬切 s，切点不破多字节字符。返回 (head, rest)。"""
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s, ""
    cut = limit
    while cut > 0 and (b[cut] & 0xC0) == 0x80:
        cut -= 1
    return b[:cut].decode("utf-8"), b[cut:].decode("utf-8")


def _split_content(title: str, content: str,
                   limit: int = _WECOM_LIMIT) -> list[str]:
    """把 (title 拼首行后的) 完整正文按字节上限分段，段边界优先 \n\n。"""
    full = f"**{title}**\n{content}"  # wecom 发送形态，量按此计
    if _utf8_len(full) <= limit:
        return [content]
    chunks: list[str] = []
    # 首段可用量要扣掉标题行
    first_budget = limit - _utf8_len(f"**{title}**\n")
    blocks = content.split("\n\n")
    cur = ""
    budget = first_budget
    for blk in blocks:
        piece = blk if not cur else cur + "\n\n" + blk
        if _utf8_len(piece) <= budget:
            cur = piece
            continue
        # 装不下：先把 cur 收尾
        if cur:
            chunks.append(cur)
            cur = ""
            budget = limit
        # 单块自身超限 → 硬切
        if _utf8_len(blk) > budget:
            rest = blk
            while _utf8_len(rest) > budget:
                head, rest = _hard_cut(rest, budget)
                chunks.append(head)
                budget = limit
            cur = rest
        else:
            cur = blk
    if cur:
        chunks.append(cur)
    return chunks


async def _send_chunked(title: str, content: str) -> tuple[bool, str]:
    """分段发送入口：单条够用走 _send 原路径（行为零变化），超限拆多条
    连发，首条带标题，返回聚合 (ok, error)。"""
    chunks = _split_content(title, content)
    if len(chunks) == 1:
        return await _send(title, content)
    channel = _cfg["channel"]
    if channel != "wecom":
        content = _FONT_RE.sub("", content)
        chunks = _split_content(title, content)
    for i, ch in enumerate(chunks):
        t = title if i == 0 else f"{title}（{i + 1}/{len(chunks)}）"
        ok, err = await notify.send_by_channel(channel, _credential(), t, ch)
        if not ok:
            return False, f"分段 {i + 1}/{len(chunks)} 失败: {err}"
        if i + 1 < len(chunks):
            await asyncio.sleep(0.4)
    return True, f"分 {len(chunks)} 条发送"


# ---------------------------------------------------------------- analysis

async def _safe_analysis(symbol: str, interval: str) -> dict | None:
    """Shared closed-bar analysis (context.closed_bar_analysis, round 55)."""
    return await closed_bar_analysis(symbol, interval)


def _plan_state(symbol: str, analysis: dict | None) -> dict | None:
    if not analysis:
        return None
    plan = (analysis.get("summary") or {}).get("tradePlan")
    summary = analysis.get("summary") or {}
    if not plan:
        return {"hasPlan": False, "score": summary.get("score"),
                "bias": summary.get("bias")}
    return {
        "hasPlan": True,
        "direction": plan["direction"],
        "entry": plan["entry"],
        "stop": plan["stop"],
        "target1": plan.get("target1"),
        "beTrigger": plan.get("beTrigger"),
        "trailRef": _trail_ref(plan),
        "trailDist": _trail_dist(plan),
        "fillBars": plan.get("fillBars"),
        "score": summary.get("score"),
        "bias": summary.get("bias"),
    }


# ---------------------------------------------------------------- push cycle

async def _run_once(allow_push: bool) -> None:
    global _cfg
    _state["lastRun"] = int(time.time() * 1000)

    symbols = [s for s in _cfg["symbols"] if s]
    intervals = _norm_intervals(_cfg.get("intervals"))
    jobs = [(s, itv) for itv in intervals for s in symbols]
    analyses = await asyncio.gather(*[_safe_analysis(s, itv) for s, itv in jobs])

    plans: dict[tuple[str, str], dict | None] = {}
    states: dict[str, dict[str, dict | None]] = {itv: {} for itv in intervals}
    failed: set[tuple[str, str]] = set()
    for (sym, itv), analysis in zip(jobs, analyses):
        if analysis is None:
            failed.add((sym, itv))
            plans[(sym, itv)] = None
            states[itv][sym] = None
        else:
            plan = (analysis.get("summary") or {}).get("tradePlan")
            plans[(sym, itv)] = plan
            states[itv][sym] = _plan_state(sym, analysis)
    _state["planStates"] = states

    # disabled / unconfigured: still refresh planStates for the status preview
    if not _cfg["enabled"] or not _credential():
        if failed and len(failed) == len(jobs):
            _set_error(f"analysis failed for all symbol/interval pairs: {sorted(failed)}")
        return

    if failed and len(failed) == len(jobs):
        _set_error(f"analysis failed for all symbol/interval pairs: {sorted(failed)}")
        return

    def fp_key(sym: str, itv: str) -> str:
        return f"{sym}|{itv}"

    current = {fp_key(sym, itv): p["direction"]
               for (sym, itv), p in plans.items() if p and (sym, itv) not in failed}

    # --- fingerprint comparison (failed pairs keep their previous state) ---
    events: list[tuple[str, str, str, dict | None, str | None]] = []
    if not _cfg["seeded"]:
        if failed:
            # partial seed: keep old fingerprints for failed pairs
            seeded = dict(_cfg["seenPlans"])
            seeded.update(current)
            _cfg["seenPlans"] = seeded
        else:
            _cfg["seenPlans"] = dict(current)
        _cfg["seeded"] = True
        _save()
        if _cfg["mode"] == "events":
            return  # silent seeding: no storm on first sight
    else:
        for sym, itv in jobs:
            if (sym, itv) in failed:
                continue
            k = fp_key(sym, itv)
            prev = _cfg["seenPlans"].get(k)
            cur = current.get(k)
            if prev == cur:
                # direction unchanged: check whether the resting order's
                # entry/stop drifted materially vs the last PUSHED plan
                if cur is not None:
                    amend = _amend_event(k, plans[(sym, itv)])
                    if amend:
                        events.append(amend)
                continue
            if prev is None and cur is not None:
                events.append(("新", sym, itv, plans[(sym, itv)], None))
            elif prev is not None and cur is None:
                # 消失：把上次推送的挂单价一并带上（文案要显示给用户定位挂单），
                # 在 pushedPlans 被清除之前抓取
                last = dict((_cfg.get("pushedPlans") or {}).get(k) or {})
                last["direction"] = prev
                events.append(("消失", sym, itv, None, last))
            else:
                last = dict((_cfg.get("pushedPlans") or {}).get(k) or {})
                last["direction"] = prev
                events.append(("转向", sym, itv, plans[(sym, itv)], last))
        new_seen = dict(_cfg["seenPlans"])
        for sym, itv in jobs:
            if (sym, itv) in failed:
                continue
            d = current.get(fp_key(sym, itv))
            if d:
                new_seen[fp_key(sym, itv)] = d
            else:
                new_seen.pop(fp_key(sym, itv), None)
        _cfg["seenPlans"] = new_seen
        # record the prices we actually pushed this cycle (for next cycle's
        # drift comparison). Pushed = appeared in an event this round, or was
        # already tracked and still has a plan.
        pushed = dict(_cfg.get("pushedPlans") or {})
        for kind, sym, itv, plan, _prev in events:
            if kind in ("新", "转向", "改单") and plan:
                pushed[fp_key(sym, itv)] = {"entry": plan["entry"], "stop": plan["stop"],
                                            "direction": plan["direction"]}
        for sym, itv in jobs:
            if (sym, itv) in failed:
                continue
            if current.get(fp_key(sym, itv)) is None:
                pushed.pop(fp_key(sym, itv), None)  # plan gone: stop tracking
        _cfg["pushedPlans"] = pushed
        _save()

    if _cfg["mode"] == "events":
        if not events:
            if not _cfg.get("heartbeat", True):
                return
            # 心跳：无事件也推一条，让用户确认服务活着（brief 模式本身
            # 每小时必有消息，不需要心跳）。失败键如实标注。
            title = f"CoinLens 无变化 {_now_label()}"
            lines = ["本小时无新计划/转向/消失/改单，服务运行正常。"]
            if failed:
                bad = sorted(f"{s} {i}" for s, i in failed)
                lines.append(f"⚠️ 本轮分析失败（未参与比对）：{'、'.join(bad)}")
            content = "\n".join(lines)
        else:
            title = f"CoinLens 信号 {_now_label()}"
            content = _events_content(events)
    else:  # brief
        title = f"CoinLens 每小时提示 {_now_label()}"
        content = _brief_content(intervals, symbols, plans, failed)

    if not allow_push:
        return
    ok, error = await _send_chunked(title, content)
    _record_push(title, ok, error)
    if not ok:
        _set_error(f"push failed: {error}")


def _next_slot_ts(now_ts: float) -> float:
    dt = datetime.fromtimestamp(now_ts)
    slot = dt.replace(minute=5, second=0, microsecond=0)
    if slot <= dt:
        slot += timedelta(hours=1)
    return slot.timestamp()


async def _loop() -> None:
    # initial silent pass shortly after startup: seeds fingerprints and fills
    # plan states for the status API without pushing anything
    await asyncio.sleep(10)
    try:
        await _run_once(allow_push=False)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _set_error(f"{type(e).__name__}: {e}")
    while True:
        try:
            target = _next_slot_ts(time.time())
            await asyncio.sleep(max(1.0, target - time.time()))
            await _run_once(allow_push=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _set_error(f"{type(e).__name__}: {e}")
            await asyncio.sleep(60)


# ---------------------------------------------------------------- public API

def start() -> None:
    """Start the hourly loop (called from FastAPI startup)."""
    global _task
    _load()
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


def update_config(enabled: bool | None = None, mode: str | None = None,
                  symbols: list[str] | None = None, intervals: list[str] | None = None,
                  token: str | None = None, channel: str | None = None,
                  wecom_key: str | None = None, heartbeat: bool | None = None) -> None:
    """Apply a config patch and re-seed the plan watcher (avoids a storm of
    fake 'new plan' events for symbols added / intervals changed)."""
    global _cfg
    if enabled is not None:
        _cfg["enabled"] = bool(enabled)
    if mode is not None:
        _cfg["mode"] = mode
    if heartbeat is not None:
        _cfg["heartbeat"] = bool(heartbeat)
    if symbols is not None:
        _cfg["symbols"] = symbols
    if intervals is not None:
        _cfg["intervals"] = _norm_intervals(intervals)
    if token is not None:
        _cfg["token"] = token.strip()
    if channel is not None:
        _cfg["channel"] = channel
    if wecom_key is not None:
        # accept a full webhook URL or just the key
        key = wecom_key.strip()
        if "key=" in key:
            key = key.split("key=", 1)[1].split("&", 1)[0]
        _cfg["wecomKey"] = key
    _cfg["seeded"] = False
    _cfg["seenPlans"] = {}
    _cfg["pushedPlans"] = {}
    _state["planStates"] = {}
    _save()


async def send_now(title: str, content: str) -> tuple[bool, str]:
    """One-off send through the active channel (used by /notify/test)."""
    return await _send(title, content)


def credential_hint() -> str:
    if _cfg["channel"] == "wecom":
        return "先配置企业微信群机器人 webhook（POST /api/notify {\"channel\":\"wecom\",\"wecomKey\":\"...\"}）"
    return "先配置 pushplus token（POST /api/notify {\"token\":\"...\"}）"


def current_token() -> str:
    return _credential()


def record_test_push(title: str, ok: bool, error: str) -> None:
    """Record a manual /notify/test send into recent history."""
    _record_push(title, ok, error)


def status() -> dict:
    token = _cfg.get("token") or ""
    masked = (token[:4] + "***" + token[-4:]) if len(token) > 8 else ("***" if token else "")
    key = _cfg.get("wecomKey") or ""
    key_masked = (key[:6] + "***" + key[-4:]) if len(key) > 12 else ("***" if key else "")
    next_run = None
    if _task is not None and not _task.done() and _cfg["enabled"] and _credential():
        next_run = int(_next_slot_ts(time.time()) * 1000)
    return {
        "enabled": _cfg["enabled"],
        "mode": _cfg["mode"],
        "channel": _cfg["channel"],
        "heartbeat": bool(_cfg.get("heartbeat", True)),
        "symbols": _cfg["symbols"],
        "intervals": _norm_intervals(_cfg.get("intervals")),
        "tokenSet": bool(token),
        "tokenMasked": masked,
        "wecomKeySet": bool(key),
        "wecomKeyMasked": key_masked,
        "lastRun": _state["lastRun"],
        "nextRun": next_run,
        "lastError": _state["lastError"],
        "recent": _state["recent"],
        "planStates": _state["planStates"],
    }
