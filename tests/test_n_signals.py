"""
tests/test_n_signals.py — Verify n_signals aggregation with carry-forward layers.

When a per-layer job (e.g. market_job) calls _score_and_write with layers={"market"},
n_signals must include carried-forward counts from other layers so that the
confidence scorer does not incorrectly apply a -20 low_signal_volume penalty.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from pipeline.confidence.scorer import LOW_VOLUME_THRESHOLD
from pipeline.scoring.subindices import SubIndexResult


async def test_carried_forward_layers_contribute_to_n_signals():
    """Carry-forward layers' n_signals should be included in the total."""
    now = datetime.now(timezone.utc)

    # Last state in Redis: narrative, influencer, macro all had >=5 signals
    last_state = {
        "sub_indices": {
            "market": {"value": 55.0, "n_signals": 4, "sources": ["yfinance"]},
            "narrative": {"value": 60.0, "n_signals": 8, "sources": ["alpha_vantage"]},
            "influencer": {"value": 50.0, "n_signals": 3, "sources": ["sec"]},
            "macro": {"value": 48.0, "n_signals": 2, "sources": ["finnhub"]},
        },
        "freshness": {
            "market_as_of": now.isoformat(),
            "narrative_as_of": now.isoformat(),
            "influencer_as_of": now.isoformat(),
            "macro_as_of": now.isoformat(),
        },
    }

    # Market layer produces 3 freshly-scored signals
    fresh_market_sigs = [
        {"signal_type": "return_1d", "score": 60, "weight": 0.9, "source": "yfinance", "value": 0.01},
        {"signal_type": "rsi_14", "score": 55, "weight": 0.85, "source": "computed", "value": 55.0},
        {"signal_type": "volume_ratio", "score": 50, "weight": 0.8, "source": "yfinance", "value": 1.0},
    ]

    captured_n_signals = {}

    original_compute_confidence = None

    def spy_compute_confidence(**kwargs):
        captured_n_signals["value"] = kwargs["n_signals"]
        from pipeline.confidence.scorer import ConfidenceResult
        return ConfidenceResult(score=80, flags=[])

    with (
        patch("pipeline.orchestrator.read_scored_state", new_callable=AsyncMock, return_value=last_state),
        patch("pipeline.orchestrator.get_latest_close", new_callable=AsyncMock, return_value=150.0),
        patch("pipeline.orchestrator._score_market", new_callable=AsyncMock, return_value=(
            SubIndexResult(55.0, 3, ["yfinance"]),
            fresh_market_sigs,
            now,
        )),
        patch("pipeline.orchestrator.compute_confidence", side_effect=spy_compute_confidence) as mock_conf,
        patch("pipeline.orchestrator.write_scored_state", new_callable=AsyncMock),
        patch("pipeline.orchestrator.persist_scored_state", new_callable=AsyncMock),
    ):
        from pipeline.orchestrator import _score_and_write
        await _score_and_write("AAPL", layers={"market"})

    # 3 fresh market sigs + 8 narrative + 3 influencer + 2 macro = 16
    assert captured_n_signals["value"] == 3 + 8 + 3 + 2
    assert captured_n_signals["value"] >= LOW_VOLUME_THRESHOLD


async def test_all_layers_recomputed_uses_fresh_sigs_only():
    """When layers=None (all recomputed), n_signals comes only from fresh sigs."""
    now = datetime.now(timezone.utc)

    fresh_sigs = [
        {"signal_type": "return_1d", "score": 60, "weight": 0.9, "source": "yfinance", "value": 0.01},
        {"signal_type": "rsi_14", "score": 55, "weight": 0.85, "source": "computed", "value": 55.0},
    ]

    captured_n_signals = {}

    def spy_compute_confidence(**kwargs):
        captured_n_signals["value"] = kwargs["n_signals"]
        from pipeline.confidence.scorer import ConfidenceResult
        return ConfidenceResult(score=80, flags=[])

    si = SubIndexResult(55.0, 2, ["yfinance"])

    with (
        patch("pipeline.orchestrator.read_scored_state", new_callable=AsyncMock, return_value=None),
        patch("pipeline.orchestrator.get_latest_close", new_callable=AsyncMock, return_value=150.0),
        patch("pipeline.orchestrator._score_market", new_callable=AsyncMock, return_value=(si, fresh_sigs, now)),
        patch("pipeline.orchestrator._score_narrative", new_callable=AsyncMock, return_value=(None, [], None)),
        patch("pipeline.orchestrator._score_influencer", new_callable=AsyncMock, return_value=(None, [], None, None, None)),
        patch("pipeline.orchestrator._score_macro", new_callable=AsyncMock, return_value=(None, [], None)),
        patch("pipeline.orchestrator.compute_confidence", side_effect=spy_compute_confidence),
        patch("pipeline.orchestrator.write_scored_state", new_callable=AsyncMock),
        patch("pipeline.orchestrator.persist_scored_state", new_callable=AsyncMock),
    ):
        from pipeline.orchestrator import _score_and_write
        await _score_and_write("AAPL", layers=None)

    # layers=None means no carry-forward; only fresh sigs count
    assert captured_n_signals["value"] == 2
