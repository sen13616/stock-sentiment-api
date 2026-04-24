"""
pipeline/orchestrator.py  — Layer 02

Coordinates the full scoring pipeline for a single ticker.

Pipeline flow
-------------
    1. Fetch fresh signals from external APIs  (Layers 03–06, parallel)
    2. Read signals from DB and apply feature engineering  (Layer 07)
    3. Compute per-layer sub-indices  (Layer 08 — subindices)
    4. Compute composite score and divergence  (Layer 08 — composite + divergence)
    5. Compute confidence score  (Layer 09)
    6. Generate explanation  (Layer 10)
    7. Persist results to Redis and PostgreSQL  (System C)

Fallback propagation (per spec §Layer 02)
------------------------------------------
    primary source fails   → fetcher's internal fallback handles it (in sources/)
    entire fetcher fails   → DB has no fresh data for this layer
    no fresh DB data       → use last known state from Redis if within staleness window
    Redis also stale       → mark layer missing, redistribute composite weights,
                             apply confidence penalty

Scheduler vs ad-hoc use
------------------------
The scheduler jobs call the lower-level helpers directly for efficiency:
    - fetch all tickers in parallel with a shared httpx.AsyncClient
    - then call _score_and_write() per ticker (DB read + compute + persist)

score_ticker() is provided for tests and one-off manual runs.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from db.queries.raw_articles import get_articles_since
from db.queries.raw_signals import get_latest_signal, get_signals_since
from pipeline.confidence.scorer import compute_confidence
from pipeline.confidence.staleness import check_staleness, stale_sources
from pipeline.explanation.templates import generate_explanation
from pipeline.features.normalize import (
    score_influencer_signals,
    score_macro_signals,
    score_market_signals,
    score_narrative_signals,
)
from pipeline.persistence.pg_writer import persist_scored_state
from pipeline.persistence.redis_writer import read_scored_state, write_scored_state
from pipeline.scoring.composite import compute_composite
from pipeline.scoring.divergence import compute_divergence
from pipeline.scoring.drivers import extract_drivers
from pipeline.scoring.subindices import SubIndexResult, compute_sub_index
from pipeline.sources.influencer import fetch_influencer_signals
from pipeline.sources.macro import fetch_macro_signals
from pipeline.sources.market import fetch_market_signals
from pipeline.sources.narrative import fetch_narrative_signals

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MACRO_TICKER = "_MACRO_"
_SECTOR_ETFS  = [
    "XLB", "XLC", "XLE", "XLF", "XLI",
    "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
]

# Maximum age of the last-scored timestamp before falling back to Redis cache.
# These are STALENESS thresholds — they determine when a layer's sub-index is
# considered too stale to trust and Redis cached state is used instead.
_LAYER_LOOKBACK: dict[str, timedelta] = {
    "market":     timedelta(minutes=90),
    "narrative":  timedelta(hours=6),
    "influencer": timedelta(days=3),
    "macro":      timedelta(hours=24),
}

# How far back to look in the DB when fetching raw data for scoring.
# Narrative articles are fetched from the last 3 days by the fetcher, so the
# scoring window must match — using the 6-hour staleness threshold here would
# miss articles published more than 6 hours ago.
_NARRATIVE_SCORE_LOOKBACK = timedelta(days=3)

_MARKET_SIGNAL_TYPES = [
    "rsi_14", "return_1d", "return_5d", "return_20d",
    "volume_ratio", "put_call_ratio", "short_interest_ratio", "implied_volatility",
]
_INFLUENCER_SIGNAL_TYPES = [
    "insider_net_shares", "analyst_buy_pct", "analyst_target_price",
]
_MACRO_SIGNAL_TYPES = ["vix", "sector_etf_return_20d"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_tz(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_ts(val: str | datetime | None) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return _ensure_tz(val)
    try:
        return _ensure_tz(datetime.fromisoformat(str(val)))
    except ValueError:
        return None


def _latest_ts(rows: list[dict], key: str = "timestamp") -> datetime | None:
    ts_list = [_ensure_tz(r[key]) for r in rows if r.get(key)]
    return max(ts_list) if ts_list else None


def _fallback_subindex(
    layer: str,
    last_state: dict | None,
    now: datetime,
) -> tuple[SubIndexResult | None, datetime | None]:
    """
    Attempt to recover a sub-index from the last known Redis state.

    Returns (SubIndexResult, as_of) if the cached value is still within
    the layer's lookback window, otherwise (None, None).
    """
    if not last_state:
        return None, None

    freshness = last_state.get("freshness") or {}
    as_of     = _parse_ts(freshness.get(f"{layer}_as_of"))

    if as_of is None or (now - as_of) > _LAYER_LOOKBACK[layer]:
        return None, None

    entry = (last_state.get("sub_indices") or {}).get(layer)
    if entry is None:
        return None, None

    value     = entry.get("value") if isinstance(entry, dict) else float(entry)
    n_signals = entry.get("n_signals", 1) if isinstance(entry, dict) else 1
    sources   = entry.get("sources", [])  if isinstance(entry, dict) else []

    if value is None:
        return None, None

    _log.debug(f"[fallback] using cached {layer} sub-index (as_of={as_of.isoformat()})")
    return SubIndexResult(float(value), n_signals, sources), as_of


# ---------------------------------------------------------------------------
# Per-layer scoring  (read DB → normalize → sub-index → fallback if empty)
# ---------------------------------------------------------------------------

async def _score_market(
    ticker: str,
    now: datetime,
    last_state: dict | None,
) -> tuple[SubIndexResult | None, list[dict], datetime | None]:
    since = now - _LAYER_LOOKBACK["market"]
    raw   = await get_signals_since(ticker, since, _MARKET_SIGNAL_TYPES)
    if raw:
        sigs  = score_market_signals(ticker, raw, now)
        si    = compute_sub_index(sigs)
        as_of = _latest_ts(raw)
        if si is not None:
            return si, sigs, as_of

    si, as_of = _fallback_subindex("market", last_state, now)
    return si, [], as_of


async def _score_narrative(
    ticker: str,
    now: datetime,
    last_state: dict | None,
) -> tuple[SubIndexResult | None, list[dict], datetime | None]:
    since    = now - _NARRATIVE_SCORE_LOOKBACK  # articles published in last 3 days
    articles = await get_articles_since(ticker, since)
    if articles:
        sigs  = score_narrative_signals(ticker, articles, now)
        si    = compute_sub_index(sigs)
        as_of = _latest_ts(articles, key="published_at")
        if si is not None:
            return si, sigs, as_of

    si, as_of = _fallback_subindex("narrative", last_state, now)
    return si, [], as_of


async def _score_influencer(
    ticker: str,
    now: datetime,
    last_state: dict | None,
    current_price: float | None,
) -> tuple[SubIndexResult | None, list[dict], datetime | None, datetime | None, datetime | None]:
    """Returns (subindex, sigs, influencer_as_of, analyst_as_of, insider_as_of)."""
    since = now - _LAYER_LOOKBACK["influencer"]
    raw   = await get_signals_since(ticker, since, _INFLUENCER_SIGNAL_TYPES)
    if raw:
        sigs  = score_influencer_signals(ticker, raw, now, current_price)
        si    = compute_sub_index(sigs)
        as_of = _latest_ts(raw)

        # Separate timestamps for analyst vs insider staleness checks
        insider_rows = [r for r in raw if r["signal_type"] == "insider_net_shares"]
        analyst_rows = [r for r in raw if r["signal_type"] in ("analyst_buy_pct", "analyst_target_price")]
        insider_as_of = _latest_ts(insider_rows)
        analyst_as_of = _latest_ts(analyst_rows)

        if si is not None:
            return si, sigs, as_of, analyst_as_of, insider_as_of

    si, as_of = _fallback_subindex("influencer", last_state, now)
    # Recover individual as_of from last_state freshness if available
    freshness    = (last_state or {}).get("freshness") or {}
    analyst_as_of = _parse_ts(freshness.get("influencer_as_of"))
    insider_as_of = analyst_as_of  # Phase 1 approximation
    return si, [], as_of, analyst_as_of, insider_as_of


async def _score_macro(
    now: datetime,
    last_state: dict | None,
) -> tuple[SubIndexResult | None, list[dict], datetime | None]:
    since  = now - _LAYER_LOOKBACK["macro"]
    # Collect VIX + all sector ETF 20d returns
    vix_rows = await get_signals_since(_MACRO_TICKER, since, ["vix"])
    etf_rows: list[dict] = []
    for etf in _SECTOR_ETFS:
        rows = await get_signals_since(etf, since, ["sector_etf_return_20d"])
        etf_rows.extend(rows)

    all_raw = vix_rows + etf_rows
    if all_raw:
        sigs  = score_macro_signals(all_raw, now)
        si    = compute_sub_index(sigs)
        as_of = _latest_ts(all_raw)
        if si is not None:
            return si, sigs, as_of

    si, as_of = _fallback_subindex("macro", last_state, now)
    return si, [], as_of


# ---------------------------------------------------------------------------
# Core compute + persist  (no external API calls)
# ---------------------------------------------------------------------------

async def _score_and_write(ticker: str) -> dict | None:
    """
    Read all layers from DB, compute the full scored state, and persist it.

    This is the inner scoring loop called by scheduler jobs after fetching.
    No external API calls are made here.

    Returns the assembled state dict, or None if the composite is completely
    missing (all layers failed and no Redis fallback available).
    """
    now        = datetime.now(timezone.utc)
    ticker     = ticker.upper()
    last_state = await read_scored_state(ticker)

    # Current close price (for analyst_target_price normalization)
    current_price: float | None = await get_latest_signal(ticker, "ohlcv_close")

    # ── Layer 07+08: feature engineering + sub-indices ────────────────────────
    (market_si,     market_sigs,     market_as_of)                             = await _score_market(ticker, now, last_state)
    (narrative_si,  narrative_sigs,  narrative_as_of)                          = await _score_narrative(ticker, now, last_state)
    (influencer_si, influencer_sigs, influencer_as_of, analyst_as_of, insider_as_of) = await _score_influencer(ticker, now, last_state, current_price)
    (macro_si,      macro_sigs,      macro_as_of)                              = await _score_macro(now, last_state)

    sub_indices: dict = {
        "market":     market_si,
        "narrative":  narrative_si,
        "influencer": influencer_si,
        "macro":      macro_si,
    }

    # ── Composite + divergence ─────────────────────────────────────────────────
    composite_result           = compute_composite(sub_indices)
    present_values             = {k: v.value for k, v in sub_indices.items() if v is not None}
    div_result, effective_score = compute_divergence(present_values, composite_result.score)

    # ── Staleness and confidence ───────────────────────────────────────────────
    as_of_map = {
        "market":  market_as_of,
        "news":    narrative_as_of,
        "analyst": analyst_as_of,
        "insider": insider_as_of,
        "macro":   macro_as_of,
    }
    stale_list  = stale_sources(as_of_map, now=now)

    all_sigs    = market_sigs + narrative_sigs + influencer_sigs + macro_sigs
    n_signals   = sum(1 for s in all_sigs if (s.get("weight") or 0) > 0)

    conf_result = compute_confidence(
        missing_layers  = composite_result.missing_layers,
        stale_sources   = stale_list,
        n_signals       = n_signals,
        divergence_flag = div_result.flag,
    )

    # ── Drivers + explanation ──────────────────────────────────────────────────
    drivers     = extract_drivers(all_sigs)
    explanation = generate_explanation(drivers)

    # ── Assemble state ─────────────────────────────────────────────────────────
    state: dict = {
        "ticker":          ticker,
        "timestamp":       now,
        "composite_score": round(effective_score, 2),
        "sub_indices": {
            k: {
                "value":     round(v.value, 2),
                "n_signals": v.n_signals,
                "sources":   v.sources,
            } if v is not None else None
            for k, v in sub_indices.items()
        },
        "confidence": {
            "score": conf_result.score,
            "flags": conf_result.flags,
        },
        "top_drivers": [d.to_dict() for d in drivers],
        "explanation": explanation,
        "freshness": {
            "market_as_of":     market_as_of,
            "narrative_as_of":  narrative_as_of,
            "influencer_as_of": influencer_as_of,
            "macro_as_of":      macro_as_of,
        },
        "divergence": div_result.flag,
        "price": {
            "close":  current_price,
            "volume": None,  # populated by market fetcher in raw_signals; snapshot uses close only
        },
    }

    # ── Persist ────────────────────────────────────────────────────────────────
    await write_scored_state(ticker, state)
    await persist_scored_state(state)

    _log.info(
        "[%s] scored  composite=%.1f  confidence=%d  divergence=%s  missing=%s",
        ticker,
        effective_score,
        conf_result.score,
        div_result.flag,
        composite_result.missing_layers or "none",
    )
    return state


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def score_ticker(
    ticker: str,
    layer_scope: str | None = None,
) -> dict | None:
    """
    Fetch fresh data for `layer_scope` then compute and persist the scored state.

    Intended for one-off runs and tests.  Scheduler jobs use the lower-level
    helpers (_score_and_write, fetch_*) for batching efficiency.

    Parameters
    ----------
    ticker      : Ticker symbol (case-insensitive).
    layer_scope : Which layer to freshly fetch from external APIs.
                  None = all four layers.
                  "market" | "narrative" | "influencer" | "macro"

    Returns
    -------
    dict — assembled scored state, or None on unrecoverable error.
    """
    ticker = ticker.upper()

    async with httpx.AsyncClient(timeout=30) as client:
        coros = []
        if layer_scope in (None, "market"):
            coros.append(fetch_market_signals(ticker, client))
        if layer_scope in (None, "narrative"):
            coros.append(fetch_narrative_signals(ticker, client))
        if layer_scope in (None, "influencer"):
            coros.append(fetch_influencer_signals(ticker, client))
        if layer_scope in (None, "macro"):
            coros.append(fetch_macro_signals(client))

        if coros:
            fetch_results = await asyncio.gather(*coros, return_exceptions=True)
            for result in fetch_results:
                if isinstance(result, BaseException):
                    _log.warning("[%s] source fetch error: %s", ticker, result)

    return await _score_and_write(ticker)
