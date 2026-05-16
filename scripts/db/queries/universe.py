"""
db/queries/universe.py

All ticker_universe table operations.
"""
from __future__ import annotations

from scripts.db.connection import get_pool


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
    """Return True if ticker is in the tier1_supported universe."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM ticker_universe WHERE ticker = $1 AND tier = 'tier1_supported'",
            ticker.upper(),
        )
    return row is not None


async def get_all_tickers() -> list[dict]:
    """
    Return all tickers in the universe sorted alphabetically.

    Each dict has:
        ticker       : str  — the ticker symbol
        company_name : str | None — full company name (None if not yet seeded)
        sector       : str | None — GICS sector (None if not yet seeded; P4.1)
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT ticker, company_name, sector FROM ticker_universe ORDER BY ticker"
        )
    return [
        {
            "ticker":       r["ticker"],
            "company_name": r["company_name"],
            "sector":       r["sector"],
        }
        for r in rows
    ]


async def get_ticker_sector(ticker: str) -> str | None:
    """
    Return the GICS sector name for `ticker`, or None if not seeded.

    Sprint P4.1 — added for use by the per-ticker macro sub-index in P4.2.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT sector FROM ticker_universe WHERE ticker = $1",
            ticker.upper(),
        )


async def get_ticker_sector_map() -> dict[str, str]:
    """
    Return ``{ticker: sector}`` for every ticker with a non-NULL sector.

    Designed for one-shot preload at the start of ``scoring_tick_job`` so
    per-ticker macro scoring (P4.2) doesn't issue 502 separate DB calls.
    Tickers with a NULL sector are omitted; the macro scorer must handle
    that case (likely by skipping the sector-ETF component and letting
    weight redistribution absorb it).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT ticker, sector FROM ticker_universe WHERE sector IS NOT NULL"
        )
    return {r["ticker"]: r["sector"] for r in rows}
