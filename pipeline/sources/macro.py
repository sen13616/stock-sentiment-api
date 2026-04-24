"""
pipeline/sources/macro.py  — Layer 06

Fetches macro context signals and writes them to raw_signals.

Unlike Layers 03–05 this module is NOT per-ticker.
It runs once per daily cycle and stores signals under a special
ticker='_MACRO_' convention so they can be joined downstream.

Signals produced
----------------
vix
  Primary:  Finnhub /quote  (symbol=^VIX)
  Fallback: Alpha Vantage GLOBAL_QUOTE (symbol=^VIX)

sector_etf_close  (one row per sector ETF, stored under ticker=ETF_SYMBOL)
  Source:   Alpha Vantage GLOBAL_QUOTE  for each GICS ETF

sector_etf_return_20d  (20-day return; needs ≥20 days of stored ETF closes)
  Computed: from raw_signals close history for each ETF

All written with upload_type='live', source as noted.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

from db.queries.raw_signals import (
    get_close_history,
    insert_signals,
)

load_dotenv(override=True)

_AV_KEY      = os.environ.get("ALPHA_VANTAGE_KEY", "")
_FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")

_AV_BASE      = "https://www.alphavantage.co/query"
_FINNHUB_BASE = "https://finnhub.io/api/v1"

# GICS sector → ETF ticker mapping
SECTOR_ETFS: dict[str, str] = {
    "Communication Services":  "XLC",
    "Consumer Discretionary":  "XLY",
    "Consumer Staples":        "XLP",
    "Energy":                  "XLE",
    "Financials":              "XLF",
    "Health Care":             "XLV",
    "Industrials":             "XLI",
    "Information Technology":  "XLK",
    "Materials":               "XLB",
    "Real Estate":             "XLRE",
    "Utilities":               "XLU",
}


# ---------------------------------------------------------------------------
# VIX helpers
# ---------------------------------------------------------------------------

async def _vix_finnhub(client: httpx.AsyncClient) -> float | None:
    """Finnhub /quote?symbol=^VIX → current VIX level."""
    try:
        resp = await client.get(
            f"{_FINNHUB_BASE}/quote",
            params={"symbol": "^VIX", "token": _FINNHUB_KEY},
        )
        resp.raise_for_status()
        body = resp.json()
        price = body.get("c")  # current price
        return float(price) if price else None
    except Exception as exc:
        print(f"    [macro] Finnhub VIX error: {exc}")
        return None


async def _vix_av(client: httpx.AsyncClient) -> float | None:
    """Alpha Vantage GLOBAL_QUOTE?symbol=^VIX fallback."""
    try:
        resp = await client.get(
            _AV_BASE,
            params={
                "function": "GLOBAL_QUOTE",
                "symbol":   "^VIX",
                "apikey":   _AV_KEY,
            },
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        print(f"    [macro] AV VIX error: {exc}")
        return None

    if "Note" in body or "Information" in body:
        return None

    q = body.get("Global Quote", {})
    price = q.get("05. price")
    if not price:
        return None

    try:
        return float(price)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Sector ETF helpers
# ---------------------------------------------------------------------------

async def _etf_close_av(etf: str, client: httpx.AsyncClient) -> tuple[float, datetime] | None:
    """Alpha Vantage GLOBAL_QUOTE for a sector ETF → (close, timestamp)."""
    try:
        resp = await client.get(
            _AV_BASE,
            params={
                "function": "GLOBAL_QUOTE",
                "symbol":   etf,
                "apikey":   _AV_KEY,
            },
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        print(f"    [macro] AV ETF quote error for {etf}: {exc}")
        return None

    if "Note" in body or "Information" in body:
        return None

    q = body.get("Global Quote", {})
    price_str    = q.get("05. price")
    date_str     = q.get("07. latest trading day", "")

    if not price_str:
        return None

    try:
        close = float(price_str)
        ts = (
            datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if date_str
            else datetime.now(timezone.utc)
        )
        return close, ts
    except ValueError:
        return None


def _compute_etf_return_20d(
    current_close: float,
    close_history: list[tuple[datetime, float]],
) -> float | None:
    """
    20-day return for a sector ETF.
    close_history is sorted ascending.  Need at least 20 prior sessions.
    """
    closes = [c for _, c in close_history]
    if len(closes) < 20:
        return None
    prior = closes[-20]
    if prior == 0:
        return None
    return round((current_close - prior) / prior, 6)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def _run_macro(client: httpx.AsyncClient) -> None:
    """Core implementation — requires a live client."""
    now  = datetime.now(timezone.utc)
    rows: list[tuple] = []

    # --- VIX (primary: Finnhub, fallback: AV) ---
    vix = await _vix_finnhub(client)
    if vix is None:
        vix = await _vix_av(client)

    if vix is not None:
        src = "finnhub" if vix is not None else "alpha_vantage"
        rows.append(("_MACRO_", "vix", vix, src, "live", now))
        print(f"    [macro] VIX = {vix:.2f}")

    # --- Sector ETF closes and 20-day returns ---
    for sector, etf in SECTOR_ETFS.items():
        result = await _etf_close_av(etf, client)
        if result is None:
            print(f"    [macro] No data for {etf} ({sector})")
            continue

        etf_close, etf_ts = result
        rows.append((etf, "sector_etf_close", etf_close, "alpha_vantage", "live", etf_ts))

        close_history = await get_close_history(etf, limit=22)
        ret_20d = _compute_etf_return_20d(etf_close, close_history)
        if ret_20d is not None:
            rows.append((etf, "sector_etf_return_20d", ret_20d, "computed", "live", now))
            print(f"    [macro] {etf}: close={etf_close:.2f}  20d_return={ret_20d:+.2%}")
        else:
            print(f"    [macro] {etf}: close={etf_close:.2f}  (insufficient history for 20d return)")

    await insert_signals(rows)


async def fetch_macro_signals(
    client: httpx.AsyncClient | None = None,
) -> None:
    """
    Fetch Layer 06 macro signals (VIX + sector ETF data) and write to raw_signals.

    VIX is stored under ticker='_MACRO_'.
    Sector ETF signals are stored under ticker=ETF_SYMBOL (e.g. 'XLK').

    If `client` is None a new AsyncClient is created internally, making this
    function callable standalone (e.g. in tests or one-off scripts).
    Pass a shared client from the orchestrator for efficiency.
    """
    if client is None:
        async with httpx.AsyncClient(timeout=30) as _client:
            return await _run_macro(_client)
    return await _run_macro(client)
