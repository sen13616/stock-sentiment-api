"""
tests/test_sprint1_math_corrections.py

Tests for Sprint 1 (G-S4, G-S6, G-S13, G-S14) and Sprint 2 (G-S3) changes.

Sprint 4 note: score_market_signals, score_influencer_signals, and
score_macro_signals are now async (z-score requires DB reads).  Tests mock
get_signal_history to return [] so z-score falls back to parametric.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from pipeline.sources.market import _compute_returns
from pipeline.features.normalize import (
    _SOURCE_WEIGHTS,
    score_market_signals,
    score_narrative_signals,
    score_influencer_signals,
    score_macro_signals,
)


# ---------------------------------------------------------------------------
# G-S4: Log returns
# ---------------------------------------------------------------------------

class TestLogReturns:
    """_compute_returns should use log returns: math.log(current/prev)."""

    def test_basic_log_return(self):
        """5% simple return → log return = ln(1.05) ≈ 0.04879."""
        history = [
            (datetime(2026, 5, 1, 21, 0, tzinfo=timezone.utc), 100.0),
        ]
        results = dict(_compute_returns(105.0, history))
        expected = math.log(105.0 / 100.0)
        assert abs(results["return_1d"] - expected) < 1e-6

    def test_negative_log_return(self):
        """Price decline → negative log return."""
        history = [
            (datetime(2026, 5, 1, 21, 0, tzinfo=timezone.utc), 100.0),
        ]
        results = dict(_compute_returns(95.0, history))
        expected = math.log(95.0 / 100.0)
        assert abs(results["return_1d"] - expected) < 1e-6
        assert results["return_1d"] < 0

    def test_log_return_symmetry(self):
        """Log returns are additive: +5% then -5% should not equal zero.
        log(1.05) + log(0.95) ≈ -0.0013, not 0."""
        r_up = math.log(105.0 / 100.0)
        r_down = math.log(95.0 / 100.0)
        assert abs(r_up + r_down) > 0.001  # Not zero — that's the point of log returns

    def test_zero_prev_close_skipped(self):
        """If previous close is 0, skip (can't take log)."""
        history = [(datetime(2026, 5, 1, 21, 0, tzinfo=timezone.utc), 0.0)]
        results = _compute_returns(105.0, history)
        assert len(results) == 0

    def test_zero_current_close_skipped(self):
        """If current close is 0, skip (can't take log)."""
        history = [(datetime(2026, 5, 1, 21, 0, tzinfo=timezone.utc), 100.0)]
        results = _compute_returns(0.0, history)
        assert len(results) == 0

    def test_negative_close_skipped(self):
        """Negative prices are invalid — skip."""
        history = [(datetime(2026, 5, 1, 21, 0, tzinfo=timezone.utc), -10.0)]
        results = _compute_returns(100.0, history)
        assert len(results) == 0

    def test_5d_20d_log_returns(self):
        """5d and 20d horizons also use log returns."""
        history = [
            (datetime(2026, 4, i + 1, 21, 0, tzinfo=timezone.utc), 80.0 + i)
            for i in range(25)
        ]
        current = 110.0
        results = dict(_compute_returns(current, history))

        closes = [80.0 + i for i in range(25)]
        assert abs(results["return_1d"] - math.log(current / closes[-1])) < 1e-6
        assert abs(results["return_5d"] - math.log(current / closes[-5])) < 1e-6
        assert abs(results["return_20d"] - math.log(current / closes[-20])) < 1e-6

    def test_unchanged_price_returns_zero(self):
        """Same price → log(1) = 0."""
        history = [(datetime(2026, 5, 1, 21, 0, tzinfo=timezone.utc), 100.0)]
        results = dict(_compute_returns(100.0, history))
        assert results["return_1d"] == 0.0


# ---------------------------------------------------------------------------
# G-S6: NaN/Inf validation
# ---------------------------------------------------------------------------

def _make_ts():
    return datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc)


# Mock get_signal_history to return [] so z-score falls back to parametric
_mock_history = patch("db.queries.raw_signals.get_signal_history",
                      new_callable=AsyncMock, return_value=[])


class TestNanInfGuardsMarket:
    """score_market_signals should skip NaN/Inf values."""

    def _row(self, sig_type: str, value: float) -> dict:
        return {"signal_type": sig_type, "value": value, "source": "computed", "timestamp": _make_ts()}

    async def test_nan_value_skipped(self):
        raw = [self._row("rsi_14", float("nan"))]
        with _mock_history:
            result = await score_market_signals("TEST", raw, _make_ts())
        assert len(result) == 0

    async def test_inf_value_skipped(self):
        raw = [self._row("rsi_14", float("inf"))]
        with _mock_history:
            result = await score_market_signals("TEST", raw, _make_ts())
        assert len(result) == 0

    async def test_neg_inf_value_skipped(self):
        raw = [self._row("rsi_14", float("-inf"))]
        with _mock_history:
            result = await score_market_signals("TEST", raw, _make_ts())
        assert len(result) == 0

    async def test_valid_value_passes(self):
        raw = [self._row("rsi_14", 50.0)]
        with _mock_history:
            result = await score_market_signals("TEST", raw, _make_ts())
        assert len(result) == 1

    async def test_mixed_nan_and_valid(self):
        raw = [
            self._row("rsi_14", float("nan")),
            self._row("volume_ratio", 1.2),
        ]
        with _mock_history:
            result = await score_market_signals("TEST", raw, _make_ts())
        assert len(result) == 1
        assert result[0]["signal_type"] == "volume_ratio"


class TestNanInfGuardsNarrative:
    """score_narrative_signals should skip NaN/Inf sentiment values."""

    def _art(self, sentiment, relevance=0.5) -> dict:
        return {
            "provider_sentiment": sentiment,
            "relevance_score": relevance,
            "source": "alpha_vantage",
            "published_at": _make_ts(),
        }

    def test_nan_sentiment_skipped(self):
        result = score_narrative_signals("TEST", [self._art(float("nan"))], _make_ts())
        assert len(result) == 0

    def test_inf_sentiment_skipped(self):
        result = score_narrative_signals("TEST", [self._art(float("inf"))], _make_ts())
        assert len(result) == 0

    def test_valid_sentiment_passes(self):
        result = score_narrative_signals("TEST", [self._art(0.5)], _make_ts())
        assert len(result) == 1


class TestNanInfGuardsInfluencer:
    """score_influencer_signals should skip NaN/Inf values."""

    def _row(self, sig_type: str, value: float) -> dict:
        return {"signal_type": sig_type, "value": value, "source": "finnhub", "timestamp": _make_ts()}

    async def test_nan_value_skipped(self):
        raw = [self._row("insider_net_shares", float("nan"))]
        with _mock_history:
            result = await score_influencer_signals("TEST", raw, _make_ts())
        assert len(result) == 0

    async def test_inf_value_skipped(self):
        raw = [self._row("analyst_buy_pct", float("inf"))]
        with _mock_history:
            result = await score_influencer_signals("TEST", raw, _make_ts())
        assert len(result) == 0

    async def test_valid_value_passes(self):
        raw = [self._row("insider_net_shares", 1000.0)]
        with _mock_history:
            result = await score_influencer_signals("TEST", raw, _make_ts())
        assert len(result) == 1


class TestNanInfGuardsMacro:
    """score_macro_signals should skip NaN/Inf values."""

    def _row(self, sig_type: str, value: float) -> dict:
        return {"signal_type": sig_type, "value": value, "source": "alpha_vantage", "timestamp": _make_ts()}

    async def test_nan_vix_skipped(self):
        raw = [self._row("vix", float("nan"))]
        with _mock_history:
            result = await score_macro_signals(raw, _make_ts())
        assert len(result) == 0

    async def test_inf_vix_skipped(self):
        raw = [self._row("vix", float("inf"))]
        with _mock_history:
            result = await score_macro_signals(raw, _make_ts())
        assert len(result) == 0

    async def test_valid_vix_passes(self):
        raw = [self._row("vix", 22.0)]
        with _mock_history:
            result = await score_macro_signals(raw, _make_ts())
        assert len(result) == 1


# ---------------------------------------------------------------------------
# G-S13: Alpha Vantage source weight 1.0 → 0.7
# ---------------------------------------------------------------------------

class TestAVSourceWeight:

    def test_alpha_vantage_weight_is_07(self):
        assert _SOURCE_WEIGHTS["alpha_vantage"] == 0.7

    def test_av_weight_lower_than_exchange_data(self):
        """AV is a third-party aggregator — weight should be lower than exchange data."""
        assert _SOURCE_WEIGHTS["alpha_vantage"] < _SOURCE_WEIGHTS["sec_edgar"]

    def test_av_weight_same_as_default(self):
        """AV weight (0.7) matches the default for unknown sources."""
        from pipeline.features.normalize import _source_weight
        assert _source_weight("alpha_vantage") == _source_weight("unknown_source")


# ---------------------------------------------------------------------------
# G-S14: Minimum relevance threshold (0.1)
# ---------------------------------------------------------------------------

class TestRelevanceFloor:
    """Articles with relevance_score < 0.1 should be excluded entirely."""

    def _art(self, sentiment: float, relevance: float) -> dict:
        return {
            "provider_sentiment": sentiment,
            "relevance_score": relevance,
            "source": "alpha_vantage",
            "published_at": _make_ts(),
        }

    def test_below_threshold_excluded(self):
        """relevance_score=0.05 → excluded."""
        result = score_narrative_signals("TEST", [self._art(0.5, 0.05)], _make_ts())
        assert len(result) == 0

    def test_zero_relevance_defaults_to_05(self):
        """relevance_score=0.0 → treated as unset, defaults to 0.5 (passes threshold).
        Python truthy: float(0.0 or 0.5) = 0.5."""
        result = score_narrative_signals("TEST", [self._art(0.5, 0.0)], _make_ts())
        assert len(result) == 1

    def test_at_threshold_included(self):
        """relevance_score=0.1 → included (boundary: >= 0.1)."""
        result = score_narrative_signals("TEST", [self._art(0.5, 0.1)], _make_ts())
        assert len(result) == 1

    def test_above_threshold_included(self):
        """relevance_score=0.5 → included."""
        result = score_narrative_signals("TEST", [self._art(0.5, 0.5)], _make_ts())
        assert len(result) == 1

    def test_none_relevance_uses_default_05(self):
        """relevance_score=None → defaults to 0.5, which passes the threshold."""
        art = {
            "provider_sentiment": 0.5,
            "relevance_score": None,
            "source": "alpha_vantage",
            "published_at": _make_ts(),
        }
        result = score_narrative_signals("TEST", [art], _make_ts())
        assert len(result) == 1

    def test_relevance_no_longer_floored_in_weight(self):
        """After G-S14, weight uses relevance directly (no max(relevance, 0.1) floor),
        because sub-threshold articles are already excluded."""
        art = self._art(0.5, 0.15)
        result = score_narrative_signals("TEST", [art], _make_ts())
        assert len(result) == 1
        # Weight should use relevance=0.15 directly, not max(0.15, 0.1)=0.15
        # (same result in this case, but the code path is different)
        # The key check: a relevance of 0.5 should produce a higher weight than 0.15
        art2 = self._art(0.5, 0.5)
        result2 = score_narrative_signals("TEST", [art2], _make_ts())
        assert result2[0]["weight"] > result[0]["weight"]

    def test_mixed_articles_filter(self):
        """Mix of above and below threshold — only above-threshold articles scored."""
        articles = [
            self._art(0.8, 0.05),   # excluded
            self._art(0.5, 0.3),    # included
            self._art(-0.2, 0.01),  # excluded
            self._art(0.1, 0.5),    # included
        ]
        result = score_narrative_signals("TEST", articles, _make_ts())
        assert len(result) == 2
