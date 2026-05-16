"""
tests/test_batch_query.py — Verify get_signals_since_batch returns correct per-ticker grouping.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


async def test_batch_groups_by_ticker():
    """Batched query returns dict with correct per-ticker signal lists."""
    from scripts.db.queries.raw_signals import get_signals_since_batch

    now = datetime.now(timezone.utc)

    # Simulate raw DB rows (asyncpg Record-like dicts)
    mock_rows = [
        {"ticker": "XLK", "signal_type": "sector_etf_return_20d", "value": 0.05, "source": "computed", "timestamp": now},
        {"ticker": "XLK", "signal_type": "sector_etf_return_20d", "value": 0.04, "source": "computed", "timestamp": now},
        {"ticker": "XLV", "signal_type": "sector_etf_return_20d", "value": -0.02, "source": "computed", "timestamp": now},
    ]

    mock_conn = MagicMock()
    mock_conn.fetch = AsyncMock(return_value=mock_rows)

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_ctx

    with patch("scripts.db.queries.raw_signals.get_pool", new_callable=AsyncMock, return_value=mock_pool):
        result = await get_signals_since_batch(
            tickers=["XLK", "XLV", "XLF"],
            since=now,
            signal_types=["sector_etf_return_20d"],
        )

    assert "XLK" in result
    assert "XLV" in result
    assert "XLF" not in result  # no data for XLF
    assert len(result["XLK"]) == 2
    assert len(result["XLV"]) == 1
    assert result["XLV"][0]["value"] == -0.02


async def test_batch_empty_tickers():
    """Empty tickers list returns empty dict without hitting DB."""
    from scripts.db.queries.raw_signals import get_signals_since_batch

    result = await get_signals_since_batch(
        tickers=[],
        since=datetime.now(timezone.utc),
        signal_types=["vix"],
    )
    assert result == {}


async def test_batch_empty_signal_types():
    """Empty signal_types list returns empty dict without hitting DB."""
    from scripts.db.queries.raw_signals import get_signals_since_batch

    result = await get_signals_since_batch(
        tickers=["XLK"],
        since=datetime.now(timezone.utc),
        signal_types=[],
    )
    assert result == {}
