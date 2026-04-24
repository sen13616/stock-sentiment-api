"""
tests/integration/test_sources.py

Integration tests for the four pipeline source fetchers.
Each test makes real external API calls and writes to the database.

Requires:
  - Local PostgreSQL running (sentimentapi DB seeded with backfill data)
  - Valid API keys in .env

Run with:
    pytest -m integration
"""
import pytest

from db.connection import close_pool, init_pool
from pipeline.sources.influencer import fetch_influencer_signals
from pipeline.sources.macro import fetch_macro_signals
from pipeline.sources.market import fetch_market_signals
from pipeline.sources.narrative import fetch_narrative_signals

TICKER = "AAPL"


@pytest.fixture(scope="module")
async def db_pool():
    await init_pool()
    yield
    await close_pool()


@pytest.mark.integration
async def test_market_signals(db_pool):
    """fetch_market_signals completes without exception for a live ticker."""
    await fetch_market_signals(TICKER)


@pytest.mark.integration
async def test_narrative_signals(db_pool):
    """fetch_narrative_signals completes without exception for a live ticker."""
    await fetch_narrative_signals(TICKER)


@pytest.mark.integration
async def test_influencer_signals(db_pool):
    """fetch_influencer_signals completes without exception for a live ticker."""
    await fetch_influencer_signals(TICKER)


@pytest.mark.integration
async def test_macro_signals(db_pool):
    """fetch_macro_signals completes without exception."""
    await fetch_macro_signals()
