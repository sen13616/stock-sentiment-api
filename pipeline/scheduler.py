"""
pipeline/scheduler.py

APScheduler configuration for the background scoring pipeline (System A).

Jobs registered:

    market_job       — weekdays 9 am–4 pm UTC, every 15 minutes
    narrative_job    — every 30 minutes (24 × 7)
    influencer_job   — every 6 hours
    macro_job        — daily at 02:00 UTC
    short_volume_job — weekdays at 21:30 UTC (FINRA REGSHO, data only)

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
import yfinance as yf
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
from pipeline.sources.short_volume import ingest_short_volume

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


async def _score_all(
    tickers: list[str],
    job_name: str,
    layers: set[str] | None = None,
) -> tuple[int, int]:
    """
    Score all tickers sequentially to avoid overwhelming the DB.

    Parameters
    ----------
    layers : Passed through to _score_and_write(); only these layers are
             recomputed, others are carried forward from Redis cache.
             None = recompute all layers.

    Returns (fetched_count, total_layers_populated).
    """
    desc = f"[{job_name}] score" if job_name else "score"
    fetched = 0
    total_layers = 0

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
                n_layers = await _score_and_write(ticker, layers=layers)
                fetched += 1
                total_layers += n_layers
            except Exception as exc:
                _log.error(
                    "%s: scoring failed for %s: %s", job_name, ticker, exc, exc_info=True
                )
            pbar.update(1)
            avg = total_layers / fetched if fetched else 0
            pbar.set_postfix_str(f"{ticker} | ✓ {fetched} avg={avg:.1f}/4", refresh=True)

    return fetched, total_layers


def _fmt_elapsed(seconds: float) -> str:
    """Format elapsed seconds as 'Xm Ys'."""
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}m {secs}s"


# ---------------------------------------------------------------------------
# yfinance batch OHLCV download
# ---------------------------------------------------------------------------

async def _yf_batch_download(tickers: list[str]) -> dict[str, dict]:
    """
    Download OHLCV data for all tickers in a single yfinance batch call.

    Runs in a thread executor since yfinance is synchronous.

    Returns
    -------
    dict mapping ticker → {open, high, low, close, volume, timestamp, source}.
    Tickers that fail parsing are silently omitted (logged at WARNING).
    """
    loop = asyncio.get_running_loop()

    def _download():
        return yf.download(
            tickers=tickers,
            period="5d",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )

    try:
        raw = await loop.run_in_executor(None, _download)
    except Exception as exc:
        _log.error("yfinance batch download failed: %s", exc)
        return {}

    if raw.empty:
        _log.warning("yfinance batch download returned empty DataFrame")
        return {}

    result: dict[str, dict] = {}
    for ticker in tickers:
        try:
            if len(tickers) == 1:
                df = raw
            else:
                df = raw[ticker]

            if df.empty:
                continue

            valid = df.dropna(subset=["Close"])
            if valid.empty:
                continue

            last = valid.iloc[-1]
            ts = last.name.to_pydatetime()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            result[ticker] = {
                "open":      float(last["Open"]),
                "high":      float(last["High"]),
                "low":       float(last["Low"]),
                "close":     float(last["Close"]),
                "volume":    float(last["Volume"]),
                "timestamp": ts,
                "source":    "yfinance",
            }
        except Exception as exc:
            _log.warning("yfinance parse error for %s: %s", ticker, exc)

    _log.info("yfinance batch: %d/%d tickers parsed", len(result), len(tickers))
    return result


# ---------------------------------------------------------------------------
# Job functions
# ---------------------------------------------------------------------------

async def market_job() -> None:
    """
    Market layer job — weekdays 9 am–4 pm, every 15 minutes.

    Batch-downloads OHLCV for all tickers via yfinance in one call, then
    fetches RSI and derived signals per-ticker in parallel, and finally
    recomputes composite scores.
    """
    job_counters.reset()
    t_start = time.monotonic()
    _log.info("market_job: starting")
    tickers = await get_active_tickers()
    n = len(tickers)
    _log.info("market_job: %d tickers", n)

    # Batch-download OHLCV for all tickers in one yfinance call
    ohlcv_batch = await _yf_batch_download(tickers)

    async def _market_fetcher(ticker: str, client: httpx.AsyncClient) -> None:
        await fetch_market_signals(ticker, client, ohlcv_batch=ohlcv_batch)

    async with httpx.AsyncClient(timeout=30) as client:
        await _fetch_all_tickers(_market_fetcher, tickers, client, job_name="MARKET")

    fetched, total_layers = await _score_all(tickers, "MARKET", layers={"market"})
    await _record_run("market")

    elapsed = time.monotonic() - t_start
    avg = total_layers / fetched if fetched else 0
    print(
        f"[MARKET] done in {_fmt_elapsed(elapsed)} — {fetched}/{n} fetched, avg {avg:.1f}/4 layers populated",
        file=sys.stderr,
    )
    _log.info(
        "market_job complete: %d/%d fetched, avg %.1f/4 layers, %d rate-limit skips, %d net-error skips",
        fetched, n, avg, job_counters.rate_limit_skips, job_counters.net_error_skips,
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

    ohlcv_batch = await _yf_batch_download(tickers)

    async def _market_fetcher(ticker: str, client: httpx.AsyncClient) -> None:
        await fetch_market_signals(ticker, client, ohlcv_batch=ohlcv_batch)

    async with httpx.AsyncClient(timeout=30) as client:
        await _fetch_all_tickers(_market_fetcher, tickers, client, job_name="MARKET_EOD")

    fetched, total_layers = await _score_all(tickers, "MARKET_EOD", layers={"market"})
    await _record_run("market_eod")

    elapsed = time.monotonic() - t_start
    avg = total_layers / fetched if fetched else 0
    print(
        f"[MARKET_EOD] done in {_fmt_elapsed(elapsed)} — {fetched}/{n} fetched, avg {avg:.1f}/4 layers populated",
        file=sys.stderr,
    )
    _log.info(
        "market_eod_job complete: %d/%d fetched, avg %.1f/4 layers, %d rate-limit skips, %d net-error skips",
        fetched, n, avg, job_counters.rate_limit_skips, job_counters.net_error_skips,
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

    fetched, total_layers = await _score_all(tickers, "NARRATIVE", layers={"narrative"})
    await _record_run("narrative")

    elapsed = time.monotonic() - t_start
    avg = total_layers / fetched if fetched else 0
    print(
        f"[NARRATIVE] done in {_fmt_elapsed(elapsed)} — {fetched}/{n} fetched, avg {avg:.1f}/4 layers populated",
        file=sys.stderr,
    )
    _log.info(
        "narrative_job complete: %d/%d fetched, avg %.1f/4 layers, %d rate-limit skips, %d net-error skips",
        fetched, n, avg, job_counters.rate_limit_skips, job_counters.net_error_skips,
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

    fetched, total_layers = await _score_all(tickers, "INFLUENCER", layers={"influencer"})
    await _record_run("influencer")

    elapsed = time.monotonic() - t_start
    avg = total_layers / fetched if fetched else 0
    print(
        f"[INFLUENCER] done in {_fmt_elapsed(elapsed)} — {fetched}/{n} fetched, avg {avg:.1f}/4 layers populated",
        file=sys.stderr,
    )
    _log.info(
        "influencer_job complete: %d/%d fetched, avg %.1f/4 layers, %d rate-limit skips, %d net-error skips",
        fetched, n, avg, job_counters.rate_limit_skips, job_counters.net_error_skips,
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
    fetched, total_layers = await _score_all(tickers, "MACRO", layers={"macro"})
    await _record_run("macro")

    elapsed = time.monotonic() - t_start
    avg = total_layers / fetched if fetched else 0
    print(
        f"[MACRO] done in {_fmt_elapsed(elapsed)} — {fetched}/{n} fetched, avg {avg:.1f}/4 layers populated",
        file=sys.stderr,
    )
    _log.info(
        "macro_job complete: %d/%d fetched, avg %.1f/4 layers, %d rate-limit skips, %d net-error skips",
        fetched, n, avg, job_counters.rate_limit_skips, job_counters.net_error_skips,
    )


async def short_volume_job() -> None:
    """
    Short volume job — weekdays at 21:30 UTC (30 min after market close).

    Fetches the latest FINRA REGSHO daily short volume file, filters to the
    active ticker universe, and writes three signals per ticker:
    short_volume_otc, short_volume_total_otc, short_volume_ratio_otc.

    This is a data-ingestion-only job — the signal types are not yet
    registered in the scoring pipeline.
    """
    t_start = time.monotonic()
    _log.info("short_volume_job: starting")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            n_tickers = await ingest_short_volume(client)
    except Exception as exc:
        _log.error("short_volume_job: failed: %s", exc, exc_info=True)
        n_tickers = 0

    await _record_run("short_volume")

    elapsed = time.monotonic() - t_start
    print(
        f"[SHORT_VOLUME] done in {_fmt_elapsed(elapsed)} — {n_tickers} tickers written",
        file=sys.stderr,
    )
    _log.info("short_volume_job complete: %d tickers in %.1fs", n_tickers, elapsed)


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

scheduler.add_job(
    short_volume_job,
    trigger=CronTrigger(day_of_week="mon-fri", hour=21, minute=30),
    id="short_volume",
    name="FINRA short volume (21:30 UTC weekdays)",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=600,
)
