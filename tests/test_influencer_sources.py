"""
tests/test_influencer_sources.py — Sprint P3.2

Covers:
  - I8: `_analyst_target_yf` fetches `targetMeanPrice` from yfinance via
        `asyncio.to_thread`, returns None on missing / non-numeric / non-positive
        values, surfaces yfinance exceptions as None.
  - I9: `score_influencer_signals` z-scores `analyst_target_price` over the
        per-row *upside* (target - current_close) / current_close series
        (option c — historical upsides computed against the current close).
        Cold-start (insufficient history) falls back to the parametric
        `_score_analyst_target` scorer.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from pipeline.sources import influencer as influencer_src
from pipeline.features import normalize as nm


# ===========================================================================
# I8 — yfinance fetcher
# ===========================================================================

class TestAnalystTargetYf:

    @pytest.mark.asyncio
    async def test_returns_target_when_present(self):
        fake_ticker = MagicMock()
        fake_ticker.info = {"targetMeanPrice": 215.5}
        with patch.object(influencer_src.yf, "Ticker", return_value=fake_ticker):
            value = await influencer_src._analyst_target_yf("AAPL")
        assert value == 215.5

    @pytest.mark.asyncio
    async def test_returns_none_when_field_missing(self):
        fake_ticker = MagicMock()
        fake_ticker.info = {}
        with patch.object(influencer_src.yf, "Ticker", return_value=fake_ticker):
            value = await influencer_src._analyst_target_yf("XYZ")
        assert value is None

    @pytest.mark.asyncio
    async def test_returns_none_when_field_none(self):
        fake_ticker = MagicMock()
        fake_ticker.info = {"targetMeanPrice": None}
        with patch.object(influencer_src.yf, "Ticker", return_value=fake_ticker):
            value = await influencer_src._analyst_target_yf("XYZ")
        assert value is None

    @pytest.mark.asyncio
    async def test_returns_none_when_field_non_numeric(self):
        fake_ticker = MagicMock()
        fake_ticker.info = {"targetMeanPrice": "n/a"}
        with patch.object(influencer_src.yf, "Ticker", return_value=fake_ticker):
            value = await influencer_src._analyst_target_yf("XYZ")
        assert value is None

    @pytest.mark.asyncio
    async def test_returns_none_when_field_non_positive(self):
        fake_ticker = MagicMock()
        fake_ticker.info = {"targetMeanPrice": 0}
        with patch.object(influencer_src.yf, "Ticker", return_value=fake_ticker):
            value = await influencer_src._analyst_target_yf("XYZ")
        assert value is None

    @pytest.mark.asyncio
    async def test_swallows_yfinance_exception(self):
        def _raise(*_a, **_kw):
            raise RuntimeError("yfinance network glitch")
        with patch.object(influencer_src.yf, "Ticker", side_effect=_raise):
            value = await influencer_src._analyst_target_yf("XYZ")
        assert value is None


# ===========================================================================
# I8 — Source-label change: yfinance row is written with source='yfinance'
# ===========================================================================

class TestRunInfluencerTargetSource:

    @pytest.mark.asyncio
    async def test_target_row_uses_yfinance_source(self):
        """End-to-end: _run_influencer with mocked sub-calls writes target with source='yfinance'."""
        captured: list = []

        async def _capture_insert(rows):
            captured.extend(rows)

        async def _no_insider(*_a, **_kw):
            return []  # no insider data this tick

        async def _no_cik(*_a, **_kw):
            return None  # skip the EDGAR path entirely

        async def _analyst_pct(*_a, **_kw):
            return None  # not under test

        async def _target_value(_ticker):
            return 250.0

        client = MagicMock()
        with patch.object(influencer_src, "insert_signals", new=AsyncMock(side_effect=_capture_insert)), \
             patch.object(influencer_src, "_get_cik", new=AsyncMock(side_effect=_no_cik)), \
             patch.object(influencer_src, "_insider_finnhub", new=AsyncMock(side_effect=_no_insider)), \
             patch.object(influencer_src, "_analyst_recommendations", new=AsyncMock(side_effect=_analyst_pct)), \
             patch.object(influencer_src, "_analyst_target_yf", new=AsyncMock(side_effect=_target_value)):
            await influencer_src._run_influencer("AAPL", client)

        # Should have exactly one row written: the analyst_target_price one
        assert len(captured) == 1
        ticker, sig_type, value, source, upload_type, _ts = captured[0]
        assert ticker == "AAPL"
        assert sig_type == "analyst_target_price"
        assert value == 250.0
        assert source == "yfinance"
        assert upload_type == "live"


# ===========================================================================
# I9 — z-score over upside (option c)
# ===========================================================================

def _row(value, *, sig_type="analyst_target_price", source="yfinance"):
    return {
        "signal_type": sig_type,
        "value": value,
        "source": source,
        "timestamp": datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
    }


class TestAnalystTargetPriceZScorePath:

    @pytest.mark.asyncio
    async def test_zscore_used_when_enough_history(self):
        """With ≥45 historical targets and a current_price, the upside z-score path engages."""
        # History of 60 target prices centered around 100 with small noise
        history = [100.0 + (i % 5) * 2 - 4 for i in range(60)]  # values in {96, 98, 100, 102, 104}
        current_price = 100.0
        # current_target = 110 → upside = +0.10; historical upsides centered near 0 → strongly bullish z
        current_target = 110.0

        with patch("db.queries.raw_signals.get_signal_history", new=AsyncMock(return_value=history)):
            scored = await nm.score_influencer_signals(
                "TEST", [_row(current_target)],
                now=datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
                current_price=current_price,
            )
        assert len(scored) == 1
        sig = scored[0]
        assert sig["signal_type"] == "analyst_target_price"
        # Strongly bullish (max z=+3 maps to 100); +10% upside vs ~0% mean is >3σ → clipped to 100
        assert sig["score"] > 90.0
        # source weight = 0.85 (P3.1), w_author × w_conf = 1 × 1, time decay ~1.0 (same ts as now)
        assert abs(sig["weight"] - 0.85) < 0.02
        assert sig["source"] == "yfinance"

    @pytest.mark.asyncio
    async def test_zscore_bearish_when_target_below_history(self):
        """Target below history → upside negative vs historical upsides → bearish score."""
        history = [120.0 + (i % 5) * 2 - 4 for i in range(60)]  # values in {116, 118, 120, 122, 124}
        current_price = 100.0
        current_target = 105.0  # +5% upside; historical upsides centered near +20% → bearish

        with patch("db.queries.raw_signals.get_signal_history", new=AsyncMock(return_value=history)):
            scored = await nm.score_influencer_signals(
                "TEST", [_row(current_target)],
                now=datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
                current_price=current_price,
            )
        assert len(scored) == 1
        assert scored[0]["score"] < 10.0  # strongly bearish (clipped at z=-3)

    @pytest.mark.asyncio
    async def test_parametric_fallback_when_insufficient_history(self):
        """Cold-start: < 45 obs → falls back to parametric _score_analyst_target."""
        history = [100.0] * 10  # only 10 obs — below the 45 effective_min
        current_price = 100.0
        current_target = 110.0  # +10% upside → parametric ≈ 50 + 50*tanh(0.10/0.15) ≈ 79

        with patch("db.queries.raw_signals.get_signal_history", new=AsyncMock(return_value=history)):
            scored = await nm.score_influencer_signals(
                "TEST", [_row(current_target)],
                now=datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
                current_price=current_price,
            )
        assert len(scored) == 1
        # Parametric score around 79
        assert 70.0 < scored[0]["score"] < 85.0

    @pytest.mark.asyncio
    async def test_parametric_fallback_when_no_current_price(self):
        """current_price=None → can't compute upside → parametric path also returns None → row skipped."""
        history = [100.0] * 60

        with patch("db.queries.raw_signals.get_signal_history", new=AsyncMock(return_value=history)):
            scored = await nm.score_influencer_signals(
                "TEST", [_row(110.0)],
                now=datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
                current_price=None,
            )
        assert scored == []  # parametric requires current_price; row dropped

    @pytest.mark.asyncio
    async def test_telemetry_records_zscore_path(self):
        """When z-score path succeeds, telemetry records 'zscore', not 'parametric_fallback'."""
        history = [100.0 + (i % 5) for i in range(60)]
        nm.reset_scoring_telemetry()
        with patch("db.queries.raw_signals.get_signal_history", new=AsyncMock(return_value=history)):
            await nm.score_influencer_signals(
                "TEST", [_row(110.0)],
                now=datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
                current_price=100.0,
            )
        counts = nm._scoring_method_counts.get("analyst_target_price", {})
        assert counts.get("zscore", 0) >= 1
        assert counts.get("parametric_fallback", 0) == 0
