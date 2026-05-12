"""
api/routes/health.py

GET /health

Always returns HTTP 200. Never raises an exception.

Without Authorization header:
    {"status": "ok"}

With Authorization: Bearer <token>:
    {"status": "ok", "tier": "pro" | "free" | null}
    (null when key is invalid or not found)
"""
from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Request

from db.queries.api_keys import get_key_tier

_log = logging.getLogger(__name__)

router = APIRouter()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@router.get("/health")
async def health(request: Request) -> dict:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return {"status": "ok"}

    token = auth_header[len("Bearer "):].strip()
    if not token:
        return {"status": "ok"}

    try:
        key_hash = _hash_token(token)
        tier = await get_key_tier(key_hash)
        return {"status": "ok", "tier": tier}
    except Exception as exc:
        _log.debug("health check tier lookup failed (returning ok): %s", exc)
        return {"status": "ok"}
