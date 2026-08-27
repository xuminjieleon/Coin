import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import (
    analysis,
    backtest,
    calendar,
    derivatives,
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
from services import binance, gateio, notifier, sysproxy

app = FastAPI(title="CoinLens")


@app.on_event("startup")
async def _start_notifier() -> None:
    notifier.start()


@app.on_event("shutdown")
async def _close_http_clients() -> None:
    await binance.close_client()
    await gateio.close_client()
    await sysproxy.close_client()
    await notify.close_client()

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


@app.get("/api/health")
async def health():
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000)
