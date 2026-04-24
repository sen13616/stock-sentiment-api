"""
api/routes/sentiment.py

GET /v1/sentiment/{ticker}

Returns the latest pre-computed sentiment score for a ticker.

Query parameters
----------------
detail  : 'summary' (default) or 'full'.  Full is only available on Pro tier.
refresh : boolean.  If true, queues a background re-score.  Returns the
          current cached score immediately (the refresh runs asynchronously).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.auth import authenticate
from api.rate_limit import check_rate_limit
from api.response.assembler import assemble
from api.response.schemas import ErrorResponse, FreeTierResponse, NoDataResponse, ProTierResponse
from db.queries.universe import get_active_tickers, is_supported_ticker
from pipeline.orchestrator import score_ticker

router = APIRouter()
_log   = logging.getLogger(__name__)
_bearer = HTTPBearer(auto_error=False)


@router.get(
    "/sentiment/{ticker}",
    response_model=None,
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
    },
)
async def get_sentiment(
    ticker:           str,
    request:          Request,
    background_tasks: BackgroundTasks,
    detail:           str  = Query(default="summary", pattern="^(summary|full)$"),
    refresh:          bool = Query(default=False),
    tier:             str  = Depends(authenticate),
) -> FreeTierResponse | ProTierResponse | NoDataResponse:
    # ── Rate limiting ─────────────────────────────────────────────────────────
    auth_header = request.headers.get("authorization", "")
    raw_token   = auth_header.removeprefix("Bearer ").strip()
    await check_rate_limit(raw_token, tier)

    # ── Ticker validation ─────────────────────────────────────────────────────
    ticker = ticker.upper()
    if not await is_supported_ticker(ticker):
        return NoDataResponse(
            ticker  = ticker,
            status  = "ticker_not_found",
            message = f"{ticker} is not in the supported universe",
        )

    # ── Optional background refresh ───────────────────────────────────────────
    if refresh:
        background_tasks.add_task(_refresh_ticker, ticker)

    # ── Assemble and return ───────────────────────────────────────────────────
    return await assemble(ticker, tier, detail)


async def _refresh_ticker(ticker: str) -> None:
    try:
        await score_ticker(ticker)
    except Exception as exc:
        _log.warning("background refresh failed for %s: %s", ticker, exc)
