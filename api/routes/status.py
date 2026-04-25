"""
api/routes/status.py

GET /v1/status

Returns API health and last pipeline run timestamps.

Run timestamps are stored in Redis by the scheduler jobs under keys:
    pipeline:last_run:{job_name}   →  ISO-8601 string (UTC)

If Redis has no record for a job, the key is absent from the response (None).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter

from api.response.schemas import StatusResponse
from db.redis import get_redis
from pipeline.confidence.staleness import is_market_hours

router = APIRouter()
_log   = logging.getLogger(__name__)

_JOB_KEYS = {
    "last_market_run":     "pipeline:last_run:market",
    "last_narrative_run":  "pipeline:last_run:narrative",
    "last_influencer_run": "pipeline:last_run:influencer",
    "last_macro_run":      "pipeline:last_run:macro",
    "last_eod_run":        "pipeline:last_run:market_eod",
}


async def _read_ts(key: str) -> datetime | None:
    try:
        client = get_redis()
        raw = await client.get(key)
        if raw is None:
            return None
        return datetime.fromisoformat(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception as exc:
        _log.warning("status: failed to read %s: %s", key, exc)
        return None


@router.get("/status", response_model=StatusResponse)
async def get_status() -> StatusResponse:
    timestamps = {field: await _read_ts(key) for field, key in _JOB_KEYS.items()}
    return StatusResponse(
        status         = "operational",
        market_is_open = is_market_hours(datetime.now(timezone.utc)),
        **timestamps,
    )
