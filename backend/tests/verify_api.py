"""Endpoint verification script against the running backend."""
import json
import sys

import httpx

BASE = "http://127.0.0.1:8000"
failures = []


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {extra}")
    if not cond:
        failures.append(name)


with httpx.Client(base_url=BASE, timeout=30.0) as client:
    # 1. health
    r = client.get("/api/health")
    check("health", r.status_code == 200 and r.json() == {"ok": True}, f"-> {r.text}")

    # 2. symbols
    r = client.get("/api/symbols", params={"q": "btc"})
    syms = r.json()
    check("symbols", r.status_code == 200 and isinstance(syms, list) and len(syms) <= 50
          and all("symbol" in s and "base" in s for s in syms),
          f"-> count={len(syms)}, sample={syms[:3]}")

    # 3. analysis BTCUSDT
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        r = client.get("/api/analysis", params={"symbol": symbol, "interval": "1h", "limit": 500})
        ok = r.status_code == 200
        if not ok:
            check(f"analysis {symbol}", False, f"-> HTTP {r.status_code}: {r.text[:200]}")
            continue
        d = r.json()
        candles = d["candles"]
        ind = d["indicators"]
        n = len(candles)
        times = [c["time"] for c in candles]
        checks = [
            (f"candles n={n}", n == 500),
            ("candles ascending", times == sorted(times)),
            ("candle fields", all(set(c) == {"time", "open", "high", "low", "close", "volume"} for c in candles[:5])),
            ("indicators length", all(len(ind[k]) == n for k in
             ("ema20", "ema50", "ema200", "rsi14", "atr14", "adx14"))),
            ("smc fields", all(k in d["smc"] for k in
             ("swings", "structureEvents", "orderBlocks", "fvgs", "liquidityPools", "premiumDiscount"))),
            ("ob limit", len(d["smc"]["orderBlocks"]) <= 10),
            ("fvg limit", len(d["smc"]["fvgs"]) <= 10),
            ("pool limit", len(d["smc"]["liquidityPools"]) <= 8),
            ("events limit", len(d["smc"]["structureEvents"]) <= 20),
            ("score numeric", isinstance(d["summary"]["score"], (int, float))),
            ("reasons limit", len(d["summary"]["reasons"]) <= 8),
            ("vp fields", all(k in d["volumeProfile"] for k in ("poc", "vah", "val", "bins"))
             and len(d["volumeProfile"]["bins"]) == 48),
        ]
        all_ok = all(c for _, c in checks)
        s = d["summary"]
        print(f"  {symbol}: score={s['score']} bias={s['bias']} regime={s['regime']} "
              f"OBs={len(d['smc']['orderBlocks'])} FVGs={len(d['smc']['fvgs'])} "
              f"pools={len(d['smc']['liquidityPools'])} events={len(d['smc']['structureEvents'])} "
              f"swings={len(d['smc']['swings'])} reasons={len(s['reasons'])}")
        bad = [name for name, c in checks if not c]
        check(f"analysis {symbol}", all_ok, ("-> FAILED: " + ", ".join(bad)) if bad else "")

    # 4. derivatives
    r = client.get("/api/derivatives", params={"symbol": "BTCUSDT"})
    if r.status_code != 200:
        check("derivatives", False, f"-> HTTP {r.status_code}: {r.text[:200]}")
    else:
        d = r.json()
        keys = {"openInterest", "openInterestValue", "oiChangePct24h", "oiHistory",
                "fundingRate", "fundingHistory", "longShortRatio", "longShortHistory",
                "takerBuySellRatio"}
        check("derivatives", set(d.keys()) == keys,
              f"-> OI={d['openInterest']} oiChg24h={d['oiChangePct24h']} "
              f"funding={d['fundingRate']} lsr={d['longShortRatio']} taker={d['takerBuySellRatio']}")

print()
if failures:
    print("FAILURES:", failures)
    sys.exit(1)
print("ALL ENDPOINT CHECKS PASSED")
