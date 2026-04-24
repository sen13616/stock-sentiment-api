"""
pipeline/sources/influencer.py  — Layer 04

Fetches insider transactions and analyst signals for a single ticker
and writes them to raw_signals.

Signals produced
----------------
insider_net_shares
  Primary:  SEC EDGAR Form 4 (non-derivative transactions)
            CIK lookup → submissions API → XML parse
  Fallback: Finnhub /stock/insider-transactions

analyst_buy_pct
  Source:   Finnhub /stock/recommendation
            = (strongBuy + buy) / total recommendations  (0–1 scale)

analyst_target_price
  Source:   Finnhub /stock/price-target
            targetMean from latest analyst consensus

All written with upload_type='live'.
"""
from __future__ import annotations

import os
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

from db.queries.raw_signals import insert_signals

_log = logging.getLogger(__name__)

load_dotenv(override=False)

_FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")

_FINNHUB_BASE    = "https://finnhub.io/api/v1"
_EDGAR_TICKERS   = "https://www.sec.gov/files/company_tickers.json"
_EDGAR_SUB       = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_EDGAR_DOC       = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nohyphen}/{filename}"

# SEC requires a descriptive User-Agent for all requests
_EDGAR_HEADERS = {
    "User-Agent": "SentimentAPI admin@sentimentapi.local",
    "Accept":     "application/json, application/xml",
}

# Lazy-loaded CIK map: ticker → CIK integer
_cik_cache: dict[str, int] = {}

_INSIDER_LOOKBACK_DAYS = 30


# ---------------------------------------------------------------------------
# EDGAR helpers
# ---------------------------------------------------------------------------

async def _load_cik_map(client: httpx.AsyncClient) -> None:
    """Load full ticker-to-CIK mapping from EDGAR once per process."""
    global _cik_cache
    if _cik_cache:
        return
    try:
        resp = await client.get(_EDGAR_TICKERS, headers=_EDGAR_HEADERS)
        resp.raise_for_status()
        data = resp.json()
        _cik_cache = {
            v["ticker"].upper(): int(v["cik_str"])
            for v in data.values()
            if "ticker" in v and "cik_str" in v
        }
    except Exception as exc:
        _log.warning("EDGAR CIK map load error: %s", exc)


async def _get_cik(ticker: str, client: httpx.AsyncClient) -> int | None:
    await _load_cik_map(client)
    return _cik_cache.get(ticker.upper())


async def _fetch_form4_transactions(
    ticker: str,
    cik: int,
    client: httpx.AsyncClient,
) -> list[tuple]:
    """
    Fetch Form 4 filings from the last INSIDER_LOOKBACK_DAYS and parse
    non-derivative transactions.

    Returns list of (ticker, 'insider_net_shares', value, 'sec_edgar', 'live', timestamp).
    """
    rows: list[tuple] = []

    # Step 1: fetch submissions JSON
    try:
        resp = await client.get(
            _EDGAR_SUB.format(cik=cik),
            headers=_EDGAR_HEADERS,
        )
        resp.raise_for_status()
        submissions = resp.json()
    except Exception as exc:
        _log.warning("EDGAR submissions error for %s (CIK %s): %s", ticker, cik, exc)
        return rows

    recent = submissions.get("filings", {}).get("recent", {})
    forms     = recent.get("form", [])
    dates     = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    cutoff = datetime.now(timezone.utc) - timedelta(days=_INSIDER_LOOKBACK_DAYS)

    for form, date_str, acc, primary_doc in zip(forms, dates, accessions, primary_docs):
        if form not in ("4", "4/A"):
            continue

        try:
            filing_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        if filing_date < cutoff:
            break  # submissions are sorted descending; stop when past lookback

        acc_nohyphen = acc.replace("-", "")
        doc_url = _EDGAR_DOC.format(
            cik=cik,
            acc_nohyphen=acc_nohyphen,
            filename=primary_doc,
        )

        # Step 2: fetch and parse the Form 4 XML
        try:
            xml_resp = await client.get(doc_url, headers=_EDGAR_HEADERS)
            xml_resp.raise_for_status()
            xml_text = xml_resp.text
        except Exception as exc:
            _log.warning("EDGAR Form 4 XML fetch error (%s): %s", acc, exc)
            continue

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            # EDGAR sometimes wraps XML in SGML — log at debug, fallback handles it
            _log.debug("EDGAR Form 4 XML parse error (%s): %s", acc, exc)
            continue

        # Iterate non-derivative transactions
        for txn in root.findall(".//nonDerivativeTransaction"):
            try:
                date_el = txn.find(".//transactionDate/value")
                shares_el = txn.find(".//transactionAmounts/transactionShares/value")
                code_el = txn.find(
                    ".//transactionAmounts/transactionAcquiredDisposedCode/value"
                )

                if date_el is None or shares_el is None or code_el is None:
                    continue

                txn_date = datetime.strptime(
                    date_el.text.strip(), "%Y-%m-%d"
                ).replace(tzinfo=timezone.utc)
                shares = float(shares_el.text.strip())
                code   = code_el.text.strip().upper()

                # A = Acquired (positive), D = Disposed (negative)
                net_shares = shares if code == "A" else -shares
                rows.append(
                    (ticker, "insider_net_shares", net_shares, "sec_edgar", "live", txn_date)
                )
            except (AttributeError, ValueError):
                continue

    return rows


# ---------------------------------------------------------------------------
# Finnhub insider transactions (fallback)
# ---------------------------------------------------------------------------

async def _insider_finnhub(ticker: str, client: httpx.AsyncClient) -> list[tuple]:
    """
    Finnhub /stock/insider-transactions fallback.
    Returns list of (ticker, 'insider_net_shares', value, 'finnhub', 'live', timestamp).
    """
    from_date = (datetime.now(timezone.utc) - timedelta(days=_INSIDER_LOOKBACK_DAYS)).date()
    rows: list[tuple] = []

    try:
        resp = await client.get(
            f"{_FINNHUB_BASE}/stock/insider-transactions",
            params={"symbol": ticker, "from": str(from_date), "token": _FINNHUB_KEY},
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        # 403 expected on Finnhub free tier
        _log.debug("Finnhub insider-transactions unavailable for %s: %s", ticker, exc)
        return rows

    for txn in body.get("data", []):
        try:
            change = float(txn.get("change") or 0)
            date_str = txn.get("transactionDate") or txn.get("filingDate", "")
            ts = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            rows.append((ticker, "insider_net_shares", change, "finnhub", "live", ts))
        except (ValueError, TypeError):
            continue

    return rows


# ---------------------------------------------------------------------------
# Analyst signals (Finnhub)
# ---------------------------------------------------------------------------

async def _analyst_recommendations(
    ticker: str,
    client: httpx.AsyncClient,
) -> float | None:
    """
    Finnhub /stock/recommendation → analyst_buy_pct.
    = (strongBuy + buy) / (strongBuy + buy + hold + sell + strongSell).
    Returns the most recent period's ratio (0–1), or None.
    """
    try:
        resp = await client.get(
            f"{_FINNHUB_BASE}/stock/recommendation",
            params={"symbol": ticker, "token": _FINNHUB_KEY},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        # 403 expected on Finnhub free tier
        _log.debug("Finnhub recommendation unavailable for %s: %s", ticker, exc)
        return None

    if not isinstance(data, list) or not data:
        return None

    try:
        latest = data[0]  # most recent period
        strong_buy = int(latest.get("strongBuy") or 0)
        buy        = int(latest.get("buy")        or 0)
        hold       = int(latest.get("hold")       or 0)
        sell       = int(latest.get("sell")       or 0)
        strong_sell = int(latest.get("strongSell") or 0)
        total = strong_buy + buy + hold + sell + strong_sell
        if total == 0:
            return None
        return round((strong_buy + buy) / total, 4)
    except (KeyError, ValueError, TypeError):
        return None


async def _analyst_target_price(
    ticker: str,
    client: httpx.AsyncClient,
) -> float | None:
    """
    Finnhub /stock/price-target → analyst_target_price (targetMean).
    """
    try:
        resp = await client.get(
            f"{_FINNHUB_BASE}/stock/price-target",
            params={"symbol": ticker, "token": _FINNHUB_KEY},
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        # 403 expected on Finnhub free tier
        _log.debug("Finnhub price-target unavailable for %s: %s", ticker, exc)
        return None

    try:
        target = body.get("targetMean")
        return float(target) if target is not None else None
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def _run_influencer(ticker: str, client: httpx.AsyncClient) -> None:
    """Core implementation — requires a live client."""
    now  = datetime.now(timezone.utc)
    rows: list[tuple] = []

    # --- Insider transactions (primary: EDGAR, fallback: Finnhub) ---
    cik = await _get_cik(ticker, client)
    if cik is not None:
        insider_rows = await _fetch_form4_transactions(ticker, cik, client)
    else:
        insider_rows = []

    if not insider_rows:
        insider_rows = await _insider_finnhub(ticker, client)

    rows.extend(insider_rows)

    # --- Analyst buy percentage ---
    buy_pct = await _analyst_recommendations(ticker, client)
    if buy_pct is not None:
        rows.append((ticker, "analyst_buy_pct", buy_pct, "finnhub", "live", now))

    # --- Analyst target price ---
    target = await _analyst_target_price(ticker, client)
    if target is not None:
        rows.append((ticker, "analyst_target_price", target, "finnhub", "live", now))

    await insert_signals(rows)


async def fetch_influencer_signals(
    ticker: str,
    client: httpx.AsyncClient | None = None,
) -> None:
    """
    Fetch Layer 04 influencer signals for `ticker` and write to raw_signals.

    If `client` is None a new AsyncClient is created internally, making this
    function callable standalone (e.g. in tests or one-off scripts).
    Pass a shared client from the orchestrator for efficiency.
    """
    if client is None:
        async with httpx.AsyncClient(timeout=30) as _client:
            return await _run_influencer(ticker, _client)
    return await _run_influencer(ticker, client)
