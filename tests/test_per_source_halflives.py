"""
tests/test_per_source_halflives.py — Verify per-source half-life time decay.

Sprint 4 (G-S1): Replaces uniform 48h half-life with per-layer/source values.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from pipeline.features.normalize import (
    _HALF_LIFE_OVERRIDE,
    _LAYER_HALF_LIFE_H,
    _build,
    _get_half_life,
    _time_weight,
)


# ===========================================================================
# _get_half_life lookup
# ===========================================================================

class TestGetHalfLife:

    def test_market_default(self):
        assert _get_half_life("market", "yfinance") == 1.0

    def test_market_polygon(self):
        assert _get_half_life("market", "polygon") == 1.0

    def test_narrative_default(self):
        assert _get_half_life("narrative", "alpha_vantage") == 12.0

    def test_narrative_finnhub(self):
        assert _get_half_life("narrative", "finnhub") == 12.0

    def test_influencer_default(self):
        """Influencer default (analyst signals) = 72h."""
        assert _get_half_life("influencer", "finnhub") == 72.0

    def test_influencer_sec_edgar_override(self):
        """SEC filings in influencer layer use 168h (7 days)."""
        assert _get_half_life("influencer", "sec_edgar") == 168.0

    def test_macro_default(self):
        assert _get_half_life("macro", "computed") == 336.0

    def test_unknown_layer_fallback(self):
        """Unknown layer falls back to 48h."""
        assert _get_half_life("unknown", "yfinance") == 48.0

    def test_case_insensitive_source(self):
        assert _get_half_life("influencer", "SEC_EDGAR") == 168.0


# ===========================================================================
# _time_weight with half-life parameter
# ===========================================================================

class TestTimeWeight:

    def test_fresh_signal_weight_one(self):
        now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
        w = _time_weight(now, now, half_life_h=1.0)
        assert abs(w - 1.0) < 1e-9

    def test_one_halflife_old_is_half(self):
        """Signal exactly one half-life old → weight = 0.5."""
        now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
        ts = now - timedelta(hours=1)
        w = _time_weight(ts, now, half_life_h=1.0)
        assert abs(w - 0.5) < 1e-6

    def test_two_halflives_old_is_quarter(self):
        now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
        ts = now - timedelta(hours=2)
        w = _time_weight(ts, now, half_life_h=1.0)
        assert abs(w - 0.25) < 1e-6

    def test_market_60min_halflife(self):
        """Market signal 60 min old → weight = 0.5."""
        now = datetime(2026, 5, 8, 15, 0, tzinfo=timezone.utc)
        ts = now - timedelta(minutes=60)
        w = _time_weight(ts, now, half_life_h=1.0)
        assert abs(w - 0.5) < 1e-6

    def test_market_30min_old(self):
        """Market signal 30 min old → weight ≈ 0.71 (one scoring tick)."""
        now = datetime(2026, 5, 8, 15, 0, tzinfo=timezone.utc)
        ts = now - timedelta(minutes=30)
        w = _time_weight(ts, now, half_life_h=1.0)
        expected = 0.5 ** 0.5  # ≈ 0.7071
        assert abs(w - expected) < 1e-4

    def test_narrative_12h_halflife(self):
        """News signal 12h old → weight = 0.5."""
        now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
        ts = now - timedelta(hours=12)
        w = _time_weight(ts, now, half_life_h=12.0)
        assert abs(w - 0.5) < 1e-6

    def test_macro_14d_halflife(self):
        """Macro signal 14 days old → weight = 0.5."""
        now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
        ts = now - timedelta(days=14)
        w = _time_weight(ts, now, half_life_h=336.0)
        assert abs(w - 0.5) < 1e-6

    def test_overnight_market_decay(self):
        """Market signal from 17h ago (overnight) → near-zero weight."""
        now = datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc)
        ts = now - timedelta(hours=17)  # previous day 21:00
        w = _time_weight(ts, now, half_life_h=1.0)
        # 0.5^17 ≈ 7.6e-6
        assert w < 0.001

    def test_default_halflife_48h(self):
        """Default half_life_h=48 maintained for backward compat."""
        now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
        ts = now - timedelta(hours=48)
        w = _time_weight(ts, now)  # uses default 48.0
        assert abs(w - 0.5) < 1e-6


# ===========================================================================
# _build uses per-source half-lives via layer parameter
# ===========================================================================

class TestBuildWithHalfLives:

    def test_market_signal_uses_1h_halflife(self):
        """Market signal 60 min old should have weight ≈ 0.5 × source_weight."""
        now = datetime(2026, 5, 8, 15, 0, tzinfo=timezone.utc)
        ts = now - timedelta(minutes=60)
        sig = _build("rsi_14", 55.0, 50.0, "yfinance", ts, "market", "AAPL", now)
        # source_weight(yfinance) = 0.9, time_weight(1h, hl=1h) = 0.5
        expected_w = 0.9 * 0.5
        assert abs(sig["weight"] - expected_w) < 0.01

    def test_narrative_signal_uses_12h_halflife(self):
        """Narrative signal 12h old → weight ≈ 0.5 × source_weight."""
        now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
        ts = now - timedelta(hours=12)
        sig = _build("provider_sentiment", 0.3, 65.0, "alpha_vantage", ts,
                      "narrative", "AAPL", now)
        expected_w = 0.7 * 0.5  # AV source weight = 0.7
        assert abs(sig["weight"] - expected_w) < 0.01

    def test_influencer_sec_edgar_uses_168h(self):
        """SEC filing 7 days old → weight ≈ 0.5 × source_weight."""
        now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
        ts = now - timedelta(days=7)
        sig = _build("insider_net_shares", 1000, 55.0, "sec_edgar", ts,
                      "influencer", "AAPL", now)
        expected_w = 1.0 * 0.5  # sec_edgar source weight = 1.0
        assert abs(sig["weight"] - expected_w) < 0.01

    def test_influencer_finnhub_uses_72h(self):
        """Finnhub analyst signal 3 days old → weight ≈ 0.5 × source_weight."""
        now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
        ts = now - timedelta(days=3)
        sig = _build("analyst_buy_pct", 0.7, 60.0, "finnhub", ts,
                      "influencer", "AAPL", now)
        expected_w = 0.8 * 0.5  # finnhub source weight = 0.8
        assert abs(sig["weight"] - expected_w) < 0.01

    def test_macro_signal_uses_336h(self):
        """Macro signal 14 days old → weight ≈ 0.5 × source_weight."""
        now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
        ts = now - timedelta(days=14)
        sig = _build("vix", 22.0, 50.0, "computed", ts, "macro", "_MACRO_", now)
        expected_w = 0.85 * 0.5  # computed source weight = 0.85
        assert abs(sig["weight"] - expected_w) < 0.01


# ===========================================================================
# Layer half-life values match paper spec
# ===========================================================================

class TestHalfLifeValues:

    def test_market_is_60_min(self):
        assert _LAYER_HALF_LIFE_H["market"] == 1.0

    def test_narrative_is_12h(self):
        assert _LAYER_HALF_LIFE_H["narrative"] == 12.0

    def test_influencer_default_is_72h(self):
        assert _LAYER_HALF_LIFE_H["influencer"] == 72.0

    def test_macro_is_336h(self):
        assert _LAYER_HALF_LIFE_H["macro"] == 336.0

    def test_sec_edgar_override_is_168h(self):
        assert _HALF_LIFE_OVERRIDE[("influencer", "sec_edgar")] == 168.0
