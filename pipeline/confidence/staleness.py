"""
pipeline/confidence/staleness.py

Checks whether each data source's most-recent timestamp is within the
spec-defined freshness threshold.

Thresholds (spec §Layer 09)
----------------------------
    market   : 90 minutes
    news     : 6 hours  (lower bound of the 6–12 hour range)
    analyst  : 3 days   (lower bound of the 3–5 day range)
    insider  : 30 days  (lower bound of the 30–45 day range)
    macro    : 24 hours

A source is stale if:
  - its as_of timestamp is None (no data received), OR
  - (now − as_of) > threshold

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

STALENESS_THRESHOLDS: dict[str, timedelta] = {
    "market":  timedelta(minutes=90),
    "news":    timedelta(hours=6),
    "analyst": timedelta(days=3),
    "insider": timedelta(days=30),
    "macro":   timedelta(hours=24),
}


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

    result: dict[str, bool] = {}
    for source, threshold in STALENESS_THRESHOLDS.items():
        ts = as_of.get(source)
        if ts is None:
            result[source] = True
        else:
            # Ensure tz-aware comparison
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
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
