"""
api/auth.py

API key authentication.

Incoming Bearer tokens are SHA-256 hashed and looked up in the api_keys
table.  The tier string ('free' or 'pro') is returned on success; an HTTP
401 is raised on failure.
"""
from __future__ import annotations

import hashlib

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from db.queries.api_keys import get_key_tier

_bearer = HTTPBearer(auto_error=False)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def authenticate(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> str:
    """
    FastAPI dependency.  Returns the caller's tier ('free' or 'pro').

    Raises
    ------
    HTTPException(401) — missing, malformed, or inactive API key.
    """
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "message": "Missing API key"},
        )
    key_hash = _hash_token(credentials.credentials)
    tier = await get_key_tier(key_hash)
    if tier is None:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "message": "Invalid or missing API key"},
        )
    return tier
