"""
api/response/assembler.py  — Layer 11

Reads the pre-computed scored state and assembles the JSON response.

Resolution order
----------------
1. Redis key  sentiment:{ticker}          — always-serve-last-known-state
2. sentiment_history table (latest row)   — fallback when Redis is cold
3. NoDataResponse                         — when no data exists at all

Tier filtering
--------------
Free tier  : score, label, confidence, timestamp, cache_age_seconds only.
Pro tier   : all fields including sub_indices, drivers, freshness, explanation.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from db.queries.sentiment_history import get_latest
from db.redis import get_redis

from .labels import score_to_label
from .schemas import (
    Driver,
    FreeTierResponse,
    Freshness,
    NoDataResponse,
    ProTierResponse,
    SubIndices,
)

_log = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _cache_age(timestamp: datetime | str | None) -> int:
    """Seconds since the state was last scored. Returns 0 on parse failure."""
    if timestamp is None:
        return 0
    try:
        if isinstance(timestamp, str):
            ts = datetime.fromisoformat(timestamp)
        else:
            ts = timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return max(0, int((_now_utc() - ts).total_seconds()))
    except Exception:
        return 0


def _parse_dt(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


async def _load_from_redis(ticker: str) -> dict | None:
    """Return the parsed state dict from Redis, or None."""
    try:
        client = get_redis()
        raw = await client.get(f"sentiment:{ticker.upper()}")
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        _log.warning("assembler: Redis read failed for %s: %s", ticker, exc)
        return None


async def _load_from_db(ticker: str) -> dict | None:
    """Return the latest sentiment_history row as a dict, or None."""
    try:
        row = await get_latest(ticker)
        if row is None:
            return None
        # Normalise to match Redis state layout expected by _build_*
        flags = row.get("confidence_flags")
        if isinstance(flags, str):
            flags = json.loads(flags)
        drivers = row.get("top_drivers")
        if isinstance(drivers, str):
            drivers = json.loads(drivers)
        return {
            "ticker":          ticker.upper(),
            "composite_score": row["composite_score"],
            "confidence":      {"score": row["confidence_score"], "flags": flags or []},
            "sub_indices": {
                "market":     {"value": row.get("market_index")},
                "narrative":  {"value": row.get("narrative_index")},
                "influencer": {"value": row.get("influencer_index")},
                "macro":      {"value": row.get("macro_index")},
            },
            "divergence":  row.get("divergence"),
            "top_drivers": drivers or [],
            "explanation": "",
            "freshness": {
                "market_as_of":     row.get("market_as_of"),
                "narrative_as_of":  row.get("narrative_as_of"),
                "influencer_as_of": row.get("influencer_as_of"),
                "macro_as_of":      row.get("macro_as_of"),
            },
            "timestamp": row["timestamp"],
        }
    except Exception as exc:
        _log.warning("assembler: DB read failed for %s: %s", ticker, exc)
        return None


def _sub_val(state: dict, layer: str) -> float | None:
    si = (state.get("sub_indices") or {}).get(layer)
    if si is None:
        return None
    if isinstance(si, dict):
        return si.get("value")
    return float(si)


def _build_free(state: dict) -> FreeTierResponse:
    score = int(round(state.get("composite_score") or state.get("score") or 0))
    conf  = state.get("confidence") or {}
    confidence = int(conf.get("score") if isinstance(conf, dict) else conf or 0)
    ts    = _parse_dt(state.get("timestamp"))
    return FreeTierResponse(
        ticker            = state["ticker"].upper(),
        score             = score,
        label             = score_to_label(score),
        confidence        = confidence,
        timestamp         = ts or _now_utc(),
        cache_age_seconds = _cache_age(state.get("timestamp")),
    )


def _build_pro(state: dict) -> ProTierResponse:
    score = int(round(state.get("composite_score") or state.get("score") or 0))
    conf  = state.get("confidence") or {}
    confidence = int(conf.get("score") if isinstance(conf, dict) else conf or 0)
    flags = conf.get("flags", []) if isinstance(conf, dict) else []
    ts    = _parse_dt(state.get("timestamp"))

    freshness_raw = state.get("freshness") or {}
    freshness = Freshness(
        market_as_of     = _parse_dt(freshness_raw.get("market_as_of")),
        narrative_as_of  = _parse_dt(freshness_raw.get("narrative_as_of")),
        influencer_as_of = _parse_dt(freshness_raw.get("influencer_as_of")),
        macro_as_of      = _parse_dt(freshness_raw.get("macro_as_of")),
    )

    raw_drivers = state.get("top_drivers") or []
    drivers = [
        Driver(
            signal       = d.get("signal", ""),
            description  = d.get("description", ""),
            direction    = d.get("direction", "neutral"),
            magnitude    = float(d.get("magnitude", 0.0)),
            source_layer = d.get("source_layer", ""),
        )
        for d in raw_drivers
        if isinstance(d, dict)
    ]

    return ProTierResponse(
        ticker            = state["ticker"].upper(),
        score             = score,
        label             = score_to_label(score),
        confidence        = confidence,
        sub_indices       = SubIndices(
            market     = _sub_val(state, "market"),
            narrative  = _sub_val(state, "narrative"),
            influencer = _sub_val(state, "influencer"),
            macro      = _sub_val(state, "macro"),
        ),
        divergence        = state.get("divergence"),
        top_drivers       = drivers,
        explanation       = state.get("explanation") or "",
        freshness         = freshness,
        confidence_flags  = list(flags),
        timestamp         = ts or _now_utc(),
        cache_age_seconds = _cache_age(state.get("timestamp")),
    )


async def assemble(
    ticker: str,
    tier: str,
    detail: str = "summary",
) -> FreeTierResponse | ProTierResponse | NoDataResponse:
    """
    Load the scored state and return the appropriate response model.

    Parameters
    ----------
    ticker : Ticker symbol (upper-cased internally).
    tier   : 'free' or 'pro'.
    detail : 'summary' or 'full' (full only honoured for pro tier).
    """
    ticker = ticker.upper()

    state = await _load_from_redis(ticker)
    if state is None:
        _log.info("assembler: Redis miss for %s — falling back to DB", ticker)
        state = await _load_from_db(ticker)

    if state is None:
        return NoDataResponse(
            ticker  = ticker,
            status  = "insufficient_data",
            message = "Not enough historical data to compute a reliable sentiment score yet.",
        )

    use_full = (tier == "pro" and detail == "full")
    if use_full:
        return _build_pro(state)
    return _build_free(state)
