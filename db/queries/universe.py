"""
db/queries/universe.py

All ticker_universe table operations.
"""
from __future__ import annotations

from db.connection import get_pool


async def get_active_tickers() -> list[str]:
    """
    Return all tier1_supported tickers from the active universe.

    Returns
    -------
    list[str] sorted alphabetically.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ticker
            FROM ticker_universe
            WHERE tier = 'tier1_supported'
            ORDER BY ticker
            """
        )
    return [r["ticker"] for r in rows]


async def is_supported_ticker(ticker: str) -> bool:
    """Return True if ticker is in the active universe (any tier)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM ticker_universe WHERE ticker = $1",
            ticker.upper(),
        )
    return row is not None


async def get_all_tickers() -> list[dict]:
    """
    Return all tickers in the universe sorted alphabetically.

    Each dict has:
        ticker       : str  — the ticker symbol
        company_name : str | None — full company name (None if not yet seeded)
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT ticker, company_name FROM ticker_universe ORDER BY ticker"
        )
    return [{"ticker": r["ticker"], "company_name": r["company_name"]} for r in rows]
