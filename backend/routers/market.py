"""Market microstructure endpoints: order book, liquidations, on-chain."""
from fastapi import APIRouter, Query

from services import liquidations, microstructure, onchain

router = APIRouter(prefix="/api")


@router.get("/orderbook")
async def get_orderbook(symbol: str = Query(...)):
    return await microstructure.orderbook_snapshot(symbol)


@router.get("/liquidations")
async def get_liquidations(symbol: str = Query(...)):
    return await liquidations.liquidation_snapshot(symbol)


@router.get("/onchain")
async def get_onchain():
    return await onchain.onchain_snapshot()
