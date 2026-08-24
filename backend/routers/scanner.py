"""Whole-market scanner endpoint."""
from fastapi import APIRouter, HTTPException, Query

from routers.analysis import ALLOWED_INTERVALS
from services import scanner

router = APIRouter(prefix="/api")


@router.get("/scan")
async def get_scan(
    interval: str = Query(default="4h"),
    top: int = Query(default=40, ge=10, le=80),
):
    if interval not in ALLOWED_INTERVALS:
        raise HTTPException(status_code=400, detail=f"interval must be one of {sorted(ALLOWED_INTERVALS)}")
    return await scanner.scan_market(interval, top)
