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
import sys
import time
from datetime import datetime, timezone

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from tqdm import tqdm as _tqdm
from tqdm.asyncio import tqdm as _atqdm

from db.queries.universe import get_active_tickers
from db.redis import get_redis
from pipeline.orchestrator import _score_and_write
from pipeline.rate_limits import job_counters
from pipeline.sources.influencer import fetch_influencer_signals
from pipeline.sources.macro import fetch_macro_signals
from pipeline.sources.market import fetch_market_signals
from pipeline.sources.narrative import fetch_narrative_signals

_log = logging.getLogger(__name__)

_RUN_KEY_TTL = 7 * 24 * 3600  # 7 days

# tqdm bar format used for both fetch and score phases
_BAR_FMT = "{desc}: {bar:20} {n}/{total} [{elapsed}<{remaining}, {rate_fmt}]{postfix}"


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

async def _fetch_all_tickers(
    fetcher,
    tickers: list[str],
    client: httpx.AsyncClient,
    job_name: str = "",
) -> None:
    """
    Fetch signals for all tickers in parallel, throttled by per-source semaphores.

    A tqdm progress bar (written to stderr) shows live completion count, elapsed
    time, ETA, and rate-limit / network-error skip counts from job_counters.
    """
    desc = f"[{job_name}] fetch" if job_name else "fetch"
    pbar = _atqdm(
        total=len(tickers),
        desc=desc,
        unit="tkr",
        file=sys.stderr,
        dynamic_ncols=True,
        bar_format=_BAR_FMT,
    )

    async def _tracked(ticker: str) -> None:
        try:
            await fetcher(ticker, client)
        except Exception as exc:
            _log.warning("fetch error for %s: %s", ticker, exc)
        finally:
            pbar.update(1)
            pbar.set_postfix_str(
                f"✗ rl={job_counters.rate_limit_skips} net={job_counters.net_error_skips}",
                refresh=False,
            )

    await asyncio.gather(*[_tracked(t) for t in tickers])
    pbar.close()


async def _score_all(tickers: list[str], job_name: str) -> int:
    """
    Score all tickers sequentially to avoid overwhelming the DB.

    A tqdm progress bar (written to stderr) shows the current ticker, live
    scored count, elapsed time, and ETA.  Returns the count of successfully
    scored tickers.
    """
    desc = f"[{job_name}] score" if job_name else "score"
    scored = 0

    with _tqdm(
        total=len(tickers),
        desc=desc,
        unit="tkr",
        file=sys.stderr,
        dynamic_ncols=True,
        bar_format=_BAR_FMT,
    ) as pbar:
        for ticker in tickers:
            try:
                await _score_and_write(ticker)
                scored += 1
            except Exception as exc:
                _log.error(
                    "%s: scoring failed for %s: %s", job_name, ticker, exc, exc_info=True
                )
            pbar.update(1)
            pbar.set_postfix_str(f"{ticker} | ✓ {scored}", refresh=True)

    return scored


def _fmt_elapsed(seconds: float) -> str:
    """Format elapsed seconds as 'Xm Ys'."""
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}m {secs}s"


# ---------------------------------------------------------------------------
# Job functions
# ---------------------------------------------------------------------------

async def market_job() -> None:
    """
    Market layer job — weekdays 9 am–4 pm, every 15 minutes.

    Fetches OHLCV, RSI, options, and derived signals for all tier-1 tickers
    in parallel, then recomputes composite scores.
    """
    job_counters.reset()
    t_start = time.monotonic()
    _log.info("market_job: starting")
    tickers = await get_active_tickers()
    n = len(tickers)
    _log.info("market_job: %d tickers", n)

    async with httpx.AsyncClient(timeout=30) as client:
        await _fetch_all_tickers(fetch_market_signals, tickers, client, job_name="MARKET")

    scored = await _score_all(tickers, "MARKET")
    await _record_run("market")

    elapsed = time.monotonic() - t_start
    print(
        f"[MARKET] done in {_fmt_elapsed(elapsed)} — {scored}/{n} scored, {n - scored} skipped",
        file=sys.stderr,
    )
    _log.info(
        "market_job complete: %d/%d tickers scored, %d rate-limit skips, %d net-error skips",
        scored, n, job_counters.rate_limit_skips, job_counters.net_error_skips,
    )


async def market_eod_job() -> None:
    """
    End-of-day market job — weekdays at 21:15 UTC (15 min after close).

    Captures a fresh end-of-day score so that Redis always holds a valid
    market sub-index before the overnight / weekend period begins.  Without
    this job, users hitting the API outside market hours would see a null
    market layer for the entire overnight period.
    """
    job_counters.reset()
    t_start = time.monotonic()
    _log.info("market_eod_job: starting")
    tickers = await get_active_tickers()
    n = len(tickers)
    _log.info("market_eod_job: %d tickers", n)

    async with httpx.AsyncClient(timeout=30) as client:
        await _fetch_all_tickers(fetch_market_signals, tickers, client, job_name="MARKET_EOD")

    scored = await _score_all(tickers, "MARKET_EOD")
    await _record_run("market_eod")

    elapsed = time.monotonic() - t_start
    print(
        f"[MARKET_EOD] done in {_fmt_elapsed(elapsed)} — {scored}/{n} scored, {n - scored} skipped",
        file=sys.stderr,
    )
    _log.info(
        "market_eod_job complete: %d/%d tickers scored, %d rate-limit skips, %d net-error skips",
        scored, n, job_counters.rate_limit_skips, job_counters.net_error_skips,
    )


async def narrative_job() -> None:
    """
    Narrative layer job — every 30 minutes.

    Fetches news articles for all tickers, deduplicates, and recomputes scores.
    """
    job_counters.reset()
    t_start = time.monotonic()
    _log.info("narrative_job: starting")
    tickers = await get_active_tickers()
    n = len(tickers)

    async with httpx.AsyncClient(timeout=30) as client:
        await _fetch_all_tickers(fetch_narrative_signals, tickers, client, job_name="NARRATIVE")

    scored = await _score_all(tickers, "NARRATIVE")
    await _record_run("narrative")

    elapsed = time.monotonic() - t_start
    print(
        f"[NARRATIVE] done in {_fmt_elapsed(elapsed)} — {scored}/{n} scored, {n - scored} skipped",
        file=sys.stderr,
    )
    _log.info(
        "narrative_job complete: %d/%d tickers scored, %d rate-limit skips, %d net-error skips",
        scored, n, job_counters.rate_limit_skips, job_counters.net_error_skips,
    )


async def influencer_job() -> None:
    """
    Influencer layer job — every 6 hours.

    Fetches SEC insider filings and analyst signals for all tickers.
    """
    job_counters.reset()
    t_start = time.monotonic()
    _log.info("influencer_job: starting")
    tickers = await get_active_tickers()
    n = len(tickers)

    async with httpx.AsyncClient(timeout=30) as client:
        await _fetch_all_tickers(fetch_influencer_signals, tickers, client, job_name="INFLUENCER")

    scored = await _score_all(tickers, "INFLUENCER")
    await _record_run("influencer")

    elapsed = time.monotonic() - t_start
    print(
        f"[INFLUENCER] done in {_fmt_elapsed(elapsed)} — {scored}/{n} scored, {n - scored} skipped",
        file=sys.stderr,
    )
    _log.info(
        "influencer_job complete: %d/%d tickers scored, %d rate-limit skips, %d net-error skips",
        scored, n, job_counters.rate_limit_skips, job_counters.net_error_skips,
    )


async def macro_job() -> None:
    """
    Macro layer job — daily at 02:00 UTC.

    Fetches VIX and sector ETF data once (global, not per-ticker), then
    recomputes composite scores for all tickers using fresh macro signals.
    """
    job_counters.reset()
    t_start = time.monotonic()
    _log.info("macro_job: starting")

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            await fetch_macro_signals(client)
        except Exception as exc:
            _log.error("macro_job: macro fetch failed: %s", exc, exc_info=True)

    tickers = await get_active_tickers()
    n = len(tickers)
    scored = await _score_all(tickers, "MACRO")
    await _record_run("macro")

    elapsed = time.monotonic() - t_start
    print(
        f"[MACRO] done in {_fmt_elapsed(elapsed)} — {scored}/{n} scored, {n - scored} skipped",
        file=sys.stderr,
    )
    _log.info(
        "macro_job complete: %d/%d tickers scored, %d rate-limit skips, %d net-error skips",
        scored, n, job_counters.rate_limit_skips, job_counters.net_error_skips,
    )


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
