"""
db/queries/raw_articles.py

All raw_articles table operations. No raw SQL anywhere else in the codebase.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from db.connection import get_pool


async def hash_exists(ticker: str, content_hash: str) -> bool:
    """Return True if an article with this (ticker, content_hash) already exists."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            """
            SELECT COUNT(*) FROM raw_articles
            WHERE ticker       = $1
              AND content_hash = $2
            """,
            ticker,
            content_hash,
        )
    return (n or 0) > 0


async def insert_article(
    ticker: str,
    title: str,
    summary: str | None,
    source: str,
    source_url: str | None,
    published_at: datetime,
    provider_sentiment: float | None,
    relevance_score: float | None,
    content_hash: str,
) -> None:
    """
    Insert one article row. Silently ignores duplicates via ON CONFLICT DO NOTHING
    (unique index on (ticker, content_hash)).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO raw_articles
                (ticker, title, summary, source, source_url, published_at,
                 provider_sentiment, relevance_score, content_hash)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (ticker, content_hash) DO NOTHING
            """,
            ticker,
            title,
            summary,
            source,
            source_url,
            published_at,
            provider_sentiment,
            relevance_score,
            content_hash,
        )


async def get_unclustered_articles(
    ticker: str,
    since_hours: float = 48.0,
) -> list[dict]:
    """
    Return articles for `ticker` published in the last `since_hours` hours
    that have no event_cluster_id assigned yet.

    Returns list of dicts with keys: id, title, published_at, relevance_score, source.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, title, published_at, relevance_score, source
            FROM raw_articles
            WHERE ticker           = $1
              AND published_at    >= $2
              AND event_cluster_id IS NULL
            ORDER BY published_at ASC
            """,
            ticker,
            since,
        )
    return [dict(r) for r in rows]


async def get_articles_since(
    ticker: str,
    since: datetime,
) -> list[dict]:
    """
    Return articles with provider_sentiment for scoring, deduplicated by
    event cluster.

    Dedup logic (Sprint 6, G-C6):
      - Clustered articles (event_cluster_id IS NOT NULL): only the
        highest-relevance article per cluster is returned.
      - Unclustered articles (event_cluster_id IS NULL): each treated as
        its own unique row via COALESCE(event_cluster_id, id::text).
      - provider_sentiment IS NOT NULL is in the WHERE clause (before
        DISTINCT ON) so Finnhub articles with NULL sentiment are excluded
        from cluster selection entirely.

    Only rows where provider_sentiment IS NOT NULL are returned.

    Returns
    -------
    list of dicts with keys: published_at, provider_sentiment, relevance_score, source.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT published_at, provider_sentiment, relevance_score, source
            FROM (
                SELECT DISTINCT ON (COALESCE(event_cluster_id, id::text))
                       published_at, provider_sentiment, relevance_score, source
                FROM raw_articles
                WHERE ticker               = $1
                  AND published_at        >= $2
                  AND provider_sentiment IS NOT NULL
                ORDER BY COALESCE(event_cluster_id, id::text),
                         relevance_score DESC NULLS LAST,
                         published_at DESC
            ) deduped
            ORDER BY published_at DESC
            """,
            ticker,
            since,
        )
    return [dict(r) for r in rows]


async def count_unclustered_articles(since_hours: float = 48.0) -> int:
    """Count total unclustered articles across all tickers in the last N hours."""
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            """
            SELECT COUNT(*) FROM raw_articles
            WHERE published_at >= $1
              AND event_cluster_id IS NULL
            """,
            since,
        )
    return n or 0


async def get_cluster_source_breakdown(since_hours: float = 48.0) -> list[dict]:
    """
    Return per-cluster source breakdown for recently clustered articles.

    Used by narrative_job telemetry to compute cross_source vs same_source
    cluster counts.

    Returns list of dicts with keys: event_cluster_id, sources (array),
    article_count.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT event_cluster_id,
                   array_agg(DISTINCT source) AS sources,
                   COUNT(*) AS article_count
            FROM raw_articles
            WHERE event_cluster_id IS NOT NULL
              AND published_at >= $1
            GROUP BY event_cluster_id
            HAVING COUNT(*) >= 2
            """,
            since,
        )
    return [dict(r) for r in rows]


async def set_cluster_ids(article_ids: list[int], cluster_id: str) -> None:
    """Assign `cluster_id` to each article in `article_ids`."""
    if not article_ids:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE raw_articles
            SET event_cluster_id = $1
            WHERE id = ANY($2::int[])
            """,
            cluster_id,
            article_ids,
        )
