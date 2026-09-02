import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import (
    analysis,
    backtest,
    calendar,
    derivatives,
    executor,
    journal,
    macro,
    market,
    notify,
    portfolio,
    position,
    scanner,
    sources,
    symbols,
)
from services import binance, binance_trade, executor as executor_service, gateio, notifier, sysproxy

app = FastAPI(title="CoinLens")


@app.on_event("startup")
async def _start_background() -> None:
    notifier.start()
    # the executor must never take the whole backend down with it (W5):
    # a locked/corrupt executor.db must not kill the 24/7 notifier
    try:
        executor_service.start()
    except Exception as exc:  # noqa: BLE001
        import sys
        print(f"[executor] start failed, executor disabled: {exc}", file=sys.stderr)


@app.on_event("shutdown")
async def _close_http_clients() -> None:
    # executor first: cancel its loops BEFORE closing the HTTP clients so an
    # in-flight tick cannot complete real order placements during teardown
    # (W6); binance_trade also refuses requests after close_client()
    try:
        await executor_service.stop()
    except Exception:  # noqa: BLE001
        pass
    await binance.close_client()
    await gateio.close_client()
    await sysproxy.close_client()
    await notify.close_client()
    await binance_trade.close_client()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
)

app.include_router(symbols.router)
app.include_router(analysis.router)
app.include_router(derivatives.router)
app.include_router(backtest.router)
app.include_router(calendar.router)
app.include_router(position.router)
app.include_router(market.router)
app.include_router(macro.router)
app.include_router(scanner.router)
app.include_router(journal.router)
app.include_router(portfolio.router)
app.include_router(sources.router)
app.include_router(notify.router)
app.include_router(executor.router)


@app.get("/api/health")
async def health():
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000)
