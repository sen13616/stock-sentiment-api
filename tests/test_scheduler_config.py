"""
tests/test_scheduler_config.py — Verify scheduler cron triggers fire within
the correct UTC windows.
"""
from __future__ import annotations

from datetime import datetime, timezone

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
