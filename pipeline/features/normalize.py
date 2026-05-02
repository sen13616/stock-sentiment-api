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
- Time decay uses a 48-hour half-life (exponential).
- Source weights: official filings / exchange data = 1.0, third-party = 0.7–0.9.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Source weights
# ---------------------------------------------------------------------------

_SOURCE_WEIGHTS: dict[str, float] = {
    "alpha_vantage": 1.0,
    "polygon":       0.9,
    "sec_edgar":     1.0,
    "finnhub":       0.8,
    "computed":      0.85,
    "newsapi":       0.7,
    "yfinance":      0.9,
    "finra_regsho":  0.9,
}

# ---------------------------------------------------------------------------
# Time decay — exponential, half-life = 48 hours
# ---------------------------------------------------------------------------

_HALF_LIFE_H = 48.0
_LAMBDA      = math.log(2) / _HALF_LIFE_H


def _time_weight(ts: datetime, now: datetime) -> float:
    """Return exponential decay weight. Fresh signal = 1.0; 48 h old = 0.5."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_h = max(0.0, (now - ts).total_seconds() / 3600)
    return math.exp(-_LAMBDA * age_h)


def _source_weight(source: str) -> float:
    return _SOURCE_WEIGHTS.get(source.lower(), 0.70)


# ---------------------------------------------------------------------------
# Per-signal-type score functions  (all return float in [0, 100])
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


def _score_put_call(pcr: float) -> float:
    """PCR < 0.7 → 75 (bullish), 1.0 → 50 (neutral), > 1.3 → 25 (bearish)."""
    return max(0.0, min(100.0, 50.0 + (1.0 - pcr) * 83.33))


def _score_short_interest(sir: float) -> float:
    """Short interest (days to cover). Low = bullish, high = bearish."""
    return max(0.0, min(100.0, 75.0 - 5.0 * max(0.0, sir - 2.0)))


def _score_implied_vol(iv: float) -> float:
    """IV ratio (0–1). Low IV = bullish, high IV = bearish."""
    return max(0.0, min(100.0, 50.0 - (iv - 0.35) / 0.15 * 25.0))


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
    "put_call_ratio":       _score_put_call,
    "short_interest_ratio": _score_short_interest,
    "implied_volatility":   _score_implied_vol,
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
    w = _source_weight(source) * _time_weight(timestamp, now)
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

def score_market_signals(
    ticker: str,
    raw: list[dict],
    now: datetime,
) -> list[dict]:
    """
    Convert raw market signal rows to scored dicts.

    Parameters
    ----------
    ticker : str
    raw    : rows from get_signals_since() — keys: signal_type, value, source, timestamp.
    now    : reference time for time decay.
    """
    result: list[dict] = []
    for row in raw:
        sig_type = row["signal_type"]
        scorer   = _SIMPLE_SCORERS.get(sig_type)
        if scorer is None:
            continue
        value  = float(row["value"])
        score  = scorer(value)
        result.append(_build(
            sig_type, value, score,
            row.get("source", "unknown"), row["timestamp"],
            "market", ticker, now,
        ))
    return result


def score_narrative_signals(
    ticker: str,
    articles: list[dict],
    now: datetime,
) -> list[dict]:
    """
    Convert raw_articles rows (with provider_sentiment) to scored dicts.

    Parameters
    ----------
    articles : rows from get_articles_since() — keys: published_at,
               provider_sentiment, relevance_score, source.
    """
    result: list[dict] = []
    for art in articles:
        sentiment = art.get("provider_sentiment")
        if sentiment is None:
            continue
        relevance = float(art.get("relevance_score") or 0.5)
        source    = art.get("source", "news")
        published = art["published_at"]

        score  = max(0.0, min(100.0, 50.0 + 50.0 * float(sentiment)))
        w_time = _time_weight(published, now)
        weight = _source_weight(source) * w_time * max(relevance, 0.1)

        result.append({
            "signal_type": "provider_sentiment",
            "value":       float(sentiment),
            "score":       round(score, 2),
            "weight":      round(max(weight, 1e-6), 6),
            "source":      source,
            "layer":       "narrative",
            "ticker":      ticker,
        })
    return result


def score_influencer_signals(
    ticker: str,
    raw: list[dict],
    now: datetime,
    current_price: float | None = None,
) -> list[dict]:
    """
    Convert raw influencer signal rows to scored dicts.

    Parameters
    ----------
    current_price : Latest close price, used to score analyst_target_price.
                    If None, analyst_target_price signals are skipped.
    """
    result: list[dict] = []
    for row in raw:
        sig_type = row["signal_type"]
        value    = float(row["value"])
        source   = row.get("source", "unknown")
        ts       = row["timestamp"]

        if sig_type == "analyst_target_price":
            score = _score_analyst_target(value, current_price)
            if score is None:
                continue
        else:
            scorer = _SIMPLE_SCORERS.get(sig_type)
            if scorer is None:
                continue
            score = scorer(value)

        result.append(_build(sig_type, value, score, source, ts, "influencer", ticker, now))
    return result


def score_macro_signals(
    raw: list[dict],
    now: datetime,
) -> list[dict]:
    """
    Convert raw macro signal rows (VIX + sector ETF returns) to scored dicts.

    Parameters
    ----------
    raw : rows from get_signals_since() across '_MACRO_' and ETF tickers.
    """
    result: list[dict] = []
    for row in raw:
        sig_type = row["signal_type"]
        if sig_type not in ("vix", "sector_etf_return_20d"):
            continue
        scorer = _SIMPLE_SCORERS.get(sig_type)
        if scorer is None:
            continue
        value  = float(row["value"])
        score  = scorer(value)
        result.append(_build(
            sig_type, value, score,
            row.get("source", "alpha_vantage"), row["timestamp"],
            "macro", "_MACRO_", now,
        ))
    return result


# ---------------------------------------------------------------------------
# Short volume ratio — per-ticker rolling z-score normalizer
# ---------------------------------------------------------------------------

def _normalize_short_volume_z(
    history: list[float],
    today_value: float,
) -> float | None:
    """
    Compute a normalized score for short_volume_ratio_otc via rolling z-score.

    Parameters
    ----------
    history     : Recent daily short_volume_ratio_otc values (oldest first).
                  Typically 20 trading days.
    today_value : Today's short_volume_ratio_otc value to score.

    Returns
    -------
    float in [-1, 1] or None if insufficient data / zero variance.

    Sign convention: high recent short volume = bearish positioning,
    so the output is NEGATED (z = +3 → -1, z = -3 → +1).
    """
    if len(history) < 10:
        return None

    n = len(history)
    mean = sum(history) / n
    variance = sum((x - mean) ** 2 for x in history) / n
    std = variance ** 0.5

    if std < 1e-12:
        return None

    z = (today_value - mean) / std
    z = max(-3.0, min(3.0, z))
    return -(z / 3.0)


async def score_short_volume_ratio(
    ticker: str,
    raw_row: dict,
    now: datetime,
) -> dict | None:
    """
    Normalize a short_volume_ratio_otc signal via per-ticker rolling z-score.

    Fetches 20-day history from the DB, computes the z-score, and returns
    a fully scored signal dict — or None if history is insufficient.

    This is async because it requires a DB read for the lookback window.
    It is called separately from score_market_signals() (which is sync).
    """
    from db.queries.raw_signals import get_signal_history

    history = await get_signal_history(ticker, "short_volume_ratio_otc", limit=20)
    today_value = float(raw_row["value"])

    normalized = _normalize_short_volume_z(history, today_value)
    if normalized is None:
        return None

    score = 50.0 + 50.0 * normalized
    return _build(
        "short_volume_ratio_otc",
        today_value,
        score,
        raw_row.get("source", "finra_regsho"),
        raw_row["timestamp"],
        "market",
        ticker,
        now,
    )
