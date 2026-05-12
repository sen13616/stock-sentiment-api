"""
pipeline/features/normalize.py  — Layer 07

Converts raw signal values fetched from the database into scored, weighted
signal dicts that can be passed to pipeline.scoring.subindices.compute_sub_index()
and pipeline.scoring.drivers.extract_drivers().

Output dict schema (per signal)
--------------------------------
    signal_type : str    — e.g. "rsi_14"
    value       : float  — raw value (preserved for driver descriptions)
    score       : float  — direction-corrected [0, 100], 50 = neutral
    weight      : float  — combined w_source × w_time  (> 0)
    source      : str    — data source label
    layer       : str    — "market" | "narrative" | "influencer" | "macro"
    ticker      : str    — ticker symbol

Scoring conventions
-------------------
- All scores are direction-corrected: > 50 = bullish, < 50 = bearish.
- Weights encode two factors: source trustworthiness and time decay.
- Time decay uses per-source half-lives: market 1h, news 12h, analyst 3d,
  filings 7d, macro 14d  (Sprint 4, G-S1).
- Source weights: official filings / exchange data = 1.0, third-party = 0.7–0.9.
- Z-score normalization (Sprint 4, G-C1): signals with sufficient history
  (≥ 30 observations) use adaptive z-score; others fall back to fixed
  parametric scorers.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Source weights
# ---------------------------------------------------------------------------

_SOURCE_WEIGHTS: dict[str, float] = {
    "alpha_vantage": 0.75,   # Paper §Event-Level Weighting (Sprint A)
    "polygon":       0.9,
    "sec_edgar":     1.0,
    "finnhub":       0.65,   # Paper §Event-Level Weighting (Sprint A)
    "computed":      0.85,
    # Removed Stage 1 — "newsapi" excluded from paper's implemented methodology. See docs/SIGNAL_CATALOG.md.
    "yfinance":      0.9,
    "finra_regsho":  0.9,
}

# ---------------------------------------------------------------------------
# Per-source half-lives (Sprint 4, G-S1)
#
# Research paper §Historical Data specifies per-category decay rates.
# Layer-level defaults with source-specific overrides where the same source
# appears in multiple layers with different decay characteristics.
# ---------------------------------------------------------------------------

_LAYER_HALF_LIFE_H: dict[str, float] = {
    "market":     1.0,      # 60 min
    "narrative":  12.0,     # 12 hours
    "influencer": 72.0,     # 3 days (analyst default)
    "macro":      336.0,    # 14 days
}

# Overrides for specific (layer, source) combinations
_HALF_LIFE_OVERRIDE: dict[tuple[str, str], float] = {
    ("influencer", "sec_edgar"): 168.0,   # SEC filings: 7 days (provider-keyed; superseded by signal-type override below for insider rows)
}

# Influencer-layer signal-type overrides (Sprint P3.1, paper §Event-Level Weighting).
# Paper specifies half-life by *signal channel*, not data provider. Takes precedence
# over the (layer, source) table above.
_INFLUENCER_SIGNAL_HALF_LIFE_H: dict[str, float] = {
    "insider_net_shares": 168.0,   # Paper: insider transactions = 7 days regardless of provider
    # analyst_buy_pct, analyst_target_price, earnings_estimate_revision use 72h layer default
}


def _get_half_life(layer: str, source: str, signal_type: str | None = None) -> float:
    """Look up half-life. Priority: (layer, signal_type) → (layer, source) → layer default."""
    if signal_type is not None and layer == "influencer":
        sig_hl = _INFLUENCER_SIGNAL_HALF_LIFE_H.get(signal_type)
        if sig_hl is not None:
            return sig_hl
    return _HALF_LIFE_OVERRIDE.get(
        (layer, source.lower()),
        _LAYER_HALF_LIFE_H.get(layer, 48.0),
    )


def _time_weight(ts: datetime, now: datetime, half_life_h: float = 48.0) -> float:
    """Return exponential decay weight.  Fresh signal = 1.0; half_life_h old = 0.5."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_h = max(0.0, (now - ts).total_seconds() / 3600)
    lam = math.log(2) / half_life_h
    return math.exp(-lam * age_h)


def _source_weight(source: str) -> float:
    return _SOURCE_WEIGHTS.get(source.lower(), 0.70)


# Influencer-layer signal-type weight overrides (Sprint P3.1, paper §Event-Level Weighting).
# Paper specifies w_src by signal channel (insider / analyst-consensus / target-price /
# earnings-revisions), not by data provider. Takes precedence over `_SOURCE_WEIGHTS`
# when layer == "influencer".
_INFLUENCER_SIGNAL_WEIGHT: dict[str, float] = {
    "insider_net_shares":         1.00,   # Paper: insider transactions
    "analyst_buy_pct":            0.85,   # Paper: analyst consensus
    "analyst_target_price":       0.85,   # Paper: analyst target price
    "earnings_estimate_revision": 0.80,   # Paper: earnings revisions (signal lands in P3.3)
}

# Influencer event-weight scaffolds (Sprint P3.1, paper §Event-Level Weighting).
# w_author = 1.00 uniformly today; scaffold for the deferred role-based hierarchy
# (CEO → CFO → Director …) which the paper places in Future Additions.
# w_conf  = 1.00 explicitly for non-textual influencer signals — paper: "Model
# confidence w_conf does not apply to non-textual signals and is set to 1.00
# throughout".
_INFLUENCER_W_AUTHOR: float = 1.0
_INFLUENCER_W_CONF:   float = 1.0


def _get_signal_weight(layer: str, source: str, signal_type: str) -> float:
    """Return w_src for a signal.

    For influencer layer, paper specifies weights keyed by signal channel rather
    than provider; the channel weight takes precedence. Other layers fall through
    to the per-source table.
    """
    if layer == "influencer":
        sig_w = _INFLUENCER_SIGNAL_WEIGHT.get(signal_type)
        if sig_w is not None:
            return sig_w
    return _source_weight(source)


# ---------------------------------------------------------------------------
# Rolling z-score normalizer (Sprint 4, G-C1)
# ---------------------------------------------------------------------------

class RollingZScorer:
    """
    Adaptive z-score normalizer.

    Computes z = (x - μ) / max(σ, σ_floor) from a historical window,
    clamps to ±3, and maps to [0, 100] (50 = neutral).

    Parameters
    ----------
    window         : Number of historical observations to fetch.
    min_obs        : Absolute minimum observations required; returns None if fewer.
    sigma_floor    : Minimum standard deviation to avoid division by near-zero.
    negate         : If True, negate z before mapping (for bearish-when-high
                     signals like VIX, RSI contrarian, bid-ask spread, short volume).
    fill_threshold : Fraction of ``window`` that must be present before z-score
                     activates.  Effective minimum = max(min_obs, ceil(window *
                     fill_threshold)).  Default 0.5 ensures the z-score baseline
                     is computed against a meaningful history span.
    """

    __slots__ = ("window", "min_obs", "sigma_floor", "negate", "fill_threshold")

    def __init__(
        self,
        window: int,
        min_obs: int = 30,
        sigma_floor: float = 1e-4,
        negate: bool = False,
        fill_threshold: float = 0.5,
    ) -> None:
        self.window = window
        self.min_obs = min_obs
        self.sigma_floor = sigma_floor
        self.negate = negate
        self.fill_threshold = fill_threshold

    @property
    def effective_min_obs(self) -> int:
        """Minimum observations considering both min_obs and fill_threshold."""
        import math
        return max(self.min_obs, math.ceil(self.window * self.fill_threshold))

    def score_from_history(
        self, history: list[float], current_value: float
    ) -> float | None:
        """
        Pure z-score computation from a pre-fetched history list.

        Parameters
        ----------
        history       : Historical values (oldest first), up to ``window``.
        current_value : The value to normalize.

        Returns
        -------
        float in [0, 100] or None if insufficient data / variance below floor.
        """
        if len(history) < self.effective_min_obs:
            return None

        n = len(history)
        mean = sum(history) / n
        variance = sum((x - mean) ** 2 for x in history) / n
        std = variance ** 0.5

        if std < self.sigma_floor:
            return None

        z = (current_value - mean) / std
        z = max(-3.0, min(3.0, z))

        if self.negate:
            z = -z

        return 50.0 + 50.0 * (z / 3.0)

    async def score(
        self, ticker: str, signal_type: str, current_value: float
    ) -> float | None:
        """Fetch history from DB and compute z-score."""
        from db.queries.raw_signals import get_signal_history

        history = await get_signal_history(ticker, signal_type, limit=self.window)
        return self.score_from_history(history, current_value)


# Per-signal-type z-score configuration.
# Signals not listed here use parametric scorers only.
_ZSCORE_CONFIG: dict[str, RollingZScorer] = {
    # Intra-day market signals: window=500 observations
    "rsi_14":               RollingZScorer(window=500, negate=True),
    "return_1d":            RollingZScorer(window=500),
    "return_5d":            RollingZScorer(window=500),
    "return_20d":           RollingZScorer(window=500),
    "volume_ratio":         RollingZScorer(window=500),
    "order_flow_imbalance": RollingZScorer(window=500),
    "bid_ask_spread_bps":   RollingZScorer(window=500, negate=True),
    # Daily-cadence signals: window=90 observations
    "short_volume_ratio_otc": RollingZScorer(window=90, negate=True),
    "insider_net_shares":     RollingZScorer(window=90),
    "analyst_buy_pct":        RollingZScorer(window=90),
    "vix":                    RollingZScorer(window=90, negate=True),
}


# ---------------------------------------------------------------------------
# Telemetry — z-score vs parametric fallback counts (Sprint 4)
# ---------------------------------------------------------------------------

_scoring_method_counts: dict[str, dict[str, int]] = {}


def _record_method(signal_type: str, method: str) -> None:
    """Record that a signal was scored via 'zscore' or 'parametric_fallback'."""
    if signal_type not in _scoring_method_counts:
        _scoring_method_counts[signal_type] = {"zscore": 0, "parametric_fallback": 0}
    _scoring_method_counts[signal_type][method] += 1


def reset_scoring_telemetry() -> None:
    """Clear telemetry counters.  Called at the start of each scoring tick."""
    _scoring_method_counts.clear()


def log_scoring_telemetry() -> None:
    """Log per-signal-type scoring method counts.  Called after each scoring tick."""
    if not _scoring_method_counts:
        return
    for sig_type in sorted(_scoring_method_counts):
        c = _scoring_method_counts[sig_type]
        _log.debug(
            "[telemetry] %s: zscore=%d parametric_fallback=%d",
            sig_type, c["zscore"], c["parametric_fallback"],
        )


# ---------------------------------------------------------------------------
# Per-signal-type parametric score functions  (all return float in [0, 100])
#
# These are the FALLBACK path when z-score has insufficient history (< 30 obs).
# Do not delete — they remain active until enough data accumulates per signal.
# ---------------------------------------------------------------------------

def _score_rsi(rsi: float) -> float:
    """RSI 30 → 75 (bullish), 50 → 50 (neutral), 70 → 25 (bearish)."""
    return max(0.0, min(100.0, 50.0 - 1.25 * (rsi - 50.0)))


def _score_return(ret: float, scale: float) -> float:
    """Price return → score.  Positive return = bullish (> 50)."""
    return max(0.0, min(100.0, 50.0 + 50.0 * math.tanh(ret / scale)))


def _score_volume_ratio(vr: float) -> float:
    """Elevated volume = mild positive (market interest). Neutral at 1×."""
    return max(20.0, min(80.0, 50.0 + 15.0 * math.tanh(vr - 1.0)))


# Removed Stage 1 — _score_put_call, _score_short_interest, _score_implied_vol
# excluded from paper's implemented methodology. See docs/SIGNAL_CATALOG.md.


def _score_order_flow_imbalance(clv: float) -> float:
    """CLV (close location value) in [-1, 1]. Positive = close near high = bullish."""
    return max(0.0, min(100.0, 50.0 + 50.0 * clv))


def _score_buy_pressure(x: float) -> float:
    """Buy fraction in [0, 1]. 0.5 = neutral, 1.0 = bullish."""
    return max(0.0, min(100.0, 100.0 * x))


def _score_sell_pressure(x: float) -> float:
    """Sell fraction in [0, 1]. Inverted: 0.5 = neutral, 1.0 = bearish → score 0."""
    return max(0.0, min(100.0, 100.0 * (1.0 - x)))


def _score_bid_ask_spread_bps(bps: float) -> float:
    """Bid-ask spread in basis points. 10 bps neutral; wider = bearish (illiquidity)."""
    return max(0.0, min(100.0, 50.0 - 50.0 * math.tanh((bps - 10.0) / 30.0)))


def _score_insider_shares(shares: float) -> float:
    """Net insider shares bought (positive) / sold (negative)."""
    return max(0.0, min(100.0, 50.0 + 50.0 * math.tanh(shares / 100_000.0)))


def _score_analyst_buy_pct(pct: float) -> float:
    """Analyst buy/outperform pct (0–1). 50 % → 50, 100 % → 75, 0 % → 25."""
    return max(0.0, min(100.0, 25.0 + pct * 50.0))


def _score_analyst_target(target: float, current_price: float | None) -> float | None:
    """Analyst price target: compute upside vs current price and score."""
    if current_price is None or current_price <= 0:
        return None
    upside = (target - current_price) / current_price
    return max(0.0, min(100.0, 50.0 + 50.0 * math.tanh(upside / 0.15)))


def _score_vix(vix: float) -> float:
    """VIX. Low fear = bullish (> 50). Linear around neutral at VIX=22."""
    return max(0.0, min(100.0, 50.0 - (vix - 22.0) / 8.0 * 25.0))


# Lookup table for simple (single-argument) scorers
_SIMPLE_SCORERS: dict[str, object] = {
    "rsi_14":               _score_rsi,
    "return_1d":            lambda v: _score_return(v, 0.02),
    "return_5d":            lambda v: _score_return(v, 0.05),
    "return_20d":           lambda v: _score_return(v, 0.10),
    "volume_ratio":         _score_volume_ratio,
    # Removed Stage 1 — put_call_ratio, short_interest_ratio, implied_volatility
    # excluded from paper's implemented methodology. See docs/SIGNAL_CATALOG.md.
    "order_flow_imbalance": _score_order_flow_imbalance,
    "buy_pressure":         _score_buy_pressure,
    "sell_pressure":        _score_sell_pressure,
    "bid_ask_spread_bps":   _score_bid_ask_spread_bps,
    "insider_net_shares":   _score_insider_shares,
    "analyst_buy_pct":      _score_analyst_buy_pct,
    "vix":                  _score_vix,
    "sector_etf_return_20d": lambda v: _score_return(v, 0.10),
}


# ---------------------------------------------------------------------------
# Signal dict builder
# ---------------------------------------------------------------------------

def _build(
    signal_type: str,
    value: float,
    score: float,
    source: str,
    timestamp: datetime,
    layer: str,
    ticker: str,
    now: datetime,
) -> dict:
    half_life = _get_half_life(layer, source, signal_type)
    w_src = _get_signal_weight(layer, source, signal_type)
    w = w_src * _time_weight(timestamp, now, half_life)
    if layer == "influencer":
        # Paper §Event-Level Weighting: w_i = w_src · w_author · w_conf · e^(−λΔt)
        # Both scaffolds are uniform 1.0 today (see _INFLUENCER_W_AUTHOR / _CONF).
        w = w * _INFLUENCER_W_AUTHOR * _INFLUENCER_W_CONF
    return {
        "signal_type": signal_type,
        "value":       value,
        "score":       round(score, 2),
        "weight":      round(max(w, 1e-6), 6),
        "source":      source,
        "layer":       layer,
        "ticker":      ticker,
    }


# ---------------------------------------------------------------------------
# Public scoring functions — one per layer
# ---------------------------------------------------------------------------

async def score_market_signals(
    ticker: str,
    raw: list[dict],
    now: datetime,
) -> list[dict]:
    """
    Convert raw market signal rows to scored dicts.

    Uses z-score normalization when sufficient history is available
    (≥ 30 observations); falls back to parametric scorers otherwise.

    Parameters
    ----------
    ticker : str
    raw    : rows from get_signals_since() — keys: signal_type, value, source, timestamp.
    now    : reference time for time decay.
    """
    from db.queries.raw_signals import get_signal_history

    result: list[dict] = []

    # Group by signal_type for efficient history fetch (one DB call per type)
    by_type: dict[str, list[dict]] = {}
    for row in raw:
        by_type.setdefault(row["signal_type"], []).append(row)

    for sig_type, rows in by_type.items():
        zscore_cfg = _ZSCORE_CONFIG.get(sig_type)
        parametric = _SIMPLE_SCORERS.get(sig_type)
        if zscore_cfg is None and parametric is None:
            continue

        # Pre-fetch z-score history once per signal_type
        history: list[float] | None = None
        if zscore_cfg is not None:
            history = await get_signal_history(ticker, sig_type, limit=zscore_cfg.window)

        for row in rows:
            value = float(row["value"])
            if math.isnan(value) or math.isinf(value):
                continue

            score: float | None = None
            method = "parametric_fallback"

            # Try z-score first
            if zscore_cfg is not None and history is not None:
                score = zscore_cfg.score_from_history(history, value)
                if score is not None:
                    method = "zscore"

            # Fallback to parametric
            if score is None and parametric is not None:
                score = parametric(value)

            if score is None:
                continue

            _record_method(sig_type, method)
            result.append(_build(
                sig_type, value, score,
                row.get("source", "unknown"), row["timestamp"],
                "market", ticker, now,
            ))

    return result


def _compute_w_conf(pos: float, neg: float, neu: float) -> float:
    """
    Compute confidence weight from FinBERT class probabilities.

    Paper §Event-Level Weighting:
        w_conf = 1 − (−Σ P_i(k) ln P_i(k)) / ln(3)

    Inverse normalized entropy of the 3-class output.  When one class
    dominates (e.g. 0.92, 0.05, 0.03), w_conf ≈ 0.74.  When probabilities
    are spread evenly (0.33, 0.33, 0.33), w_conf ≈ 0.

    Returns float in [0, 1].  Returns 0.0 if any probability is invalid.
    """
    probs = [pos, neg, neu]
    if any(p < 0 for p in probs):
        return 0.0
    total = sum(probs)
    if total < 1e-9:
        return 0.0

    entropy = 0.0
    for p in probs:
        if p > 0:
            entropy -= p * math.log(p)

    ln3 = math.log(3)
    w_conf = 1.0 - entropy / ln3
    return max(0.0, min(1.0, w_conf))


def score_narrative_signals(
    ticker: str,
    articles: list[dict],
    now: datetime,
) -> list[dict]:
    """
    Convert raw_articles rows (with finbert_score) to scored dicts.

    Sprint A: uses finbert_score (FinBERT S_i = P(pos) - P(neg)) instead of
    provider_sentiment.  Weight formula now includes w_conf from FinBERT class
    probabilities per paper §Event-Level Weighting:
        w_i = w_src · w_rel · w_conf · e^(−λΔt_i)

    Parameters
    ----------
    articles : rows from get_articles_since() — keys: published_at,
               finbert_score, relevance_score, source, finbert_pos,
               finbert_neg, finbert_neu.
    """
    result: list[dict] = []
    for art in articles:
        sentiment = art.get("finbert_score")
        if sentiment is None:
            continue
        sent_f = float(sentiment)
        if math.isnan(sent_f) or math.isinf(sent_f):
            continue
        # Sprint D: explicit None check + 0.6 threshold (paper Stage 2)
        relevance_raw = art.get("relevance_score")
        if relevance_raw is None:
            continue  # Paper Stage 2: exclude articles without explicit relevance
        relevance = float(relevance_raw)
        if relevance < 0.6:
            continue  # Paper Stage 2: direct narrative threshold
        source    = art.get("source", "news")
        published = art["published_at"]

        score  = max(0.0, min(100.0, 50.0 + 50.0 * sent_f))
        half_life = _get_half_life("narrative", source)
        w_time = _time_weight(published, now, half_life)

        # w_conf from FinBERT class probabilities (paper §Event-Level Weighting)
        pos = art.get("finbert_pos")
        neg = art.get("finbert_neg")
        neu = art.get("finbert_neu")
        if pos is not None and neg is not None and neu is not None:
            w_conf = _compute_w_conf(float(pos), float(neg), float(neu))
        else:
            w_conf = 1.0  # Fallback: treat as fully confident if no probs available

        # Paper formula: w_i = w_src · w_rel · w_conf · e^(−λΔt_i)
        weight = _source_weight(source) * w_time * relevance * w_conf

        result.append({
            "signal_type": "finbert_sentiment",
            "value":       float(sentiment),
            "score":       round(score, 2),
            "weight":      round(max(weight, 1e-6), 6),
            "source":      source,
            "layer":       "narrative",
            "ticker":      ticker,
        })
    return result


async def score_influencer_signals(
    ticker: str,
    raw: list[dict],
    now: datetime,
    current_price: float | None = None,
) -> list[dict]:
    """
    Convert raw influencer signal rows to scored dicts.

    Uses z-score normalization for insider_net_shares and analyst_buy_pct
    when sufficient history is available; falls back to parametric otherwise.
    analyst_target_price always uses parametric scoring (Decision 5).

    Parameters
    ----------
    current_price : Latest close price, used to score analyst_target_price.
                    If None, analyst_target_price signals are skipped.
    """
    from db.queries.raw_signals import get_signal_history

    result: list[dict] = []

    by_type: dict[str, list[dict]] = {}
    for row in raw:
        by_type.setdefault(row["signal_type"], []).append(row)

    for sig_type, rows in by_type.items():
        zscore_cfg = _ZSCORE_CONFIG.get(sig_type)
        parametric = _SIMPLE_SCORERS.get(sig_type)
        is_target = (sig_type == "analyst_target_price")

        if zscore_cfg is None and parametric is None and not is_target:
            continue

        # Pre-fetch z-score history once per signal_type
        history: list[float] | None = None
        if zscore_cfg is not None:
            history = await get_signal_history(ticker, sig_type, limit=zscore_cfg.window)

        for row in rows:
            value = float(row["value"])
            if math.isnan(value) or math.isinf(value):
                continue
            source = row.get("source", "unknown")
            ts = row["timestamp"]

            score: float | None = None
            method = "parametric_fallback"

            if is_target:
                score = _score_analyst_target(value, current_price)
                if score is None:
                    continue
            else:
                # Try z-score first
                if zscore_cfg is not None and history is not None:
                    score = zscore_cfg.score_from_history(history, value)
                    if score is not None:
                        method = "zscore"

                # Fallback to parametric
                if score is None and parametric is not None:
                    score = parametric(value)

            if score is None:
                continue

            _record_method(sig_type, method)
            result.append(_build(sig_type, value, score, source, ts, "influencer", ticker, now))

    return result


async def score_macro_signals(
    raw: list[dict],
    now: datetime,
) -> list[dict]:
    """
    Convert raw macro signal rows (VIX + sector ETF returns) to scored dicts.

    VIX uses z-score normalization when sufficient history is available.
    sector_etf_return_20d always uses parametric scoring (deferred — insufficient
    per-ETF history).

    Parameters
    ----------
    raw : rows from get_signals_since() across '_MACRO_' and ETF tickers.
    """
    from db.queries.raw_signals import get_signal_history

    result: list[dict] = []

    by_type: dict[str, list[dict]] = {}
    for row in raw:
        sig_type = row["signal_type"]
        if sig_type not in ("vix", "sector_etf_return_20d"):
            continue
        by_type.setdefault(sig_type, []).append(row)

    for sig_type, rows in by_type.items():
        zscore_cfg = _ZSCORE_CONFIG.get(sig_type)
        parametric = _SIMPLE_SCORERS.get(sig_type)
        if zscore_cfg is None and parametric is None:
            continue

        # For VIX z-score, fetch history using the _MACRO_ ticker
        history: list[float] | None = None
        if zscore_cfg is not None:
            # VIX is stored under _MACRO_; ETF signals under their ticker.
            # Since only VIX has z-score config, _MACRO_ is always correct here.
            history = await get_signal_history("_MACRO_", sig_type, limit=zscore_cfg.window)

        for row in rows:
            value = float(row["value"])
            if math.isnan(value) or math.isinf(value):
                continue

            score: float | None = None
            method = "parametric_fallback"

            if zscore_cfg is not None and history is not None:
                score = zscore_cfg.score_from_history(history, value)
                if score is not None:
                    method = "zscore"

            if score is None and parametric is not None:
                score = parametric(value)

            if score is None:
                continue

            _record_method(sig_type, method)
            result.append(_build(
                sig_type, value, score,
                row.get("source", "alpha_vantage"), row["timestamp"],
                "macro", "_MACRO_", now,
            ))

    return result


