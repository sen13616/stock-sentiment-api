"""
api/rate_limit.py

Per-tier sliding-window rate limiting backed by Redis.

Limits
------
    free  — 10 requests / minute
    pro   — 120 requests / minute

Each API key gets its own Redis counter key that expires after 60 seconds.
On the first request in a window the key is set with a 60-second TTL; each
subsequent request within the same minute increments the counter.  If the
counter exceeds the tier limit, HTTP 429 is raised.
"""
from __future__ import annotations

import hashlib

from fastapi import HTTPException

from db.redis import get_redis

_LIMITS: dict[str, int] = {
    "free": 10,
    "pro":  120,
}


async def check_rate_limit(raw_token: str, tier: str) -> None:
    """
    Raise HTTP 429 if the caller has exceeded their per-minute quota.

    Parameters
    ----------
    raw_token : The plaintext Bearer token (used only to derive a Redis key).
    tier      : 'free' or 'pro'.
    """
    client = get_redis()
    key_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    redis_key = f"rate:{key_hash}"

    count = await client.incr(redis_key)
    if count == 1:
        # First request in this window — set TTL
        await client.expire(redis_key, 60)

    limit = _LIMITS.get(tier, _LIMITS["free"])
    if count > limit:
        raise HTTPException(
            status_code=429,
            detail={
                "error":   "rate_limit_exceeded",
                "message": "Too many requests for your tier",
            },
        )
