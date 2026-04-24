"""
pipeline/persistence/pg_writer.py

Persists a fully scored state to PostgreSQL in a single atomic transaction:
    1. INSERT into sentiment_history  (one row per scoring cycle per ticker)
    2. INSERT into price_snapshots    (close + volume at moment of scoring)

Both inserts share the same connection and are committed together, so a
partial write is impossible.

Accepted state dict shapes
--------------------------
The function accepts both the nested shape produced by orchestrator.py and a
flat shape for direct callers.  Each field is resolved with a primary key
checked first, then a fallback alias.

    Field               Primary key          Fallback / alias
    ─────────────────── ──────────────────── ──────────────────────────────────
    composite score     composite_score      score
    market sub-index    market_index         sub_indices.market.value
    narrative sub-index narrative_index      sub_indices.narrative.value
    influencer sub-idx  influencer_index     sub_indices.influencer.value
    macro sub-index     macro_index          sub_indices.macro.value
    confidence score    confidence_score     confidence.score
    confidence flags    confidence_flags     confidence.flags
    market freshness    market_as_of         freshness.market_as_of
    narrative freshness narrative_as_of      freshness.narrative_as_of
    influencer freshness influencer_as_of    freshness.influencer_as_of
    macro freshness     macro_as_of          freshness.macro_as_of
    close price         close_price          price.close
    volume              volume               price.volume

Required keys
    ticker      str
    timestamp   datetime (UTC) or ISO-8601 string

Required (either primary or alias must be present)
    composite_score / score
"""
from __future__ import annotations

from datetime import datetime, timezone

from db.connection import get_pool
from db.queries import price_snapshots as ps_queries
from db.queries import sentiment_history as sh_queries


def _sub_value(sub_indices: dict | None, layer: str) -> float | None:
    """Extract the numeric sub-index value for `layer` from a sub_indices dict."""
    if not sub_indices:
        return None
    entry = sub_indices.get(layer)
    if entry is None:
        return None
    if isinstance(entry, dict):
        return entry.get("value")
    return float(entry)


def _as_datetime(value: str | datetime | None) -> datetime | None:
    """Coerce an ISO-8601 string or datetime to a tz-aware datetime, or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def persist_scored_state(state: dict) -> None:
    """
    Write one scored state to sentiment_history and price_snapshots atomically.

    Accepts both flat and nested key layouts.  See module docstring for the
    full alias table.

    Raises
    ------
    ValueError
        If neither composite_score nor score is present.
    RuntimeError
        If the DB pool has not been initialised.
    """
    ticker    = state["ticker"]
    timestamp = _as_datetime(state["timestamp"])

    # ── composite score: "composite_score" or "score" ──────────────────────────
    _composite_raw = state.get("composite_score") or state.get("score")
    if _composite_raw is None:
        raise ValueError("state must contain 'composite_score' or 'score'")
    composite = float(_composite_raw)

    # ── sub-indices: flat keys take priority, nested sub_indices as fallback ───
    sub_indices = state.get("sub_indices") or {}
    market_index     = state.get("market_index")     or _sub_value(sub_indices, "market")
    narrative_index  = state.get("narrative_index")  or _sub_value(sub_indices, "narrative")
    influencer_index = state.get("influencer_index") or _sub_value(sub_indices, "influencer")
    macro_index      = state.get("macro_index")      or _sub_value(sub_indices, "macro")

    # ── confidence: flat keys take priority, nested confidence dict as fallback ─
    confidence       = state.get("confidence") or {}
    conf_score: int  = int(state.get("confidence_score") or confidence.get("score") or 0)
    conf_flags       = list(state.get("confidence_flags") or confidence.get("flags") or [])

    # ── freshness timestamps: flat keys take priority, nested freshness as fallback
    freshness        = state.get("freshness") or {}
    market_as_of     = _as_datetime(state.get("market_as_of")     or freshness.get("market_as_of"))
    narrative_as_of  = _as_datetime(state.get("narrative_as_of")  or freshness.get("narrative_as_of"))
    influencer_as_of = _as_datetime(state.get("influencer_as_of") or freshness.get("influencer_as_of"))
    macro_as_of      = _as_datetime(state.get("macro_as_of")      or freshness.get("macro_as_of"))

    # ── other fields ───────────────────────────────────────────────────────────
    top_drivers = state.get("top_drivers") or []
    divergence  = state.get("divergence")

    # ── price snapshot: flat keys take priority, nested price dict as fallback ─
    price_info  = state.get("price") or {}
    close       = state.get("close_price") or price_info.get("close")
    volume_raw  = state.get("volume")      or price_info.get("volume")

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await sh_queries.insert_row(
                conn,
                ticker           = ticker,
                composite_score  = composite,
                market_index     = market_index,
                narrative_index  = narrative_index,
                influencer_index = influencer_index,
                macro_index      = macro_index,
                confidence_score = conf_score,
                confidence_flags = conf_flags,
                top_drivers      = top_drivers,
                divergence       = divergence,
                market_as_of     = market_as_of,
                narrative_as_of  = narrative_as_of,
                influencer_as_of = influencer_as_of,
                macro_as_of      = macro_as_of,
                timestamp        = timestamp,
            )

            if close is not None:
                await ps_queries.insert_row(
                    conn,
                    ticker    = ticker,
                    close     = float(close),
                    volume    = int(volume_raw) if volume_raw is not None else None,
                    timestamp = timestamp,
                )
