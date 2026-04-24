"""
pipeline/sources/market.py  — Layer 03

Fetches live market signals for a single ticker and writes them to raw_signals.

Signals produced
----------------
ohlcv_open / ohlcv_high / ohlcv_low / ohlcv_close / ohlcv_volume
  Primary:  Alpha Vantage GLOBAL_QUOTE
  Fallback: Polygon /v2/aggs/ticker/{ticker}/prev

rsi_14
  Primary:  Alpha Vantage RSI (daily, period=14)
  Fallback: Finnhub /indicator (rsi)

put_call_ratio
  Primary:  Finnhub /stock/option-chain  (put OI / call OI)
  Fallback: Polygon /v3/snapshot/options/{ticker}

short_interest_ratio
  Source:   Finnhub /stock/short-interest  (no fallback; updates bimonthly)

implied_volatility
  Source:   Finnhub /stock/option-chain (mean IV of near-term contracts)

return_1d / return_5d / return_20d / volume_ratio
  Computed: from DB close/volume history vs current live close/volume
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

_log = logging.getLogger(__name__)

import httpx
from dotenv import load_dotenv

from db.queries.raw_signals import (
    get_close_history,
    get_volume_history,
    insert_signals,
)

load_dotenv(override=True)

_AV_KEY      = os.environ.get("ALPHA_VANTAGE_KEY", "")
_FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")
_POLYGON_KEY = os.environ.get("POLYGON_KEY", "")

_AV_BASE      = "https://www.alphavantage.co/query"
_FINNHUB_BASE = "https://finnhub.io/api/v1"
_POLYGON_BASE = "https://api.polygon.io"


# ---------------------------------------------------------------------------
# OHLCV helpers
# ---------------------------------------------------------------------------

async def _ohlcv_av(ticker: str, client: httpx.AsyncClient) -> dict | None:
    """AV GLOBAL_QUOTE → {open, high, low, close, volume, timestamp}."""
    try:
        resp = await client.get(
            _AV_BASE,
            params={
                "function": "GLOBAL_QUOTE",
                "symbol":   ticker,
                "apikey":   _AV_KEY,
            },
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        print(f"    [market] AV GLOBAL_QUOTE error for {ticker}: {exc}")
        return None

    if "Note" in body or "Information" in body:
        return None

    q = body.get("Global Quote", {})
    if not q or not q.get("05. price"):
        return None

    try:
        trading_day = q.get("07. latest trading day", "")
        ts = (
            datetime.strptime(trading_day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if trading_day
            else datetime.now(timezone.utc)
        )
        return {
            "open":      float(q["02. open"]),
            "high":      float(q["03. high"]),
            "low":       float(q["04. low"]),
            "close":     float(q["05. price"]),
            "volume":    float(q["06. volume"]),
            "timestamp": ts,
            "source":    "alpha_vantage",
        }
    except (KeyError, ValueError) as exc:
        print(f"    [market] AV GLOBAL_QUOTE parse error for {ticker}: {exc}")
        return None


async def _ohlcv_polygon(ticker: str, client: httpx.AsyncClient) -> dict | None:
    """Polygon /v2/aggs/ticker/{ticker}/prev fallback."""
    try:
        resp = await client.get(
            f"{_POLYGON_BASE}/v2/aggs/ticker/{ticker}/prev",
            params={"adjusted": "true", "apiKey": _POLYGON_KEY},
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        print(f"    [market] Polygon prev-day error for {ticker}: {exc}")
        return None

    results = body.get("results", [])
    if not results:
        return None

    try:
        r = results[0]
        ts = datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc)
        return {
            "open":      float(r["o"]),
            "high":      float(r["h"]),
            "low":       float(r["l"]),
            "close":     float(r["c"]),
            "volume":    float(r["v"]),
            "timestamp": ts,
            "source":    "polygon",
        }
    except (KeyError, ValueError) as exc:
        print(f"    [market] Polygon prev-day parse error for {ticker}: {exc}")
        return None


# ---------------------------------------------------------------------------
# RSI helpers
# ---------------------------------------------------------------------------

async def _rsi_av(ticker: str, client: httpx.AsyncClient) -> tuple[float, str] | None:
    """AV RSI(14, daily) → (value, timestamp_str)."""
    try:
        resp = await client.get(
            _AV_BASE,
            params={
                "function":    "RSI",
                "symbol":      ticker,
                "interval":    "daily",
                "time_period": "14",
                "series_type": "close",
                "apikey":      _AV_KEY,
            },
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        print(f"    [market] AV RSI error for {ticker}: {exc}")
        return None

    if "Note" in body or "Information" in body:
        return None

    ts_data = body.get("Technical Analysis: RSI", {})
    if not ts_data:
        return None

    try:
        latest_date = next(iter(ts_data))
        rsi_val = float(ts_data[latest_date]["RSI"])
        ts = datetime.strptime(latest_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return (rsi_val, ts)
    except (StopIteration, KeyError, ValueError) as exc:
        print(f"    [market] AV RSI parse error for {ticker}: {exc}")
        return None


async def _rsi_finnhub(ticker: str, client: httpx.AsyncClient) -> float | None:
    """Finnhub /indicator RSI fallback — returns most recent RSI value."""
    try:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        from_ts = now_ts - 86400 * 30  # 30 days back for indicator history
        resp = await client.get(
            f"{_FINNHUB_BASE}/indicator",
            params={
                "symbol":     ticker,
                "resolution": "D",
                "from":       from_ts,
                "to":         now_ts,
                "indicator":  "rsi",
                "timeperiod": 14,
                "token":      _FINNHUB_KEY,
            },
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        print(f"    [market] Finnhub RSI error for {ticker}: {exc}")
        return None

    rsi_list = body.get("rsi", [])
    if not rsi_list:
        return None

    # last element is most recent; filter None values
    valid = [v for v in rsi_list if v is not None]
    return float(valid[-1]) if valid else None


# ---------------------------------------------------------------------------
# Put/call ratio helpers
# ---------------------------------------------------------------------------

def _pcr_from_chain(data: list) -> float | None:
    """Compute put/call OI ratio from Finnhub option-chain data list."""
    total_call_oi = 0.0
    total_put_oi  = 0.0
    for expiry in data:
        opts = expiry.get("options", {})
        for contract in opts.get("CALL", []):
            total_call_oi += float(contract.get("openInterest") or 0)
        for contract in opts.get("PUT", []):
            total_put_oi += float(contract.get("openInterest") or 0)

    if total_call_oi == 0:
        return None
    return round(total_put_oi / total_call_oi, 4)


async def _pcr_iv_finnhub(
    ticker: str,
    client: httpx.AsyncClient,
) -> tuple[float | None, float | None]:
    """
    Finnhub /stock/option-chain → (put_call_ratio, implied_volatility).
    IV is the mean of non-zero IV values across all near-term contracts.
    """
    try:
        resp = await client.get(
            f"{_FINNHUB_BASE}/stock/option-chain",
            params={"symbol": ticker, "token": _FINNHUB_KEY},
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        # 403 is expected on Finnhub free tier — keep this silent in production
        _log.debug("Finnhub option-chain unavailable for %s: %s", ticker, exc)
        return None, None

    data = body.get("data", [])
    if not data:
        return None, None

    pcr = _pcr_from_chain(data)

    # IV: mean of all non-zero impliedVolatility values
    iv_values: list[float] = []
    for expiry in data:
        opts = expiry.get("options", {})
        for side in ("CALL", "PUT"):
            for contract in opts.get(side, []):
                iv = contract.get("impliedVolatility")
                if iv and iv > 0:
                    iv_values.append(float(iv))

    iv = round(sum(iv_values) / len(iv_values), 4) if iv_values else None
    return pcr, iv


async def _pcr_polygon(ticker: str, client: httpx.AsyncClient) -> float | None:
    """Polygon /v3/snapshot/options/{ticker} PCR fallback."""
    try:
        resp = await client.get(
            f"{_POLYGON_BASE}/v3/snapshot/options/{ticker}",
            params={"limit": 250, "apiKey": _POLYGON_KEY},
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        # Options data requires paid tier on Polygon — keep fallback silent
        _log.debug("Polygon options unavailable for %s: %s", ticker, exc)
        return None

    results = body.get("results", [])
    if not results:
        return None

    call_oi = sum(
        float(r.get("open_interest") or 0)
        for r in results
        if r.get("details", {}).get("contract_type") == "call"
    )
    put_oi = sum(
        float(r.get("open_interest") or 0)
        for r in results
        if r.get("details", {}).get("contract_type") == "put"
    )

    if call_oi == 0:
        return None
    return round(put_oi / call_oi, 4)


# ---------------------------------------------------------------------------
# Short interest
# ---------------------------------------------------------------------------

async def _short_interest_finnhub(ticker: str, client: httpx.AsyncClient) -> float | None:
    """
    Finnhub /stock/short-interest — shortInterestRatio (shares short / float).
    Returns the most recent ratio, or None.
    """
    try:
        resp = await client.get(
            f"{_FINNHUB_BASE}/stock/short-interest",
            params={"symbol": ticker, "token": _FINNHUB_KEY},
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        # 403 is expected on Finnhub free tier — keep this silent in production
        _log.debug("Finnhub short-interest unavailable for %s: %s", ticker, exc)
        return None

    data = body.get("data", [])
    if not data:
        return None

    # data is sorted descending; take the most recent entry
    try:
        latest = data[0]
        ratio = latest.get("shortInterestRatio") or latest.get("shortInterest")
        return float(ratio) if ratio is not None else None
    except (IndexError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Computed signals (returns, volume ratio)
# ---------------------------------------------------------------------------

def _compute_returns(
    current_close: float,
    close_history: list[tuple[datetime, float]],
) -> list[tuple[str, float]]:
    """
    Return [(signal_type, value)] for 1d/5d/20d returns.
    close_history is sorted ascending (oldest first).
    The most recent entry is history[-1] (previous session's close).
    """
    closes = [c for _, c in close_history]
    results = []

    if len(closes) >= 1 and closes[-1] != 0:
        r1 = (current_close - closes[-1]) / closes[-1]
        results.append(("return_1d", round(r1, 6)))

    if len(closes) >= 5 and closes[-5] != 0:
        r5 = (current_close - closes[-5]) / closes[-5]
        results.append(("return_5d", round(r5, 6)))

    if len(closes) >= 20 and closes[-20] != 0:
        r20 = (current_close - closes[-20]) / closes[-20]
        results.append(("return_20d", round(r20, 6)))

    return results


def _compute_volume_ratio(
    current_volume: float,
    volume_history: list[float],
) -> float | None:
    """volume_ratio = current_volume / mean(last 20 sessions' volume)."""
    if not volume_history:
        return None
    avg = sum(volume_history) / len(volume_history)
    if avg == 0:
        return None
    return round(current_volume / avg, 4)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def _run_market(ticker: str, client: httpx.AsyncClient) -> None:
    """Core implementation — requires a live client."""
    now = datetime.now(timezone.utc)
    rows: list[tuple] = []

    # --- OHLCV (primary: AV, fallback: Polygon) ---
    ohlcv = await _ohlcv_av(ticker, client)
    if ohlcv is None:
        ohlcv = await _ohlcv_polygon(ticker, client)

    if ohlcv:
        src = ohlcv["source"]
        ts  = ohlcv["timestamp"]
        rows.extend([
            (ticker, "ohlcv_open",   ohlcv["open"],   src, "live", ts),
            (ticker, "ohlcv_high",   ohlcv["high"],   src, "live", ts),
            (ticker, "ohlcv_low",    ohlcv["low"],    src, "live", ts),
            (ticker, "ohlcv_close",  ohlcv["close"],  src, "live", ts),
            (ticker, "ohlcv_volume", ohlcv["volume"], src, "live", ts),
        ])

    # --- RSI (primary: AV, fallback: Finnhub) ---
    rsi_result = await _rsi_av(ticker, client)
    if rsi_result is not None:
        rsi_val, rsi_ts = rsi_result
        rows.append((ticker, "rsi_14", rsi_val, "alpha_vantage", "live", rsi_ts))
    else:
        rsi_fallback = await _rsi_finnhub(ticker, client)
        if rsi_fallback is not None:
            rows.append((ticker, "rsi_14", rsi_fallback, "finnhub", "live", now))

    # --- PCR + IV (primary: Finnhub option chain, PCR fallback: Polygon) ---
    pcr, iv = await _pcr_iv_finnhub(ticker, client)
    if pcr is None:
        pcr = await _pcr_polygon(ticker, client)
    if pcr is not None:
        rows.append((ticker, "put_call_ratio", pcr, "finnhub", "live", now))
    if iv is not None:
        rows.append((ticker, "implied_volatility", iv, "finnhub", "live", now))

    # --- Short interest (Finnhub only; no fallback — updates bimonthly) ---
    si = await _short_interest_finnhub(ticker, client)
    if si is not None:
        rows.append((ticker, "short_interest_ratio", si, "finnhub", "live", now))

    # --- Computed: returns and volume ratio ---
    if ohlcv:
        close_history  = await get_close_history(ticker, limit=22)
        volume_history = await get_volume_history(ticker, limit=20)

        for sig_type, val in _compute_returns(ohlcv["close"], close_history):
            rows.append((ticker, sig_type, val, "computed", "live", now))

        vr = _compute_volume_ratio(ohlcv["volume"], volume_history)
        if vr is not None:
            rows.append((ticker, "volume_ratio", vr, "computed", "live", now))

    await insert_signals(rows)


async def fetch_market_signals(
    ticker: str,
    client: httpx.AsyncClient | None = None,
) -> None:
    """
    Fetch all Layer 03 market signals for `ticker` and write to raw_signals
    with upload_type='live'.

    If `client` is None a new AsyncClient is created internally, making this
    function callable standalone (e.g. in tests or one-off scripts).
    Pass a shared client from the orchestrator for efficiency.
    """
    if client is None:
        async with httpx.AsyncClient(timeout=30) as _client:
            return await _run_market(ticker, _client)
    return await _run_market(ticker, client)
