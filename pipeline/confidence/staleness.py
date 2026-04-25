"""
pipeline/confidence/staleness.py

Checks whether each data source's most-recent timestamp is within the
spec-defined freshness threshold.

Thresholds (spec §Layer 09)
----------------------------
    market   : 90 minutes during market hours; market-hours-aware outside
    news     : 6 hours  (lower bound of the 6–12 hour range)
    analyst  : 3 days   (lower bound of the 3–5 day range)
    insider  : 30 days  (lower bound of the 30–45 day range)
    macro    : 24 hours

A source is stale if:
  - its as_of timestamp is None (no data received), OR
  - (now − as_of) > threshold

Market staleness is market-hours-aware.  A score produced at Friday
21:15 UTC (end-of-day run) remains fresh all weekend and is not penalised
simply because markets are closed.  The 90-minute threshold only applies
while markets are actually open.

Usage
-----
    from pipeline.confidence.staleness import check_staleness

    stale = check_staleness({
        "market":  datetime(2026, 4, 24, 10, 0, tzinfo=timezone.utc),
        "news":    None,  # missing → stale
        ...
    })
    # stale == {"market": False, "news": True, ...}
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# US equity market hours in UTC (9:30 am – 4:00 pm Eastern = 14:30 – 21:00 UTC)
_MARKET_OPEN_UTC  = (14, 30)   # (hour, minute)
_MARKET_CLOSE_UTC = (21,  0)

# How long after the most-recent market close the end-of-day score is still fresh.
# Set to 30 minutes so the EOD job at 21:15 UTC has plenty of margin.
_EOD_GRACE = timedelta(minutes=30)

STALENESS_THRESHOLDS: dict[str, timedelta] = {
    "market":  timedelta(minutes=90),
    "news":    timedelta(hours=6),
    "analyst": timedelta(days=3),
    "insider": timedelta(days=30),
    "macro":   timedelta(hours=24),
}


def is_market_hours(now: datetime) -> bool:
    """
    Return True if *now* falls within US equity market hours.

    Market hours: weekdays 14:30–21:00 UTC (9:30 am – 4:00 pm Eastern).
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    # isoweekday: Monday=1 … Friday=5, Saturday=6, Sunday=7
    if now.isoweekday() > 5:
        return False
    open_minutes  = _MARKET_OPEN_UTC[0]  * 60 + _MARKET_OPEN_UTC[1]
    close_minutes = _MARKET_CLOSE_UTC[0] * 60 + _MARKET_CLOSE_UTC[1]
    now_minutes   = now.hour * 60 + now.minute
    return open_minutes <= now_minutes < close_minutes


def _last_market_close(now: datetime) -> datetime:
    """
    Return the UTC datetime of the most-recent weekday market close (21:00 UTC).

    If today is a weekday and markets have already closed, that is today's 21:00.
    If today is a weekday before open, or a weekend, walk back to the previous
    weekday's 21:00.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    close_today = now.replace(hour=_MARKET_CLOSE_UTC[0], minute=_MARKET_CLOSE_UTC[1],
                               second=0, microsecond=0)

    candidate = now if now >= close_today else now - timedelta(days=1)
    # Walk back until we land on a weekday
    while candidate.isoweekday() > 5:
        candidate -= timedelta(days=1)

    return candidate.replace(hour=_MARKET_CLOSE_UTC[0], minute=_MARKET_CLOSE_UTC[1],
                              second=0, microsecond=0)


def _market_stale(ts: datetime, now: datetime) -> bool:
    """
    Market-hours-aware staleness check for the "market" source.

    During market hours    : stale if (now − ts) > 90 minutes.
    Outside market hours   : stale if ts is older than the most-recent market
                             close plus EOD_GRACE (30 min). This means an
                             end-of-day score produced at 21:15 UTC stays
                             fresh through the entire overnight / weekend
                             period.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    if is_market_hours(now):
        return (now - ts) > STALENESS_THRESHOLDS["market"]

    # Outside market hours: compare ts against the last close
    last_close = _last_market_close(now)
    # Score is fresh if it was produced at or after the last close
    # (with a 30-minute grace window so the EOD job has time to run)
    freshness_cutoff = last_close - _EOD_GRACE
    return ts < freshness_cutoff


def check_staleness(
    as_of: dict[str, datetime | None],
    now: datetime | None = None,
) -> dict[str, bool]:
    """
    Return a dict of {source_name: is_stale} for each key in
    STALENESS_THRESHOLDS that appears in `as_of`.

    Sources present in STALENESS_THRESHOLDS but absent from `as_of`
    are treated as stale (no data = stale).

    Parameters
    ----------
    as_of : dict mapping source name → last-fetched datetime (UTC) or None.
    now   : reference time (defaults to datetime.now(UTC)).

    Returns
    -------
    dict[str, bool]  — True = stale, False = fresh.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    result: dict[str, bool] = {}
    for source, threshold in STALENESS_THRESHOLDS.items():
        ts = as_of.get(source)
        if ts is None:
            result[source] = True
            continue

        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        if source == "market":
            result[source] = _market_stale(ts, now)
        else:
            result[source] = (now - ts) > threshold

    return result


def stale_sources(
    as_of: dict[str, datetime | None],
    now: datetime | None = None,
) -> list[str]:
    """
    Convenience wrapper — returns only the names of stale sources.

    Useful for passing directly to pipeline.confidence.scorer.compute_confidence.
    """
    return [src for src, is_stale in check_staleness(as_of, now=now).items() if is_stale]
