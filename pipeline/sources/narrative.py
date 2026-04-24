"""
pipeline/sources/narrative.py  — Layer 05

Fetches news articles for a single ticker and writes them to raw_articles.
Deduplication is performed via SHA-256 hash of the article URL (content_hash).

Sources
-------
Primary:  Alpha Vantage NEWS_SENTIMENT
          Returns title, summary, source, URL, provider_sentiment, relevance_score.

Fallback: Finnhub /company-news
          Returns title, summary, URL, published_at.
          No provider sentiment — stored with provider_sentiment=None.

Phase 1 note: FinBERT scoring is NOT applied here.  The finbert_score column
is left NULL and will be populated in Phase 2.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

from db.queries.raw_articles import hash_exists, insert_article

load_dotenv(override=False)

_AV_KEY      = os.environ.get("ALPHA_VANTAGE_KEY", "")
_FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")

_AV_BASE      = "https://www.alphavantage.co/query"
_FINNHUB_BASE = "https://finnhub.io/api/v1"

_ARTICLE_LOOKBACK_DAYS = 3   # fetch news from the last N days


def _hash_url(url: str) -> str:
    """SHA-256 of the article URL, hex-encoded (64 chars)."""
    return hashlib.sha256(url.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Alpha Vantage NEWS_SENTIMENT
# ---------------------------------------------------------------------------

def _parse_av_time(time_str: str) -> datetime | None:
    """Parse AV timestamp format: '20240115T163000' → datetime UTC."""
    try:
        return datetime.strptime(time_str, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def _fetch_av_news(ticker: str, client: httpx.AsyncClient) -> list[dict]:
    """
    AV NEWS_SENTIMENT for ticker.
    Returns list of normalized article dicts.
    """
    try:
        resp = await client.get(
            _AV_BASE,
            params={
                "function": "NEWS_SENTIMENT",
                "tickers":  ticker,
                "limit":    50,
                "apikey":   _AV_KEY,
            },
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        print(f"    [narrative] AV news error for {ticker}: {exc}")
        return []

    if "Note" in body or "Information" in body:
        return []

    articles = []
    for item in body.get("feed", []):
        url = item.get("url", "")
        if not url:
            continue

        published_at = _parse_av_time(item.get("time_published", ""))
        if published_at is None:
            continue

        # Extract per-ticker relevance and sentiment from ticker_sentiment list
        provider_sentiment: float | None = None
        relevance_score:    float | None = None
        for ts in item.get("ticker_sentiment", []):
            if ts.get("ticker", "").upper() == ticker.upper():
                try:
                    provider_sentiment = float(ts["ticker_sentiment_score"])
                    relevance_score    = float(ts["relevance_score"])
                except (KeyError, ValueError):
                    pass
                break

        articles.append({
            "ticker":             ticker,
            "title":              item.get("title", "")[:500],
            "summary":            (item.get("summary") or "")[:2000] or None,
            "source":             "alpha_vantage",
            "source_url":         url,
            "published_at":       published_at,
            "provider_sentiment": provider_sentiment,
            "relevance_score":    relevance_score,
            "content_hash":       _hash_url(url),
        })

    return articles


# ---------------------------------------------------------------------------
# Finnhub company-news (fallback)
# ---------------------------------------------------------------------------

async def _fetch_finnhub_news(ticker: str, client: httpx.AsyncClient) -> list[dict]:
    """
    Finnhub /company-news for last N days.
    Returns list of normalized article dicts (provider_sentiment=None).
    """
    today      = datetime.now(timezone.utc).date()
    from_date  = (datetime.now(timezone.utc) - timedelta(days=_ARTICLE_LOOKBACK_DAYS)).date()

    try:
        resp = await client.get(
            f"{_FINNHUB_BASE}/company-news",
            params={
                "symbol": ticker,
                "from":   str(from_date),
                "to":     str(today),
                "token":  _FINNHUB_KEY,
            },
        )
        resp.raise_for_status()
        items = resp.json()
    except Exception as exc:
        print(f"    [narrative] Finnhub news error for {ticker}: {exc}")
        return []

    if not isinstance(items, list):
        return []

    articles = []
    for item in items:
        url = item.get("url", "")
        if not url:
            continue

        unix_ts = item.get("datetime")
        if unix_ts is None:
            continue

        try:
            published_at = datetime.fromtimestamp(int(unix_ts), tz=timezone.utc)
        except (ValueError, OSError):
            continue

        articles.append({
            "ticker":             ticker,
            "title":              (item.get("headline") or "")[:500],
            "summary":            (item.get("summary") or "")[:2000] or None,
            "source":             "finnhub",
            "source_url":         url,
            "published_at":       published_at,
            "provider_sentiment": None,
            "relevance_score":    None,
            "content_hash":       _hash_url(url),
        })

    return articles


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def _run_narrative(ticker: str, client: httpx.AsyncClient) -> None:
    """Core implementation — requires a live client."""
    # Collect articles from both sources; AV first (higher credibility weight)
    articles = await _fetch_av_news(ticker, client)

    finnhub_articles = await _fetch_finnhub_news(ticker, client)
    articles.extend(finnhub_articles)

    # Deduplicate within this batch first (same URL from both sources)
    seen_hashes: set[str] = set()
    for article in articles:
        h = article["content_hash"]
        if h in seen_hashes:
            continue
        seen_hashes.add(h)

        # Skip if already in DB
        try:
            if await hash_exists(ticker, h):
                continue
        except Exception as exc:
            print(f"    [narrative] hash_exists error for {ticker}: {exc}")
            continue

        try:
            await insert_article(
                ticker             = article["ticker"],
                title              = article["title"],
                summary            = article["summary"],
                source             = article["source"],
                source_url         = article["source_url"],
                published_at       = article["published_at"],
                provider_sentiment = article["provider_sentiment"],
                relevance_score    = article["relevance_score"],
                content_hash       = article["content_hash"],
            )
        except Exception as exc:
            print(f"    [narrative] insert_article error for {ticker}: {exc}")


async def fetch_narrative_signals(
    ticker: str,
    client: httpx.AsyncClient | None = None,
) -> None:
    """
    Fetch news for `ticker` from AV (primary) and Finnhub (additional),
    deduplicate by content_hash, and write new articles to raw_articles.

    If `client` is None a new AsyncClient is created internally, making this
    function callable standalone (e.g. in tests or one-off scripts).
    Pass a shared client from the orchestrator for efficiency.
    """
    if client is None:
        async with httpx.AsyncClient(timeout=30) as _client:
            return await _run_narrative(ticker, _client)
    return await _run_narrative(ticker, client)
