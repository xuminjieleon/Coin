"""Trade journal endpoints: CRUD + close-with-plan-replay + stats."""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from routers.analysis import ALLOWED_INTERVALS
from services import journal_store

router = APIRouter(prefix="/api/journal")


class TradeInput(BaseModel):
    symbol: str
    interval: str
    direction: str
    entry: float = Field(gt=0)
    stop: float | None = None
    qty: float | None = Field(default=None, gt=0)
    leverage: float | None = Field(default=None, ge=1, le=200)
    openedAt: int | None = None
    plan: dict | None = None  # snapshot of summary.tradePlan at open time
    notes: str | None = None


class CloseInput(BaseModel):
    exit: float = Field(gt=0)
    reason: str  # stop | target | trail | time | manual
    closedAt: int | None = None
    notes: str | None = None


@router.get("/trades")
async def list_trades(
    status: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    return journal_store.list_trades(status=status, symbol=symbol, limit=limit)


@router.post("/trades")
async def create_trade(t: TradeInput):
    if t.interval not in ALLOWED_INTERVALS:
        raise HTTPException(status_code=400, detail=f"interval must be one of {sorted(ALLOWED_INTERVALS)}")
    if t.direction not in ("long", "short"):
        raise HTTPException(status_code=400, detail="direction must be long or short")
    if t.stop is not None:
        if t.direction == "long" and t.stop >= t.entry:
            raise HTTPException(status_code=400, detail="做多止损必须低于入场价")
        if t.direction == "short" and t.stop <= t.entry:
            raise HTTPException(status_code=400, detail="做空止损必须高于入场价")
    return journal_store.create_trade({
        "symbol": t.symbol.upper(), "interval": t.interval, "direction": t.direction,
        "entry": t.entry, "stop": t.stop, "qty": t.qty, "leverage": t.leverage,
        "openedAt": t.openedAt, "plan": t.plan, "notes": t.notes,
    })


@router.post("/trades/{tid}/close")
async def close_trade(tid: int, c: CloseInput):
    if c.reason not in ("stop", "target", "trail", "time", "manual"):
        raise HTTPException(status_code=400, detail="reason must be stop/target/trail/time/manual")
    result = await journal_store.close_trade(tid, c.exit, c.reason, c.closedAt, c.notes)
    if result is None:
        raise HTTPException(status_code=404, detail="trade not found or already closed")
    return result


@router.delete("/trades/{tid}")
async def delete_trade(tid: int):
    if not journal_store.delete_trade(tid):
        raise HTTPException(status_code=404, detail="trade not found")
    return {"ok": True}


@router.get("/stats")
async def journal_stats():
    return journal_store.stats()
