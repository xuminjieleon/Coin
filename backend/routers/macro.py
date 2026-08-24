"""Macro linkage endpoint: BTC vs macro assets correlations."""
from fastapi import APIRouter

from services import macro

router = APIRouter(prefix="/api")


@router.get("/macro")
async def get_macro():
    return await macro.macro_snapshot()
