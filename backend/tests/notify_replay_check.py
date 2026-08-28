"""Diagnose "no push events since X": replay the notifier's decision path
(run_analysis) at each bar boundary and list plan-direction transitions —
i.e. what SHOULD have fired as 【新】/【转向】/【消失】events.

Reproduces services/notifier.py exactly (same run_analysis call, limit 500,
fingerprint = symbol|interval -> direction, entry drift does not re-fire),
but in asOf replay mode: asOf = bar open time → that bar is the last row
with full data.

Honest fidelity notes vs the live notifier on the deployment machine:
- live klines include the FORMING bar (decided intra-bar); replay uses
  closed bars — consecutive closed-bar states bound what live saw at each
  hourly run. Transient intra-bar flips (there and back within one bar)
  are not reconstructed.
- live funding/OI components use live values; replay uses fully-closed
  daily rows (derivs_store.daily_rates) — small score deltas possible
  near the ±10 (4h) / ±25 (1h) plan thresholds.
- this machine's klines come from the same Binance priority chain.

Usage: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/notify_replay_check.py
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import derivs_store
from services.analysis.context import run_analysis

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
H1_MS, H4_MS = 3_600_000, 14_400_000
NOW_MS = int(datetime.now(timezone.utc).timestamp() * 1000)


def ts(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def bj(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000 + 8 * 3600, tz=timezone.utc) \
        .strftime("%m-%d %H:%M")


async def main() -> None:
    print("[backfill] daily_rates (Gate.io -> Binance fallback)...")
    for s in SYMBOLS:
        try:
            src = await derivs_store.ensure_backfill(s)
            print(f"  {s}: {src}")
        except Exception as exc:
            print(f"  {s}: FAILED {exc!r}")

    marks = {
        "4h": [ts("2026-08-26 00:00") + i * H4_MS for i in range(13)],  # .. 08-28 00:00 UTC
        "1h": [ts("2026-08-27 08:00") + i * H1_MS for i in range(20)],  # .. 08-28 03:00 UTC
    }
    step = {"4h": H4_MS, "1h": H1_MS}

    sem = asyncio.Semaphore(8)

    async def one(sym: str, itv: str, m: int):
        async with sem:
            try:
                a = await run_analysis(sym, itv, 500, as_of=m)
                plan = (a.get("summary") or {}).get("tradePlan")
                sc = (a.get("summary") or {}).get("score")
                return (itv, m, sym, plan["direction"] if plan else None, sc)
            except Exception as exc:
                return (itv, m, sym, "FAIL", repr(exc)[:90])

    jobs = [(s, itv, m) for itv, ms in marks.items() for m in ms for s in SYMBOLS]
    results = await asyncio.gather(*[one(*j) for j in jobs])

    table: dict = {itv: {} for itv in marks}
    for itv, m, sym, d, sc in results:
        table[itv].setdefault(m, {})[sym] = (d, sc)

    for itv in ("4h", "1h"):
        ms = marks[itv]
        print(f"\n{'='*100}\n===== {itv} 各根K线收盘口径的计划状态"
              f"（asOf=bar open；最后一行为当前未收盘K）=====\n{'='*100}")
        print(f"{'K线open(UTC)':<15}{'北京':<12}" +
              "".join(f"{s[:-4]:>16}" for s in SYMBOLS))
        transitions = []
        prev = None
        for m in ms:
            cells = []
            cur = {}
            for s in SYMBOLS:
                d, sc = table[itv][m][s]
                cur[s] = d
                if d == "FAIL":
                    tag = "FAIL"
                elif d is None:
                    tag = f"无计划{sc:+.0f}"
                else:
                    tag = f"{'多' if d == 'long' else '空'}{sc:+.0f}"
                cells.append(f"{tag:>16}")
            forming = " (未收盘)" if m + step[itv] > NOW_MS else ""
            print(datetime.fromtimestamp(m / 1000, tz=timezone.utc)
                  .strftime("%m-%d %H:%M") .ljust(15) +
                  bj(m).ljust(12) + "".join(cells) + forming)
            if prev is not None:
                for s in SYMBOLS:
                    if cur[s] != prev[s]:
                        transitions.append((m, s, prev[s], cur[s]))
            prev = cur

        print(f"\n{itv} 状态变化（每一条=本应推送一个事件；在新K线周期内某次整点+5 触发）: "
              f"{len(transitions)}")
        for m, s, a, b in transitions:
            fa = "无计划" if a is None else ("做多" if a == "long" else "做空")
            fb = "无计划" if b is None else ("做多" if b == "long" else "做空")
            win = f"{bj(m)}~{bj(m + step[itv])}"
            print(f"  {s:<9} {fa} -> {fb}   （北京 {win}）")

    print("\n注：表内为收盘口径决策；实盘 notifier 每整点+5 用含未收盘K的实时数据，"
          "状态介于前后两根收盘K之间；偶发的盘中翻转-翻回无法事后重建。")


if __name__ == "__main__":
    asyncio.run(main())
