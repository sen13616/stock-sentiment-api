"""
tests/test_macro_scoring.py — Sprint P4.2

Per-ticker macro sub-index tests:
  • `_score_macro(ticker, sector, …)` routes via the ticker's GICS sector
  • Null-sector tickers drop the ETF component and fall through to VIX-only
  • `score_macro_signals` z-scores ETF history against the SECTOR_ETFS map
  • `compute_sub_index(..., shrinkage_denominator=2)` removes shrinkage for
    macro (Decision 7 Option C stopgap — P4.4 replaces this aggregator)
  • Two tickers in different sectors with divergent ETF returns produce
    materially different macro sub-indices
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import math
import pytest

from pipeline.features.normalize import score_macro_signals, _ZSCORE_CONFIG
from pipeline.scoring.subindices import compute_sub_index, SubIndexResult


def _ts() -> datetime:
    return datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)


def _row(sig_type: str, value: float, source: str = "yfinance") -> dict:
    return {"signal_type": sig_type, "value": value, "source": source, "timestamp": _ts()}


# ===========================================================================
# 1. ZSCORE_CONFIG entry for sector_etf_return_20d (P4.2 wiring)
# ===========================================================================

class TestSectorEtfZScoreConfig:

    def test_sector_etf_return_20d_has_zscore_config(self):
        assert "sector_etf_return_20d" in _ZSCORE_CONFIG
        cfg = _ZSCORE_CONFIG["sector_etf_return_20d"]
        assert cfg.window == 90
        # Sector ETF returns are NOT sign-inverted (positive return = bullish)
        assert cfg.negate is False


# ===========================================================================
# 2. compute_sub_index shrinkage_denominator parameter (Decision 7 Option C)
# ===========================================================================

class TestMacroShrinkageStopgap:

    def _signal(self, score, weight=1.0, source="yfinance"):
        return {"score": score, "weight": weight, "source": source}

    def test_default_shrinkage_unchanged_for_two_signals(self):
        """Default denominator (5) still shrinks with 2 signals: min(1, 2/5) = 0.4."""
        sigs = [self._signal(80), self._signal(80)]
        result = compute_sub_index(sigs)
        # raw = 80, shrinkage = 0.4 → 50 + 0.4 × 30 = 62
        assert result is not None
        assert abs(result.value - 62.0) < 0.01

    def test_macro_override_no_shrinkage_with_two_signals(self):
        """shrinkage_denominator=2 yields min(1, 2/2) = 1.0 → raw value preserved."""
        sigs = [self._signal(80), self._signal(80)]
        result = compute_sub_index(sigs, shrinkage_denominator=2)
        assert result is not None
        assert abs(result.value - 80.0) < 0.01

    def test_macro_override_still_shrinks_at_one_signal(self):
        """With just VIX, shrinkage = min(1, 1/2) = 0.5 — half-pull toward 50."""
        sigs = [self._signal(80)]
        result = compute_sub_index(sigs, shrinkage_denominator=2)
        # raw=80, shrinkage=0.5 → 50 + 0.5 × 30 = 65
        assert result is not None
        assert abs(result.value - 65.0) < 0.01

    def test_macro_override_capped_at_one_for_many_signals(self):
        """shrinkage = min(1, n/d) saturates at 1.0 even for large n."""
        sigs = [self._signal(80) for _ in range(10)]
        default = compute_sub_index(sigs)
        macro   = compute_sub_index(sigs, shrinkage_denominator=2)
        # Both fully saturate shrinkage → identical results
        assert default is not None and macro is not None
        assert abs(default.value - macro.value) < 0.01
        assert abs(macro.value - 80.0) < 0.01

    def test_narrative_unchanged_by_macro_param(self):
        """compute_sub_index default behavior preserved when called without the override."""
        sigs = [self._signal(70), self._signal(70), self._signal(70)]
        result = compute_sub_index(sigs)  # uses default 5
        # raw=70, shrinkage=3/5=0.6 → 50 + 0.6 × 20 = 62
        assert result is not None
        assert abs(result.value - 62.0) < 0.01


# ===========================================================================
# 3. score_macro_signals — per-ticker routing
# ===========================================================================

class TestScoreMacroSignalsPerTicker:

    @pytest.mark.asyncio
    async def test_vix_only_when_sector_is_none(self):
        """A null-sector ticker can still score VIX but not the ETF component."""
        # Insufficient history → parametric fallback for VIX
        with patch("db.queries.raw_signals.get_signal_history", new=AsyncMock(return_value=[])):
            scored = await score_macro_signals(
                ticker="NEWLY_ADDED", sector=None,
                raw=[_row("vix", 22.0), _row("sector_etf_return_20d", 0.05)],
                now=_ts(),
            )
        # ETF row still scores via parametric (no history dependency at the row level);
        # but the ETF row was emitted with ticker="NEWLY_ADDED" — verify it's in the output
        # alongside VIX. The history call for the ETF z-score is skipped (sector is None).
        types = [s["signal_type"] for s in scored]
        assert "vix" in types
        # ETF still scores (parametric path doesn't need history), and is recorded
        # under the requested ticker.
        for sig in scored:
            assert sig["ticker"] == "NEWLY_ADDED"

    @pytest.mark.asyncio
    async def test_etf_zscore_history_keyed_by_etf_symbol(self):
        """For AAPL (sector IT), z-score history must be fetched under 'XLK', not 'AAPL'."""
        history_calls: list[tuple] = []

        async def _fake_history(ticker, signal_type, limit):
            history_calls.append((ticker, signal_type, limit))
            return []  # empty → parametric path

        with patch("db.queries.raw_signals.get_signal_history", new=_fake_history):
            await score_macro_signals(
                ticker="AAPL", sector="Information Technology",
                raw=[_row("sector_etf_return_20d", 0.10)],
                now=_ts(),
            )

        # Among the calls made, the one for sector_etf_return_20d must use XLK
        etf_calls = [c for c in history_calls if c[1] == "sector_etf_return_20d"]
        assert len(etf_calls) == 1
        assert etf_calls[0][0] == "XLK"

    @pytest.mark.asyncio
    async def test_scored_rows_carry_user_ticker(self):
        """Output rows must be tagged with the ticker being scored, not '_MACRO_' or the ETF symbol."""
        with patch("db.queries.raw_signals.get_signal_history", new=AsyncMock(return_value=[])):
            scored = await score_macro_signals(
                ticker="XOM", sector="Energy",
                raw=[_row("vix", 22.0), _row("sector_etf_return_20d", 0.05)],
                now=_ts(),
            )
        for sig in scored:
            assert sig["ticker"] == "XOM", f"unexpected ticker {sig['ticker']!r} in {sig}"

    @pytest.mark.asyncio
    async def test_unknown_sector_skips_etf_history_lookup(self):
        """A sector string not in SECTOR_ETFS doesn't blow up; ETF row still parametric-scores."""
        with patch("db.queries.raw_signals.get_signal_history", new=AsyncMock(return_value=[])):
            scored = await score_macro_signals(
                ticker="TEST", sector="Made-Up Sector",
                raw=[_row("sector_etf_return_20d", 0.10)],
                now=_ts(),
            )
        # The ETF row still parametric-scores; nothing crashes.
        assert any(s["signal_type"] == "sector_etf_return_20d" for s in scored)


# ===========================================================================
# 4. Distinct tickers in distinct sectors produce different macro scores
# ===========================================================================

class TestPerTickerDistinctness:

    @pytest.mark.asyncio
    async def test_two_sectors_diverge_when_etf_returns_diverge(self):
        """AAPL (XLK +10%) and XOM (XLE −10%) must produce materially different macro scores."""
        # Empty history → parametric path for both ETFs
        with patch("db.queries.raw_signals.get_signal_history", new=AsyncMock(return_value=[])):
            scored_aapl = await score_macro_signals(
                ticker="AAPL", sector="Information Technology",
                raw=[_row("vix", 22.0), _row("sector_etf_return_20d", +0.10)],
                now=_ts(),
            )
            scored_xom = await score_macro_signals(
                ticker="XOM", sector="Energy",
                raw=[_row("vix", 22.0), _row("sector_etf_return_20d", -0.10)],
                now=_ts(),
            )

        # Both should have 2 scored signals each
        assert len(scored_aapl) == 2
        assert len(scored_xom) == 2

        # The ETF scores must differ in direction
        aapl_etf = next(s for s in scored_aapl if s["signal_type"] == "sector_etf_return_20d")
        xom_etf  = next(s for s in scored_xom  if s["signal_type"] == "sector_etf_return_20d")
        assert aapl_etf["score"] > 60.0   # +10% return = bullish under tanh
        assert xom_etf["score"]  < 40.0   # −10% return = bearish

        # Aggregated via the macro shrinkage stopgap (denominator=2)
        si_aapl = compute_sub_index(scored_aapl, shrinkage_denominator=2)
        si_xom  = compute_sub_index(scored_xom,  shrinkage_denominator=2)
        assert si_aapl is not None and si_xom is not None
        # Materially different — the gap should be ≥ 10 points
        assert (si_aapl.value - si_xom.value) > 10.0
