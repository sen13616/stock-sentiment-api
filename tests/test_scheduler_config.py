"""
tests/test_scheduler_config.py — Verify scheduler cron triggers fire within
the correct UTC windows, and that the scoring tick job is correctly configured.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from apscheduler.triggers.cron import CronTrigger


def _market_trigger() -> CronTrigger:
    """Reconstruct the market_job trigger from scheduler.py configuration."""
    return CronTrigger(day_of_week="mon-fri", hour="14-20", minute="*/15", timezone="UTC")


def test_market_job_fires_within_utc_market_window():
    """Next fire time after Mon 13:00 UTC must be within 14:00-20:45 UTC."""
    trigger = _market_trigger()
    # Monday 2026-05-04 at 13:00 UTC (before market open)
    ref = datetime(2026, 5, 4, 13, 0, tzinfo=timezone.utc)
    nxt = trigger.get_next_fire_time(None, ref)
    assert nxt is not None
    # Must fire at 14:00 UTC (first eligible slot)
    assert nxt.hour == 14
    assert nxt.minute == 0


def test_market_job_does_not_fire_before_14_utc():
    """Trigger must not fire during 9-13 UTC (old broken window)."""
    trigger = _market_trigger()
    # Monday at 09:00 UTC
    ref = datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc)
    nxt = trigger.get_next_fire_time(None, ref)
    assert nxt is not None
    # Should jump to 14:00, not fire at 09:00 or 09:15
    assert nxt.hour >= 14


def test_market_job_last_fire_is_2045_utc():
    """Last fire in a session should be 20:45 UTC, not 21:00 or later."""
    trigger = _market_trigger()
    # Monday at 20:46 UTC — just past last fire
    ref = datetime(2026, 5, 4, 20, 46, tzinfo=timezone.utc)
    nxt = trigger.get_next_fire_time(None, ref)
    assert nxt is not None
    # Should be next day (Tuesday) at 14:00
    assert nxt.day == 5
    assert nxt.hour == 14


# ---------------------------------------------------------------------------
# Sprint 3 — scoring tick tests
# ---------------------------------------------------------------------------


def test_scoring_tick_job_registered():
    """scoring_tick job must be registered with 30-minute interval."""
    from pipeline.scheduler import scheduler

    job = scheduler.get_job("scoring_tick")
    assert job is not None, "scoring_tick job not found in scheduler"
    # IntervalTrigger stores interval as a timedelta
    assert job.trigger.interval.total_seconds() == 30 * 60


async def test_scoring_tick_calls_score_all_for_active_tickers():
    """scoring_tick_job must call _score_all with the full ticker list + preloaded sector map (Sprint P4.2)."""
    mock_tickers = ["AAPL", "MSFT", "GOOGL"]
    mock_sector_map = {
        "AAPL":  "Information Technology",
        "MSFT":  "Information Technology",
        "GOOGL": "Communication Services",
    }

    with (
        patch("pipeline.scheduler.get_active_tickers", new_callable=AsyncMock, return_value=mock_tickers),
        patch("pipeline.scheduler.get_ticker_sector_map", new_callable=AsyncMock, return_value=mock_sector_map),
        patch("pipeline.scheduler._score_all", new_callable=AsyncMock, return_value=(3, 12)) as mock_score_all,
        patch("pipeline.scheduler._record_run", new_callable=AsyncMock),
    ):
        from pipeline.scheduler import scoring_tick_job
        await scoring_tick_job()

    mock_score_all.assert_called_once_with(mock_tickers, "SCORING_TICK", sector_map=mock_sector_map)


async def test_score_and_write_recomputes_all_four_layers():
    """_score_and_write must call all 4 layer scorers — none skipped."""
    now = datetime.now(timezone.utc)
    from pipeline.scoring.subindices import SubIndexResult

    si = SubIndexResult(50.0, 1, ["test"])

    with (
        patch("pipeline.orchestrator.read_scored_state", new_callable=AsyncMock, return_value=None),
        patch("pipeline.orchestrator.get_latest_close", new_callable=AsyncMock, return_value=100.0),
        patch("pipeline.orchestrator._score_market", new_callable=AsyncMock, return_value=(si, [], now)) as mock_market,
        patch("pipeline.orchestrator._score_narrative", new_callable=AsyncMock, return_value=(si, [], now)) as mock_narrative,
        patch("pipeline.orchestrator._score_influencer", new_callable=AsyncMock, return_value=(si, [], now, now, now)) as mock_influencer,
        patch("pipeline.orchestrator._score_macro", new_callable=AsyncMock, return_value=(si, [], now)) as mock_macro,
        patch("pipeline.orchestrator.compute_confidence", return_value=__import__("pipeline.confidence.scorer", fromlist=["ConfidenceResult"]).ConfidenceResult(score=80, flags=[])),
        patch("pipeline.orchestrator.write_scored_state", new_callable=AsyncMock),
        patch("pipeline.orchestrator.persist_scored_state", new_callable=AsyncMock),
    ):
        from pipeline.orchestrator import _score_and_write
        await _score_and_write("AAPL")

    mock_market.assert_called_once()
    mock_narrative.assert_called_once()
    mock_influencer.assert_called_once()
    mock_macro.assert_called_once()
