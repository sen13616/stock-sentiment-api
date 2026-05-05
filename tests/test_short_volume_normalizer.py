"""
tests/test_short_volume_normalizer.py

Unit tests for the short_volume_ratio_otc rolling z-score normalizer
and the updated _MARKET_SIGNAL_TYPES registry.

All tests are pure and synchronous — no DB, Redis, or async required.
"""
from __future__ import annotations

import pytest

from pipeline.features.normalize import _normalize_short_volume_z


# ---------------------------------------------------------------------------
# Helpers — build synthetic history with known mean and std
# ---------------------------------------------------------------------------

def _make_history(mean: float, std: float, n: int = 20) -> list[float]:
    """
    Build a synthetic history of `n` values with known mean and std.

    Uses half values at (mean - std) and half at (mean + std).
    Population std of this arrangement = std exactly.
    """
    half = n // 2
    low  = mean - std
    high = mean + std
    return [low] * half + [high] * (n - half)


# ---------------------------------------------------------------------------
# (a) Today is +2 std above mean → output near -0.67
# ---------------------------------------------------------------------------

class TestZScorePlusTwoSigma:

    def test_output_near_negative_067(self):
        """z = +2 → -(2/3) ≈ -0.667"""
        history = _make_history(mean=0.50, std=0.10, n=20)
        today = 0.50 + 2 * 0.10  # = 0.70
        result = _normalize_short_volume_z(history, today)
        assert result is not None
        assert abs(result - (-2.0 / 3.0)) < 0.01

    def test_output_sign_is_negative(self):
        """High short volume (positive z) → bearish → negative output."""
        history = _make_history(mean=0.45, std=0.05, n=20)
        today = 0.45 + 2 * 0.05
        result = _normalize_short_volume_z(history, today)
        assert result is not None
        assert result < 0


# ---------------------------------------------------------------------------
# (b) Today is -2 std below mean → output near +0.67
# ---------------------------------------------------------------------------

class TestZScoreMinusTwoSigma:

    def test_output_near_positive_067(self):
        """z = -2 → -(-2/3) = +0.667"""
        history = _make_history(mean=0.50, std=0.10, n=20)
        today = 0.50 - 2 * 0.10  # = 0.30
        result = _normalize_short_volume_z(history, today)
        assert result is not None
        assert abs(result - (2.0 / 3.0)) < 0.01

    def test_output_sign_is_positive(self):
        """Low short volume (negative z) → bullish → positive output."""
        history = _make_history(mean=0.45, std=0.05, n=20)
        today = 0.45 - 2 * 0.05
        result = _normalize_short_volume_z(history, today)
        assert result is not None
        assert result > 0


# ---------------------------------------------------------------------------
# (c) Today is at mean (z = 0) → output 0.0
# ---------------------------------------------------------------------------

class TestZScoreAtMean:

    def test_output_is_zero(self):
        history = _make_history(mean=0.50, std=0.10, n=20)
        today = 0.50
        result = _normalize_short_volume_z(history, today)
        assert result is not None
        assert abs(result) < 0.01

    def test_exact_zero(self):
        history = _make_history(mean=0.42, std=0.08, n=20)
        today = 0.42
        result = _normalize_short_volume_z(history, today)
        assert result is not None
        assert abs(result) < 1e-9


# ---------------------------------------------------------------------------
# (d) Only 8 historical points → returns None
# ---------------------------------------------------------------------------

class TestInsufficientHistory:

    def test_8_points_returns_none(self):
        history = _make_history(mean=0.50, std=0.10, n=8)
        assert _normalize_short_volume_z(history, 0.50) is None

    def test_9_points_returns_none(self):
        history = _make_history(mean=0.50, std=0.10, n=9)
        assert _normalize_short_volume_z(history, 0.50) is None

    def test_0_points_returns_none(self):
        assert _normalize_short_volume_z([], 0.50) is None

    def test_10_points_returns_value(self):
        """10 is the minimum — should produce a result."""
        history = _make_history(mean=0.50, std=0.10, n=10)
        result = _normalize_short_volume_z(history, 0.50)
        assert result is not None


# ---------------------------------------------------------------------------
# (e) std = 0 (constant history) → returns None
# ---------------------------------------------------------------------------

class TestZeroVariance:

    def test_constant_history_returns_none(self):
        history = [0.45] * 20
        assert _normalize_short_volume_z(history, 0.45) is None

    def test_constant_history_different_today_returns_none(self):
        history = [0.50] * 15
        assert _normalize_short_volume_z(history, 0.60) is None


# ---------------------------------------------------------------------------
# Clipping at ±3 sigma → output clamped to [-1, +1]
# ---------------------------------------------------------------------------

class TestClipping:

    def test_extreme_positive_z_clamped(self):
        """z > 3 → clamped to 3 → output = -1.0"""
        history = _make_history(mean=0.50, std=0.01, n=20)
        today = 0.50 + 5 * 0.01  # z = 5 → clamped to 3
        result = _normalize_short_volume_z(history, today)
        assert result is not None
        assert abs(result - (-1.0)) < 0.01

    def test_extreme_negative_z_clamped(self):
        """z < -3 → clamped to -3 → output = +1.0"""
        history = _make_history(mean=0.50, std=0.01, n=20)
        today = 0.50 - 5 * 0.01  # z = -5 → clamped to -3
        result = _normalize_short_volume_z(history, today)
        assert result is not None
        assert abs(result - 1.0) < 0.01

    def test_output_always_in_range(self):
        """Output is always in [-1, 1]."""
        history = _make_history(mean=0.40, std=0.05, n=20)
        for today in [0.0, 0.20, 0.40, 0.60, 0.80, 1.0]:
            result = _normalize_short_volume_z(history, today)
            assert result is not None
            assert -1.0 <= result <= 1.0, f"Out of range for today={today}: {result}"


# ---------------------------------------------------------------------------
# _MARKET_SIGNAL_TYPES includes new types
# ---------------------------------------------------------------------------

class TestMarketSignalTypesRegistry:

    def test_short_volume_types_registered(self):
        from pipeline.orchestrator import _MARKET_SIGNAL_TYPES
        assert "short_volume_otc" in _MARKET_SIGNAL_TYPES
        assert "short_volume_total_otc" in _MARKET_SIGNAL_TYPES
        assert "short_volume_ratio_otc" in _MARKET_SIGNAL_TYPES

    def test_existing_types_still_present(self):
        from pipeline.orchestrator import _MARKET_SIGNAL_TYPES
        for expected in [
            "rsi_14", "return_1d", "return_5d", "return_20d",
            "volume_ratio", "order_flow_imbalance", "buy_pressure",
            "sell_pressure", "bid_ask_spread_bps",
        ]:
            assert expected in _MARKET_SIGNAL_TYPES, f"Missing: {expected}"


# ---------------------------------------------------------------------------
# Score conversion: normalized [-1, 1] → score [0, 100]
# ---------------------------------------------------------------------------

class TestScoreConversion:
    """Verify that the [0, 100] score computed from the normalized value
    aligns with the standard pipeline convention (50 = neutral)."""

    def test_neutral_maps_to_50(self):
        """z = 0 → normalized = 0 → score = 50"""
        history = _make_history(mean=0.50, std=0.10, n=20)
        normalized = _normalize_short_volume_z(history, 0.50)
        assert normalized is not None
        score = 50.0 + 50.0 * normalized
        assert abs(score - 50.0) < 0.5

    def test_bearish_maps_below_50(self):
        """High short volume (z = +2) → bearish → score < 50"""
        history = _make_history(mean=0.50, std=0.10, n=20)
        normalized = _normalize_short_volume_z(history, 0.70)
        assert normalized is not None
        score = 50.0 + 50.0 * normalized
        assert score < 50.0

    def test_bullish_maps_above_50(self):
        """Low short volume (z = -2) → bullish → score > 50"""
        history = _make_history(mean=0.50, std=0.10, n=20)
        normalized = _normalize_short_volume_z(history, 0.30)
        assert normalized is not None
        score = 50.0 + 50.0 * normalized
        assert score > 50.0


# ---------------------------------------------------------------------------
# Driver label and description
# ---------------------------------------------------------------------------

class TestDriverMetadata:

    def test_signal_label_exists(self):
        from pipeline.scoring.drivers import _SIGNAL_LABELS
        assert "short_volume_ratio_otc" in _SIGNAL_LABELS
        assert _SIGNAL_LABELS["short_volume_ratio_otc"] == "Short volume ratio"

    def test_description_handler(self):
        from pipeline.scoring.drivers import _describe
        desc = _describe("short_volume_ratio_otc", 0.60, ticker="AAPL")
        assert "60.0%" in desc
        assert "elevated" in desc
        assert "AAPL" in desc

    def test_description_normal_level(self):
        from pipeline.scoring.drivers import _describe
        desc = _describe("short_volume_ratio_otc", 0.45, ticker="MSFT")
        assert "normal" in desc

    def test_description_low_level(self):
        from pipeline.scoring.drivers import _describe
        desc = _describe("short_volume_ratio_otc", 0.30, ticker="GOOG")
        assert "low" in desc


# ---------------------------------------------------------------------------
# Explanation template
# ---------------------------------------------------------------------------

class TestExplanationTemplate:

    def test_phrase_exists(self):
        from pipeline.explanation.templates import _PHRASES
        assert "Short volume ratio" in _PHRASES
        phrases = _PHRASES["Short volume ratio"]
        assert "bullish" in phrases
        assert "bearish" in phrases
        assert "neutral" in phrases

    def test_source_weight_registered(self):
        from pipeline.features.normalize import _SOURCE_WEIGHTS
        assert "finra_regsho" in _SOURCE_WEIGHTS
        assert _SOURCE_WEIGHTS["finra_regsho"] == 0.9
