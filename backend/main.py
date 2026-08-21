import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import analysis, backtest, calendar, derivatives, symbols

app = FastAPI(title="CoinLens")

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


@app.get("/api/health")
async def health():
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000)
