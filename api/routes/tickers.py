"""
api/routes/tickers.py

GET /v1/tickers

Returns all tickers in the supported universe.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from api.auth import authenticate
from api.response.schemas import TickersResponse
from db.queries.universe import get_all_tickers

router = APIRouter()


@router.get("/tickers", response_model=TickersResponse)
async def list_tickers(
    tier: str = Depends(authenticate),
) -> TickersResponse:
    tickers = await get_all_tickers()
    return TickersResponse(universe_size=len(tickers), tickers=tickers)
