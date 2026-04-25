"""
pipeline/scheduler.py

APScheduler configuration for the background scoring pipeline (System A).

Four jobs are registered, each responsible for one data layer:

    market_job      — weekdays 9 am–4 pm UTC, every 15 minutes
    narrative_job   — every 30 minutes (24 × 7)
    influencer_job  — every 6 hours
    macro_job       — daily at 02:00 UTC

Each job:
  1. Fetches fresh signals from external APIs (in parallel for per-ticker layers)
  2. Calls _score_and_write() for every ticker in the active universe

The macro fetch is global (not per-ticker), so it is executed once before the
per-ticker scoring loop to avoid 500 duplicate API calls.

Usage
-----
    from pipeline.scheduler import scheduler
    scheduler.start()   # call from FastAPI lifespan
    scheduler.shutdown()
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from db.queries.universe import get_active_tickers
from db.redis import get_redis
from pipeline.orchestrator import _score_and_write
from pipeline.sources.influencer import fetch_influencer_signals
from pipeline.sources.macro import fetch_macro_signals
from pipeline.sources.market import fetch_market_signals
from pipeline.sources.narrative import fetch_narrative_signals

_log = logging.getLogger(__name__)

_RUN_KEY_TTL = 7 * 24 * 3600  # 7 days


async def _record_run(job_id: str) -> None:
    """Write the current UTC timestamp to Redis for /v1/status."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        await get_redis().set(f"pipeline:last_run:{job_id}", now, ex=_RUN_KEY_TTL)
    except Exception as exc:
        _log.warning("_record_run failed for %s: %s", job_id, exc)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _fetch_all_tickers(fetcher, tickers: list[str], client: httpx.AsyncClient) -> None:
    """Fetch signals for all tickers in parallel, logging but not re-raising errors."""
    results = await asyncio.gather(
        *[fetcher(t, client) for t in tickers],
        return_exceptions=True,
    )
    for ticker, result in zip(tickers, results):
        if isinstance(result, BaseException):
            _log.warning("fetch error for %s: %s", ticker, result)


async def _score_all(tickers: list[str], job_name: str) -> None:
    """Score all tickers sequentially to avoid overwhelming the DB."""
    for ticker in tickers:
        try:
            await _score_and_write(ticker)
        except Exception as exc:
            _log.error("%s: scoring failed for %s: %s", job_name, ticker, exc, exc_info=True)


# ---------------------------------------------------------------------------
# Job functions
# ---------------------------------------------------------------------------

async def market_job() -> None:
    """
    Market layer job — weekdays 9 am–4 pm, every 15 minutes.

    Fetches OHLCV, RSI, options, and derived signals for all tier-1 tickers
    in parallel, then recomputes composite scores.
    """
    _log.info("market_job: starting")
    tickers = await get_active_tickers()
    _log.info("market_job: %d tickers", len(tickers))

    async with httpx.AsyncClient(timeout=30) as client:
        await _fetch_all_tickers(fetch_market_signals, tickers, client)

    await _score_all(tickers, "market_job")
    await _record_run("market")
    _log.info("market_job: complete")


async def market_eod_job() -> None:
    """
    End-of-day market job — weekdays at 21:15 UTC (15 min after close).

    Captures a fresh end-of-day score so that Redis always holds a valid
    market sub-index before the overnight / weekend period begins.  Without
    this job, users hitting the API outside market hours would see a null
    market layer for the entire overnight period.
    """
    _log.info("market_eod_job: starting")
    tickers = await get_active_tickers()
    _log.info("market_eod_job: %d tickers", len(tickers))

    async with httpx.AsyncClient(timeout=30) as client:
        await _fetch_all_tickers(fetch_market_signals, tickers, client)

    await _score_all(tickers, "market_eod_job")
    await _record_run("market_eod")
    _log.info("market_eod_job: complete — scored %d tickers", len(tickers))


async def narrative_job() -> None:
    """
    Narrative layer job — every 30 minutes.

    Fetches news articles for all tickers, deduplicates, and recomputes scores.
    """
    _log.info("narrative_job: starting")
    tickers = await get_active_tickers()

    async with httpx.AsyncClient(timeout=30) as client:
        await _fetch_all_tickers(fetch_narrative_signals, tickers, client)

    await _score_all(tickers, "narrative_job")
    await _record_run("narrative")
    _log.info("narrative_job: complete")


async def influencer_job() -> None:
    """
    Influencer layer job — every 6 hours.

    Fetches SEC insider filings and analyst signals for all tickers.
    """
    _log.info("influencer_job: starting")
    tickers = await get_active_tickers()

    async with httpx.AsyncClient(timeout=30) as client:
        await _fetch_all_tickers(fetch_influencer_signals, tickers, client)

    await _score_all(tickers, "influencer_job")
    await _record_run("influencer")
    _log.info("influencer_job: complete")


async def macro_job() -> None:
    """
    Macro layer job — daily at 02:00 UTC.

    Fetches VIX and sector ETF data once (global, not per-ticker), then
    recomputes composite scores for all tickers using fresh macro signals.
    """
    _log.info("macro_job: starting")

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            await fetch_macro_signals(client)
        except Exception as exc:
            _log.error("macro_job: macro fetch failed: %s", exc, exc_info=True)

    tickers = await get_active_tickers()
    await _score_all(tickers, "macro_job")
    await _record_run("macro")
    _log.info("macro_job: complete")


# ---------------------------------------------------------------------------
# Scheduler instance
# ---------------------------------------------------------------------------

scheduler = AsyncIOScheduler(timezone="UTC")

scheduler.add_job(
    market_job,
    trigger=CronTrigger(day_of_week="mon-fri", hour="9-16", minute="*/15"),
    id="market",
    name="Market data (15 min, market hours)",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=60,
)

scheduler.add_job(
    market_eod_job,
    trigger=CronTrigger(day_of_week="mon-fri", hour=21, minute=15),
    id="market_eod",
    name="Market EOD snapshot (21:15 UTC weekdays)",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=300,
)

scheduler.add_job(
    narrative_job,
    trigger=IntervalTrigger(minutes=30),
    id="narrative",
    name="Narrative sentiment (30 min)",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=120,
)

scheduler.add_job(
    influencer_job,
    trigger=IntervalTrigger(hours=6),
    id="influencer",
    name="Influencer activity (6 h)",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=300,
)

scheduler.add_job(
    macro_job,
    trigger=CronTrigger(hour=2, minute=0),
    id="macro",
    name="Macro context (daily 02:00 UTC)",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=600,
)
