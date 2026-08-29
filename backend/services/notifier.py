"""Hourly WeChat push of trade-plan signals (PushPlus channel).

Design (agreed with the user, 2026-08-27):
- Runs inside the FastAPI process (startup task, strong ref anti-GC — same
  pattern as the derivatives background backfill).
- Fires at HH:05 every hour so the 1h candle is closed and cached.
- Decision path is EXACTLY the API's: services.analysis.context.run_analysis.
- mode "events" (default): push only on new plan / direction flip / plan
  gone, one aggregated message. First cycle after (re)seed is silent.
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
- Fingerprint = "symbol|interval" -> direction (entry drift does NOT
  re-fire, same semantics as the frontend plan watcher in
  frontend/src/utils/alerts.ts).
- Message prices need NO user-side math (2026-08-28): trail plans (4h/1d/1w)
  print the trail DISTANCE in price units and the concrete stop right after
  the half-off (journal replay semantics: stop = max(entry, MFE -
  trailR*risk)) instead of the old "trail activation" price, which was below
  the actual arming level on 4h/1d and required the user to compute
  trailR x risk by hand.
- Symbols whose analysis FAILED are excluded from fingerprint comparison
  (a network hiccup must not push a fake "plan gone").
- Config + state persist to backend/data/notify.json (gitignored: token).
"""

import asyncio
import json
import math
import time
from datetime import datetime, timedelta
from pathlib import Path

from services import notify
from services.analysis.context import ALLOWED_INTERVALS, NoKlinesError, STEP_MS, run_analysis

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "notify.json"

MODES = {"events", "brief"}

_EPOCH_MONDAY_MS = 4 * 86_400_000  # weekly bars open Monday 00:00 UTC (epoch was a Thursday)


def _last_closed_open(now_ms: int, interval: str) -> int:
    """Open time of the newest bar that has already CLOSED at now_ms.
    UTC-epoch aligned for 1h/4h/1d; weekly anchored to Monday."""
    step = STEP_MS[interval]
    anchor = _EPOCH_MONDAY_MS if interval == "1w" else 0
    return (now_ms - anchor) // step * step + anchor - step

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
}


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


def _plan_lines(plan: dict) -> list[str]:
    """Multi-line block for one plan (events mode). Trail semantics follow
    journal_store.replay_plan: at beTrigger half exits and the stop becomes
    max(entry, MFE - trailR*risk) — all printed as concrete prices."""
    long = plan["direction"] == "long"
    lines = [
        f"入场 {_fmt(plan['entry'])}（回踩限价）",
        f"止损 {_fmt(plan['stop'])}",
    ]
    if plan.get("target1") is not None:
        lines.append(f"止盈 {_fmt(plan['target1'])}")
    dist = _trail_dist(plan)
    if dist is None:
        lines.append(f"{_fmt(plan['beTrigger'])} 减半保本（半仓止盈，此后止损提到入场价）")
    else:
        lines.append(f"{_fmt(plan['beTrigger'])} 减半保本（半仓止盈，此后启用跟踪止损）")
        if long:
            z = max(plan["entry"], plan["beTrigger"] - dist)
            lines.append(f"跟踪止损 = 最高价−{_fmt(dist)}（半仓离场时止损设 {_fmt(z)}，只上移，不低于入场价）")
        else:
            z = min(plan["entry"], plan["beTrigger"] + dist)
            lines.append(f"跟踪止损 = 最低价+{_fmt(dist)}（半仓离场时止损设 {_fmt(z)}，只下移，不高于入场价）")
    return lines


def _plan_block(sym: str, itv: str, plan: dict) -> str:
    """【sym itv direction】 header + concrete-price lines (one plan block)."""
    head = f"【{sym} {itv} {'做多' if plan['direction'] == 'long' else '做空'}】"
    return head + "\n" + "\n".join(_plan_lines(plan))


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


def _credential() -> str:
    """Active channel credential ('' when unconfigured)."""
    if _cfg["channel"] == "wecom":
        return _cfg.get("wecomKey") or ""
    return _cfg.get("token") or ""


async def _send(title: str, content: str) -> tuple[bool, str]:
    return await notify.send_by_channel(_cfg["channel"], _credential(), title, content)


# ---------------------------------------------------------------- analysis

async def _safe_analysis(symbol: str, interval: str) -> dict | None:
    """Closed-bar-only analysis (option B, 2026-08-29): replay semantics with
    as_of = newest CLOSED bar's open time (kline_cache end_time is inclusive
    and self-heals missing pages, so the just-closed bar is always fetched).
    Guard: if the returned last candle is not the requested as_of bar (data
    gap / partial fetch), treat this round as failed — the previous
    fingerprint stands, never compare against stale data."""
    try:
        as_of = _last_closed_open(int(time.time() * 1000), interval)
        analysis = await run_analysis(symbol, interval, 500, as_of=as_of)
        candles = analysis.get("candles") or []
        if not candles or int(candles[-1]["time"]) != as_of:
            return None
        return analysis
    except NoKlinesError:
        return None
    except Exception:
        return None


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
                continue
            if prev is None and cur is not None:
                events.append(("新", sym, itv, plans[(sym, itv)], None))
            elif prev is not None and cur is None:
                events.append(("消失", sym, itv, None, prev))
            else:
                events.append(("转向", sym, itv, plans[(sym, itv)], prev))
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
        _save()

    if _cfg["mode"] == "events":
        if not events:
            return
        title = f"CoinLens 信号 {_now_label()}"
        blocks: list[str] = []
        for kind, sym, itv, plan, prev_dir in events:
            tag = f"{sym} {itv} "
            if kind == "消失":
                d = "多头" if prev_dir == "long" else "空头"
                blocks.append(f"【消失】{tag}{d}计划已消失")
                continue
            head = f"【{kind}】{tag}"
            if kind == "转向":
                head += f"{'做多' if prev_dir == 'long' else '做空'} → "
            head += "做多" if plan["direction"] == "long" else "做空"
            blocks.append(head + "\n" + "\n".join(_plan_lines(plan)))
        content = "\n----------\n".join(blocks)
    else:  # brief
        title = f"CoinLens 每小时提示 {_now_label()}"
        content = _brief_content(intervals, symbols, plans, failed)

    if not allow_push:
        return
    ok, error = await _send(title, content)
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
                  wecom_key: str | None = None) -> None:
    """Apply a config patch and re-seed the plan watcher (avoids a storm of
    fake 'new plan' events for symbols added / intervals changed)."""
    global _cfg
    if enabled is not None:
        _cfg["enabled"] = bool(enabled)
    if mode is not None:
        _cfg["mode"] = mode
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
