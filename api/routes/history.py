"""
api/routes/history.py

GET /v1/sentiment/{ticker}/history

Returns historical sentiment scores for a ticker.  Pro tier only.

Query parameters
----------------
days     : Lookback window in days.  Default 30, max 365.
interval : 'daily' (default, one record per day) or 'raw' (every scoring cycle).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from api.auth import authenticate
from api.response.labels import score_to_label
from api.response.schemas import ErrorResponse, HistoryEntry, HistoryResponse, HistorySubIndices
from db.queries.sentiment_history import get_history
from db.queries.universe import is_supported_ticker

router = APIRouter()


@router.get(
    "/sentiment/{ticker}/history",
    response_model=HistoryResponse,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
async def get_sentiment_history(
    ticker:   str,
    days:     int = Query(default=30, ge=1, le=365),
    interval: str = Query(default="daily", pattern="^(daily|raw)$"),
    tier:     str = Depends(authenticate),
) -> HistoryResponse:
    # Pro tier only
    if tier != "pro":
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "message": "History endpoint requires Pro tier"},
        )

    ticker = ticker.upper()
    if not await is_supported_ticker(ticker):
        raise HTTPException(
            status_code=404,
            detail={"error": "ticker_not_found", "message": f"Ticker {ticker!r} is not in the supported universe"},
        )

    rows = await get_history(ticker, days=days, interval=interval)

    entries = [
        HistoryEntry(
            timestamp   = row["timestamp"],
            score       = int(round(row["composite_score"])),
            label       = score_to_label(int(round(row["composite_score"]))),
            confidence  = int(row["confidence_score"]),
            sub_indices = HistorySubIndices(
                market     = row.get("market_index"),
                narrative  = row.get("narrative_index"),
                influencer = row.get("influencer_index"),
                macro      = row.get("macro_index"),
            ),
        )
        for row in rows
    ]

    return HistoryResponse(ticker=ticker, history=entries)
