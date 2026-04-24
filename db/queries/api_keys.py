"""
db/queries/api_keys.py

Lookup and update operations for the api_keys table.
"""
from __future__ import annotations

from datetime import datetime, timezone

from db.connection import get_pool


async def get_key_tier(key_hash: str) -> str | None:
    """
    Return the tier for an active API key hash, or None if not found / inactive.

    Also updates last_used_at on each successful lookup.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE api_keys
               SET last_used_at = $2
             WHERE key_hash = $1
               AND is_active  = TRUE
            RETURNING tier
            """,
            key_hash,
            datetime.now(timezone.utc),
        )
    return row["tier"] if row else None
