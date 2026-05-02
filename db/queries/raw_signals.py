"""
db/queries/raw_signals.py

All raw_signals table operations. No raw SQL anywhere else in the codebase.
"""
from __future__ import annotations

from datetime import datetime

from db.connection import get_pool


async def get_signals_since(
    ticker: str,
    since: datetime,
    signal_types: list[str] | None = None,
) -> list[dict]:
    """
    Return raw_signals rows for `ticker` with timestamp >= `since`.

    Parameters
    ----------
    ticker       : Ticker symbol (or '_MACRO_' for global signals).
    since        : Earliest timestamp to include.
    signal_types : Optional allowlist of signal_type values.

    Returns
    -------
    list of dicts with keys: signal_type, value, source, timestamp.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if signal_types:
            rows = await conn.fetch(
                """
                SELECT signal_type, value, source, timestamp
                FROM raw_signals
                WHERE ticker      = $1
                  AND timestamp  >= $2
                  AND signal_type = ANY($3::text[])
                ORDER BY timestamp DESC
                """,
                ticker,
                since,
                signal_types,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT signal_type, value, source, timestamp
                FROM raw_signals
                WHERE ticker     = $1
                  AND timestamp >= $2
                ORDER BY timestamp DESC
                """,
                ticker,
                since,
            )
    return [dict(r) for r in rows]


async def insert_signals(rows: list[tuple]) -> None:
    """Bulk-insert (ticker, signal_type, value, source, upload_type, timestamp) tuples."""
    if not rows:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO raw_signals
                (ticker, signal_type, value, source, upload_type, timestamp)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            rows,
        )


async def get_close_history(
    ticker: str,
    limit: int = 25,
) -> list[tuple[datetime, float]]:
    """
    Return the most recent `limit` close-price rows sorted ascending.
    Accepts both legacy ohlcv_close (backfill/Polygon) and yf_close (yfinance).
    Includes both manual_backfill and live rows so returns can be computed
    against the most recent available close.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT timestamp, value
            FROM raw_signals
            WHERE ticker      = $1
              AND signal_type IN ('ohlcv_close', 'yf_close')
            ORDER BY timestamp DESC
            LIMIT $2
            """,
            ticker,
            limit,
        )
    return [(r["timestamp"], float(r["value"])) for r in reversed(rows)]


async def get_volume_history(
    ticker: str,
    limit: int = 25,
) -> list[float]:
    """
    Return the most recent `limit` volume values sorted ascending.
    Accepts both legacy ohlcv_volume (backfill/Polygon) and yf_volume (yfinance).
    Used for computing volume_ratio = current_vol / avg_vol.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT value
            FROM raw_signals
            WHERE ticker      = $1
              AND signal_type IN ('ohlcv_volume', 'yf_volume')
            ORDER BY timestamp DESC
            LIMIT $2
            """,
            ticker,
            limit,
        )
    return [float(r["value"]) for r in reversed(rows)]


async def get_latest_signal(ticker: str, signal_type: str) -> float | None:
    """Return the most recent value for a given signal_type, or None if absent."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            """
            SELECT value
            FROM raw_signals
            WHERE ticker      = $1
              AND signal_type = $2
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            ticker,
            signal_type,
        )
    return float(val) if val is not None else None


async def get_signal_history(
    ticker: str,
    signal_type: str,
    limit: int = 20,
) -> list[float]:
    """
    Return the most recent `limit` values for a given signal_type, oldest first.

    Used for rolling z-score normalizers (e.g. short_volume_ratio_otc) that
    need a lookback window of historical daily values.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT value
            FROM raw_signals
            WHERE ticker      = $1
              AND signal_type = $2
            ORDER BY timestamp DESC
            LIMIT $3
            """,
            ticker,
            signal_type,
            limit,
        )
    return [float(r["value"]) for r in reversed(rows)]


async def get_latest_close(ticker: str) -> float | None:
    """Return the most recent close price (yf_close or ohlcv_close), or None."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            """
            SELECT value
            FROM raw_signals
            WHERE ticker      = $1
              AND signal_type IN ('yf_close', 'ohlcv_close')
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            ticker,
        )
    return float(val) if val is not None else None
